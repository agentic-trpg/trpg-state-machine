"""In-process foundation data; not a frozen cross-module wire protocol."""

from dataclasses import dataclass

from .package import RulesetBinding


@dataclass(frozen=True, slots=True)
class TrustedContext:
    """Supplied ONLY by an authenticated integration, never deserialized from payload.

    This library does not implement authentication. The adapter must establish this
    principal's control of player_id before constructing the context.
    """

    principal_id: str
    player_id: str
    controlled_npc_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlayerSelection:
    player_id: str
    pc_template_id: str


@dataclass(frozen=True, slots=True)
class CreateSessionRequest:
    command_id: str
    adventure_id: str
    package_version: str
    package_digest: str
    players: tuple[PlayerSelection, ...]


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
    flags: dict[str, bool]


@dataclass(frozen=True, slots=True)
class SceneInstance:
    scene_id: str
    visited_count: int
    local_flags: dict[str, bool]
    objects: dict[str, str]


@dataclass(frozen=True, slots=True)
class ActorState:
    actor_id: str
    kind: str
    current_scene_id: str


@dataclass(frozen=True, slots=True)
class CharacterState:
    actor_id: str
    current_hp: int
    max_hp: int
    resources: dict
    inventory: tuple
    effects: tuple = ()


@dataclass(frozen=True, slots=True)
class RNGState:
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
