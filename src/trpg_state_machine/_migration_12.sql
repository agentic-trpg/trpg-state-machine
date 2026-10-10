BEGIN IMMEDIATE;
-- Extend the existing provenance relation, retaining all initial package/memory rows.
DROP TRIGGER npc_sources_immutable;
DROP TRIGGER npc_sources_retained;
ALTER TABLE npc_record_sources RENAME TO npc_record_sources_old;
CREATE TABLE npc_record_sources (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    content_version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 3),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('package','memory','observation')),
    reference_id TEXT NOT NULL CHECK(length(reference_id)<=128),
    reference_version INTEGER,
    memory_kind TEXT NOT NULL CHECK(memory_kind='memory'),
    observation_id TEXT GENERATED ALWAYS AS
        (CASE WHEN source_kind='observation' THEN reference_id ELSE NULL END) VIRTUAL,
    PRIMARY KEY(session_id,npc_id,record_kind,record_id,content_version,ordinal),
    FOREIGN KEY(session_id,npc_id,record_kind,record_id,content_version)
        REFERENCES npc_private_records(session_id,npc_id,record_kind,record_id,content_version),
    FOREIGN KEY(session_id,npc_id,memory_kind,reference_id,reference_version)
        REFERENCES npc_private_records(session_id,npc_id,record_kind,record_id,content_version)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id,npc_id,observation_id)
        REFERENCES npc_inbox(session_id,observer_id,observation_id),
    CHECK((source_kind='package' AND reference_id=npc_id AND reference_version IS NULL)
       OR (source_kind='memory' AND reference_version>=1 AND typeof(reference_version)='integer')
       OR (source_kind='observation' AND reference_version IS NULL))
);
INSERT INTO npc_record_sources SELECT * FROM npc_record_sources_old;
DROP TABLE npc_record_sources_old;
CREATE TRIGGER npc_sources_immutable BEFORE UPDATE ON npc_record_sources
BEGIN SELECT RAISE(ABORT,'Record provenance is immutable'); END;
CREATE TRIGGER npc_sources_retained BEFORE DELETE ON npc_record_sources
BEGIN SELECT RAISE(ABORT,'Record provenance cannot be deleted'); END;
CREATE TABLE npc_evaluation_receipts (
    evaluation_id TEXT PRIMARY KEY REFERENCES npc_evaluation_tasks(evaluation_id),
    fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
    result_json TEXT NOT NULL CHECK(json_valid(result_json) AND length(result_json)<=262144),
    receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json) AND length(receipt_json)<=131072)
);
CREATE TRIGGER npc_result_immutable BEFORE UPDATE ON npc_evaluation_receipts
BEGIN SELECT RAISE(ABORT,'Selected Evaluation Result is immutable'); END;
CREATE TRIGGER npc_result_retained BEFORE DELETE ON npc_evaluation_receipts
BEGIN SELECT RAISE(ABORT,'Selected Evaluation Result is retained'); END;
CREATE TABLE npc_record_evaluations (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    content_version INTEGER NOT NULL,
    evaluation_id TEXT NOT NULL REFERENCES npc_evaluation_receipts(evaluation_id)
        DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(session_id,npc_id,record_kind,record_id,content_version),
    FOREIGN KEY(session_id,npc_id,record_kind,record_id,content_version)
        REFERENCES npc_private_records(session_id,npc_id,record_kind,record_id,content_version)
);
CREATE TABLE npc_output_handoffs (
    evaluation_id TEXT NOT NULL REFERENCES npc_evaluation_tasks(evaluation_id),
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 100),
    kind TEXT NOT NULL CHECK(kind IN ('dialogue','action')),
    command_id TEXT UNIQUE,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(payload_json)<=262144),
    status TEXT NOT NULL CHECK(status IN ('pending','rejected','published','dispatched','private')),
    error_code TEXT,
    event_seq INTEGER,
    PRIMARY KEY(evaluation_id,ordinal),
    CHECK(kind!='action' OR status='rejected' OR command_id IS NOT NULL)
);
PRAGMA user_version = 12;
COMMIT;
