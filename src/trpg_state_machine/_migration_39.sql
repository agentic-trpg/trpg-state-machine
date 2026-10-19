-- Effective configuration is separate from changing domain facts. Existing saves
-- deliberately remain incomplete until the explicit, source-checked upgrade.
CREATE TABLE IF NOT EXISTS instance_configs (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    domain TEXT NOT NULL CHECK(domain IN ('session','scene','object','actor')),
    instance_id TEXT NOT NULL,
    config_json TEXT NOT NULL CHECK(json_valid(config_json)),
    PRIMARY KEY(session_id,domain,instance_id)
);
-- Existing (session,container) index loses to the PK for ordered contents.
-- Extend it to satisfy both lookup predicates AND the public object-id order.
DROP INDEX IF EXISTS objects_container;
CREATE INDEX objects_container ON objects(session_id,container_id,object_id)
    WHERE container_id IS NOT NULL;
PRAGMA user_version = 39;
