BEGIN IMMEDIATE;
-- References to the single committed log/evidence, never copies of world facts.
CREATE TABLE npc_perception_queue (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK(event_seq>0 AND typeof(event_seq)='integer'),
    signal_ids_json TEXT NOT NULL CHECK(json_valid(signal_ids_json)
        AND json_type(signal_ids_json)='array' AND length(signal_ids_json)<=131072),
    provenance TEXT NOT NULL CHECK(provenance IN ('discovery','legacy_projection')),
    complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0,1)),
    PRIMARY KEY(session_id,npc_id,event_seq),
    FOREIGN KEY(session_id,npc_id) REFERENCES actors(session_id,actor_id),
    FOREIGN KEY(session_id,event_seq) REFERENCES event_records(session_id,event_seq)
);
CREATE INDEX npc_perception_event ON npc_perception_queue(session_id,event_seq,complete);
CREATE TRIGGER npc_perception_insert BEFORE INSERT ON npc_perception_queue
WHEN NOT EXISTS (SELECT 1 FROM actors a WHERE a.session_id=NEW.session_id
    AND a.actor_id=NEW.npc_id AND a.kind='npc')
    OR (NEW.provenance='discovery' AND json_array_length(NEW.signal_ids_json)=0)
    OR EXISTS (SELECT 1 FROM json_each(NEW.signal_ids_json) WHERE type!='text')
    OR (SELECT count(*) FROM json_each(NEW.signal_ids_json)) !=
       (SELECT count(DISTINCT value) FROM json_each(NEW.signal_ids_json))
    OR (NEW.complete=1 AND NOT EXISTS (SELECT 1 FROM perception_results p
        WHERE p.session_id=NEW.session_id AND p.observer_id=NEW.npc_id
        AND p.event_seq=NEW.event_seq AND p.complete=1))
BEGIN SELECT RAISE(ABORT,'Queue requires NPC identity, unique Signals and own proof'); END;
CREATE TRIGGER npc_perception_identity BEFORE UPDATE ON npc_perception_queue
WHEN NEW.session_id!=OLD.session_id OR NEW.npc_id!=OLD.npc_id
    OR NEW.event_seq!=OLD.event_seq OR NEW.signal_ids_json!=OLD.signal_ids_json
    OR NEW.provenance!=OLD.provenance OR NEW.complete<OLD.complete
BEGIN SELECT RAISE(ABORT,'Perception scope and completion are immutable'); END;
CREATE TRIGGER npc_perception_completion BEFORE UPDATE OF complete ON npc_perception_queue
WHEN NEW.complete=1 AND NOT EXISTS (
    SELECT 1 FROM perception_results p WHERE p.session_id=NEW.session_id
    AND p.observer_id=NEW.npc_id AND p.event_seq=NEW.event_seq AND p.complete=1)
BEGIN SELECT RAISE(ABORT,'NPC completion requires its own perception result'); END;
CREATE TRIGGER npc_perception_retained BEFORE DELETE ON npc_perception_queue
BEGIN SELECT RAISE(ABORT,'Perception queue history is retained'); END;
CREATE TABLE npc_perception_cursors (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL CHECK(event_seq>=0 AND typeof(event_seq)='integer'),
    PRIMARY KEY(session_id,npc_id),
    FOREIGN KEY(session_id,npc_id) REFERENCES actors(session_id,actor_id)
);
-- Unknown membership/stage quarantines an event once, including future NPCs.
-- Own completed historical results may prove completion for that NPC only.
CREATE TABLE perception_event_barriers (
    session_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 256),
    PRIMARY KEY(session_id,event_seq),
    FOREIGN KEY(session_id,event_seq) REFERENCES event_records(session_id,event_seq)
);
PRAGMA user_version = 19;
COMMIT;
