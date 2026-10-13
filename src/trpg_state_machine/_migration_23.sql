BEGIN IMMEDIATE;
-- Preserve the old values verbatim while widening the local contract constraint.
-- No old session is assigned /17 or supplied missing component facts.
CREATE TEMP TABLE rf_previous_mechanical_versions AS
    SELECT session_id,mechanical_schema_version FROM sessions;
ALTER TABLE sessions DROP COLUMN mechanical_schema_version;
ALTER TABLE sessions ADD COLUMN mechanical_schema_version TEXT
    CHECK(mechanical_schema_version IN ('engine-snapshot/1','engine-snapshot/2','engine-snapshot/17'));
UPDATE sessions SET mechanical_schema_version = (
    SELECT mechanical_schema_version FROM rf_previous_mechanical_versions old
    WHERE old.session_id = sessions.session_id
);
DROP TABLE rf_previous_mechanical_versions;
CREATE TABLE mechanical_components (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    schema_version TEXT NOT NULL CHECK(schema_version = 'engine-snapshot/17'),
    inventory_json TEXT NOT NULL CHECK(json_valid(inventory_json)),
    resources_json TEXT NOT NULL CHECK(json_valid(resources_json)),
    casting_json TEXT NOT NULL CHECK(json_valid(casting_json)),
    effects_json TEXT NOT NULL CHECK(json_valid(effects_json))
);
PRAGMA user_version = 23;
COMMIT;
