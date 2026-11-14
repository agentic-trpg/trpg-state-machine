-- Fresh-development Schema47: per-actor life facts captured separately from rules JSON.
CREATE TABLE mechanical_life (
    session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    temp_hp INTEGER NOT NULL CHECK(typeof(temp_hp)='integer' AND temp_hp >= 0),
    is_alive INTEGER NOT NULL CHECK(typeof(is_alive)='integer' AND is_alive IN (0,1)),
    death_save_successes INTEGER
        CHECK(death_save_successes IS NULL OR
              (typeof(death_save_successes)='integer' AND death_save_successes BETWEEN 0 AND 3)),
    death_save_failures INTEGER
        CHECK(death_save_failures IS NULL OR
              (typeof(death_save_failures)='integer' AND death_save_failures BETWEEN 0 AND 4)),
    death_save_stable INTEGER
        CHECK((death_save_successes IS NULL AND death_save_failures IS NULL AND death_save_stable IS NULL)
           OR (death_save_successes IS NOT NULL AND death_save_failures IS NOT NULL
               AND typeof(death_save_stable)='integer' AND death_save_stable IN (0,1))),
    last_damaged_by TEXT CHECK(last_damaged_by IS NULL OR typeof(last_damaged_by)='text'),
    PRIMARY KEY(session_id,actor_id),
    FOREIGN KEY(session_id,actor_id) REFERENCES mechanical_actors(session_id,actor_id)
        ON DELETE CASCADE
);
PRAGMA user_version = 47;
