"""Spatial members of Effects, with the existing Session clock and concentration owner."""

import json

from dnd5e_srd_data.schema.environment import EnvironmentalSpec

from .errors import StateMachineError
from .package import canonical_json
from .scoped_reads import scoped_read

_OBSCURING = "json_extract(environment_json, '$.obscurement') != 'none'"
_SILENCE = "json_extract(environment_json, '$.kind') = 'magical_silence'"


class SpatialLightEffects:
    """Store admitted spell contributions and keyed lifecycle pre-values in the caller's UoW."""

    def __init__(self, owner):
        """Share the actor-effect concentration owner and transaction-bound Store."""
        self.owner = owner
        self.store = owner.store

    @property
    def db(self):
        """Use the transaction connection that owns Effects writes."""
        return self.store.connection

    def fixed(self, sid, scene):
        """Read fixed effects through the cached range for their actual Scene."""
        return tuple(
            self.db.execute(
                "SELECT * FROM spatial_light_effects WHERE session_id=? AND fixed_scene_id=? "
                "ORDER BY effect_id",
                (sid, scene),
            )
        )

    @scoped_read
    def fixed_magic_at(self, sid, scene, cells):
        """Locate exact fixed spell origins of every admitted kind in selected cells."""
        return tuple(
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT fixed_cell FROM spatial_light_effects "
                "WHERE session_id=? AND fixed_scene_id=? AND fixed_cell IS NOT NULL "
                "AND fixed_cell IN (SELECT value FROM json_each(?))",
                (sid, scene, json.dumps(cells)),
            )
        )

    @scoped_read
    def fixed_illuminating(self, sid, scene):
        """Read light-bearing fixed spells without scanning obscuring area rows."""
        return tuple(
            self.db.execute(
                "SELECT * FROM spatial_light_effects WHERE session_id=? AND fixed_scene_id=? "
                "AND fixed_cell IS NOT NULL "
                "AND json_extract(environment_json, '$.light') IS NOT NULL "
                "ORDER BY effect_id",
                (sid, scene),
            )
        )

    @scoped_read
    def maximum_fixed_radius(self, sid, scene, *, silence=False):
        """Use the source-owned radius index to bound exact origin-cell lookup."""
        predicate = _SILENCE if silence else _OBSCURING
        row = self.db.execute(
            "SELECT radius_ft FROM spatial_light_effects "
            "WHERE session_id=? AND fixed_scene_id=? AND fixed_cell IS NOT NULL "
            f"AND {predicate} "
            "ORDER BY radius_ft DESC LIMIT 1",
            (sid, scene),
        ).fetchone()
        return row[0] if row else 0

    @scoped_read
    def fixed_at(self, sid, scene, cells, *, silence=False):
        """Fetch only matching fixed spells whose origins are exact candidate cells."""
        predicate = _SILENCE if silence else _OBSCURING
        index = "spatial_silence_cell" if silence else "spatial_light_obscuring_cell"
        records = []
        selected = tuple(sorted(cells))
        for start in range(0, len(selected), 400):
            batch = selected[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            records.extend(
                self.db.execute(
                    f"SELECT * FROM spatial_light_effects INDEXED BY {index} "
                    "WHERE session_id=? AND fixed_scene_id=? AND fixed_cell IS NOT NULL "
                    f"AND {predicate} AND fixed_cell IN ({placeholders})",
                    (sid, scene, *batch),
                )
            )
        return tuple(sorted(records, key=lambda record: record["effect_id"]))

    def attached(self, sid, oid):
        """Read the cached range of one known object's attached spatial effects."""
        return tuple(
            self.db.execute(
                "SELECT * FROM spatial_light_effects WHERE session_id=? AND object_id=? "
                "ORDER BY effect_id",
                (sid, oid),
            )
        )

    @scoped_read
    def get(self, sid, effect_id):
        """Resolve the complete application identity in this Session."""
        return self.db.execute(
            "SELECT * FROM spatial_light_effects WHERE session_id=? AND effect_id=?",
            (sid, effect_id),
        ).fetchone()

    @scoped_read
    def for_concentration(self, sid, concentration_id):
        """Resolve the actual spatial member through the unique group link."""
        return self.db.execute(
            "SELECT * FROM spatial_light_effects WHERE session_id=? AND concentration_id=?",
            (sid, concentration_id),
        ).fetchone()

    @scoped_read
    def concentration(self, sid, caster):
        """Find a caster's spatial member of the shared concentration group."""
        group = self.db.execute(
            "SELECT concentration_id FROM effect_concentrations WHERE session_id=? AND caster_id=?",
            (sid, caster),
        ).fetchone()
        return self.for_concentration(sid, group[0]) if group else None

    @scoped_read
    def spec(self, record):
        """Rehydrate the immutable canonical rule profile stored at cast adoption."""
        return EnvironmentalSpec.model_validate(self.store.memory.json(record["environment_json"]))

    def add(self, sid, scene, command_id, proposal, deadline):
        """Store a verified cast with its elapsed-time deadline and optional concentration link."""
        self.store.require_write()
        identity = f"spatial:{command_id}"
        if self.get(sid, identity) is not None:
            raise StateMachineError("invalid_effect", "Spatial effect already exists")
        concentration = identity if proposal.concentration else None
        if concentration is not None:
            self.db.execute(
                "INSERT INTO effect_concentrations VALUES (?,?,?,?,0)",
                (sid, concentration, proposal.source_id, identity),
            )
        fixed = proposal.anchor.kind == "point"
        self.db.execute(
            "INSERT INTO spatial_light_effects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                identity,
                proposal.source_id,
                proposal.spell_source_id,
                proposal.spell_id,
                proposal.slot_level,
                proposal.radius_ft,
                proposal.shape,
                canonical_json(proposal.environment.model_dump(mode="json")),
                scene if fixed else None,
                proposal.anchor.cell_id if fixed else None,
                None if fixed else proposal.anchor.item_id,
                deadline,
                concentration,
            ),
        )
        return identity

    @scoped_read
    def by_spell(self, sid, caster, spell):
        """Read exact applications eligible for a rules-declared same-caster replacement."""
        return tuple(
            self.db.execute(
                "SELECT * FROM spatial_light_effects WHERE session_id=? AND caster_id=? "
                "AND spell_id=? ORDER BY effect_id",
                (sid, caster, spell),
            )
        )

    def remove(self, record):
        """End one expected application, releasing its anchor relation and concentration."""
        self.store.require_write()
        deleted = self.db.execute(
            "DELETE FROM spatial_light_effects WHERE session_id=? AND effect_id=? "
            "AND expires_tick=?",
            (record["session_id"], record["effect_id"], record["expires_tick"]),
        )
        if deleted.rowcount != 1:
            raise StateMachineError("invalid_effect", "Spatial effect identity or deadline changed")

    def next_deadline(self, sid):
        """Read the earliest active spatial deadline from its Session index."""
        row = self.db.execute(
            "SELECT expires_tick FROM spatial_light_effects WHERE session_id=? "
            "ORDER BY expires_tick,effect_id LIMIT 1",
            (sid,),
        ).fetchone()
        return row[0] if row else None

    def due(self, sid, tick):
        """Select one exact active application whose deadline has arrived."""
        return self.db.execute(
            "SELECT * FROM spatial_light_effects WHERE session_id=? AND expires_tick<=? "
            "ORDER BY expires_tick,effect_id LIMIT 1",
            (sid, tick),
        ).fetchone()
