"""In-process foundation data; not a frozen cross-module wire protocol."""

from dataclasses import dataclass

from .package import RulesetBinding
from .time_binding import LegacyTimeBinding, TimeBinding


@dataclass(frozen=True, slots=True)
class TrustedContext:
    """Supplied ONLY by an authenticated integration, never deserialized from payload.

    This library does not implement authentication. The adapter must establish this
    principal's control of player_id before constructing the context.
    """

    principal_id: str
    player_id: str
    controlled_npc_ids: tuple[str, ...] = ()
    can_adjudicate_checks: bool = False
    can_advance_time: bool = False
    can_place_world_objects: bool = False
    can_manage_environment: bool = False


@dataclass(frozen=True, slots=True)
class PlayerSelection:
    """Bind a selected player identity to its PC creation template."""

    player_id: str
    pc_template_id: str


@dataclass(frozen=True, slots=True)
class CreateSessionRequest:
    """Carry idempotent creation identity, fixed Package binding and player selection."""

    command_id: str
    adventure_id: str
    package_version: str
    package_digest: str
    players: tuple[PlayerSelection, ...]
    time_binding: TimeBinding | None = None


@dataclass(frozen=True, slots=True)
class CreationResult:
    """Limited metadata; the SAME stored result is returned on creation retries."""

    session_id: str
    adventure_id: str
    package_version: str
    package_digest: str
    ruleset_binding: RulesetBinding
    world_version: int
    event_seq: int


@dataclass(frozen=True, slots=True)
class WorldState:
    """Carry detached world flags and the sole game-clock value/binding."""

    flags: dict[str, bool]
    game_tick: int = 0
    time_binding: TimeBinding | LegacyTimeBinding = LegacyTimeBinding()


@dataclass(frozen=True, slots=True)
class SceneInstance:
    """Carry detached scene visit, flag and object placement facts."""

    scene_id: str
    visited_count: int
    local_flags: dict[str, bool]
    objects: dict[str, str | None]
    object_cells: dict[str, str | None] | None = None


@dataclass(frozen=True, slots=True)
class ActorState:
    """Carry an actor's identity, kind and authoritative placement projection."""

    actor_id: str
    kind: str
    current_scene_id: str
    cell: str | None = None


@dataclass(frozen=True, slots=True)
class CharacterState:
    """Carry detached HP, resources, inventory, effects and experience facts."""

    actor_id: str
    current_hp: int
    max_hp: int
    resources: dict
    inventory: tuple
    effects: tuple = ()
    xp: int = 0


@dataclass(frozen=True, slots=True)
class RNGState:
    """Carry persisted RNG identity, algorithm, position and encoded state/version."""

    state_id: str
    algorithm: str
    encoding: str
    position: int
    rng_version: int = 0
    state_json: str | None = None


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Detached trusted runtime snapshot. Editing it never changes authority."""

    session_id: str
    adventure_id: str
    package_version: str
    package_digest: str
    ruleset_binding: RulesetBinding
    player: PlayerSelection
    pc_actor_id: str
    current_scene_id: str
    world_version: int
    event_seq: int
    world: WorldState
    scenes: dict[str, SceneInstance]
    actors: dict[str, ActorState]
    character: CharacterState
    rng: RNGState
    phase: str = "exploration"
