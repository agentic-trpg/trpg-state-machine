-- Absent binding means game-clock/1, with every historical tick left untouched.
CREATE TABLE world_time_bindings (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    binding_json TEXT NOT NULL CHECK(json_valid(binding_json))
);
-- Binding selection is legal only before publishing a new Session's first event.
CREATE TRIGGER world_time_binding_initialization
BEFORE INSERT ON world_time_bindings
WHEN EXISTS (
    SELECT 1 FROM sessions s JOIN world_states w USING(session_id)
    WHERE s.session_id=NEW.session_id AND (s.event_seq<>0 OR w.game_tick<>0)
)
BEGIN
    SELECT RAISE(ABORT, 'Cannot reinterpret an existing Session clock');
END;
CREATE TRIGGER immutable_world_time_binding_update
BEFORE UPDATE ON world_time_bindings BEGIN
    SELECT RAISE(ABORT, 'Session time binding is immutable');
END;
CREATE TRIGGER immutable_world_time_binding_delete
BEFORE DELETE ON world_time_bindings BEGIN
    SELECT RAISE(ABORT, 'Session time binding is immutable');
END;
PRAGMA user_version = 32;
