BEGIN IMMEDIATE;
ALTER TABLE object_mechanisms RENAME TO old_object_mechanisms;
CREATE TABLE object_mechanisms (
    session_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('armed','spent','unprepared','blocked','disarmed','broken')),
    remaining_uses INTEGER CHECK(remaining_uses IS NULL OR remaining_uses >= 0),
    activation_count INTEGER NOT NULL CHECK(activation_count >= 0),
    reset_at_tick INTEGER CHECK(reset_at_tick IS NULL OR reset_at_tick BETWEEN 0 AND 2147483647),
    resume_status TEXT CHECK(resume_status IN ('armed','spent','unprepared')),
    CHECK ((status='blocked') = (resume_status IS NOT NULL)),
    CHECK (reset_at_tick IS NULL OR status='spent' OR (status='blocked' AND resume_status='spent')),
    CHECK (status!='armed' OR remaining_uses IS NULL OR remaining_uses>0),
    PRIMARY KEY(session_id,object_id),
    FOREIGN KEY(session_id,object_id) REFERENCES objects(session_id,object_id)
);
INSERT INTO object_mechanisms
SELECT session_id,object_id,status,CASE status WHEN 'armed' THEN 1 ELSE 0 END,
       CASE status WHEN 'armed' THEN 0 ELSE 1 END,NULL,NULL FROM old_object_mechanisms;
DROP TABLE old_object_mechanisms;
CREATE INDEX mechanism_reset_due ON object_mechanisms(session_id,reset_at_tick)
    WHERE reset_at_tick IS NOT NULL;
CREATE TABLE mechanism_activations (
    session_id TEXT NOT NULL,
    activation_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    source_command_id TEXT NOT NULL,
    source_event_seq INTEGER NOT NULL,
    actor_id TEXT,
    operation_id TEXT NOT NULL,
    PRIMARY KEY(session_id,activation_id),
    FOREIGN KEY(session_id,object_id) REFERENCES object_mechanisms(session_id,object_id)
);
CREATE INDEX mechanism_actor_frequency ON mechanism_activations(session_id,object_id,actor_id);
PRAGMA user_version = 34;
COMMIT;
