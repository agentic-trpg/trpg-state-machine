"""Versioned local state machine foundation."""

from ._operations import (
    DropItem,
    DropWorldConcentration,
    EnterScene,
    LockDoor,
    MoveActor,
    ObjectState,
    PickupItem,
    PlaceObject,
    SetObjectState,
    SetSceneFlag,
    StepActor,
    TraversePassage,
    UnlockDoor,
)
from .choice_models import ChoiceRespondOperationPayload, PendingChoice
from .combat import InitializeCombat
from .commands import CheckWorldEffects, CommandReceipt, TypedCommand, world_command_from_data
from .environment import EnvironmentService
from .environment_commands import (
    AddEnvironmentSource,
    BatchEnvironment,
    InitializeEnvironment,
    RemoveEnvironmentSource,
    ReplaceEnvironmentSource,
    SetEnvironmentBase,
    SetEnvironmentProtection,
)
from .environment_models import (
    EnvironmentArea,
    EnvironmentConditions,
    EnvironmentSceneFacts,
    EnvironmentSource,
    HeightInterval,
    Obscurant,
    Precipitation,
    SceneEnvironmentProtection,
    StaticPrecipitationProtection,
    StaticWindProtection,
    Temperature,
    Wind,
)
from .errors import StateMachineError
from .game_time import AdvanceGameTime
from .models import CharacterAC, CreateSessionRequest, PlayerSelection, TrustedContext
from .navigation_driver import pump_navigation
from .navigation_models import (
    AdvanceNavigation,
    CancelNavigation,
    ResumeNavigation,
    StartNavigation,
)
from .package import load_adventure_package
from .rest_activity import HitDiceSpend, LongRest, ShortRest
from .scene_lifecycle import SceneLifecycleService
from .service import StateMachine
from .time_binding import LegacyTimeBinding, TimeBinding, time_binding_from_data
from .world_commands import (
    ActivateMechanism,
    AimLight,
    BarObject,
    CheckMechanism,
    ClearJam,
    EquipArmor,
    EquipShield,
    EquipWeapon,
    ForceOpen,
    LockObject,
    PickLock,
    PutItem,
    RepositionObject,
    ServiceMechanism,
    SetLightEnabled,
    SpendItemCharge,
    SwitchLightMode,
    TakeItem,
    UnbarObject,
    UnequipArmor,
    UnequipShield,
    UnequipWeapon,
    UnlockObject,
)
from .world_time import (
    BoundaryPage,
    TimeBoundary,
    TimeDuration,
    WorldTimeService,
    WorldTimeSnapshot,
)

__all__ = [
    "EnvironmentService",
    "EnvironmentArea",
    "EnvironmentConditions",
    "EnvironmentSceneFacts",
    "EnvironmentSource",
    "HeightInterval",
    "Obscurant",
    "Precipitation",
    "SceneEnvironmentProtection",
    "StaticPrecipitationProtection",
    "StaticWindProtection",
    "Temperature",
    "Wind",
    "AddEnvironmentSource",
    "BatchEnvironment",
    "InitializeEnvironment",
    "RemoveEnvironmentSource",
    "ReplaceEnvironmentSource",
    "SetEnvironmentBase",
    "SetEnvironmentProtection",
    "TimeBinding",
    "LegacyTimeBinding",
    "time_binding_from_data",
    "WorldTimeService",
    "WorldTimeSnapshot",
    "TimeDuration",
    "TimeBoundary",
    "BoundaryPage",
    "ActivateMechanism",
    "ServiceMechanism",
    "CheckMechanism",
    "SetLightEnabled",
    "SwitchLightMode",
    "AimLight",
    "RepositionObject",
    "PutItem",
    "TakeItem",
    "SpendItemCharge",
    "EquipWeapon",
    "CharacterAC",
    "EquipArmor",
    "UnequipArmor",
    "EquipShield",
    "UnequipShield",
    "UnequipWeapon",
    "LockObject",
    "UnlockObject",
    "PickLock",
    "ForceOpen",
    "ClearJam",
    "BarObject",
    "UnbarObject",
    "UnlockDoor",
    "LockDoor",
    "DropItem",
    "DropWorldConcentration",
    "PickupItem",
    "StartNavigation",
    "AdvanceNavigation",
    "ResumeNavigation",
    "CancelNavigation",
    "HitDiceSpend",
    "LongRest",
    "ShortRest",
    "pump_navigation",
    "AdvanceGameTime",
    "CheckWorldEffects",
    "ChoiceRespondOperationPayload",
    "PendingChoice",
    "InitializeCombat",
    "CommandReceipt",
    "TypedCommand",
    "EnterScene",
    "MoveActor",
    "PlaceObject",
    "StepActor",
    "TraversePassage",
    "world_command_from_data",
    "ObjectState",
    "SetObjectState",
    "SetSceneFlag",
    "CreateSessionRequest",
    "PlayerSelection",
    "StateMachine",
    "SceneLifecycleService",
    "StateMachineError",
    "TrustedContext",
    "load_adventure_package",
]

from .time_coordination import AdvanceSessionTime, SetTimeRunning, TimeSync
from .time_driver import TimeDriver
from .time_waits import DomainTimeWait, TimeWaitReference, TimeWaitResolution

__all__ += ["AdvanceSessionTime", "SetTimeRunning", "TimeSync", "TimeDriver"]
__all__ += ["DomainTimeWait", "TimeWaitReference", "TimeWaitResolution"]
