-- RUO-55: explicit initial surface stance belongs to the actor's current facts.
CREATE TABLE actor_surface_stance (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    wall_contact_json TEXT NOT NULL DEFAULT '[]',
    airborne INTEGER NOT NULL DEFAULT 0 CHECK(airborne IN (0,1)),
    PRIMARY KEY(session_id,actor_id),
    FOREIGN KEY(session_id,actor_id) REFERENCES actors(session_id,actor_id) ON DELETE CASCADE
);
CREATE TRIGGER clear_actor_wall_contact_after_move
AFTER UPDATE OF current_scene_id,cell ON actors
WHEN OLD.current_scene_id IS NOT NEW.current_scene_id OR OLD.cell IS NOT NEW.cell
BEGIN
    UPDATE actor_surface_stance SET wall_contact_json='[]',airborne=0
    WHERE session_id=NEW.session_id AND actor_id=NEW.actor_id;
END;
PRAGMA user_version = 71;
