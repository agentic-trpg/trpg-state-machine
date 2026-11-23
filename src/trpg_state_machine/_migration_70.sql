-- Fresh-development Schema70: source-linked target visual effects.
-- Each source persists even when every creature saves. Target rows describe
-- independently selected creatures or objects. World owns their placement.
CREATE TABLE visual_effect_sources (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    source_id TEXT NOT NULL CHECK(length(source_id) > 0),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('spell','activity')),
    caster_id TEXT NOT NULL,
    spell_id TEXT,
    spell_source_id TEXT,
    expires_tick INTEGER NOT NULL CHECK(expires_tick >= 0),
    concentration_id TEXT,
    PRIMARY KEY(session_id,source_id),
    FOREIGN KEY(session_id,caster_id) REFERENCES mechanical_actors(session_id,actor_id),
    FOREIGN KEY(session_id,concentration_id)
        REFERENCES effect_concentrations(session_id,concentration_id)
);
CREATE INDEX visual_effect_sources_deadline
    ON visual_effect_sources(session_id,expires_tick,source_id);
CREATE INDEX visual_effect_sources_group
    ON visual_effect_sources(session_id,concentration_id);
CREATE TABLE target_visual_effects (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    effect_id TEXT NOT NULL CHECK(length(effect_id) > 0),
    source_id TEXT NOT NULL CHECK(length(source_id) > 0),
    target_kind TEXT NOT NULL CHECK(target_kind IN ('creature','object')),
    target_id TEXT NOT NULL CHECK(length(target_id) > 0),
    dim_radius_ft INTEGER NOT NULL CHECK(dim_radius_ft >= 0),
    suppresses_invisible INTEGER NOT NULL CHECK(suppresses_invisible IN (0,1)),
    visible_attack_advantage INTEGER NOT NULL CHECK(visible_attack_advantage IN (0,1)),
    PRIMARY KEY(session_id,source_id,effect_id,target_kind,target_id),
    FOREIGN KEY(session_id,source_id) REFERENCES visual_effect_sources(session_id,source_id)
        ON DELETE CASCADE
);
CREATE INDEX target_visual_effects_target
    ON target_visual_effects(session_id,target_kind,target_id,source_id,effect_id);
-- Derived membership only: live positions stay in actors/objects.
CREATE TABLE visual_light_anchors (
    session_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    effect_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    scene_id TEXT,
    PRIMARY KEY(session_id,source_id,effect_id,target_kind,target_id),
    FOREIGN KEY(session_id,source_id,effect_id,target_kind,target_id)
        REFERENCES target_visual_effects(session_id,source_id,effect_id,target_kind,target_id)
);
CREATE INDEX visual_light_anchors_scene
    ON visual_light_anchors(session_id,scene_id,source_id,target_id);
CREATE TRIGGER add_visual_light_anchor AFTER INSERT ON target_visual_effects
WHEN NEW.dim_radius_ft>0 BEGIN
    INSERT INTO visual_light_anchors VALUES (
        NEW.session_id,NEW.source_id,NEW.effect_id,NEW.target_kind,NEW.target_id,
        CASE WHEN NEW.target_kind='creature' THEN (
            SELECT current_scene_id FROM actors
            WHERE session_id=NEW.session_id AND actor_id=NEW.target_id
        ) ELSE (
            WITH RECURSIVE ancestors(scene_id,carrier_id,container_id) AS (
                SELECT scene_id,carrier_id,container_id FROM objects
                WHERE session_id=NEW.session_id AND object_id=NEW.target_id
                UNION SELECT o.scene_id,o.carrier_id,o.container_id FROM objects o JOIN ancestors a
                    ON o.object_id=a.container_id WHERE o.session_id=NEW.session_id
            )
            SELECT coalesce(p.scene_id,a.current_scene_id) FROM ancestors p LEFT JOIN actors a
                ON a.session_id=NEW.session_id AND a.actor_id=p.carrier_id
                WHERE p.container_id IS NULL
        ) END
    );
END;
CREATE TRIGGER remove_visual_light_anchor AFTER DELETE ON target_visual_effects BEGIN
    DELETE FROM visual_light_anchors WHERE session_id=OLD.session_id
        AND source_id=OLD.source_id AND effect_id=OLD.effect_id
        AND target_kind=OLD.target_kind AND target_id=OLD.target_id;
END;
CREATE TRIGGER remove_visual_object_target AFTER DELETE ON objects BEGIN
    DELETE FROM target_visual_effects WHERE session_id=OLD.session_id
        AND target_kind='object' AND target_id=OLD.object_id;
END;
CREATE TRIGGER remove_visual_creature_target AFTER DELETE ON mechanical_actors BEGIN
    DELETE FROM target_visual_effects WHERE session_id=OLD.session_id
        AND target_kind='creature' AND target_id=OLD.actor_id;
END;
CREATE TRIGGER move_visual_creature AFTER UPDATE OF current_scene_id ON actors
WHEN OLD.current_scene_id IS NOT NEW.current_scene_id BEGIN
    UPDATE visual_light_anchors SET scene_id=NEW.current_scene_id
    WHERE session_id=NEW.session_id AND target_kind='creature' AND target_id=NEW.actor_id;
    UPDATE visual_light_anchors SET scene_id=NEW.current_scene_id
    WHERE session_id=NEW.session_id AND target_kind='object' AND target_id IN (
        WITH RECURSIVE descendants(object_id) AS (
            SELECT object_id FROM objects WHERE session_id=NEW.session_id AND carrier_id=NEW.actor_id
            UNION SELECT o.object_id FROM objects o JOIN descendants d ON o.container_id=d.object_id
                WHERE o.session_id=NEW.session_id
        ) SELECT object_id FROM descendants
    );
END;
CREATE TRIGGER move_visual_object AFTER UPDATE OF scene_id,carrier_id,container_id ON objects
WHEN OLD.scene_id IS NOT NEW.scene_id OR OLD.carrier_id IS NOT NEW.carrier_id
     OR OLD.container_id IS NOT NEW.container_id BEGIN
    UPDATE visual_light_anchors SET scene_id=(
        WITH RECURSIVE ancestors(scene_id,carrier_id,container_id) AS (
            SELECT NEW.scene_id,NEW.carrier_id,NEW.container_id
            UNION SELECT o.scene_id,o.carrier_id,o.container_id FROM objects o JOIN ancestors a
                ON o.object_id=a.container_id WHERE o.session_id=NEW.session_id
        )
        SELECT coalesce(p.scene_id,a.current_scene_id) FROM ancestors p LEFT JOIN actors a
            ON a.session_id=NEW.session_id AND a.actor_id=p.carrier_id WHERE p.container_id IS NULL
    ) WHERE session_id=NEW.session_id AND target_kind='object' AND target_id IN (
        WITH RECURSIVE descendants(object_id) AS (
            SELECT NEW.object_id
            UNION SELECT o.object_id FROM objects o JOIN descendants d ON o.container_id=d.object_id
                WHERE o.session_id=NEW.session_id
        ) SELECT object_id FROM descendants
    );
END;
PRAGMA user_version = 70;
