-- Orientation is an instance fact beside its anchor; old footprints face north.
ALTER TABLE objects ADD COLUMN orientation TEXT NOT NULL DEFAULT 'north'
    CHECK(orientation IN ('north','east','south','west'));
PRAGMA user_version = 33;
