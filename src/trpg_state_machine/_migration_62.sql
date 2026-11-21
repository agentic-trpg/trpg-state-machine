-- Activity owns cast progress; its active focus refers to the Effects owner.
CREATE TABLE spell_cast_activities (
    session_id TEXT NOT NULL,
    activity_id TEXT NOT NULL CHECK(length(activity_id) BETWEEN 1 AND 256),
    actor_id TEXT NOT NULL,
    spell_id TEXT NOT NULL CHECK(length(spell_id) BETWEEN 1 AND 256),
    spell_source_id TEXT NOT NULL CHECK(length(spell_source_id) BETWEEN 1 AND 256),
    slot_level INTEGER,
    as_ritual INTEGER NOT NULL CHECK(as_ritual IN (0, 1)),
    start_tick INTEGER NOT NULL CHECK(start_tick >= 0),
    deadline_tick INTEGER NOT NULL CHECK(deadline_tick > start_tick AND deadline_tick <= 2147483647),
    status TEXT NOT NULL CHECK(status IN ('casting', 'waiting', 'completed', 'cancelled')),
    activity_version INTEGER NOT NULL CHECK(activity_version >= 1),
    concentration_id TEXT,
    source_command_id TEXT NOT NULL,
    last_command_id TEXT NOT NULL,
    PRIMARY KEY(session_id, activity_id),
    UNIQUE(session_id, concentration_id),
    FOREIGN KEY(session_id, actor_id) REFERENCES mechanical_actors(session_id, actor_id),
    FOREIGN KEY(session_id, concentration_id)
        REFERENCES effect_concentrations(session_id, concentration_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id, source_command_id)
        REFERENCES command_receipts(session_id, command_id) DEFERRABLE INITIALLY DEFERRED,
    -- A time prefix can reach the deadline before its outer command has a receipt.
    CHECK((status IN ('casting', 'waiting')) = (concentration_id IS NOT NULL)),
    CHECK(
        (as_ritual = 1 AND slot_level IS NULL)
        OR (as_ritual = 0 AND slot_level IS NOT NULL AND slot_level BETWEEN 1 AND 9)
    )
);
CREATE UNIQUE INDEX spell_cast_actor_active
    ON spell_cast_activities(session_id, actor_id) WHERE status IN ('casting', 'waiting');
CREATE INDEX spell_cast_next_deadline
    ON spell_cast_activities(session_id, deadline_tick, activity_id) WHERE status = 'casting';
PRAGMA user_version = 62;
