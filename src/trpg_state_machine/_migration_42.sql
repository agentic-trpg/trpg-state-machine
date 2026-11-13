-- Extend the admitted version of fixed mechanical inputs without rewriting actor facts.
ALTER TABLE sessions RENAME COLUMN mechanical_schema_version TO prior_mechanical_schema_version;
ALTER TABLE sessions ADD COLUMN mechanical_schema_version TEXT
    CHECK(mechanical_schema_version IN ('engine-snapshot/1','engine-snapshot/2','engine-snapshot/17','engine-snapshot/18'));
UPDATE sessions SET mechanical_schema_version = prior_mechanical_schema_version;
ALTER TABLE sessions DROP COLUMN prior_mechanical_schema_version;

ALTER TABLE mechanical_components RENAME COLUMN schema_version TO prior_schema_version;
ALTER TABLE mechanical_components ADD COLUMN schema_version TEXT NOT NULL DEFAULT 'engine-snapshot/17'
    CHECK(schema_version IN ('engine-snapshot/17','engine-snapshot/18'));
ALTER TABLE mechanical_components DROP COLUMN prior_schema_version;
PRAGMA user_version = 42;
