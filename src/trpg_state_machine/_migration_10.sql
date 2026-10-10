BEGIN IMMEDIATE;
-- One private state per existing Actor. Legacy data cannot reconstruct a Profile.
CREATE TABLE npc_states (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    schema_version TEXT NOT NULL CHECK(schema_version='npc-state/1'),
    name TEXT CHECK(name IS NULL OR length(name) BETWEEN 1 AND 4096),
    controller_kind TEXT CHECK(controller_kind IN ('sub_agent','deterministic_policy','scripted_event')),
    initialization_origin TEXT NOT NULL CHECK(initialization_origin IN ('package','legacy')),
    configuration TEXT NOT NULL CHECK(configuration IN ('configured','unconfigured')),
    profile_json TEXT CHECK(profile_json IS NULL OR (json_valid(profile_json) AND length(profile_json)<=262144)),
    runtime_json TEXT CHECK(runtime_json IS NULL OR (json_valid(runtime_json) AND length(runtime_json)<=131072)),
    npc_state_version INTEGER NOT NULL CHECK(npc_state_version>=1 AND typeof(npc_state_version)='integer'),
    PRIMARY KEY(session_id,npc_id),
    FOREIGN KEY(session_id,npc_id) REFERENCES actors(session_id,actor_id),
    CHECK((configuration='unconfigured' AND profile_json IS NULL AND runtime_json IS NULL)
       OR (configuration='configured' AND profile_json IS NOT NULL AND runtime_json IS NOT NULL))
);
CREATE TRIGGER npc_states_require_npc BEFORE INSERT ON npc_states
WHEN NOT EXISTS (SELECT 1 FROM actors WHERE session_id=NEW.session_id AND actor_id=NEW.npc_id AND kind='npc')
BEGIN
    SELECT RAISE(ABORT,'NPC state requires existing NPC Actor');
END;
INSERT INTO npc_states
SELECT session_id,actor_id,'npc-state/1',NULL,NULL,'legacy','unconfigured',NULL,NULL,1
FROM actors WHERE kind='npc';

-- Static World Facts belong to SM; private knowledge is an explicitly granted subset.
CREATE TABLE world_facts (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    fact_id TEXT NOT NULL CHECK(length(fact_id)<=128 AND fact_id GLOB 'fact.*'),
    text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 4096),
    visibility TEXT NOT NULL CHECK(visibility IN ('npc_authorized','dm_only')),
    authorized_npc_ids_json TEXT NOT NULL CHECK(json_valid(authorized_npc_ids_json) AND length(authorized_npc_ids_json)<=16384),
    PRIMARY KEY(session_id,fact_id)
);
CREATE TABLE npc_known_facts (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY(session_id,npc_id,fact_id),
    FOREIGN KEY(session_id,npc_id) REFERENCES npc_states(session_id,npc_id),
    FOREIGN KEY(session_id,fact_id) REFERENCES world_facts(session_id,fact_id)
);
CREATE TRIGGER npc_known_facts_require_grant BEFORE INSERT ON npc_known_facts
WHEN NOT EXISTS (
    SELECT 1 FROM world_facts f,json_each(f.authorized_npc_ids_json) g
    WHERE f.session_id=NEW.session_id AND f.fact_id=NEW.fact_id
      AND f.visibility='npc_authorized' AND g.value=NEW.npc_id
)
BEGIN
    SELECT RAISE(ABORT,'World Fact is not authorized for this NPC');
END;

-- Closed domain kinds, immutable version content, one current version per stable ID.
-- JSON contains the bounded, type-specific fields, never arbitrary runtime patches.
CREATE TABLE npc_private_records (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    record_kind TEXT NOT NULL CHECK(record_kind IN ('memory','belief','relationship','goal','plan')),
    record_id TEXT NOT NULL CHECK(length(record_id)<=128 AND record_id GLOB (record_kind || '.*')),
    content_version INTEGER NOT NULL CHECK(content_version>=1 AND typeof(content_version)='integer'),
    is_current INTEGER NOT NULL CHECK(is_current IN (0,1)),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(payload_json)<=131072
        AND json_type(payload_json,'$.text') IS 'text'
        AND length(json_extract(payload_json,'$.text')) BETWEEN 1 AND 4096),
    npc_state_version INTEGER NOT NULL CHECK(npc_state_version>=1 AND typeof(npc_state_version)='integer'),
    PRIMARY KEY(session_id,npc_id,record_kind,record_id,content_version),
    FOREIGN KEY(session_id,npc_id) REFERENCES npc_states(session_id,npc_id),
    CHECK(record_kind NOT IN ('memory','belief') OR (
        json_type(payload_json,'$.epistemic_status') IS 'text'
        AND json_extract(payload_json,'$.epistemic_status') IN ('observed','inferred','told'))),
    CHECK(record_kind!='belief' OR (
        json_type(payload_json,'$.status') IS 'text'
        AND json_extract(payload_json,'$.status') IN ('active','retracted')
        AND (json_type(payload_json,'$.confidence_basis_points') IS 'null'
          OR (json_type(payload_json,'$.confidence_basis_points') IS 'integer'
            AND json_extract(payload_json,'$.confidence_basis_points') BETWEEN 0 AND 10000)))),
    CHECK(record_kind!='relationship' OR (
        json_type(payload_json,'$.target_id') IS 'text'
        AND json_type(payload_json,'$.stance') IS 'text'
        AND json_extract(payload_json,'$.stance') IN ('friendly','neutral','hostile'))),
    CHECK(record_kind NOT IN ('goal','plan') OR (
        json_type(payload_json,'$.status') IS 'text'
        AND json_extract(payload_json,'$.status') IN ('active','completed','abandoned'))),
    CHECK(record_kind!='plan' OR (
        json_type(payload_json,'$.goal_id') IS 'text'
        AND json_type(payload_json,'$.goal_version') IS 'integer'
        AND json_extract(payload_json,'$.goal_version')>=1
        AND json_type(payload_json,'$.steps') IS 'array'))
);
CREATE UNIQUE INDEX npc_current_record ON npc_private_records(session_id,npc_id,record_kind,record_id)
WHERE is_current=1;
CREATE TRIGGER npc_record_content_immutable BEFORE UPDATE ON npc_private_records
WHEN NEW.session_id!=OLD.session_id OR NEW.npc_id!=OLD.npc_id OR NEW.record_kind!=OLD.record_kind
  OR NEW.record_id!=OLD.record_id OR NEW.content_version!=OLD.content_version
  OR NEW.payload_json!=OLD.payload_json OR NEW.npc_state_version!=OLD.npc_state_version
  OR NEW.is_current>OLD.is_current
BEGIN
    SELECT RAISE(ABORT,'Private record history is immutable');
END;
CREATE TRIGGER npc_record_history_retained BEFORE DELETE ON npc_private_records
BEGIN
    SELECT RAISE(ABORT,'Private record history cannot be deleted');
END;
CREATE TABLE npc_record_sources (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    content_version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 3),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('package','memory')),
    reference_id TEXT NOT NULL CHECK(length(reference_id)<=128),
    reference_version INTEGER,
    memory_kind TEXT NOT NULL CHECK(memory_kind='memory'),
    PRIMARY KEY(session_id,npc_id,record_kind,record_id,content_version,ordinal),
    FOREIGN KEY(session_id,npc_id,record_kind,record_id,content_version)
        REFERENCES npc_private_records(session_id,npc_id,record_kind,record_id,content_version),
    FOREIGN KEY(session_id,npc_id,memory_kind,reference_id,reference_version)
        REFERENCES npc_private_records(session_id,npc_id,record_kind,record_id,content_version)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK((source_kind='package' AND reference_id=npc_id AND reference_version IS NULL)
       OR (source_kind='memory' AND reference_version>=1 AND typeof(reference_version)='integer'))
);
CREATE TRIGGER npc_sources_immutable BEFORE UPDATE ON npc_record_sources
BEGIN
    SELECT RAISE(ABORT,'Record provenance is immutable');
END;
CREATE TRIGGER npc_sources_retained BEFORE DELETE ON npc_record_sources
BEGIN
    SELECT RAISE(ABORT,'Record provenance cannot be deleted');
END;
CREATE TABLE npc_plan_goals (
    session_id TEXT NOT NULL,
    npc_id TEXT NOT NULL,
    plan_kind TEXT NOT NULL CHECK(plan_kind='plan'),
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    goal_kind TEXT NOT NULL CHECK(goal_kind='goal'),
    goal_id TEXT NOT NULL,
    goal_version INTEGER NOT NULL,
    PRIMARY KEY(session_id,npc_id,plan_id,plan_version),
    FOREIGN KEY(session_id,npc_id,plan_kind,plan_id,plan_version)
        REFERENCES npc_private_records(session_id,npc_id,record_kind,record_id,content_version),
    FOREIGN KEY(session_id,npc_id,goal_kind,goal_id,goal_version)
        REFERENCES npc_private_records(session_id,npc_id,record_kind,record_id,content_version)
        DEFERRABLE INITIALLY DEFERRED
);
PRAGMA user_version = 10;
COMMIT;
