"""Current-schema save acceptance and fail-closed pre-release storage boundaries."""

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from trpg_state_machine import StateMachine, StateMachineError
from trpg_state_machine.environment_versions import protection_key, source_key, wind_key
from trpg_state_machine.runtime_integrity import spatial_cells
from trpg_state_machine.storage import CURRENT_SCHEMA_VERSION


def test_pre41_database_is_refused_before_ddl_and_preserves_source_bytes(tmp_path):
    """Opening an old development save cannot silently discard aggregate stock."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE old_inventory (item TEXT NOT NULL)")
        conn.execute("INSERT INTO old_inventory VALUES ('item.kept')")
        conn.execute("PRAGMA user_version = 40")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 40
        assert conn.execute("SELECT item FROM old_inventory").fetchall() == [("item.kept",)]


def test_schema43_resource_aggregate_is_refused_before_writable_open(tmp_path):
    """The previous development schema cannot be promoted after resource ownership moves."""
    path = tmp_path / "schema43.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_resources (balance TEXT)")
        conn.execute("INSERT INTO old_resources VALUES ('preserve')")
        conn.execute("PRAGMA user_version = 43")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT balance FROM old_resources").fetchone() == ("preserve",)


def test_schema44_casting_aggregate_is_refused_before_writable_open(tmp_path):
    """Old casting JSON stays byte-for-byte intact when the former schema is opened."""
    path = tmp_path / "schema44.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_casting (value TEXT)")
        conn.execute("INSERT INTO old_casting VALUES ('preserve')")
        conn.execute("PRAGMA user_version = 44")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM old_casting").fetchone() == ("preserve",)


def test_schema46_life_aggregate_is_refused_before_writable_open(tmp_path):
    """The former JSON life facts cannot be implicitly promoted on reopen."""
    path = tmp_path / "schema46.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_life (state_json TEXT)")
        conn.execute("INSERT INTO old_life VALUES ('{\"temp_hp\":3}')")
        conn.execute("PRAGMA user_version = 46")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_schema48_item_json_is_refused_before_writable_open(tmp_path):
    """Old portable JSON facts cannot be implicitly promoted on reopen."""
    path = tmp_path / "schema48.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_objects (item_json TEXT NOT NULL)")
        conn.execute('INSERT INTO old_objects VALUES (\'{"item_slug":"kept"}\')')
        conn.execute("PRAGMA user_version = 48")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 48
        assert conn.execute("SELECT item_json FROM old_objects").fetchone()[0] == (
            '{"item_slug":"kept"}'
        )


def test_schema49_save_is_refused_before_writable_open(tmp_path):
    """The previous development schema stays intact when opening is refused."""
    path = tmp_path / "schema49.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_items (item_slug TEXT NOT NULL)")
        conn.execute("INSERT INTO old_items VALUES ('item.kept')")
        conn.execute("PRAGMA user_version = 49")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 49
        assert conn.execute("SELECT item_slug FROM old_items").fetchone() == ("item.kept",)


def test_storage02_schema50_is_refused_without_modifying_source(tmp_path):
    """A genuine empty Schema50 save is refused before a writable open."""
    path = tmp_path / "storage02.db"
    schema_dir = Path(__file__).resolve().parents[1] / "src" / "trpg_state_machine"
    with sqlite3.connect(path) as conn:
        for name, function in (
            ("runtime_spatial_cells", spatial_cells),
            ("env_source_key", source_key),
            ("env_protection_key", protection_key),
            ("env_wind_key", wind_key),
        ):
            conn.create_function(
                name, 5 if name == "runtime_spatial_cells" else 1, function, deterministic=True
            )
        conn.executescript((schema_dir / "_schema.sql").read_text("utf-8"))
        for version in range(2, 51):
            conn.executescript((schema_dir / f"_migration_{version}.sql").read_text("utf-8"))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 50
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='character_attributes'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='character_proficiencies'"
        ).fetchone() == (1,)
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 50


def test_schema51_equipment_json_save_is_refused_without_writing(tmp_path):
    """The previous save format is left intact after equipment becomes a typed fact."""
    path = tmp_path / "schema51.db"
    with StateMachine(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE character_equipment")
        conn.execute("PRAGMA user_version = 51")
    before = path.read_bytes()
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert path.read_bytes() == before


def test_pre41_crashed_wal_is_refused_without_checkpointing_source(tmp_path):
    """An orphan WAL keeps its committed stock and original main/WAL bytes."""
    path = tmp_path / "orphan.db"
    writer = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('PRAGMA wal_autocheckpoint=0')
conn.execute('CREATE TABLE old_inventory (item TEXT NOT NULL)')
conn.execute("INSERT INTO old_inventory VALUES ('item.kept')")
conn.execute('PRAGMA user_version=40')
conn.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", writer, str(path)], check=True)
    wal = path.with_name(path.name + "-wal")
    before = (hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(wal.read_bytes()).digest())
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert (
        hashlib.sha256(path.read_bytes()).digest(),
        hashlib.sha256(wal.read_bytes()).digest(),
    ) == before
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 40
        assert conn.execute("SELECT item FROM old_inventory").fetchall() == [("item.kept",)]


def test_unversioned_nonempty_database_is_refused_without_writes(tmp_path):
    """A view-only development database is not an empty schema-setup target."""
    path = tmp_path / "unversioned.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE VIEW existing_data AS SELECT 1 AS value")
    before = hashlib.sha256(path.read_bytes()).digest()
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_current_save_reopens_without_inventory_aggregate_columns(
    tmp_path, package, request_for, context
):
    """Empty database setup and current noncombat save/resume stay usable."""
    path = tmp_path / "current.db"
    with StateMachine(path) as sm:
        created = sm.create_session(request_for(), package, context=context)
        saved = sm.save_session(created.session_id, package, context=context)
        assert sm._conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        for table in ("characters", "mechanical_components"):
            columns = {row[1] for row in sm._conn.execute(f"PRAGMA table_info({table})")}
            assert "inventory_json" not in columns
            assert "casting_json" not in columns
    with StateMachine(path) as sm:
        assert sm.resume_session(created.session_id, package, context=context) == saved


def test_current_schema_refuses_missing_required_spatial_trigger(tmp_path):
    """Reopen cannot trust a current database without its maintenance trigger."""
    path = tmp_path / "damaged.db"
    with StateMachine(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER spatial_index_objects_update")
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"


def test_current_schema_refuses_missing_spell_pair_guard(tmp_path):
    """Reopen rejects a missing pair trigger before serving material bindings."""
    path = tmp_path / "damaged-spells.db"
    with StateMachine(path):
        pass
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER character_spells_material_pair_insert")
    with pytest.raises(StateMachineError) as error:
        StateMachine(path)
    assert error.value.code == "unsupported_storage_schema"
