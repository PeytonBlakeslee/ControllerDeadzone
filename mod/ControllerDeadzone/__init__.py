from __future__ import annotations

import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from typing import Any

import unrealsdk
from mods_base import (
    BoolOption, ButtonOption, GroupedOption, NestedOption, SliderOption,
    EInputEvent, Game, build_mod, get_pc,
)
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import WeakPointer

from .input_math import NATIVE_EVENT_KINDS, event_mapping, key_event_kind, radial_multiplier

IS_TPS = Game.get_current() == Game.TPS
_event_kinds = event_mapping(EInputEvent)

VIEWPORT_AXIS = "WillowGame.WillowGameViewportClient:InputAxis"
VIEWPORT_KEY = "WillowGame.WillowGameViewportClient:InputKey"
PLAYER_INPUT = "WillowGame.WillowPlayerInput:PlayerInput"
PRE_PROCESS_INPUT = "WillowGame.WillowPlayerInput:PreProcessInput"
MENU_TICK = "WillowGame.OptionsGFxMovie:OnTick"
RECOVERY_TICK = "WillowGame.WillowPlayerController:PlayerTick"
PRE_CLIENT_TRAVEL = "WillowGame.WillowPlayerController:PreClientTravel"
SETUP_INPUT_PATHS = (
    "WillowGame.WillowPlayerInput:SetupInputDevices",
    "WillowGame.WillowPlayerController:SetupInputDevices",
)

AXIS_INDEX = {
    "XboxTypeS_LeftX": 0,
    "XboxTypeS_LeftY": 1,
    "XboxTypeS_RightX": 2,
    "XboxTypeS_RightY": 3,
}
LEFT_X, LEFT_Y, RIGHT_X, RIGHT_Y = range(4)

# Fallback raw input state. Timestamps are only needed while a non-zero value is
# held; zero events directly clear the cached value.
_raw = [0.0, 0.0, 0.0, 0.0]
_seen = [0.0, 0.0, 0.0, 0.0]

# Fallback gain calibration for aTurn, aLookUp, aStrafe, aBaseY and aForward.
# Calibration is learned quickly, then only rechecked at a low frequency instead
# of being recomputed on every PlayerInput call forever.
_gain = [None, None, None, None, None]
_gain_samples = [0, 0, 0, 0, 0]
GAIN_RIGHT_X, GAIN_RIGHT_Y, GAIN_LEFT_X, GAIN_LEFT_BASEY, GAIN_LEFT_FORWARD = range(5)

STALE_SECONDS = 0.20
CALIBRATION_MIN_RAW = 0.20
EPS_OUTPUT = 0.001
CALIBRATION_SAMPLES = 3
GAIN_RECHECK_SECONDS = 1.0
GAIN_RECHECK_TOLERANCE = 0.05
KBM_PRIORITY_SECONDS = 0.10
KBM_SAFETY_SECONDS = 0.035  # Minimum mixed-device overlap guard, never user-disableable
# Unreal's native deadzone paths do not behave correctly at exactly 1.0.
# Keep the native field just below the singular endpoint and use the existing
# Python fallback only for an exact user-facing 100% setting, where output is
# explicitly forced to zero. Values 0-99% remain fully native.
NATIVE_DEADZONE_ENDPOINT_GUARD = 0.999
# The exact 100% endpoint is additionally guarded at the controller's native
# InputDeviceDefinition.Axes[SourceIndex].Speed scalar. Unlike viewport hook
# arguments (copies), this is an actual game-owned object property. The native
# engine's consumption of Speed still requires in-game validation. The existing
# KBM-safe Python fallback remains active as a safety net.
SPEED_EPSILON = 1e-6
# KBM prioritization may suppress *idle* radial sticks but must not suppress
# intentional physical stick travel, including while a keyboard key is held.
# Hysteresis prevents an analog stick at the threshold from toggling Speed on
# every InputAxis call. This never changes the radial response curve.
RADIAL_WAKE_HYSTERESIS = 0.02
_kbm_radial_active = [False, False]
_kbm_guard_last_requested = [None]
CONTROLLER_SOURCE_NAME = "InputDevice_Controller"
# Map physical axis to (weak device, source index, device address, original speed).
_speed_targets: dict[str, tuple[WeakPointer, int, int, float]] = {}
# Only a successful modification from a nonzero original is owned/restorable.
_speed_applied: set[str] = set()
_speed_owner_pi: int | None = None
_speed_block: tuple[int, str] | None = None
# Failed verified restores retain their weak identity/baseline for a later
# lifecycle reconciliation. Expired identities are relinquished without writes.
_pending_field_restores: dict[tuple[int, int, str, str], tuple[Any, float, float, float]] = {}
_field_cleanup_deadline = 0.0
_field_cleanup_next_scan = 0.0

_active_device = "unknown"
# Public integration contract. None means consumers should defer to BL2's native
# bUsingGamepad state. _active_device is retained as a compatibility alias.
active_device: str | None = None
_settings_cache = None
_error_once: set[str] = set()
_last_kbm_activity = 0.0
_kbm_keys_down: set[str] = set()
_left_backend_issue: str | None = None
_right_backend_issue: str | None = None

# A backend which detects an external edit or unsafe read/write condition is
# quarantined until the next real input lifecycle boundary. This prevents a
# later settings tick from immediately re-discovering the same objects and
# overwriting another mod's ownership.
_linked_left_block: tuple[int, str] | None = None
_native_left_block: tuple[int, str] | None = None
_native_right_block_reason: str | None = None

# All hooks are dynamic. If native deadzones handle every enabled stick, there
# are no per-input Python hooks at all.
_hooks: list[tuple[str, Type, str]] = []
# Callers use stable logical names; SDK registrations use a fresh opaque ID.
_hook_ids: dict[tuple[str, Type, str], str] = {}
_hook_pending_removal: set[tuple[str, Type, str]] = set()
_hook_nonce = secrets.token_hex(16)
_hook_sequence = 0
_enabled = False
_runtime_left_fallback = False
_runtime_right_fallback = False
_runtime_protect = False
# None when all required hooks are available; otherwise names missing
# dependencies. Gameplay callbacks are deactivated on a failure.
_runtime_hook_issue: str | None = None
_runtime_kbm_priority = True
_runtime_lx_dz = 0.10
_runtime_ly_dz = 0.10
_runtime_rx_dz = 0.10
_runtime_ry_dz = 0.10
_runtime_wake = 0.05

# Radial mode uses the same physical native controller DeadZone rows, set to
# zero *before* the paired transform.  Never transform an already filtered pair.
# Axial mode retains its original native and fallback processing unchanged.
_runtime_radial_left = False
_runtime_radial_right = False
_runtime_radial_left_inner = 0.10
_runtime_radial_left_outer = 1.0
_runtime_radial_right_inner = 0.10
_runtime_radial_right_outer = 1.0
_menu_refresh_pending = False
# v1.5.8 C++ owns the high-frequency InputAxis, InputKey and PreProcessInput
# hooks when enabled and verified. Python remains independently selectable,
# and retains configuration/UI and verified binding/Speed restoration.
# The Windows 32-bit extension is also loadable under 32-bit Wine/Proton.
_native_radial_module = None
_native_radial_failed = False
_native_field_io_disabled = False
_native_radial_warned = False
_native_radial_attempted = False
_native_axis_hook_live = False
_native_key_hook_live = False
_native_pre_hook_live = False
_native_lifecycle_hook_live = False
_native_discovery_partial_since: float | None = None
_recovery_deadline = 0.0
_recovery_next_scan = 0.0
_recovery_running = False
_recovery_deferred = False
RECOVERY_SECONDS = 15.0
RECOVERY_INTERVAL = 0.25
_native_pre_skip_once = False
_mode_change_guard = False
_setting_batch = False

# Cached local input identity for the fallback PlayerInput hook.
_pi_ref: WeakPointer | None = None
_pi_address: int | None = None
# A failed external input rebuild is not proof of a new, stable binding table.
# Keep this weak identity blocked until the local PlayerInput actually changes.
_failed_rebuild_pi: WeakPointer | None = None
_next_gain_recheck = 0.0

# Native-path state. Each stick is independent: if one side cannot be verified,
# only that side uses the optimized fallback.
_native_left_supported: bool | None = None
_native_right_supported: bool | None = None
_native_left_owner: WeakPointer | None = None
_native_left_address: int | None = None
_native_left_original: dict[str, str] = {}
_native_left_applied: dict[str, str] = {}

# Preferred left-stick backend discovered through the live PlayerInput axis handlers.
# Each physical axis maps to a shared InputDeviceDefinition axis row. We always
# re-resolve the struct through SourceDevice.Axes[index] before read/write; struct
# wrappers are never retained across rebuilds.
_linked_left_supported: bool | None = None
_linked_left_owner_address: int | None = None
_linked_left_targets: dict[str, tuple[WeakPointer, int, float]] = {}
_linked_left_applied: dict[str, float] = {}
_linked_source_addresses: dict[str, int] = {}

# Physical right-stick axes share the already-proven controller source with the
# left stick. AdvancedAxisDefinition.DeadZone has not been verified as the live
# physical stick deadzone; prioritize SourceDevice.Axes[SourceIndex].DeadZone.
_linked_right_supported: bool | None = None
_linked_right_owner_address: int | None = None
_linked_right_targets: dict[str, tuple[WeakPointer, int, float]] = {}
_linked_right_applied: dict[str, float] = {}
_linked_right_block: tuple[int, str] | None = None

_native_right_original: dict[str, tuple[WeakPointer, str, float]] = {}
_native_right_applied: dict[str, float] = {}

_DEADZONE_RE = re.compile(r"(?i)(\bDeadZone\s*=\s*)([-+]?(?:\d+(?:\.\d*)?|\.\d+))")


def _settings_tuple(override_option=None, override_value=None):
    def value(option):
        return override_value if option is override_option else option.value

    # Layout intentionally preserves the original eight axial tuple slots.
    # The added fields are consumed only by the radial backend.
    axial = bool(value(axial_mode))
    radial = bool(value(radial_mode)) and not axial
    return (
        axial and bool(value(left_enabled)),
        axial and bool(value(right_enabled)),
        bool(value(protect_kbm)),
        max(0.0, min(1.0, float(value(left_x)) / 100.0)),
        max(0.0, min(1.0, float(value(left_y)) / 100.0)),
        max(0.0, min(1.0, float(value(right_x)) / 100.0)),
        max(0.0, min(1.0, float(value(right_y)) / 100.0)),
        max(0.01, min(0.25, float(value(controller_wake)) / 100.0)),
        radial and bool(value(radial_left_enabled)),
        radial and bool(value(radial_right_enabled)),
        max(0.0, min(1.0, float(value(radial_left_inner)) / 100.0)),
        max(0.0, min(1.0, float(value(radial_left_outer)) / 100.0)),
        max(0.0, min(1.0, float(value(radial_right_inner)) / 100.0)),
        max(0.0, min(1.0, float(value(radial_right_outer)) / 100.0)),
    )


def _native_safe_deadzone(value: float) -> float:
    """Avoid Unreal's broken/singular native DeadZone=1.0 endpoint."""
    return NATIVE_DEADZONE_ENDPOINT_GUARD if value >= 1.0 else value


def _needs_endpoint_guard(x_deadzone: float, y_deadzone: float) -> bool:
    return x_deadzone >= 1.0 or y_deadzone >= 1.0


def _settings():
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = _settings_tuple()
    return _settings_cache


# Flat option names are retained for saved-settings and older Willow2 menu
# compatibility.
left_enabled = BoolOption(
    identifier="left_enabled",
    value=True,
    display_name="Enabled",
    description="Enable custom deadzones for the left stick.",
)
left_x = SliderOption(
    identifier="left_x_deadzone", value=10, min_value=0, max_value=100, step=1,
    is_integer=True, display_name="X Deadzone (%)",
    description="Left-stick horizontal deadzone.",
)
left_y = SliderOption(
    identifier="left_y_deadzone", value=10, min_value=0, max_value=100, step=1,
    is_integer=True, display_name="Y Deadzone (%)",
    description="Left-stick vertical deadzone.",
)
right_enabled = BoolOption(
    identifier="right_enabled",
    value=True,
    display_name="Enabled",
    description="Enable custom deadzones for the right stick.",
)
right_x = SliderOption(
    identifier="right_x_deadzone", value=10, min_value=0, max_value=100, step=1,
    is_integer=True, display_name="X Deadzone (%)",
    description="Right-stick horizontal deadzone.",
)
right_y = SliderOption(
    identifier="right_y_deadzone", value=10, min_value=0, max_value=100, step=1,
    is_integer=True, display_name="Y Deadzone (%)",
    description="Right-stick vertical deadzone.",
)
protect_kbm = BoolOption(
    identifier="protect_kbm",
    value=True,
    display_name="Keyboard & Mouse Priority",
    description="When both devices send input, briefly prefer keyboard and mouse. Basic mixed-input protection stays active when Off.",
)
controller_wake = SliderOption(
    identifier="controller_wake", value=5, min_value=1, max_value=25, step=1,
    is_integer=True, display_name="Controller Wake (%)",
    description="Stick movement needed to return control to the controller after keyboard/mouse input.",
)


# Keep the flat persisted option IDs from v1.4.6.  The nested UI references
# these exact objects, but only the top-level mod.options are serialized.
axial_mode = BoolOption(
    identifier="axial_mode", value=False, display_name="Enable Axial Deadzone",
    description="Use separate horizontal and vertical deadzones. Turning this On disables radial mode.",
)
radial_mode = BoolOption(
    identifier="radial_mode", value=True, display_name="Enable Radial Deadzone",
    description="Use circular deadzones on enabled sticks. Turning this On disables axial mode.",
)
radial_left_enabled = BoolOption(
    identifier="radial_left_enabled", value=True, display_name="Enabled",
    description="Enable the circular deadzone on the movement stick.",
)
radial_right_enabled = BoolOption(
    identifier="radial_right_enabled", value=True, display_name="Enabled",
    description="Enable the circular deadzone on the camera stick.",
)
radial_left_inner = SliderOption(
    identifier="radial_left_inner", value=10, min_value=0, max_value=100,
    step=1, is_integer=True, display_name="Inner Deadzone (%)",
    description="Stick radius below which movement is zero. At 100%, the stick is blocked.",
)
radial_left_outer = SliderOption(
    identifier="radial_left_outer", value=100, min_value=0, max_value=100,
    step=1, is_integer=True, display_name="Outer Limit (%)",
    description="Physical radius which produces full movement. Must exceed inner, unless both are 100%.",
)
radial_right_inner = SliderOption(
    identifier="radial_right_inner", value=10, min_value=0, max_value=100,
    step=1, is_integer=True, display_name="Inner Deadzone (%)",
    description="Camera stick radius below which movement is zero. At 100%, the stick is blocked.",
)
radial_right_outer = SliderOption(
    identifier="radial_right_outer", value=100, min_value=0, max_value=100,
    step=1, is_integer=True, display_name="Outer Limit (%)",
    description="Physical radius which produces maximum camera turn. Must exceed inner, unless both are 100%.",
)


use_native_radial = BoolOption(
    identifier="use_native_radial", value=False, is_hidden=IS_TPS,
    display_name="Native Input Backend (C++)",
    description="Optional C++ processing for radial deadzones and mixed-device input. "
                "Falls back to Python when unavailable; axial filtering stays game-native.",
)


tps_experimental_native_radial = BoolOption(
    identifier="tps_experimental_native_radial", value=False, is_hidden=not IS_TPS,
    display_name="Native Input Backend (Experimental)",
    description="TPS only: experimental C++ input processing may cause input problems or crashes. Off uses Python.",
)


def _native_requested() -> bool:
    """Each game owns its own explicitly saved native-backend preference."""
    return bool(tps_experimental_native_radial.value if IS_TPS else use_native_radial.value)


def _warn_once(key: str, message: str) -> None:
    if key in _error_once:
        return
    _error_once.add(key)
    logging.warning(f"Controller Deadzone Control: {message}")


def _load_native_radial() -> None:
    """Optional local x86 extension; do not break the mod if absent."""
    global _native_radial_module, _native_radial_warned, _native_radial_attempted
    if (_native_radial_module is not None or _native_radial_failed or
            _native_radial_attempted or not _native_requested()):
        return
    _native_radial_attempted = True  # Never attempt imports on every input frame.
    try:
        from . import deadzone_backend
        if not callable(deadzone_backend.apply_radial):
            raise RuntimeError("native extension missing required functions")
        if deadzone_backend.backend_version() != "1.0.5":
            raise RuntimeError("native extension version does not match this player build")
        _native_radial_module = deadzone_backend
        _configure_native_runtime()
        logging.info("Controller Deadzone Control: native radial backend "
                     + str(deadzone_backend.backend_version()) + " loaded")
    except Exception as exc:
        _native_radial_module = None  # A failed configuration must not leave native selected.
        if not _native_radial_warned:
            _native_radial_warned = True
            logging.warning("Controller Deadzone Control: C++ radial backend unavailable "
                            f"({type(exc).__name__}: {exc}); retaining Python radial")


def _reset_native_radial() -> None:
    module = _native_radial_module
    if module is not None:
        try:
            module.reset()  # Invalidate typed Unreal property metadata on input rebuild.
            # Field ownership survives input-cache resets and backend switches.
            # Restore or explicitly relinquish each row before discarding it.
        except Exception as exc:
            _warn_once("native_reset", f"native reset failed: {type(exc).__name__}: {exc}")


def _stop_native_lifecycle() -> None:
    """Stop native PRE lifecycle observers without touching engine-owned fields."""
    global _native_lifecycle_hook_live
    _native_lifecycle_hook_live = False
    module = _native_radial_module
    stop = getattr(module, "stop_lifecycle_hooks", None) if module is not None else None
    if callable(stop):
        try:
            stop()
        except Exception as exc:
            _warn_once("native_lifecycle_stop", "native lifecycle stop failed: "
                       f"{type(exc).__name__}: {exc}")


def _native_lifecycle_begin() -> None:
    """Manual boundary for Manager rebuilds that may bypass Unreal POST hooks."""
    if not _native_lifecycle_hook_live or _pi_address is None:
        return
    module = _native_radial_module
    begin = getattr(module, "lifecycle_begin", None) if module is not None else None
    if callable(begin):
        try:
            begin(int(_pi_address))
        except Exception as exc:
            _warn_once("native_lifecycle_begin", "native lifecycle begin failed: "
                       f"{type(exc).__name__}: {exc}")


def _arm_native_lifecycle() -> bool:
    """Arm native PRE only after C++ verifies a complete current input map.

    All native observers and a complete binding snapshot are mandatory for
    direct PRE processing. Otherwise keep the verified Python PRE bridge.
    """
    global _native_lifecycle_hook_live
    module = _native_radial_module
    start = getattr(module, "start_lifecycle_hooks", None) if module is not None else None
    if not callable(start) or _pi_address is None:
        return False
    try:
        _native_lifecycle_hook_live = bool(start(int(_pi_address)))
        resume = getattr(module, "lifecycle_resume", None)
        if not _native_lifecycle_hook_live or not callable(resume):
            _warn_once("native_lifecycle_missing", "native rebuild protection unavailable; retaining Python PRE bridge")
            return False
        if not bool(resume(int(_pi_address))):
            _schedule_input_recovery()
            return False
        return True
    except Exception as exc:
        _warn_once("native_lifecycle_arm", "native lifecycle registration unavailable: "
                   f"{type(exc).__name__}: {exc}; Python POST hooks retained")
        return False


def _native_hot_available() -> bool:
    """C++ runtime is active only for enabled radial paths and valid loaded code."""
    return (_native_requested() and not _native_radial_failed
            and _native_radial_module is not None
            and bool(_runtime_radial_left or _runtime_radial_right)
            and not (_runtime_left_fallback or _runtime_right_fallback))


def _configure_native_runtime() -> None:
    module = _native_radial_module
    if module is None:
        return
    module.configure_input(
        bool(_runtime_radial_left), bool(_runtime_radial_right),
        bool(_runtime_kbm_priority), bool(_runtime_protect),
        float(_runtime_wake), float(_runtime_radial_left_inner),
        float(_runtime_radial_right_inner),
    )


def _native_axis_signal(owner: int, guard_left: bool, guard_right: bool,
                        owner_changed: bool) -> None:
    """C++ calls this only on input-owner or radial Speed-guard transitions.

    The 30k+ ordinary axis events observed in v1.5.5 never enter Python.
    Python retains authoritative Speed ownership and exact restoration.
    """
    if not _enabled or _native_radial_failed:
        return
    if owner_changed:
        _native_owner_changed(int(owner), sync_guard=False)
    # These values were computed for this synchronous native event. Publishing
    # ownership must not query/recalculate its guard at a later timestamp.
    _sync_radial_kbm_guard(native_targets=(bool(guard_left), bool(guard_right)))


def _stop_native_axis_hook() -> None:
    global _native_axis_hook_live
    module = _native_radial_module
    if module is not None and callable(getattr(module, "stop_axis_hook", None)):
        try:
            module.stop_axis_hook()
        except Exception as exc:
            _warn_once("native_axis_stop", "native axis-hook cleanup failed: "
                       f"{type(exc).__name__}: {exc}")
    _native_axis_hook_live = False


def _try_native_axis_hook() -> bool:
    """Own InputAxis in C++ only with verified radial mapping and no axial fallback.

    If registration fails, leave the original Python hook fully intact.
    """
    global _native_axis_hook_live
    module = _native_radial_module
    if not _native_hot_available() or module is None:
        return False
    try:
        if not callable(getattr(module, "start_axis_hook", None)):
            return False
        success = bool(module.start_axis_hook(_native_axis_signal))
        if success and bool(module.axis_hook_active()):
            _native_axis_hook_live = True
            return True
        _stop_native_axis_hook()
        _warn_once("native_axis_register", "native InputAxis hook could not be "
                   "registered; using Python InputAxis bridge")
    except Exception as exc:
        _stop_native_axis_hook()
        _warn_once("native_axis_register", "native InputAxis hook registration "
                   f"failed ({type(exc).__name__}: {exc}); using Python bridge")
    return False


def _stop_native_key_hook() -> None:
    global _native_key_hook_live
    module = _native_radial_module
    if module is not None and callable(getattr(module, "stop_key_hook", None)):
        try:
            module.stop_key_hook()
        except Exception as exc:
            _warn_once("native_key_stop", "native key-hook cleanup failed: "
                       f"{type(exc).__name__}: {exc}")
    _native_key_hook_live = False


def _try_native_key_hook() -> bool:
    """Replace Python InputKey only after the native hook is registered.

    Failure leaves the original Python hook installed, even when C++ radial
    and native axis processing remain active.
    """
    global _native_key_hook_live
    module = _native_radial_module
    if not _native_hot_available() or module is None:
        return False
    try:
        if not callable(getattr(module, "start_key_hook", None)):
            return False
        success = bool(module.start_key_hook(_native_axis_signal))
        if success and bool(module.key_hook_active()):
            _native_key_hook_live = True
            return True
        _stop_native_key_hook()
        _warn_once("native_key_register", "native InputKey hook could not be "
                   "registered; retaining Python InputKey bridge")
    except Exception as exc:
        _stop_native_key_hook()
        _warn_once("native_key_register", "native InputKey hook registration "
                   f"failed ({type(exc).__name__}: {exc}); retaining Python bridge")
    return False


def _stop_native_pre_hook() -> None:
    """Disarm before restoring bindings; remove the registered native PRE hook."""
    global _native_pre_hook_live
    module = _native_radial_module
    _native_pre_hook_live = False
    if module is not None and callable(getattr(module, "stop_preprocess_hook", None)):
        try:
            module.stop_preprocess_hook()
        except Exception as exc:
            _warn_once("native_pre_stop", "native radial-hook cleanup failed: "
                       f"{type(exc).__name__}: {exc}")


def _native_pre_fault(reason: str) -> None:
    """Native fault transition: install Python before abandoning radial ownership.

    Called only on native hook failure, never during ordinary frames. Avoid
    native deregistration inside its own UnrealSDK dispatch iteration.
    """
    global _native_radial_failed
    global _native_pre_hook_live, _native_pre_skip_once
    _native_pre_hook_live = False
    _native_radial_failed = True
    _native_pre_skip_once = True  # Never double-transform a possibly written frame.
    try:
        ok = _add_hook(PRE_PROCESS_INPUT, Type.PRE,
                       "ControllerDeadzoneControl:Radial", _apply_radial)
    except Exception:
        ok = False
    if not ok:
        # The verified radial native source is only safe with a radial hook.
        # If Python cannot take over, restore the underlying engine mapping.
        pi = _pi_ref() if _pi_ref is not None else None
        try:
            if _runtime_radial_left:
                _apply_native_left(pi, False, 0.0, 0.0)
            if _runtime_radial_right:
                _apply_native_right(pi, False, 0.0, 0.0)
        except Exception:
            pass
    _warn_once("native_pre_fault", "native PreProcessInput stopped (" +
               str(reason) + "); Python radial fallback=" + str(bool(ok)))


def _try_native_pre_hook() -> bool:
    """Replace Python PRE only for the current verified local PlayerInput."""
    global _native_pre_hook_live
    module = _native_radial_module
    if (not _native_hot_available() or not _native_axis_hook_live or
            module is None or _pi_address is None):
        return False
    start = getattr(module, "start_preprocess_hook", None)
    if not callable(start):
        return False
    try:
        success = bool(start(
            int(_pi_address), bool(_runtime_radial_left), bool(_runtime_radial_right),
            float(_runtime_radial_left_inner), float(_runtime_radial_left_outer),
            float(_runtime_radial_right_inner), float(_runtime_radial_right_outer),
            bool(_native_key_hook_live), _native_pre_fault,
        ))
        if success:
            if not _arm_native_lifecycle():
                _stop_native_pre_hook()  # Python PRE installed by caller.
                return False
            if bool(module.preprocess_hook_active()):
                _native_pre_hook_live = True
                return True
        _stop_native_pre_hook()
        _warn_once("native_pre_register", "native PreProcessInput registration failed; "
                   "retaining Python PRE hook")
    except Exception as exc:
        _stop_native_pre_hook()
        _warn_once("native_pre_register", "native PreProcessInput registration "
                   f"failed ({type(exc).__name__}: {exc}); retaining Python PRE hook")
    return False


def _native_runtime_failed(stage: str, exc: Exception) -> None:
    global _native_radial_failed
    _native_radial_failed = True
    _warn_once(f"native_runtime_{stage}",
               f"C++ input runtime failure at {stage}; using Python from next event: "
               f"{type(exc).__name__}: {exc}")


def _native_owner_changed(code: int, *, sync_guard: bool = True) -> None:
    if code == 1:
        _set_active_device("kbm", sync_guard=sync_guard)
    elif code == 2:
        _set_active_device("controller", sync_guard=sync_guard)
    elif code == 0:
        _set_active_device(None, sync_guard=sync_guard)




def _native_radial_changed(option, value) -> None:
    global _native_radial_attempted, _native_radial_failed, _native_radial_warned
    global _native_field_io_disabled
    if _setting_batch or option is not (tps_experimental_native_radial if IS_TPS else use_native_radial):
        return
    # Called before BoolOption stores its new value. No engine properties change.
    if bool(value):
        _reset_fallback_state(clear_device=True)
        _native_radial_failed = False  # User deliberately retried after a quarantined native failure.
        _native_field_io_disabled = False
        _native_radial_warned = False
        _reset_native_radial()
        _native_radial_attempted = False  # Single explicit retry on re-enable.
        _load_native_radial_on_next_frame[0] = True
    else:
        _load_native_radial_on_next_frame[0] = False
        # Restore the Python physical-input hook BEFORE disarming the C++ hook.
        # This option callback runs prior to saving the new False value, and
        # does not invoke the normal _settings_changed reconciler.
        if _enabled and (_runtime_radial_left or _runtime_radial_right):
            _add_hook(VIEWPORT_AXIS, Type.PRE,
                      "ControllerDeadzoneControl:Axis", _capture_axis)
            _add_hook(VIEWPORT_KEY, Type.PRE,
                      "ControllerDeadzoneControl:InputKey", _input_key)
            _add_hook(PRE_PROCESS_INPUT, Type.PRE,
                      "ControllerDeadzoneControl:Radial", _apply_radial)
        _stop_native_lifecycle()
        _stop_native_pre_hook()
        _stop_native_key_hook()
        _stop_native_axis_hook()
        _reset_native_radial()
        _reset_fallback_state(clear_device=True)


_load_native_radial_on_next_frame = [False]


def _intentional_radial_motion(stick: int, now: float) -> bool:
    """True if the physical stick has moved intentionally during KBM use.

    A thumbstick beyond the inner radial threshold must remain usable while
    keyboard or mouse input is active. Resting stick drift stays blocked using
    the same physical source Speed guard introduced in v1.5.2.
    """
    if stick == 0:
        inner, indices = _runtime_radial_left_inner, (LEFT_X, LEFT_Y)
    else:
        inner, indices = _runtime_radial_right_inner, (RIGHT_X, RIGHT_Y)
    if inner >= 1.0:
        _kbm_radial_active[stick] = False
        return False
    x = _raw_value(indices[0], now)
    y = _raw_value(indices[1], now)
    magnitude = math.hypot(x, y)
    # Reclaim on deliberate paired stick motion, not on 3-9% idle drift.
    # Release only once the stick settles back within the deadzone. The
    # extra hysteresis is applied only when waking, never to radial math.
    wake_threshold = max(inner + RADIAL_WAKE_HYSTERESIS,
                         _runtime_wake + RADIAL_WAKE_HYSTERESIS)
    rest_threshold = max(inner, min(_runtime_wake, 0.07) * 0.8)
    active = _kbm_radial_active[stick]
    if active:
        active = magnitude > rest_threshold
    else:
        active = magnitude >= wake_threshold
    _kbm_radial_active[stick] = active
    return active


def _sync_radial_kbm_guard(settings: tuple | None = None, *,
                           native_targets: tuple[bool, bool] | None = None) -> None:
    """Guard idle controller sources, never suppress a deliberately held stick.

    100% deadzones are still handled by _apply_speed_guard regardless of input
    ownership. KBM Priority limits controller drift, not simultaneous inputs.
    """
    if not _enabled or not (_runtime_radial_left or _runtime_radial_right):
        return
    pi = _pi_ref() if _pi_ref is not None else None
    if pi is None:
        return
    isolate = _runtime_kbm_priority and _active_device != "controller"
    if _native_hot_available():
        try:
            desired = (tuple(bool(x) for x in native_targets) if native_targets is not None
                       else tuple(bool(x) for x in _native_radial_module.guard_targets()))
        except Exception as exc:
            _native_runtime_failed("guard", exc)
            return
        left_block = bool(isolate and desired[0] and _runtime_radial_left_inner < 1.0)
        right_block = bool(isolate and desired[1] and _runtime_radial_right_inner < 1.0)
    else:
        if isolate:
            now = time.monotonic()
            live_left = _intentional_radial_motion(0, now) if _runtime_radial_left else False
            live_right = _intentional_radial_motion(1, now) if _runtime_radial_right else False
        else:
            live_left = live_right = False
        left_block = bool(isolate and _runtime_radial_left and not live_left)
        right_block = bool(isolate and _runtime_radial_right and not live_right)
        desired = (
            left_block or (_runtime_radial_left and _runtime_radial_left_inner >= 1.0),
            right_block or (_runtime_radial_right and _runtime_radial_right_inner >= 1.0),
        )
    # Native ownership routines are relatively expensive. Only reapply when
    # the desired source guard changes. Exact axial 100% remains independently
    # configured during settings reconciliation.
    if desired == _kbm_guard_last_requested[0]:
        return
    _kbm_guard_last_requested[0] = desired
    _apply_speed_guard(pi, _settings() if settings is None else settings,
                       kbm_radial_left=left_block,
                       kbm_radial_right=right_block)


def _set_active_device(device: str | None, *, sync_guard: bool = True) -> None:
    """Publish input owner; protect only *idle* radial sticks during KBM use."""
    global active_device, _active_device
    public = device if device in ("controller", "kbm") else None
    legacy = public if public is not None else "unknown"
    if public == active_device and legacy == _active_device:
        return
    active_device = public
    _active_device = legacy
    if sync_guard:
        _sync_radial_kbm_guard()


def _mark_kbm_activity(now: float | None = None) -> None:
    global _last_kbm_activity
    if now is None:
        now = time.monotonic()
    _last_kbm_activity = now
    _set_active_device("kbm")


def _controller_can_wake(now: float) -> bool:
    # A held key or mouse button always keeps KBM ownership. Disabling the
    # optional priority grace period never permits controller fallback to edit KBM.
    if _kbm_keys_down:
        return False
    if not _runtime_kbm_priority:
        return True
    return _last_kbm_activity <= 0.0 or now - _last_kbm_activity >= KBM_PRIORITY_SECONDS


def _object_address(obj: Any | None) -> int | None:
    if obj is None:
        return None
    try:
        getter = getattr(obj, "_get_address")
    except Exception:
        return None
    try:
        return int(getter())
    except Exception:
        return None


def _reset_fallback_state(*, clear_device: bool = True) -> None:
    global _next_gain_recheck, _last_kbm_activity
    for index in range(4):
        _raw[index] = 0.0
        _seen[index] = 0.0
    for index in range(5):
        _gain[index] = None
        _gain_samples[index] = 0
    _next_gain_recheck = 0.0
    _last_kbm_activity = 0.0
    _kbm_keys_down.clear()
    _kbm_radial_active[:] = [False, False]
    _kbm_guard_last_requested[0] = None
    if clear_device:
        _set_active_device(None)


def _add_hook(path: str, kind: Type, identifier: str, callback) -> bool:
    """Register once per logical name, using a new unpredictable SDK ID.

    SDK presence does not prove callback identity. A False result may mean
    either a successful append or an exact-ID collision; the random namespace
    limits accidental collisions but cannot settle that adversarial ambiguity.
    """
    global _hook_sequence
    entry = (path, kind, identifier)
    if entry in _hook_pending_removal:
        return False  # An interrupted removal is not an active registration.
    if entry in _hooks:
        actual = _hook_ids[entry]
        try:
            if unrealsdk.hooks.has_hook(path, kind, actual):
                return True
        except Exception as exc:
            _warn_once(f"hook:{identifier}", f"hook ownership check failed ({identifier}): {type(exc).__name__}: {exc}")
            return False  # Retain the unresolved record; do not register twice.
        _hooks.remove(entry)
        del _hook_ids[entry]

    _hook_sequence += 1
    actual = f"{identifier}/{_hook_nonce}/{_hook_sequence}"
    try:
        if unrealsdk.hooks.has_hook(path, kind, actual):
            _warn_once(f"hook:{identifier}", f"generated hook identifier already in use ({identifier})")
            return False
    except Exception as exc:
        _warn_once(f"hook:{identifier}", f"hook preflight failed ({identifier}): {type(exc).__name__}: {exc}")
        return False
    added = None
    try:
        added = unrealsdk.hooks.add_hook(path, kind, actual, callback)
    except Exception as exc:
        _warn_once(f"hook:{identifier}", f"hook unavailable ({identifier}): {type(exc).__name__}: {exc}")
    try:
        live = unrealsdk.hooks.has_hook(path, kind, actual)
    except Exception as exc:
        # The call may have installed a callback even when it raised or
        # returned False. Retain this ID until presence can be resolved.
        _hooks.append(entry)
        _hook_ids[entry] = actual
        _warn_once(f"hook:{identifier}", f"hook ownership check failed ({identifier}): {type(exc).__name__}: {exc}")
        return False
    if live:
        _hooks.append(entry)
        _hook_ids[entry] = actual
        return True
    if added is False:
        _warn_once(f"hook:{identifier}", f"hook registration unconfirmed ({identifier})")
    return False


def _remove_owned_hook_entry(entry: tuple[str, Type, str]) -> None:
    """Keep uncertain removals for retry; retire confirmed replacements."""
    path, kind, ident = entry
    actual = _hook_ids.get(entry)
    if actual is None:
        return  # A reentrant cleanup already retired this entry.
    try:
        removed = unrealsdk.hooks.remove_hook(path, kind, actual)
        # The pinned SDK's True result confirms removal. Retire the ledger
        # immediately: a later presence check could fail after a foreign hook
        # reuses this ID, and a retry must not remove that replacement.
        if removed is not True and unrealsdk.hooks.has_hook(path, kind, actual):
            raise RuntimeError("hook remains registered")
    except Exception as exc:
        _hook_pending_removal.add(entry)
        _warn_once(f"unhook:{ident}", f"hook removal deferred ({ident}): {type(exc).__name__}: {exc}")
        return
    _hook_pending_removal.discard(entry)
    _hooks[:] = [item for item in _hooks if item != entry]
    _hook_ids.pop(entry, None)


def _remove_hook(identifier: str) -> None:
    for entry in tuple(_hooks):
        if entry[2] == identifier:
            _remove_owned_hook_entry(entry)


def _remove_all_hooks() -> None:
    for entry in reversed(tuple(_hooks)):
        _remove_owned_hook_entry(entry)


def _clear_native_left_state(*, clear_block: bool = False) -> None:
    global _native_left_supported, _native_left_owner, _native_left_address
    global _native_left_original, _native_left_applied, _native_left_block
    _native_left_supported = None
    _native_left_owner = None
    _native_left_address = None
    _native_left_original = {}
    _native_left_applied = {}
    if clear_block:
        _native_left_block = None


def _clear_linked_left_state(*, clear_block: bool = False) -> None:
    global _linked_left_supported, _linked_left_owner_address
    global _linked_left_targets, _linked_left_applied, _linked_left_block
    _linked_left_supported = None
    _linked_left_owner_address = None
    _linked_left_targets = {}
    _linked_left_applied = {}
    if clear_block:
        _linked_left_block = None


def _clear_native_right_state(*, clear_block: bool = False) -> None:
    global _native_right_supported, _native_right_original, _native_right_applied
    global _native_right_block_reason
    _native_right_supported = None
    _native_right_original = {}
    _native_right_applied = {}
    if clear_block:
        _native_right_block_reason = None


def _clear_linked_right_state(*, clear_block: bool = False) -> None:
    global _linked_right_supported, _linked_right_owner_address
    global _linked_right_targets, _linked_right_applied, _linked_right_block
    _linked_right_supported = None
    _linked_right_owner_address = None
    _linked_right_targets = {}
    _linked_right_applied = {}
    if clear_block:
        _linked_right_block = None


def _speed_row(pointer: WeakPointer, index: int, address: int, physical: str) -> Any | None:
    """Resolve fresh struct wrapper; never retain a potentially invalid array row."""
    try:
        device = pointer()
        if (_object_address(device) != address
                or str(device.Class.Name) != "InputDeviceDefinition"
                or str(device.Name) != CONTROLLER_SOURCE_NAME):
            return None
        axes = device.Axes
        if index < 0 or index >= len(axes):
            return None
        row = axes[index]
        if str(row.AxisName) != physical:
            return None
        return row
    except Exception:
        return None


def _source_field_read(row: Any, address: int, index: int,
                       physical: str, name: str) -> float:
    """Type-checked native source read, with read-only Python fallback.

    Caller re-resolves a verified live row first. No UObject pointer is cached.
    """
    global _native_field_io_disabled
    module = _native_radial_module
    if (_native_requested() and module is not None and not _native_radial_failed
            and not _native_field_io_disabled and physical in AXIS_INDEX
            and name in ("Speed", "DeadZone")):
        try:
            ok, value = module.read_axis_field(
                int(address), int(index), AXIS_INDEX[physical],
                0 if name == "Speed" else 1,
            )
            if int(ok) == 1:
                return float(value)
            raise ValueError("native property read unavailable")
        except Exception as exc:
            _native_field_io_disabled = True
            _warn_once("native_field_read", "C++ controller field read unavailable; "
                       "reverting binding management to Python: "
                       f"{type(exc).__name__}: {exc}")
    value = float(getattr(row, name))
    if not math.isfinite(value):
        raise ValueError("nonfinite controller field")
    return value


def _source_field_cas(row: Any, address: int, index: int, physical: str,
                      name: str, old: float, new: float) -> bool:
    """Compare-before-write. Never retry a potentially partial C++ mutation.

    C++ can change only a freshly verified physical controller source row.
    The Python branch is the original behavior with an extra owner check.
    """
    global _native_field_io_disabled
    if not math.isfinite(old) or not math.isfinite(new):
        return False
    if (address, index, physical, name) in _pending_field_restores:
        return False  # Repair uncertain ownership before accepting new settings.
    module = _native_radial_module
    if (_native_requested() and module is not None and not _native_radial_failed
            and not _native_field_io_disabled and physical in AXIS_INDEX
            and name in ("Speed", "DeadZone")):
        try:
            code = int(module.cas_axis_field(
                int(address), int(index), AXIS_INDEX[physical],
                0 if name == "Speed" else 1, float(old), float(new),
            ))
            if code in (1, 2):
                return True
            if code == 0:  # An external writer changed the field.
                return False
            _native_field_io_disabled = True
            _warn_once("native_field_write", "C++ controller field write failed; "
                       "binding is left for existing Python safety restoration")
            return False
        except Exception as exc:
            _native_field_io_disabled = True
            _warn_once("native_field_write", "C++ controller field write failed; "
                       f"{type(exc).__name__}: {exc}; no blind retry")
            return False
    current = float(getattr(row, name))
    if not math.isfinite(current) or abs(current - old) > SPEED_EPSILON:
        return False
    setattr(row, name, float(new))
    _relinquish_field(address, index, physical, name)  # Ownership transferred to Python.
    return True


def _relinquish_field(address: int, index: int, physical: str, name: str) -> None:
    module = _native_radial_module
    release = getattr(module, "relinquish_owned_axis_field", None) if module is not None else None
    if callable(release):
        release(address, index, AXIS_INDEX[physical], 0 if name == "Speed" else 1)


def _queue_field_restore(pointer: Any, index: int, address: int, physical: str,
                         name: str, original: float, applied: float) -> None:
    key = (address, index, physical, name)
    _pending_field_restores.setdefault(key, (pointer, original, applied, time.monotonic() + 0.25))
    _schedule_field_cleanup()


def _schedule_field_cleanup() -> None:
    global _field_cleanup_deadline, _field_cleanup_next_scan
    if not _pending_field_restores or _field_cleanup_deadline > 0.0:
        return
    if _add_hook(RECOVERY_TICK, Type.POST, "ControllerDeadzoneControl:FieldCleanup", _field_cleanup_tick):
        now = time.monotonic()
        _field_cleanup_deadline = now + RECOVERY_SECONDS
        _field_cleanup_next_scan = now + RECOVERY_INTERVAL


def _field_cleanup_tick(obj: Any, args: Any, ret: Any, func: Any) -> None:
    """Emergency cleanup also runs briefly after disable, with no gameplay hook."""
    global _field_cleanup_deadline, _field_cleanup_next_scan
    if _field_cleanup_deadline <= 0.0:
        return
    now = time.monotonic()
    if _pending_field_restores and now < _field_cleanup_next_scan:
        return
    if now < _field_cleanup_deadline:
        _field_cleanup_next_scan = now + RECOVERY_INTERVAL
        _retry_field_restores()
    if not _pending_field_restores or now >= _field_cleanup_deadline:
        if _pending_field_restores:
            _warn_once("field_cleanup_exhausted", "controller field restoration remains unverified; "
                       "ownership records retained for the next rebuild or re-enable.")
        _field_cleanup_deadline = _field_cleanup_next_scan = 0.0
        _remove_hook("ControllerDeadzoneControl:FieldCleanup")


def _retry_field_restores() -> None:
    """Only verified repair, on later reconciliation; never retry an input frame."""
    now = time.monotonic()
    for key, (pointer, original, applied, earliest) in tuple(_pending_field_restores.items()):
        if now < earliest:
            continue
        address, index, physical, name = key
        row = _speed_row(pointer, index, address, physical)
        try:
            if row is None:
                _relinquish_field(address, index, physical, name)
                _pending_field_restores.pop(key, None)
                continue
            native = _native_radial_module
            restore = getattr(native, "restore_owned_axis_field", None) if native is not None else None
            result = int(restore(address, index, AXIS_INDEX[physical],
                                 0 if name == "Speed" else 1, original)) if callable(restore) else 2
            if result == -1:
                continue  # Keep uncertain ownership; never blind-write Python.
            if result == 0:
                _relinquish_field(address, index, physical, name)
            if result == 2:
                current = float(getattr(row, name))
                if not math.isfinite(current):
                    continue
                if abs(current - original) > SPEED_EPSILON:
                    if abs(current - applied) > SPEED_EPSILON:
                        _relinquish_field(address, index, physical, name)
                        _pending_field_restores.pop(key, None)
                        continue
                    setattr(row, name, original)
                    fresh = _speed_row(pointer, index, address, physical)
                    observed = float(getattr(fresh, name)) if fresh is not None else math.nan
                    if not math.isfinite(observed) or abs(observed - original) > SPEED_EPSILON:
                        continue
            _pending_field_restores.pop(key, None)
        except Exception as exc:
            _warn_once("field_restore_pending", f"controller field restoration deferred: {type(exc).__name__}: {exc}")


def _restore_speed_axis(physical: str) -> None:
    """Compare-before-restore; never overwrite another mod's intervening Speed."""
    record = _speed_targets.get(physical)
    if record is None:
        return
    pointer, index, address, original = record
    if physical in _speed_applied:
        row = _speed_row(pointer, index, address, physical)
        if row is not None:
            try:
                # Prefer the C++ ownership ledger when one exists, even if the
                # UI has since switched to Python. It is the only writer allowed
                # to restore its own field; absence of ownership uses the proven
                # Python compare-before-restore path below.
                result = 2
                native = _native_radial_module
                if native is not None and callable(getattr(native, "restore_owned_axis_field", None)):
                    result = int(native.restore_owned_axis_field(
                        address, index, AXIS_INDEX[physical], 0, float(original)))
                if result == 1:
                    pass  # Native owner restored the field.
                elif result == 0:
                    _relinquish_field(address, index, physical, "Speed")
                    _warn_once(f"speed_owner_conflict:{physical}",
                               f"skipped Speed restore for {physical}: external writer owns field")
                elif result == -1:
                    # On a possible partial mutation never attempt a blind
                    # Python retry; leave the field for input-rebuild recovery.
                    raise ValueError("native-owned Speed restore could not be verified")
                else:
                    current = _source_field_read(row, address, index, physical, "Speed")
                    if abs(current) <= SPEED_EPSILON:
                        if not _source_field_cas(row, address, index, physical, "Speed", current, original):
                            raise ValueError("controller Speed changed during restore")
                        readback = _speed_row(pointer, index, address, physical)
                        if readback is None or abs(_source_field_read(readback, address, index,
                                                                    physical, "Speed") - original) > SPEED_EPSILON:
                            raise ValueError("native speed restore readback mismatch")
                    else:
                        _warn_once(f"speed_external_restore:{physical}",
                                   f"skipped Speed restore for {physical}: another writer changed the value")
            except Exception as exc:
                _queue_field_restore(pointer, index, address, physical, "Speed", original, 0.0)
                _warn_once(f"speed_restore:{physical}",
                           f"native Speed restore failed for {physical}: {type(exc).__name__}: {exc}")
        else:
            _relinquish_field(address, index, physical, "Speed")
            _warn_once(f"speed_stale_restore:{physical}",
                       f"cannot restore Speed for {physical}: old source row is no longer available")
    _speed_targets.pop(physical, None)
    _speed_applied.discard(physical)


def _restore_speed_guard(*, clear_block: bool = False) -> None:
    global _speed_owner_pi, _speed_block
    for physical in tuple(_speed_targets):
        _restore_speed_axis(physical)
    if clear_block:
        _speed_owner_pi = None
        _speed_block = None


def _discover_native_physical_sources(pi: Any) -> tuple[dict[str, tuple[Any, int, int, str]], set[str]] | None:
    """Read-only native source discovery for a verified local PlayerInput.

    C++ resolves physical axis identity, SourceDevice, SourceIndex, and logical
    AxisName from reflected data. Python only converts already-validated native
    addresses into existing SDK UObject wrappers. No raw address is retained.

    Fall back to original Python discovery if the engine layout is different or
    no longer agrees with a fresh wrapper (e.g. after Controller Manager rebuild).
    """
    global _native_discovery_partial_since
    module = _native_radial_module
    if (not _native_requested() or module is None or _native_radial_failed
            or not hasattr(module, "scan_controller_bindings")):
        return None
    try:
        pi_address = _object_address(pi)
        if pi_address is None:
            return None
        ok, rows, ambiguous = module.scan_controller_bindings(pi_address)
        if int(ok) != 1:
            raise ValueError("native reflection scan unavailable")
        physical_names = tuple(AXIS_INDEX)
        wanted: dict[tuple[int, int], tuple[str, str]] = {}
        conflicts = {physical_names[int(i)] for i in ambiguous}
        for phys_index, address, source_index, logical in rows:
            physical = physical_names[int(phys_index)]
            if physical in conflicts:
                continue
            key = (int(address), int(source_index))
            if key in wanted and wanted[key][0] != physical:
                raise ValueError("native mapping aliases different physical axes")
            wanted[key] = (physical, str(logical))
        # Incomplete or unfamiliar layouts retain the fully proven Python path.
        # Ordinary BL2 controller mappings contain four independent physical axes.
        if len(wanted) + len(conflicts) != 4:
            raise ValueError("native physical mapping incomplete")
        found: dict[str, tuple[Any, int, int, str]] = {}
        for binding in pi.Axes:
            try:
                handler = binding.InputDeviceHandler
                device = handler.SourceDevice
                index = int(handler.SourceIndex)
                address = _object_address(device)
                if address is None:
                    continue
                matched = wanted.get((address, index))
                if matched is None:
                    continue
                physical, logical = matched
                if str(device.Class.Name) != "InputDeviceDefinition" or str(device.Name) != CONTROLLER_SOURCE_NAME:
                    raise ValueError("native source UObject identity changed")
                if str(device.Axes[index].AxisName) != physical:
                    raise ValueError("native source physical axis changed")
                found[physical] = (device, index, address, logical)
            except ValueError:
                raise
            except Exception:
                continue
            if len(found) >= len(wanted):
                break
        if len(found) != len(wanted):
            raise ValueError("native source no longer available through PlayerInput")
        _native_discovery_partial_since = None
        return found, conflicts
    except Exception as exc:
        # A transient, incomplete table during fast travel is not an engine
        # error. Python still enumerates live physical rows below; no cached
        # engine pointer is ever reused. Warn if the gap persists >0.75 s.
        if str(exc) == "native physical mapping incomplete":
            now = time.monotonic()
            if _native_discovery_partial_since is None:
                _native_discovery_partial_since = now
            if now - _native_discovery_partial_since < 0.75:
                return None
        else:
            _native_discovery_partial_since = None
        _warn_once("native_binding_discovery", "C++ binding discovery unavailable; "
                   f"using verified Python source enumeration: {type(exc).__name__}: {exc}")
        return None


def _controller_speed_candidates(pi: Any) -> tuple[dict[str, tuple[Any, int, int]], set[str]]:
    """Resolve only verified Xbox controller source rows from the live PlayerInput.

    A duplicated physical key referring to different rows is ambiguous and cannot
    be changed safely. A duplicate pointing to the same physical source is safe.
    """
    native = _discover_native_physical_sources(pi)
    if native is not None:
        verified, conflicts = native
        return {physical: (entry[0], entry[1], entry[2])
                for physical, entry in verified.items()}, conflicts
    found: dict[str, tuple[Any, int, int]] = {}
    conflicts: set[str] = set()
    try:
        bindings = pi.Axes
    except Exception:
        return found, set(AXIS_INDEX)
    for binding in bindings:
        try:
            handler = binding.InputDeviceHandler
            device = handler.SourceDevice
            source_index = int(handler.SourceIndex)
            if (device is None or str(device.Class.Name) != "InputDeviceDefinition"
                    or str(device.Name) != CONTROLLER_SOURCE_NAME):
                continue
            address = _object_address(device)
            if address is None or source_index < 0 or source_index >= len(device.Axes):
                continue
            physical = str(device.Axes[source_index].AxisName)
            if physical not in AXIS_INDEX:
                continue
            existing = found.get(physical)
            if existing is not None and (existing[1], existing[2]) != (source_index, address):
                conflicts.add(physical)
                continue
            found[physical] = (device, source_index, address)
        except Exception:
            continue
    for physical in conflicts:
        found.pop(physical, None)
    return found, conflicts


def _speed_guard_still_current(pi: Any | None) -> bool:
    """Avoid needlessly restoring/reapplying unchanged native 100% ownership.

    Input lifecycle notifications may fire in clusters on the same live input
    definition. Reconcile only if the source mapping or actual Speed changed.
    """
    address = _object_address(pi)
    if address is None or _speed_owner_pi != address or _speed_block is not None:
        return False
    try:
        found, conflicts = _controller_speed_candidates(pi)
        if conflicts & _speed_applied:
            return False
        for physical in _speed_applied:
            target = _speed_targets.get(physical)
            candidate = found.get(physical)
            if target is None or candidate is None:
                return False
            pointer, idx, dev_address, _original = target
            if candidate[1:] != (idx, dev_address):
                return False
            row = _speed_row(pointer, idx, dev_address, physical)
            if row is None or abs(_source_field_read(row, dev_address, idx, physical, "Speed")) > SPEED_EPSILON:
                return False
        return True
    except Exception:
        return False


def _apply_speed_guard(pi: Any | None, settings: tuple, *,
                       kbm_radial_left: bool = False,
                       kbm_radial_right: bool = False) -> None:
    """Event-driven controller-only exact 100% native Speed guard.

    Covers axial 100% per-axis and radial 100% per-stick inner thresholds.
    This uses verified v1.4.6 physical axis Speed ownership. Idle-only
    KBM suppression reuses the same compare-before-restore ownership guard;
    intentional physical stick movement releases the temporary block.
    """
    global _speed_owner_pi, _speed_block
    desired = {
        physical for physical, on in (
            ("XboxTypeS_LeftX", (bool(settings[0]) and settings[3] >= 1.0)
             or (bool(settings[8]) and settings[10] >= 1.0)
             or kbm_radial_left),
            ("XboxTypeS_LeftY", (bool(settings[0]) and settings[4] >= 1.0)
             or (bool(settings[8]) and settings[10] >= 1.0)
             or kbm_radial_left),
            ("XboxTypeS_RightX", (bool(settings[1]) and settings[5] >= 1.0)
             or (bool(settings[9]) and settings[12] >= 1.0)
             or kbm_radial_right),
            ("XboxTypeS_RightY", (bool(settings[1]) and settings[6] >= 1.0)
             or (bool(settings[9]) and settings[12] >= 1.0)
             or kbm_radial_right),
        ) if on
    }
    pi_address = _object_address(pi)
    if pi_address != _speed_owner_pi:
        _restore_speed_guard(clear_block=True)
        _speed_owner_pi = pi_address
    for physical in tuple(_speed_targets):
        if physical not in desired:
            _restore_speed_axis(physical)
    if not desired or pi_address is None:
        return
    if _speed_block is not None and _speed_block[0] == pi_address:
        return

    found, conflicts = _controller_speed_candidates(pi)
    if conflicts & desired:
        issue = "ambiguous duplicate controller source for " + ", ".join(sorted(conflicts & desired))
        _speed_block = (pi_address, issue)
        _restore_speed_guard()
        _warn_once("speed_ambiguous", issue + "; native Speed guard skipped")
        return

    for physical in sorted(desired):
        candidate = found.get(physical)
        if candidate is None:
            _schedule_input_recovery()
            continue
        device, index, address = candidate
        record = _speed_targets.get(physical)
        if record is not None and (record[1], record[2]) != (index, address):
            _restore_speed_axis(physical)
            record = None
        if record is None:
            try:
                row = device.Axes[index]
                original = _source_field_read(row, address, index, physical, "Speed")
                if not (-10000.0 < original < 10000.0):
                    raise ValueError("invalid Speed scalar")
                record = (WeakPointer(device), index, address, original)
                _speed_targets[physical] = record
            except Exception as exc:
                _warn_once(f"speed_read:{physical}",
                           f"native Speed discovery failed for {physical}: {type(exc).__name__}: {exc}")
                continue
        pointer, source_index, device_address, original = record
        row = _speed_row(pointer, source_index, device_address, physical)
        if row is None:
            _speed_block = (pi_address, f"stale controller Speed row for {physical}")
            _restore_speed_guard()
            _warn_once(f"speed_stale:{physical}", "native Speed row changed; quarantined until input rebuild")
            return
        try:
            current = _source_field_read(row, device_address, source_index, physical, "Speed")
        except Exception as exc:
            _speed_block = (pi_address, f"unreadable Speed for {physical}")
            _restore_speed_guard()
            _warn_once(f"speed_unreadable:{physical}", f"Speed read failed: {type(exc).__name__}: {exc}")
            return
        if physical in _speed_applied:
            if abs(current) > SPEED_EPSILON:
                _speed_block = (pi_address, f"externally changed Speed for {physical}")
                _restore_speed_guard()
                _warn_once(f"speed_conflict:{physical}",
                           f"native Speed changed externally for {physical}; quarantined until input rebuild")
                return
            continue
        if abs(current) <= SPEED_EPSILON:
            # Already zeroed by another system (or native default). Do not own
            # or restore this field, since we did not change it.
            continue
        # If the captured baseline was edited before we modified it, don't
        # claim ownership. Avoid writing over a value another mod just set.
        if abs(current - original) > SPEED_EPSILON:
            _speed_block = (pi_address, f"baseline Speed changed for {physical}")
            _restore_speed_guard()
            _warn_once(f"speed_baseline_conflict:{physical}",
                       f"native Speed changed before guard applied for {physical}; quarantined")
            return
        try:
            if not _source_field_cas(row, device_address, source_index, physical,
                                     "Speed", current, 0.0):
                raise ValueError("controller Speed changed during guard")
            readback = _speed_row(pointer, source_index, device_address, physical)
            if readback is None or abs(_source_field_read(readback, device_address,
                        source_index, physical, "Speed")) > SPEED_EPSILON:
                raise ValueError("native Speed write readback mismatch")
            _speed_applied.add(physical)
        except Exception as exc:
            _speed_block = (pi_address, f"Speed write failure for {physical}")
            # A write may have reached the UObject even if verification raised.
            try:
                if abs(_source_field_read(row, device_address, source_index, physical, "Speed")) <= SPEED_EPSILON and abs(original) > SPEED_EPSILON:
                    _speed_applied.add(physical)
            except Exception:
                pass
            _restore_speed_guard()
            _warn_once(f"speed_write:{physical}",
                       f"native Speed guard failed for {physical}: {type(exc).__name__}: {exc}; "
                       "existing fallback remains active")
            return


def _cache_player_input(pi: Any | None, *, restore_retired: bool = True) -> None:
    global _pi_ref, _pi_address, _speed_owner_pi
    address = _object_address(pi)
    if pi is not None and address is None:
        pi = None
    if address == _pi_address and ((_pi_ref() if _pi_ref is not None else None) is not None or pi is None):
        return
    # Old native hook must never process a retired PlayerInput identity.
    _stop_native_lifecycle()
    _stop_native_pre_hook()
    if restore_retired and _pi_address is not None:
        _restore_speed_guard(clear_block=True)
        if _linked_left_applied:
            _restore_linked_left()
        if _native_left_applied:
            _restore_native_left()
        if _native_right_applied:
            _restore_native_right()
        if _linked_right_applied:
            _restore_linked_right()
    _clear_linked_right_state(clear_block=True)
    _clear_linked_left_state(clear_block=True)
    _clear_native_left_state(clear_block=True)
    # AdvancedAxis definitions are global rather than PlayerInput-owned, but a
    # real local PlayerInput replacement is still a valid retry boundary. Clear
    # right-stick quarantine/support state as well so an earlier compatibility
    # conflict cannot strand the right stick on fallback after travel/restart.
    _clear_native_right_state(clear_block=True)
    _speed_owner_pi = None
    _pi_ref = WeakPointer(pi) if pi is not None else None
    _pi_address = address
    _reset_native_radial()
    _reset_fallback_state(clear_device=True)


def _refresh_local_input(candidate: Any | None = None) -> Any | None:
    """Resolve the current local PlayerInput; never trust a still-valid retired object."""
    cached = _pi_ref() if _pi_ref is not None else None
    candidate_address = _object_address(candidate)
    if candidate_address is not None and cached is not None and candidate_address == _pi_address:
        return cached
    try:
        pc = get_pc(possibly_loading=True)
        pi = pc.PlayerInput if pc is not None else None
    except Exception:
        pi = None
    current_address = _object_address(pi)
    if current_address != _pi_address or (pi is not None and cached is None):
        _cache_player_input(pi)
    return pi


def _linked_axis_candidates(pi: Any) -> dict[str, tuple[Any, int, float, str]]:
    """Resolve physical controller axes through the current live PlayerInput handlers."""
    native = _discover_native_physical_sources(pi)
    if native is not None:
        verified, conflicts = native
        return {physical: (device, index, float(device.Axes[index].DeadZone), logical)
                for physical, (device, index, _address, logical) in verified.items()
                if physical not in conflicts}
    verified, conflicts = _controller_speed_candidates(pi)
    found: dict[str, tuple[Any, int, float, str]] = {}
    try:
        rows = pi.Axes
    except Exception:
        return found

    for row in rows:
        try:
            handler = row.InputDeviceHandler
            device = handler.SourceDevice
            source_index = int(handler.SourceIndex)
            if device is None or source_index < 0:
                continue
            axes = device.Axes
            if source_index >= len(axes):
                continue
            source = axes[source_index]
            physical = str(source.AxisName)
            candidate = verified.get(physical)
            if (physical in conflicts or candidate is None or
                    candidate[1:] != (source_index, _object_address(device))):
                continue
            if physical in found:
                # One physical row driving different logical axes is ambiguous.
                if found[physical][3] != str(row.AxisName):
                    conflicts.add(physical)
                continue
            found[physical] = (device, source_index, float(source.DeadZone), str(row.AxisName))
        except Exception:
            continue
    return {physical: entry for physical, entry in found.items() if physical not in conflicts}


def _linked_left_mapping_current(pi: Any) -> bool:
    """Check whether cached linked-left ownership still matches live handlers."""
    if _linked_left_supported is not True or _linked_left_owner_address != _object_address(pi):
        return False
    found = _linked_axis_candidates(pi)
    for physical in ("XboxTypeS_LeftX", "XboxTypeS_LeftY"):
        entry = found.get(physical)
        target = _linked_left_targets.get(physical)
        if entry is None or target is None:
            return False
        device, source_index, _deadzone, _logical = entry
        pointer, cached_index, _original = target
        if source_index != cached_index:
            return False
        if _object_address(device) != _object_address(pointer()):
            return False
    return True


def _native_right_state_valid() -> bool:
    if _native_right_supported is not True or len(_native_right_original) != 2:
        return False
    for pointer, field, _original in _native_right_original.values():
        obj = pointer()
        if obj is None:
            return False
        try:
            float(getattr(obj, field))
        except Exception:
            return False
    return True


def _discover_linked_left(pi: Any) -> bool:
    global _linked_left_supported, _linked_left_owner_address, _linked_left_targets
    global _left_backend_issue
    address = _object_address(pi)
    if _linked_left_block is not None and _linked_left_block[0] == address:
        _left_backend_issue = f"{_linked_left_block[1]}; retry after input rebuild"
        return False
    if _linked_left_supported is True and _linked_left_mapping_current(pi):
        return True
    if _linked_left_applied:
        _restore_linked_left()
    _clear_linked_left_state()

    found = _linked_axis_candidates(pi)
    targets = {}
    identities = set()
    for physical in ("XboxTypeS_LeftX", "XboxTypeS_LeftY"):
        entry = found.get(physical)
        if entry is None:
            _left_backend_issue = f"linked mapping missing {physical}"
            _linked_left_supported = False
            return False
        device, source_index, original, _logical = entry
        device_address = _object_address(device)
        if device_address is None:
            _left_backend_issue = f"linked source device invalid for {physical}"
            _linked_left_supported = False
            return False
        identity = (device_address, source_index)
        if identity in identities:
            _left_backend_issue = "linked left axes share one native source row"
            _linked_left_supported = False
            return False
        identities.add(identity)
        targets[physical] = (WeakPointer(device), source_index, original)
        _linked_source_addresses[physical] = device_address

    _linked_left_owner_address = address
    _linked_left_targets = targets
    _linked_left_supported = True
    return True


def _binding_rows(pi: Any) -> dict[str, Any] | None:
    try:
        rows = {}
        for row in pi.Bindings:
            name = str(row.Name)
            if name in ("XboxTypeS_LeftX", "XboxTypeS_LeftY"):
                rows[name] = row
        return rows if len(rows) == 2 else None
    except Exception:
        return None


def _replace_deadzone(command: str, value: float) -> str | None:
    if _DEADZONE_RE.search(command) is None:
        return None
    return _DEADZONE_RE.sub(lambda m: f"{m.group(1)}{value:.6g}", command, count=1)


def _discover_native_left(pi: Any) -> bool:
    global _native_left_supported, _native_left_owner, _native_left_address
    global _native_left_original, _native_left_applied, _left_backend_issue
    address = _object_address(pi)
    if _native_left_block is not None and _native_left_block[0] == address:
        _left_backend_issue = f"{_native_left_block[1]}; retry after input rebuild"
        return False
    if _native_left_supported is True and _native_left_address == address:
        return True

    rows = _binding_rows(pi)
    if rows is None:
        _left_backend_issue = "legacy left-stick binding rows unavailable"
        _native_left_supported = False
        return False

    # The binding names identify the physical left-stick axes. Do not require
    # a particular logical target such as aStrafe/aBaseY: Southpaw/Legacy-style
    # layouts may legitimately route these physical axes elsewhere. We still
    # require an existing DeadZone token and never append or invent commands.
    original = {}
    for key, row in rows.items():
        command = str(row.Command)
        if _DEADZONE_RE.search(command) is None:
            _left_backend_issue = f"legacy binding {key} has no DeadZone token"
            _native_left_supported = False
            return False
        original[key] = command

    _native_left_owner = WeakPointer(pi)
    _native_left_address = _object_address(pi)
    _native_left_original = original
    _native_left_applied = {}
    _native_left_supported = True
    return True


def _find_deadzone_field(obj: Any) -> str | None:
    for name in ("DeadZone", "Deadzone", "DeadZoneSize", "DeadZonePercent", "DeadZonePercentage"):
        try:
            prop = obj.Class._find_prop(name)
            if prop is not None and str(prop.Class.Name) in ("FloatProperty", "DoubleProperty") and int(prop.ArrayDim) == 1:
                return name
        except Exception:
            continue
    return None


def _discover_native_right() -> bool:
    global _native_right_supported, _native_right_original, _native_right_applied
    if _native_right_block_reason is not None:
        return False
    if _native_right_supported is True:
        return True

    discovered = {}
    for axis, path in (("x", "GD_Input.AdvancedAxis.Turn"), ("y", "GD_Input.AdvancedAxis.LookUp")):
        try:
            obj = unrealsdk.find_object("AdvancedAxisDefinition", path)
        except Exception:
            obj = None
        if obj is None:
            _native_right_supported = False
            return False
        field = _find_deadzone_field(obj)
        if field is None:
            _native_right_supported = False
            return False
        try:
            value = float(getattr(obj, field))
        except Exception:
            _native_right_supported = False
            return False
        discovered[axis] = (WeakPointer(obj), field, value)

    _native_right_original = discovered
    _native_right_applied = {}
    _native_right_supported = True
    return True


def _linked_deadzone(pointer: WeakPointer, source_index: int) -> float | None:
    device = pointer()
    if device is None:
        return None
    try:
        axes = device.Axes
        if source_index < 0 or source_index >= len(axes):
            return None
        row = axes[source_index]
        address = _object_address(device)
        physical = str(row.AxisName)
        if address is None:
            return None
        return _source_field_read(row, address, source_index, physical, "DeadZone")
    except Exception:
        return None


def _set_linked_deadzone(pointer: WeakPointer, source_index: int, value: float) -> bool:
    device = pointer()
    if device is None:
        return False
    try:
        axes = device.Axes
        if source_index < 0 or source_index >= len(axes):
            return False
        row = axes[source_index]
        address = _object_address(device)
        physical = str(row.AxisName)
        if address is None:
            return False
        current = _source_field_read(row, address, source_index, physical, "DeadZone")
        if abs(current - value) <= 1e-6:
            return True
        if not _source_field_cas(row, address, source_index, physical,
                                 "DeadZone", current, value):
            return False
        # Re-resolve after mutation, never trust a stale WrappedStruct view.
        fresh = device.Axes[source_index]
        return abs(_source_field_read(fresh, address, source_index,
                                      physical, "DeadZone") - value) <= 1e-6
    except Exception:
        return False


def _restore_linked_left() -> None:
    global _linked_left_applied
    for physical, (pointer, source_index, original) in _linked_left_targets.items():
        applied = _linked_left_applied.get(physical)
        if applied is None:
            continue
        current = _linked_deadzone(pointer, source_index)
        # Preserve external edits. Only restore a value which still equals ours.
        address = _linked_source_addresses.get(physical)
        if address is None:
            continue
        if current is None:
            _queue_field_restore(pointer, source_index, address, physical, "DeadZone", original, applied)
            continue
        if abs(current - applied) <= 1e-6:
            if not _set_linked_deadzone(pointer, source_index, original):
                _queue_field_restore(pointer, source_index, address, physical, "DeadZone", original, applied)
        else:
            _relinquish_field(address, source_index, physical, "DeadZone")
    _linked_left_applied = {}


def _apply_linked_left(pi: Any, lx_dz: float, ly_dz: float) -> bool:
    global _linked_left_applied, _linked_left_block, _left_backend_issue
    if not _discover_linked_left(pi):
        return False
    targets = {"XboxTypeS_LeftX": lx_dz, "XboxTypeS_LeftY": ly_dz}
    for physical, target in targets.items():
        pointer, source_index, original = _linked_left_targets[physical]
        if (_object_address(pointer()), source_index, physical, "DeadZone") in _pending_field_restores:
            _left_backend_issue = "pending verified deadzone restoration"
            return False
        desired = target
        current = _linked_deadzone(pointer, source_index)
        if current is None:
            _left_backend_issue = f"linked deadzone row became unreadable for {physical}"
            _linked_left_block = (_object_address(pi), _left_backend_issue)
            _restore_linked_left()
            return False
        previous = _linked_left_applied.get(physical)
        if previous is not None and abs(current - previous) > 1e-6 and abs(current - original) > 1e-6:
            _left_backend_issue = f"linked deadzone changed externally for {physical}"
            _linked_left_block = (_object_address(pi), _left_backend_issue)
            _restore_linked_left()
            return False
        # Record ownership before mutation so rollback still knows our intent if readback fails.
        _linked_left_applied[physical] = desired
        if abs(current - desired) <= 1e-6:
            continue
        if not _set_linked_deadzone(pointer, source_index, desired):
            _left_backend_issue = f"linked write/readback failed for {physical}"
            _linked_left_block = (_object_address(pi), _left_backend_issue)
            _restore_linked_left()
            return False
    return True


def _linked_right_mapping_current(pi: Any) -> bool:
    if _linked_right_supported is not True or _linked_right_owner_address != _object_address(pi):
        return False
    found, conflicts = _controller_speed_candidates(pi)
    if any(key in conflicts for key in ("XboxTypeS_RightX", "XboxTypeS_RightY")):
        return False
    for physical in ("XboxTypeS_RightX", "XboxTypeS_RightY"):
        entry = found.get(physical)
        target = _linked_right_targets.get(physical)
        if entry is None or target is None:
            return False
        pointer, idx, _original = target
        if idx != entry[1] or _object_address(pointer()) != entry[2]:
            return False
    return True


def _linked_right_still_current(pi: Any) -> bool:
    if not _linked_right_mapping_current(pi):
        return False
    for physical, applied in _linked_right_applied.items():
        pointer, idx, _original = _linked_right_targets[physical]
        row = _speed_row(pointer, idx, _object_address(pointer()), physical)
        if row is None or abs(float(row.DeadZone) - applied) > 1e-6:
            return False
    return bool(_linked_right_applied)


def _discover_linked_right(pi: Any) -> bool:
    """Use verified live *physical* controller rows for right-stick deadzones."""
    global _linked_right_supported, _linked_right_owner_address, _linked_right_targets
    global _right_backend_issue
    address = _object_address(pi)
    if _linked_right_block is not None and _linked_right_block[0] == address:
        _right_backend_issue = f"{_linked_right_block[1]}; retry after input rebuild"
        return False
    if _linked_right_supported is True and _linked_right_mapping_current(pi):
        return True
    if _linked_right_applied:
        _restore_linked_right()
    _clear_linked_right_state()
    found, conflicts = _controller_speed_candidates(pi)
    expected = ("XboxTypeS_RightX", "XboxTypeS_RightY")
    if any(key in conflicts for key in expected):
        _right_backend_issue = "ambiguous physical right-stick controller mapping"
        return False
    targets = {}
    ids = set()
    for physical in expected:
        entry = found.get(physical)
        if entry is None:
            _right_backend_issue = f"physical controller source missing for {physical}"
            return False
        device, idx, dev_address = entry
        if (dev_address, idx) in ids:
            _right_backend_issue = "right-stick axes share one source row"
            return False
        ids.add((dev_address, idx))
        try:
            original = float(device.Axes[idx].DeadZone)
            if not 0.0 <= original <= 1.0:
                raise ValueError('invalid native deadzone')
            targets[physical] = (WeakPointer(device), idx, original)
            _linked_source_addresses[physical] = dev_address
        except Exception:
            _right_backend_issue = f"physical native deadzone unreadable for {physical}"
            return False
    _linked_right_owner_address = address
    _linked_right_targets = targets
    _linked_right_supported = True
    return True


def _restore_linked_right() -> None:
    global _linked_right_applied
    for physical, (pointer, idx, original) in _linked_right_targets.items():
        applied = _linked_right_applied.get(physical)
        if applied is None:
            continue
        address = _linked_source_addresses.get(physical)
        row = _speed_row(pointer, idx, address, physical) if address is not None else None
        if row is not None and abs(float(row.DeadZone) - applied) <= 1e-6:
            if not _set_linked_deadzone(pointer, idx, original):
                _queue_field_restore(pointer, idx, address, physical, "DeadZone", original, applied)
        elif address is not None:
            if row is None:
                _queue_field_restore(pointer, idx, address, physical, "DeadZone", original, applied)
            else:
                _relinquish_field(address, idx, physical, "DeadZone")
    _linked_right_applied = {}


def _apply_linked_right(pi: Any, rx_dz: float, ry_dz: float) -> bool:
    global _linked_right_block, _linked_right_applied, _right_backend_issue
    if not _discover_linked_right(pi):
        return False
    for physical, desired in (("XboxTypeS_RightX", rx_dz), ("XboxTypeS_RightY", ry_dz)):
        pointer, idx, original = _linked_right_targets[physical]
        device_address = _object_address(pointer())
        if (device_address, idx, physical, "DeadZone") in _pending_field_restores:
            _right_backend_issue = "pending verified deadzone restoration"
            return False
        row = _speed_row(pointer, idx, device_address, physical) if device_address is not None else None
        current = float(row.DeadZone) if row is not None else None
        if current is None:
            _right_backend_issue = f"physical deadzone unreadable for {physical}"
            _linked_right_block = (_object_address(pi), _right_backend_issue)
            _restore_linked_right()
            return False
        previous = _linked_right_applied.get(physical)
        if previous is not None and abs(current - previous) > 1e-6 and abs(current - original) > 1e-6:
            _right_backend_issue = f"physical deadzone externally changed for {physical}"
            _linked_right_block = (_object_address(pi), _right_backend_issue)
            _restore_linked_right()
            return False
        _linked_right_applied[physical] = desired
        if abs(current - desired) <= 1e-6:
            continue
        if not _set_linked_deadzone(pointer, idx, desired):
            _right_backend_issue = f"physical deadzone write/readback failed for {physical}"
            _linked_right_block = (_object_address(pi), _right_backend_issue)
            _restore_linked_right()
            return False
    return True


def _restore_native_left() -> None:
    global _native_left_applied
    pi = _native_left_owner() if _native_left_owner is not None else None
    if pi is None or not _native_left_original:
        _native_left_applied = {}
        return
    rows = _binding_rows(pi)
    if rows is None:
        return
    for key, original in _native_left_original.items():
        applied = _native_left_applied.get(key)
        if applied is None:
            continue
        try:
            if str(rows[key].Command) == applied:
                rows[key].Command = original
        except Exception:
            pass
    _native_left_applied = {}


def _restore_native_right() -> None:
    global _native_right_applied
    for axis, (pointer, field, original) in _native_right_original.items():
        obj = pointer()
        applied = _native_right_applied.get(axis)
        if obj is None or applied is None:
            continue
        try:
            if abs(float(getattr(obj, field)) - applied) <= 1e-6:
                setattr(obj, field, original)
        except Exception:
            pass
    _native_right_applied = {}


def _apply_native_left(
    pi: Any | None, left_on: bool, lx_dz: float, ly_dz: float
) -> tuple[bool, str]:
    """Apply the best verified left-stick backend for the live PlayerInput."""
    global _native_left_applied, _native_left_block, _left_backend_issue
    if not left_on:
        _restore_linked_left()
        _restore_native_left()
        return True, "off"
    if pi is None or _object_address(pi) is None:
        return False, "fallback"

    if _apply_linked_left(pi, lx_dz, ly_dz):
        if _native_left_applied:
            _restore_native_left()
        return True, "native-linked"

    if _discover_native_left(pi):
        rows = _binding_rows(pi)
        if rows is not None:
            targets = {"XboxTypeS_LeftX": lx_dz, "XboxTypeS_LeftY": ly_dz}
            success = True
            for key, target in targets.items():
                original = _native_left_original[key]
                desired = _replace_deadzone(original, target)
                if desired is None:
                    success = False
                    break
                current = str(rows[key].Command)
                previous = _native_left_applied.get(key)
                if previous is not None and current not in (previous, original):
                    _left_backend_issue = f"legacy binding changed externally for {key}"
                    _native_left_block = (_object_address(pi), _left_backend_issue)
                    success = False
                    break
                _native_left_applied[key] = desired
                if current == desired:
                    continue
                try:
                    rows[key].Command = desired
                    if str(rows[key].Command) != desired:
                        _left_backend_issue = f"legacy binding readback failed for {key}"
                        _native_left_block = (_object_address(pi), _left_backend_issue)
                        success = False
                        break
                except Exception:
                    _left_backend_issue = f"legacy binding write failed for {key}"
                    _native_left_block = (_object_address(pi), _left_backend_issue)
                    success = False
                    break
            if success:
                return True, "native-binding"
            _restore_native_left()

    if _left_backend_issue is None:
        _left_backend_issue = "no verified native left-stick backend"
    return False, "fallback"


def _apply_native_right(
    pi: Any | None, right_on: bool, rx_dz: float, ry_dz: float
) -> tuple[bool, str]:
    """Prefer physical controller DeadZone; retain AdvancedAxis fallback."""
    global _native_right_applied, _native_right_block_reason, _right_backend_issue
    if not right_on:
        _restore_linked_right()
        _restore_native_right()
        return True, "off"
    if pi is not None and _object_address(pi) is not None:
        if _apply_linked_right(pi, rx_dz, ry_dz):
            if _native_right_applied:
                _restore_native_right()
            return True, "native-linked"
    if _native_right_supported is True and not _native_right_state_valid():
        _restore_native_right()
        _clear_native_right_state(clear_block=True)

    if not _discover_native_right():
        if _native_right_block_reason is not None:
            _right_backend_issue = f"{_native_right_block_reason}; retry after input rebuild"
        else:
            _right_backend_issue = "native right-stick backend unavailable"
        return False, "fallback"

    targets = {"x": rx_dz, "y": ry_dz}
    for axis, target in targets.items():
        pointer, field, original = _native_right_original[axis]
        obj = pointer()
        if obj is None:
            _right_backend_issue = "native AdvancedAxis object became unavailable"
            _native_right_block_reason = _right_backend_issue
            _restore_native_right()
            _clear_native_right_state()
            return False, "fallback"
        desired = target
        previous = _native_right_applied.get(axis)
        try:
            current = float(getattr(obj, field))
        except Exception:
            _right_backend_issue = "native AdvancedAxis read failed"
            _native_right_block_reason = _right_backend_issue
            _restore_native_right()
            _clear_native_right_state()
            return False, "fallback"
        if previous is not None and abs(current - previous) > 1e-6 and abs(current - original) > 1e-6:
            _right_backend_issue = "native AdvancedAxis value changed externally"
            _native_right_block_reason = _right_backend_issue
            _restore_native_right()
            return False, "fallback"
        _native_right_applied[axis] = desired
        if abs(current - desired) <= 1e-6:
            continue
        try:
            setattr(obj, field, desired)
            if abs(float(getattr(obj, field)) - desired) > 1e-6:
                raise ValueError("readback mismatch")
        except Exception:
            _right_backend_issue = "native AdvancedAxis write/readback failed"
            _native_right_block_reason = _right_backend_issue
            _restore_native_right()
            return False, "fallback"
    return True, "native-advanced"


def _apply_native(settings=None) -> tuple[bool, bool, str, str, bool]:
    """Apply native backends and report whether a live PlayerInput exists."""
    global _left_backend_issue, _right_backend_issue
    _left_backend_issue = None
    _right_backend_issue = None
    if settings is None:
        settings = _settings()
    left_on, right_on, _, lx_dz, ly_dz, rx_dz, ry_dz = settings[:7]
    radial_left, radial_right = settings[8], settings[9]
    if radial_left:
        left_on, lx_dz, ly_dz = True, 0.0, 0.0
    if radial_right:
        right_on, rx_dz, ry_dz = True, 0.0, 0.0
    left_endpoint_guard = left_on and _needs_endpoint_guard(lx_dz, ly_dz)
    right_endpoint_guard = right_on and _needs_endpoint_guard(rx_dz, ry_dz)

    # Exactly 1.0 is a bad endpoint for BL2's native deadzone math. Keep native
    # ownership at a safe near-1 value, then force the existing compatibility
    # path for that stick so a user-facing 100% means truly zero output. This
    # special case is the only reason a valid native backend uses hot hooks.
    native_lx = _native_safe_deadzone(lx_dz)
    native_ly = _native_safe_deadzone(ly_dz)
    native_rx = _native_safe_deadzone(rx_dz)
    native_ry = _native_safe_deadzone(ry_dz)

    pi = _refresh_local_input()
    pi_available = pi is not None and _object_address(pi) is not None
    left_native, left_backend = _apply_native_left(
        pi if pi_available else None, left_on, native_lx, native_ly
    )
    right_native, right_backend = _apply_native_right(
        pi if pi_available else None, right_on, native_rx, native_ry
    )

    if left_endpoint_guard and left_native:
        left_native = False
        left_backend = "100%-guard"
        _left_backend_issue = "exact 100% endpoint is forced to zero by compatibility guard"
    if right_endpoint_guard and right_native:
        right_native = False
        right_backend = "100%-guard"
        _right_backend_issue = "exact 100% endpoint is forced to zero by compatibility guard"

    return left_native, right_native, left_backend, right_backend, pi_available


def _fallback_needs(settings, left_native: bool, right_native: bool) -> tuple[bool, bool, bool]:
    left_on, right_on, *_ = settings
    fallback_left = left_on and not left_native
    fallback_right = right_on and not right_native
    # Device tracking and KBM protection are mandatory for any Python fallback.
    # The saved protect_kbm option controls only mixed-input priority/grace.
    return fallback_left, fallback_right, fallback_left or fallback_right


def _sync_runtime_hooks(need_hot: bool, need_key: bool, need_radial: bool) -> bool:
    """Verify *all* required callbacks before reporting active input filtering."""
    global _runtime_hook_issue
    axis_ok = True
    player_input_ok = True
    key_ok = True
    radial_ok = True
    native_axis = bool(need_radial and not need_hot and _try_native_axis_hook())
    if not native_axis:
        _stop_native_axis_hook()
    if native_axis:
        _remove_hook("ControllerDeadzoneControl:Axis")
    elif need_hot or need_radial:
        axis_ok = _add_hook(VIEWPORT_AXIS, Type.PRE, "ControllerDeadzoneControl:Axis", _capture_axis)
    else:
        _remove_hook("ControllerDeadzoneControl:Axis")

    if need_hot:
        player_input_ok = _add_hook(PLAYER_INPUT, Type.POST_UNCONDITIONAL,
                                    "ControllerDeadzoneControl:PlayerInput", _apply_deadzones)
    else:
        _remove_hook("ControllerDeadzoneControl:PlayerInput")

    # Install native InputKey first. The native PRE hook can then monitor both
    # native event hooks without entering Python on normal frames.
    native_key = bool(need_key and native_axis and _try_native_key_hook())
    if not native_key:
        _stop_native_key_hook()
    if native_key:
        _remove_hook("ControllerDeadzoneControl:InputKey")
    elif need_key:
        key_ok = _add_hook(VIEWPORT_KEY, Type.PRE,
                           "ControllerDeadzoneControl:InputKey", _input_key)
    else:
        _remove_hook("ControllerDeadzoneControl:InputKey")

    # Native PlayerInput PRE hook replaces the last per-frame Python radial
    # callback. It only runs with verified PlayerInput and physical mappings.
    native_pre = bool(need_radial and native_axis and _try_native_pre_hook())
    if not native_pre:
        _stop_native_lifecycle()  # No observers needed with Python or axial input.
        _stop_native_pre_hook()
    if native_pre:
        _remove_hook("ControllerDeadzoneControl:Radial")
    elif need_radial:
        radial_ok = _add_hook(PRE_PROCESS_INPUT, Type.PRE,
                              "ControllerDeadzoneControl:Radial", _apply_radial)
    else:
        _remove_hook("ControllerDeadzoneControl:Radial")
    missing = []
    if (need_hot or need_radial) and not axis_ok:
        missing.append("InputAxis")
    if need_hot and not player_input_ok:
        missing.append("PlayerInput")
    if need_key and not key_ok:
        missing.append("InputKey")
    if need_radial and not radial_ok:
        missing.append("Radial")
    _runtime_hook_issue = ", ".join(missing) if missing else None
    return not missing


def _cancel_input_recovery() -> None:
    global _recovery_deadline, _recovery_next_scan
    _recovery_deadline = _recovery_next_scan = 0.0
    _remove_hook("ControllerDeadzoneControl:InputRecovery")


def _schedule_input_recovery() -> None:
    """Bounded, game-thread retries only while a binding table is incomplete."""
    global _recovery_deadline, _recovery_next_scan, _recovery_deferred
    if not _enabled:
        return
    _recovery_deferred = True
    if _recovery_deadline > 0.0:
        return  # Nested reconciliation cannot extend the retry budget.
    now = time.monotonic()
    if _add_hook(RECOVERY_TICK, Type.POST,
                 "ControllerDeadzoneControl:InputRecovery", _input_recovery_tick):
        _recovery_deadline = now + RECOVERY_SECONDS
        _recovery_next_scan = now + RECOVERY_INTERVAL


def _input_recovery_tick(obj: Any, args: Any, ret: Any, func: Any) -> None:
    global _recovery_next_scan, _recovery_running
    if not _enabled or _recovery_deadline <= 0.0:
        _cancel_input_recovery()
        return
    now = time.monotonic()
    if now >= _recovery_deadline:
        _cancel_input_recovery()
        _warn_once("input_recovery_exhausted", "controller bindings did not recover within 15 seconds; "
                   "native PRE remains disarmed. Retry after an input rebuild or re-enable the mod.")
        return
    if _recovery_running or now < _recovery_next_scan:
        return
    # Co-op controllers must never drive our local reconciliation.
    try:
        if _object_address(obj) != _object_address(get_pc(possibly_loading=True)):
            return
    except Exception:
        return
    _recovery_next_scan = now + RECOVERY_INTERVAL
    _recovery_running = True
    try:
        _reconcile_runtime()
    except Exception as exc:
        _cancel_input_recovery()
        _warn_once("input_recovery_error", f"controller recovery stopped: {type(exc).__name__}: {exc}")
    finally:
        _recovery_running = False


def _reconcile_runtime(settings=None) -> None:
    global _runtime_left_fallback, _runtime_right_fallback, _runtime_protect
    global _runtime_hook_issue
    global _runtime_kbm_priority
    global _runtime_lx_dz, _runtime_ly_dz, _runtime_rx_dz, _runtime_ry_dz, _runtime_wake
    global _runtime_radial_left, _runtime_radial_right
    global _runtime_radial_left_inner, _runtime_radial_left_outer
    global _runtime_radial_right_inner, _runtime_radial_right_outer
    global _recovery_deferred
    if not _enabled:
        return
    if _failed_rebuild_is_current():
        return  # Never reapply radial/Speed writes to a partial input table.
    if settings is None:
        settings = _settings()
    _recovery_deferred = False
    _retry_field_restores()

    was_hot = (_runtime_left_fallback or _runtime_right_fallback or
               _runtime_radial_left or _runtime_radial_right)
    left_native, right_native, left_backend, right_backend, pi_available = _apply_native(settings)
    # Use only verified physical controller rows; the native Speed guard runs
    # once after radial backend + hook verification below, to avoid a temporary
    # unguarded transition during mode changes.
    fallback_left, fallback_right, need_key = _fallback_needs(settings, left_native, right_native)
    # Only the verified physical native linked backend is allowed to bypass
    # the original independent axial filtering. Never silently apply radial
    # *after* an unknown native/advanced/legacy deadzone stage.
    radial_left_ok = bool(settings[8]) and left_native and left_backend == "native-linked"
    radial_right_ok = bool(settings[9]) and right_native and right_backend == "native-linked"
    if ((settings[8] and not radial_left_ok) or (settings[9] and not radial_right_ok)):
        current = _pi_ref() if _pi_ref is not None else None
        found, conflicts = _controller_speed_candidates(current) if current is not None else ({}, set())
        if not conflicts and any(physical not in found for physical in AXIS_INDEX):
            _schedule_input_recovery()
    if settings[8] and pi_available and not radial_left_ok and not _recovery_deferred:
        _warn_once("radial_left_backend", "radial left input is unavailable without "
                   "verified unfiltered physical controller axes; leaving its native path unchanged")
    if settings[9] and pi_available and not radial_right_ok and not _recovery_deferred:
        _warn_once("radial_right_backend", "radial right input is unavailable without "
                   "verified unfiltered physical controller axes; leaving its native path unchanged")
    # If linked physical native mapping failed, undo a possible legacy binding
    # or AdvancedAxis write of zero. Do not leave a deadzone-only partial mode.
    if settings[8] and not radial_left_ok:
        _apply_native_left(_pi_ref() if _pi_ref is not None else None, False, 0.0, 0.0)
    if settings[9] and not radial_right_ok:
        _apply_native_right(_pi_ref() if _pi_ref is not None else None, False, 0.0, 0.0)
    # During load/rebuild windows there may be no live local PlayerInput. Do not
    # install the expensive compatibility fallback just because discovery is
    # temporarily impossible. Setup/lifecycle hooks reconcile again when the
    # actual PlayerInput exists.
    if not pi_available:
        if settings[0] and not left_native:
            fallback_left = False
            left_backend = "pending-playerinput"
        if settings[1] and not right_native:
            fallback_right = False
            right_backend = "pending-playerinput"
        radial_left_ok = False
        radial_right_ok = False
        need_key = False
    need_key = need_key or radial_left_ok or radial_right_ok
    _runtime_radial_left = radial_left_ok
    _runtime_radial_right = radial_right_ok
    _runtime_radial_left_inner, _runtime_radial_left_outer = settings[10], settings[11]
    _runtime_radial_right_inner, _runtime_radial_right_outer = settings[12], settings[13]
    _runtime_left_fallback = fallback_left
    _runtime_right_fallback = fallback_right
    _runtime_protect = need_key
    _runtime_kbm_priority = bool(settings[2])
    _runtime_lx_dz, _runtime_ly_dz = settings[3], settings[4]
    _runtime_rx_dz, _runtime_ry_dz = settings[5], settings[6]
    _runtime_wake = settings[7]
    need_hot = fallback_left or fallback_right
    need_radial = radial_left_ok or radial_right_ok
    if not (need_hot or need_radial):
        # Once native/native is established there is no fallback state to clear on
        # every slider tick. Clear it only when leaving a hot fallback runtime.
        if was_hot:
            _reset_fallback_state(clear_device=True)
        else:
            _set_active_device(None)
    # Reconfigure one C++ state machine per runtime reconciliation, never per
    # controller event. The selected Python path remains entirely independent.
    if _native_requested() and _native_radial_module is not None and not _native_radial_failed:
        try:
            _configure_native_runtime()
        except Exception as exc:
            _native_runtime_failed("configure", exc)
    hooks_ok = _sync_runtime_hooks(need_hot, need_key, need_radial)
    if not hooks_ok:
        # PlayerInput and InputKey are mandatory when Python owns an axial,
        # endpoint or radial fallback. Never retain a partly installed pipeline:
        # an unfiltered native DeadZone=0 or 100%-endpoint would be unsafe.
        missing = _runtime_hook_issue or "unknown required callback"
        _warn_once("required_input_hooks", "required input hook unavailable ("
                   + missing + "); deadzone filtering is suspended until a "
                   "successful input/settings reconciliation")
        pi = _pi_ref() if _pi_ref is not None else None
        if settings[0] or settings[8]:
            _apply_native_left(pi, False, 0.0, 0.0)
        if settings[1] or settings[9]:
            _apply_native_right(pi, False, 0.0, 0.0)
        _runtime_left_fallback = _runtime_right_fallback = False
        _runtime_radial_left = _runtime_radial_right = False
        _runtime_protect = False
        _sync_runtime_hooks(False, False, False)  # Remove only our tracked IDs.
        _runtime_hook_issue = missing  # Preserve the degraded state after cleanup.
        _restore_speed_guard()
        _reset_fallback_state(clear_device=True)
        if _recovery_deadline > 0.0 and not _recovery_deferred:
            _cancel_input_recovery()
        return
    # Suppress only *idle* radial sticks while KBM owns the mixed input frame.
    # Held controller sticks must still produce input when KBM Priority is On.
    _kbm_guard_last_requested[0] = None  # Settings or backend may have changed.
    # Hook registration may have disabled radial mode; use the committed state,
    # not the pre-registration request, when reconciling native Speed ownership.
    if _runtime_radial_left or _runtime_radial_right:
        _sync_radial_kbm_guard(settings)
    else:
        _apply_speed_guard(_pi_ref() if _pi_ref is not None else None, settings)

    if _recovery_deadline > 0.0 and not _recovery_deferred:
        _cancel_input_recovery()


def _settings_changed(option, value) -> None:
    global _settings_cache
    _settings_cache = None
    if _enabled and not _setting_batch:
        # Option callbacks run before the option stores its new value, so reconcile
        # using a prospective settings tuple built with the callback's value.
        _reconcile_runtime(_settings_tuple(option, value))


def _refresh_visible_options_page() -> None:
    """Repopulate our current WillowScrollingList without leaving the page.

    The shipped willow2_mod_menu has no dangerous_reload_current_page. Its
    current OptionsDataProvider is publicly exposed in data_provider_stack,
    and WillowScrollingList.Refresh() is the native supported rebuild route.
    Clear the Python provider's cached drawn-options table before repopulating
    so EventID mappings do not accumulate on successive refreshes.
    """
    from willow2_mod_menu import options_menu
    if not options_menu.data_provider_stack:
        return
    provider = options_menu.data_provider_stack[-1]
    if not getattr(provider, "drawn_options", None):
        return
    if not any(option is stored for option in provider.drawn_options for stored in _stored_options):
        return  # Never refresh an unrelated mod's options page.
    the_list = options_menu.latest_list()
    if the_list is None:
        return
    selected = int(the_list.GetSelectedIndex())
    provider.drawn_options.clear()
    # Refresh repopulates native rows via the existing current data provider.
    the_list.Refresh()
    try:
        the_list.SetSelectedIndex(selected)
    except Exception:
        pass  # A changed row count should not prevent the options from updating.


def _menu_refresh_tick(obj: Any, args: Any, ret: Any, func: Any) -> None:
    """Refresh after the current spinner or button callback has completed."""
    global _menu_refresh_pending
    if not _menu_refresh_pending:
        return
    _menu_refresh_pending = False
    _remove_hook("ControllerDeadzoneControl:RefreshMenu")
    try:
        _refresh_visible_options_page()
    except Exception as exc:
        _warn_once("menu_refresh", f"options refresh failed: {type(exc).__name__}: {exc}")


def _schedule_menu_refresh() -> None:
    global _menu_refresh_pending
    if _menu_refresh_pending:
        return
    try:
        from willow2_mod_menu import options_menu
        if not options_menu.data_provider_stack or options_menu.latest_list() is None:
            return
    except (ImportError, AttributeError):
        return
    except Exception as exc:
        _warn_once("menu_detection", f"could not inspect mod menu: {type(exc).__name__}: {exc}")
        return
    if _add_hook(MENU_TICK, Type.POST, "ControllerDeadzoneControl:RefreshMenu", _menu_refresh_tick):
        _menu_refresh_pending = True
    else:
        _warn_once("menu_tick_unavailable", "automatic menu refresh hook unavailable")


def _mode_changed(option, value) -> None:
    global _mode_change_guard, _settings_cache
    if _mode_change_guard or _setting_batch:
        return
    _mode_change_guard = True
    try:
        if bool(value):
            # A single active style at a time, with either or both disabled.
            counterpart = radial_mode if option is axial_mode else axial_mode
            counterpart.value = False
        _settings_cache = None
        _settings_changed(option, value)
        _schedule_menu_refresh()
    finally:
        _mode_change_guard = False


def _reset_to_defaults(option=None) -> None:
    """Reset via stable saved option objects, including older mods_base builds.

    ValueOption.reset() was added after the version shipped in some BL2 installs;
    use each option's default_value if present, otherwise explicit constructor
    defaults. Batch callbacks so a partial intermediate mode is never applied.
    """
    global _setting_batch, _settings_cache
    fallback_defaults = (
        True, 10, 10, True, 10, 10,
        True, 5,
        False, True, True, True,
        10, 100, 10, 100, False, False,
    )
    _setting_batch = True
    try:
        for item, fallback in zip(_stored_options, fallback_defaults):
            item.value = getattr(item, "default_value", fallback)
    finally:
        _setting_batch = False
    _settings_cache = None
    if _enabled:
        _reconcile_runtime(_settings_tuple())
    mod.save_settings()
    _schedule_menu_refresh()


def _capture_axis(obj: Any, args: Any, ret: Any, func: Any) -> None:
    if _native_axis_hook_live:
        return  # A deferred Python-hook removal must not duplicate native input.
    if not _enabled or not (_runtime_left_fallback or _runtime_right_fallback
                            or _runtime_radial_left or _runtime_radial_right):
        return
    protect = _runtime_protect
    key_name = None

    # In native radial mode the C++ state machine owns raw stick arrays,
    # staleness, ownership decisions and wake hysteresis. These UnrealSDK
    # callbacks are deliberately kept as thin argument bridges for safety.
    if _native_hot_available():
        try:
            value = float(args.Delta)
            try:
                is_gamepad = bool(args.bGamepad)
            except Exception:
                key_name = str(args.Key)
                is_gamepad = key_name.startswith("XboxTypeS_")
            if is_gamepad:
                if key_name is None:
                    key_name = str(args.Key)
                index = AXIS_INDEX.get(key_name, -1)
            else:
                index = -1
            state = int(_native_radial_module.axis_event(index, value, is_gamepad))
            _native_owner_changed(state)
            if is_gamepad and _runtime_kbm_priority and _active_device != "controller":
                _sync_radial_kbm_guard()
            return
        except Exception as exc:
            _native_runtime_failed("axis", exc)
            # Safe: the native axis bridge never mutates game-owned properties.

    try:
        is_gamepad = bool(args.bGamepad)
        value = float(args.Delta)
    except Exception:
        try:
            value = float(args.Delta)
            if key_name is None:
                key_name = str(args.Key)
            is_gamepad = key_name.startswith("XboxTypeS_")
        except Exception:
            return

    if not is_gamepad:
        if protect and value != 0.0:
            _mark_kbm_activity()
        return

    try:
        key = key_name if key_name is not None else str(args.Key)
    except Exception:
        return
    index = AXIS_INDEX.get(key)
    if index is None:
        return

    value = max(-1.0, min(1.0, value))
    needs_raw = ((index < RIGHT_X and (_runtime_left_fallback or _runtime_radial_left)) or
                 (index >= RIGHT_X and (_runtime_right_fallback or _runtime_radial_right)))
    event_time = 0.0
    if value != 0.0 and (needs_raw or protect):
        event_time = time.monotonic()

    # Any real controller-stick movement may wake fallback processing, even when
    # that physical axis is native. During simultaneous KBM + controller input,
    # Protect KBM intentionally keeps KBM ownership until KBM has been quiet for
    # a short interval instead of bouncing state on every event.
    # A resting controller can have 3-9% hardware drift. Do not allow that
    # drift to reclaim a KBM-owned frame and release native isolation. Reclaim
    # only on deliberate stick movement beyond the effective radial center.
    wake = _runtime_wake
    if _active_device != "controller":
        inner = (_runtime_radial_left_inner if index < RIGHT_X and _runtime_radial_left
                 else _runtime_radial_right_inner if index >= RIGHT_X and _runtime_radial_right
                 else None)
        if inner is not None and (_active_device == "kbm" or _last_kbm_activity > 0.0):
            wake = max(wake, 0.11, min(1.0, inner + 0.015))
    if (protect and _active_device != "controller" and abs(value) >= wake
            and _controller_can_wake(event_time)):
        _set_active_device("controller")

    if not needs_raw:
        return

    _raw[index] = value
    _seen[index] = event_time if value != 0.0 else 0.0
    # KBM Priority must not mute a deliberately moved physical stick. Update
    # the native idle-only guard at the actual gamepad event boundary; a held
    # KBM key does not prevent this one stick from working.
    if _runtime_kbm_priority and _active_device != "controller" and (
        _runtime_radial_left or _runtime_radial_right
    ):
        _sync_radial_kbm_guard()

    # Do not assign to args.Delta: SDK hook arguments are *copies* and that
    # assignment cannot modify the native engine's InputAxis parameter. Native
    # Speed ownership, above, handles the 100%-source test independently of KBM.


def _radial_multiplier(x: float, y: float, inner: float, outer: float) -> float:
    return radial_multiplier(x, y, inner, outer)


def _apply_radial(obj: Any, args: Any, ret: Any, func: Any) -> None:
    """Paired physical-axis filter at the proven PreProcessInput PRE stage.

    C++ computes the exact existing curve and reads/writes reflected accumulators
    in one batch. The Python path remains an independent known-good fallback.
    """
    global _native_radial_failed
    if _native_pre_hook_live:
        return  # The verified direct native PRE hook owns this frame.
    if not _enabled or not (_runtime_radial_left or _runtime_radial_right):
        return
    try:
        address = obj._get_address()
    except Exception:
        return
    if _pi_address is None or address != _pi_address:
        pi = _refresh_local_input(obj)
        if pi is None or _pi_address != address:
            return
    else:
        pi = obj
    # A failed native PRE hook may have modified part of this frame. Do not
    # apply a second radial transform on the first Python recovery callback.
    global _native_pre_skip_once
    if _native_pre_skip_once:
        _native_pre_skip_once = False
        _add_hook(VIEWPORT_AXIS, Type.PRE,
                  "ControllerDeadzoneControl:Axis", _capture_axis)
        _add_hook(VIEWPORT_KEY, Type.PRE,
                  "ControllerDeadzoneControl:InputKey", _input_key)
        _stop_native_pre_hook()
        _stop_native_key_hook()
        _stop_native_axis_hook()
        _reset_fallback_state(clear_device=True)
        return
    # A runtime fallback may be requested by an edge-only C++ notification.
    # Recover the original Python axis bridge before trying its radial samples.
    if _native_radial_failed and (_native_axis_hook_live or _native_key_hook_live):
        _add_hook(VIEWPORT_AXIS, Type.PRE,
                  "ControllerDeadzoneControl:Axis", _capture_axis)
        _add_hook(VIEWPORT_KEY, Type.PRE,
                  "ControllerDeadzoneControl:InputKey", _input_key)
        _stop_native_key_hook()
        _stop_native_axis_hook()
        _reset_fallback_state(clear_device=True)
        return  # First fallback frame must not use stale samples.
    if _native_axis_hook_live and _native_radial_module is not None:
        try:
            axis_hook_active = bool(_native_radial_module.axis_hook_active())
        except Exception:
            axis_hook_active = False
        if not axis_hook_active:
            _native_runtime_failed("native-axis-hook", RuntimeError(
                "direct InputAxis hook stopped unexpectedly"))
            _sync_runtime_hooks(False, True, True)  # Python hot bridge from next input.
            return  # Don't apply stale native physical samples this frame.
    if _native_key_hook_live and _native_radial_module is not None:
        try:
            key_hook_active = bool(_native_radial_module.key_hook_active())
        except Exception:
            key_hook_active = False
        if not key_hook_active:
            # Keep C++ axis/radial processing active: only Key decoding failed.
            # Restore the Python Key listener for the next key event.
            _warn_once("native_key_stopped", "native InputKey stopped; "
                       "restoring Python key listener")
            _add_hook(VIEWPORT_KEY, Type.PRE,
                      "ControllerDeadzoneControl:InputKey", _input_key)
            _stop_native_key_hook()
    if _native_requested() and not _native_radial_failed:
        if _load_native_radial_on_next_frame[0] or _native_radial_module is None:
            _load_native_radial_on_next_frame[0] = False
            _load_native_radial()
            # A user who toggles C++ ON in the menu doesn't trigger the usual
            # settings reconciliation. Promote the Python Axis bridge to the
            # native hook exactly once, after the new module is available.
            if (_native_hot_available() and not (_runtime_left_fallback or _runtime_right_fallback)):
                if not _native_axis_hook_live and _try_native_axis_hook():
                    _remove_hook("ControllerDeadzoneControl:Axis")
                if (_native_axis_hook_live and not _native_key_hook_live and
                        _try_native_key_hook()):
                    _remove_hook("ControllerDeadzoneControl:InputKey")
                if _native_axis_hook_live and _try_native_pre_hook():
                    _remove_hook("ControllerDeadzoneControl:Radial")
                    return  # Native hook takes ownership; no double-apply.
        if _native_hot_available():
            try:
                # Engine gamepad flag is the last gate; C++ validates KBM
                # ownership and samples the four physical stick axes itself.
                result = int(_native_radial_module.apply_cached_radial(
                    address, bool(pi.bUsingGamepad),
                    _runtime_radial_left, _runtime_radial_right,
                    _runtime_radial_left_inner, _runtime_radial_left_outer,
                    _runtime_radial_right_inner, _runtime_radial_right_outer,
                ))
            except Exception as exc:
                # No double-apply on an unknown partial native write outcome.
                _native_runtime_failed("frame", exc)
                return
            if result in (1, 2):  # 1: applied; 2: KBM-owned skip.
                return
            _native_radial_failed = True
            _warn_once("native_apply_failed", "C++ backend disabled for this session; "
                       f"status={result}")
            # The native path owns its own physical sample buffer. If a native
            # call fails, Python's previous samples may be stale; skip ONE
            # native-owned frame rather than applying an incorrect second pass.
            # Subsequent InputAxis events repopulate Python's known-good state.
            return
    # Original Python pipeline, independently selectable for parity checks.
    if not _fallback_controller_allowed(pi):
        return
    now = time.monotonic() if any(_raw) else 0.0
    lx = _raw_value(LEFT_X, now) if now and _runtime_radial_left else 0.0
    ly = _raw_value(LEFT_Y, now) if now and _runtime_radial_left else 0.0
    rx = _raw_value(RIGHT_X, now) if now and _runtime_radial_right else 0.0
    ry = _raw_value(RIGHT_Y, now) if now and _runtime_radial_right else 0.0

    if _runtime_radial_left:
        factor = _radial_multiplier(lx, ly, _runtime_radial_left_inner,
                                    _runtime_radial_left_outer)
        try:
            pi.aStrafe = float(pi.aStrafe) * factor
            pi.aForward = float(pi.aForward) * factor
            pi.aBaseY = float(pi.aBaseY) * factor
            pi.RawJoyRight = float(pi.RawJoyRight) * factor
            pi.RawJoyUp = float(pi.RawJoyUp) * factor
        except Exception as exc:
            _warn_once("radial_left_apply", f"radial left apply failed: {type(exc).__name__}: {exc}")
    if _runtime_radial_right:
        factor = _radial_multiplier(rx, ry, _runtime_radial_right_inner,
                                    _runtime_radial_right_outer)
        try:
            pi.aTurn = float(pi.aTurn) * factor
            pi.aLookUp = float(pi.aLookUp) * factor
            pi.RawJoyLookRight = float(pi.RawJoyLookRight) * factor
            pi.RawJoyLookUp = float(pi.RawJoyLookUp) * factor
        except Exception as exc:
            _warn_once("radial_right_apply", f"radial right apply failed: {type(exc).__name__}: {exc}")


def _key_event_kind(event: Any) -> str:
    return key_event_kind(event, _event_kinds)


def _input_key(obj: Any, args: Any, ret: Any, func: Any) -> None:
    if _native_key_hook_live:
        return  # Native InputKey owns this event while cleanup is pending.
    if not _enabled or not _runtime_protect:
        return
    try:
        event_kind = _key_event_kind(args.EventType)
    except Exception:
        event_kind = "unknown"
    released = event_kind == "released"
    repeated = event_kind == "repeat"
    key = None

    try:
        is_gamepad = bool(args.bGamepad)
    except Exception:
        try:
            key = str(args.Key)
            is_gamepad = key.startswith("XboxTypeS_")
        except Exception:
            return

    try:
        if key is None:
            key = str(args.Key)
    except Exception:
        key = ""

    if _native_hot_available():
        try:
            kind = NATIVE_EVENT_KINDS.get(event_kind, 4)
            state = int(_native_radial_module.key_event(key, is_gamepad, kind))
            _native_owner_changed(state)
            return
        except Exception as exc:
            _native_runtime_failed("key", exc)

    if not is_gamepad:
        if released:
            if key:
                _kbm_keys_down.discard(key)
            return
        if repeated:
            # Repeats signal genuine KBM activity, but must not create a new
            # sticky held key if the press/release pair was not observed.
            if key in _kbm_keys_down:
                _mark_kbm_activity()
            return
        if event_kind == "pressed" and key and "mouse" not in key.lower():
            # Mouse button releases are not guaranteed to reach this viewport
            # while switching input/modes. A click claims KBM briefly, not
            # forever. Normal keyboard keys remain protected until release.
            _kbm_keys_down.add(key)
        # Unknown EInputEvent representations are momentary too: never
        # silently latch a key which cannot be reliably released.
        _mark_kbm_activity()
        return

    # Repeats/releases do not represent a new device choice. Controller presses
    # may take ownership only when KBM is not held/recently active.
    if released or repeated:
        return
    now = time.monotonic()
    if _active_device != "controller" and _controller_can_wake(now):
        _set_active_device("controller")


def _raw_value(index: int, now: float) -> float:
    value = _raw[index]
    if value == 0.0:
        return 0.0
    stamp = _seen[index]
    if stamp and now - stamp <= STALE_SECONDS:
        return value
    _raw[index] = 0.0
    _seen[index] = 0.0
    return 0.0


def _learn_gain(index: int, raw: float, vanilla: float, allow_recheck: bool) -> None:
    if abs(raw) < CALIBRATION_MIN_RAW or abs(vanilla) < EPS_OUTPUT:
        return
    sample = vanilla / raw
    if not (-100000.0 < sample < 100000.0):
        return

    old = _gain[index]
    count = _gain_samples[index]
    if count < CALIBRATION_SAMPLES:
        _gain[index] = sample if old is None else (old * count + sample) / (count + 1)
        _gain_samples[index] = count + 1
        return

    if allow_recheck and old is not None:
        tolerance = max(abs(old) * GAIN_RECHECK_TOLERANCE, 1e-6)
        if abs(sample - old) > tolerance:
            # Sensitivity or another native multiplier changed. Adopt the new
            # scale with a small blend, but only on the bounded recheck cadence.
            _gain[index] = old * 0.5 + sample * 0.5


def _custom_value(raw: float, vanilla: float, gain_index: int, deadzone: float) -> float:
    if deadzone >= 1.0 or abs(raw) <= deadzone:
        return 0.0
    if abs(vanilla) >= EPS_OUTPUT:
        return vanilla
    gain = _gain[gain_index]
    return 0.0 if gain is None else raw * gain


def _fallback_controller_allowed(pi: Any) -> bool:
    """Never transform an input frame owned by keyboard/mouse.

    The previous protect_kbm=False shortcut accepted *all* frames, including
    genuine KBM movement/look, and could replace them with cached stick zeros.
    A saved preference is never permission to modify KBM axes.
    """
    if _active_device == "kbm" or _kbm_keys_down:
        return False
    # InputKey/InputAxis order can vary within a frame. Even when optional
    # KBM Priority is off, never edit native KBM fields immediately after a
    # mouse/key event and a coincident gamepad-axis event.
    if _last_kbm_activity > 0.0 and time.monotonic() - _last_kbm_activity < KBM_SAFETY_SECONDS:
        return False
    # Require BL2's native gamepad decision as well as our device tracking.
    # If these temporarily disagree after a switch, skip the fallback frame;
    # dropping a custom deadzone frame is safer than erasing real KBM input.
    try:
        return bool(pi.bUsingGamepad)
    except Exception:
        return False


def _apply_deadzones(obj: Any, args: Any, ret: Any, func: Any) -> None:
    global _next_gain_recheck
    if not _enabled:
        return
    fallback_left = _runtime_left_fallback
    fallback_right = _runtime_right_fallback
    protect = _runtime_protect
    if not fallback_left and not fallback_right:
        return
    if protect and _active_device == "kbm":
        return
    if _pi_address is None or obj._get_address() != _pi_address:
        pi = _refresh_local_input(obj)
        if pi is None or obj._get_address() != _pi_address:
            return
    else:
        pi = obj

    if not _fallback_controller_allowed(pi):
        return

    # Only take a clock reading if a cached axis is non-zero. Centered sticks
    # therefore avoid timing work entirely when the engine emits zero events.
    if ((fallback_left and (_raw[LEFT_X] or _raw[LEFT_Y])) or
            (fallback_right and (_raw[RIGHT_X] or _raw[RIGHT_Y]))):
        now = time.monotonic()
    else:
        now = 0.0

    allow_recheck = now >= _next_gain_recheck if now else False
    if allow_recheck:
        _next_gain_recheck = now + GAIN_RECHECK_SECONDS

    if fallback_right:
        rx = _raw_value(RIGHT_X, now) if now else 0.0
        ry = _raw_value(RIGHT_Y, now) if now else 0.0
        try:
            vanilla_turn = float(pi.aTurn)
            vanilla_lookup = float(pi.aLookUp)
        except Exception as exc:
            _warn_once("read_right", f"could not read right-stick input: {type(exc).__name__}: {exc}")
        else:
            _learn_gain(GAIN_RIGHT_X, rx, vanilla_turn, allow_recheck)
            _learn_gain(GAIN_RIGHT_Y, ry, vanilla_lookup, allow_recheck)
            try:
                turn = _custom_value(rx, vanilla_turn, GAIN_RIGHT_X, _runtime_rx_dz)
                lookup = _custom_value(ry, vanilla_lookup, GAIN_RIGHT_Y, _runtime_ry_dz)
                if turn != vanilla_turn:
                    pi.aTurn = turn
                if lookup != vanilla_lookup:
                    pi.aLookUp = lookup
            except Exception as exc:
                _warn_once("write_right", f"right-stick apply failed: {type(exc).__name__}: {exc}")

    if fallback_left:
        lx = _raw_value(LEFT_X, now) if now else 0.0
        ly = _raw_value(LEFT_Y, now) if now else 0.0
        try:
            vanilla_strafe = float(pi.aStrafe)
            vanilla_basey = float(pi.aBaseY)
            vanilla_forward = float(pi.aForward)
        except Exception as exc:
            _warn_once("read_left", f"could not read left-stick input: {type(exc).__name__}: {exc}")
        else:
            _learn_gain(GAIN_LEFT_X, lx, vanilla_strafe, allow_recheck)
            _learn_gain(GAIN_LEFT_BASEY, ly, vanilla_basey, allow_recheck)
            _learn_gain(GAIN_LEFT_FORWARD, ly, vanilla_forward, allow_recheck)
            try:
                strafe = _custom_value(lx, vanilla_strafe, GAIN_LEFT_X, _runtime_lx_dz)
                basey = _custom_value(ly, vanilla_basey, GAIN_LEFT_BASEY, _runtime_ly_dz)
                forward = _custom_value(ly, vanilla_forward, GAIN_LEFT_FORWARD, _runtime_ly_dz)
                if strafe != vanilla_strafe:
                    pi.aStrafe = strafe
                if basey != vanilla_basey:
                    pi.aBaseY = basey
                if forward != vanilla_forward:
                    pi.aForward = forward
            except Exception as exc:
                _warn_once("write_left", f"left-stick apply failed: {type(exc).__name__}: {exc}")


def _failed_rebuild_is_current(pi: Any | None = None) -> bool:
    """A blocked weak identity never becomes trusted merely through a settings tick."""
    global _failed_rebuild_pi
    if _failed_rebuild_pi is None:
        return False
    blocked = _failed_rebuild_pi()
    if blocked is None:
        _failed_rebuild_pi = None
        return False
    if pi is None:
        pi = _pi_ref() if _pi_ref is not None else None
    if pi is None:
        return True
    if _object_address(pi) == _object_address(blocked):
        return True
    _failed_rebuild_pi = None  # Verified different PlayerInput lifecycle.
    return False


def notify_input_rebuild_failed(pc: Any | None = None, pi: Any | None = None) -> bool:
    """Disarm input processing after an ambiguous external SetupInputDevices failure.

    Do not restore, reset ownership ledgers, or reconcile potentially partial
    engine input data. A new PlayerInput identity is required before rearming.
    """
    global _failed_rebuild_pi
    global _runtime_left_fallback, _runtime_right_fallback, _runtime_protect
    global _runtime_radial_left, _runtime_radial_right
    if not _enabled:
        return False
    try:
        local = get_pc(possibly_loading=True)
        current = local.PlayerInput if local is not None else None
        if (_object_address(current) is None
                or _object_address(current) != _object_address(pi)
                or (pc is not None and _object_address(pc) != _object_address(local))):
            return False  # Never disarm the local player for a remote notification.
        _failed_rebuild_pi = WeakPointer(current)
        _native_lifecycle_begin()
        _runtime_left_fallback = _runtime_right_fallback = _runtime_protect = False
        _runtime_radial_left = _runtime_radial_right = False
        _stop_native_pre_hook()
        _stop_native_key_hook()
        _stop_native_axis_hook()
        _stop_native_lifecycle()
        _sync_runtime_hooks(False, False, False)
        _cancel_input_recovery()
        _warn_once("external_rebuild_partial",
                   "native input rebuild failed; deadzone processing suspended "
                   "for this PlayerInput until replacement or game restart")
        return True
    except Exception as exc:
        _warn_once("external_rebuild_suspend_failed",
                   f"failed to suspend partial input rebuild: {type(exc).__name__}: {exc}")
        return False


def notify_input_rebuilt(pc: Any | None = None, pi: Any | None = None) -> bool:
    """Compatibility API for mods which rebuild input under hook suppression."""
    if not _enabled:
        return False
    try:
        if pi is None and pc is not None:
            candidate = getattr(pc, "PlayerInput", None)
            if _object_address(candidate) is not None:
                pi = candidate
        pi_address = _object_address(pi)
        if pi is not None and pi_address is None:
            pi = None
        current = _refresh_local_input(pi) if pi is not None else _refresh_local_input()
        current_address = _object_address(current)
        if current is None or current_address is None:
            return False
        if pi is not None and pi_address != current_address:
            return False
        if _failed_rebuild_is_current(current):
            return False  # A failed rebuild's POST callback is not a success proof.

        _native_lifecycle_begin()
        # SetupInputDevices has already replaced per-PlayerInput binding rows, so
        # the old left-binding ownership record no longer describes the live table.
        # Relinquish it without writing stale originals back into the fresh rebuild.
        if not _speed_guard_still_current(current):
            _restore_speed_guard(clear_block=True)
        _restore_linked_left()
        _clear_linked_left_state(clear_block=True)
        # Customizer can notify multiple times for one unchanged binding table.
        # Keep a verified physical right deadzone in place rather than briefly
        # restoring vanilla and rewriting it with every notification.
        if not _linked_right_still_current(current):
            _restore_linked_right()
            _clear_linked_right_state(clear_block=True)
        _clear_native_left_state(clear_block=True)
        # Controller Customizer may perform SetupInputDevices with direct hook
        # dispatch suppressed. Treat its explicit bridge as the same lifecycle
        # boundary as the native hooks, including right-stick quarantine retry.
        _restore_native_right()
        _clear_native_right_state(clear_block=True)
        _reset_native_radial()
        _reset_fallback_state(clear_device=False)
        _reconcile_runtime()
        return True
    except Exception as exc:
        _warn_once("notify_rebuild", f"input rebuild reconciliation failed: {type(exc).__name__}: {exc}")
        return False


def _native_rebuild(obj: Any, args: Any, ret: Any, func: Any) -> None:
    if not _enabled:
        return
    # On WillowPlayerInput:SetupInputDevices, obj already *is* the PlayerInput.
    # Asking that object for .PlayerInput resolves the Unreal function of the same
    # name and returns a BoundFunction, which v1.2.0 then mistook for a UObject.
    pi = None
    try:
        class_name = str(obj.Class.Name)
    except Exception:
        class_name = ""
    if "PlayerInput" in class_name and _object_address(obj) is not None:
        pi = obj
    else:
        candidate = getattr(obj, "PlayerInput", None)
        if _object_address(candidate) is not None:
            pi = candidate
    notify_input_rebuilt(pc=obj if pi is not obj else None, pi=pi)


def _lifecycle_refresh(obj: Any, args: Any, ret: Any, func: Any) -> None:
    global _native_right_block_reason
    if not _enabled:
        return
    _native_lifecycle_begin()
    before = _pi_address
    current = _refresh_local_input()
    changed = _pi_address != before
    # A real input lifecycle boundary is the retry point for backends previously
    # quarantined after an external edit or unsafe access. Settings ticks alone
    # never clear quarantine.
    if not changed:
        if _linked_left_block is not None and _linked_left_block[0] == _pi_address:
            _clear_linked_left_state(clear_block=True)
        if _native_left_block is not None and _native_left_block[0] == _pi_address:
            _clear_native_left_state(clear_block=True)
        if _native_right_block_reason is not None:
            _restore_native_right()
            _clear_native_right_state(clear_block=True)
    if current is not None and not changed:
        # Re-evaluate controller Speed ownership on in-place context rebuilds;
        # source rows may be replaced without changing PlayerInput identity.
        if not _speed_guard_still_current(current):
            _restore_speed_guard(clear_block=True)
        # Context changes can rebuild handler/binding arrays in-place on the same
        # PlayerInput UObject. Revalidate cached ownership before using it again.
        if _linked_left_supported is True and not _linked_left_mapping_current(current):
            _restore_linked_left()
            _clear_linked_left_state(clear_block=True)
        if _linked_left_supported is not True and _native_left_supported is True:
            _restore_native_left()
            _clear_native_left_state(clear_block=True)
        if _linked_right_block is not None and _linked_right_block[0] == _pi_address:
            _clear_linked_right_state(clear_block=True)
        elif _linked_right_supported is True and not _linked_right_mapping_current(current):
            _restore_linked_right()
            _clear_linked_right_state(clear_block=True)
    if _native_right_supported is True and not _native_right_state_valid():
        _restore_native_right()
        _clear_native_right_state(clear_block=True)
    # _cache_player_input already clears device state when identity changes. A
    # same-object context refresh only invalidates fallback samples/gains.
    _reset_native_radial()
    _reset_fallback_state(clear_device=changed)
    _reconcile_runtime()


def _lifecycle_departure(obj: Any, args: Any, ret: Any, func: Any) -> None:
    """Retire local input before travel can replace its bindings or objects."""
    global _runtime_left_fallback, _runtime_right_fallback, _runtime_protect
    global _runtime_radial_left, _runtime_radial_right
    if not _enabled:
        return
    try:
        local = get_pc(possibly_loading=True)
        if local is None or _object_address(obj) != _object_address(local):
            return
    except Exception:
        return
    # PlayerInput setup observers alone do not cover PreClientTravel. This rare
    # controller PRE bridge disarms first and restores while weak identities
    # still describe the departing table. Reconciliation runs on later local
    # ticks or setup POST, never within the departure callback.
    _native_lifecycle_begin()
    _cache_player_input(None)
    _runtime_left_fallback = _runtime_right_fallback = _runtime_protect = False
    _runtime_radial_left = _runtime_radial_right = False
    _sync_runtime_hooks(False, False, False)
    _cancel_input_recovery()
    _schedule_input_recovery()


def _install_lifecycle_hooks() -> None:
    try:
        departure = unrealsdk.find_object("Function", PRE_CLIENT_TRAVEL)
    except Exception:
        departure = None
    if departure is not None:
        _add_hook(PRE_CLIENT_TRAVEL, Type.PRE,
                  "ControllerDeadzoneControl:Departure", _lifecycle_departure)
    for path in SETUP_INPUT_PATHS:
        try:
            fn = unrealsdk.find_object("Function", path)
        except Exception:
            continue
        if fn is not None:
            _add_hook(path, Type.POST, f"ControllerDeadzoneControl:Rebuild:{path}", _native_rebuild)
    for path in (
        "WillowGame.WillowPlayerInput:InitInputSystem",
        "WillowGame.WillowPlayerInput:PostChangeInputContext",
    ):
        try:
            fn = unrealsdk.find_object("Function", path)
        except Exception:
            continue
        if fn is not None:
            _add_hook(path, Type.POST, f"ControllerDeadzoneControl:Lifecycle:{path}", _lifecycle_refresh)


def _on_enable() -> None:
    global _enabled, _settings_cache
    global _native_left_supported, _native_right_supported, _linked_left_supported
    global _linked_right_supported, _linked_right_block
    global _linked_left_block, _native_left_block, _native_right_block_reason
    global _runtime_left_fallback, _runtime_right_fallback, _runtime_protect
    global _runtime_kbm_priority, _runtime_radial_left, _runtime_radial_right
    global _native_radial_failed, _native_radial_attempted
    global _native_field_io_disabled
    global _field_cleanup_deadline
    _native_field_io_disabled = False
    global _runtime_hook_issue
    _runtime_hook_issue = None
    global _native_discovery_partial_since
    _native_discovery_partial_since = None
    _native_radial_failed = False
    _native_radial_attempted = False
    _stop_native_lifecycle()
    _stop_native_pre_hook()
    _stop_native_key_hook()
    _stop_native_axis_hook()
    _remove_all_hooks()
    _field_cleanup_deadline = 0.0
    _schedule_field_cleanup()
    _enabled = True
    _cancel_input_recovery()
    _settings_cache = None
    _error_once.clear()
    _runtime_left_fallback = False
    _runtime_right_fallback = False
    _runtime_radial_left = False
    _runtime_radial_right = False
    _runtime_protect = False
    _runtime_kbm_priority = True
    _restore_speed_guard(clear_block=True)
    _linked_left_block = None
    _linked_right_block = None
    _native_left_block = None
    _native_right_block_reason = None
    _reset_fallback_state(clear_device=True)
    if _native_requested():
        _load_native_radial()

    # Re-evaluate native support each enable; game/mod versions may have changed.
    _native_left_supported = None
    _native_right_supported = None
    _linked_left_supported = None
    _linked_right_supported = None
    _cache_player_input(None)
    _refresh_local_input()
    _install_lifecycle_hooks()
    _reconcile_runtime()

    logging.info("Controller Deadzone Control: enabled v1.7.2-beta.3 (cached native event metadata, verified recovery, Python fallback).")


def _on_disable() -> None:
    global _enabled, _runtime_left_fallback, _runtime_right_fallback, _runtime_protect
    global _runtime_hook_issue
    _runtime_hook_issue = None
    global _runtime_radial_left, _runtime_radial_right, _menu_refresh_pending
    global _field_cleanup_deadline
    _enabled = False
    _cancel_input_recovery()
    _runtime_left_fallback = False
    _runtime_right_fallback = False
    _runtime_radial_left = False
    _runtime_radial_right = False
    _runtime_protect = False
    _menu_refresh_pending = False
    _stop_native_lifecycle()
    _stop_native_pre_hook()
    _stop_native_key_hook()
    _stop_native_axis_hook()
    _remove_all_hooks()
    _field_cleanup_deadline = 0.0
    _restore_speed_guard(clear_block=True)
    _restore_linked_left()
    _restore_linked_right()
    _restore_native_left()
    _restore_native_right()
    _retry_field_restores()
    _schedule_field_cleanup()
    _reset_native_radial()  # Invalidate native ledger AFTER verified restoration.
    _cache_player_input(None, restore_retired=False)
    _reset_fallback_state(clear_device=True)
    logging.info("Controller Deadzone Control: disabled.")


# Keep exactly one flat settings record for every value. These are the original
# saved keys plus the new settings. Nested menus below are presentation-only:
# returning the same option objects from iter_display_options does NOT move
# the keys in JSON, so v1.4.6 configurations migrate with no file rewrite.
_stored_options = (
    left_enabled, left_x, left_y, right_enabled, right_x, right_y,
    protect_kbm, controller_wake,
    axial_mode, radial_mode, radial_left_enabled, radial_right_enabled,
    radial_left_inner, radial_left_outer, radial_right_inner, radial_right_outer,
    use_native_radial, tps_experimental_native_radial,
)
for _option in _stored_options:
    if _option is use_native_radial or _option is tps_experimental_native_radial:
        _option.on_change_anytime = _native_radial_changed
    elif _option is axial_mode or _option is radial_mode:
        _option.on_change_anytime = _mode_changed
    else:
        _option.on_change_anytime = _settings_changed

del _option

from mods_base import SETTINGS_DIR


def migrate_settings(settings_dir):
    root = Path(settings_dir)
    previous = (root / "3ControllerDeadzoneControl.json", root / "ControllerDeadzoneControl.json")
    destination = root / "ControllerDeadzone.json"
    if destination.exists():
        return
    for old in previous:
        if not old.is_file():
            continue
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix=".ControllerDeadzone.", suffix=".tmp", delete=False) as target:
                temporary = Path(target.name)
                target.write(old.read_bytes())
                target.flush()
                os.fsync(target.fileno())
            if not destination.exists():
                os.replace(temporary, destination)
                temporary = None
            return
        except OSError:
            return
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

migrate_settings(SETTINGS_DIR)
mod = build_mod(
    settings_file=SETTINGS_DIR / "ControllerDeadzone.json",
    # mods_base's default parser accepts numeric versions only. Keep the PEP
    # 440 metadata compatible with earlier RCs and the numeric version API.
    version_info_parser=lambda version: tuple(int(part) for part in version.split("-", 1)[0].partition("rc")[0].split(".")),
    options=list(_stored_options),
    on_enable=_on_enable,
    on_disable=_on_disable,
)

# Corrupted or externally edited settings can contain both modes enabled.
# Avoid simultaneously active styles before the first menu opens.
if axial_mode.value and radial_mode.value:
    radial_mode.value = False


def _display_options():
    # Native Willow scrolling list spacing is shared across other menus.
    # GroupedOption adds non-interactive titled separators without changing the
    # game's global row height, controller navigation, or stored option IDs.
    # Each mode's own toggle stays directly above its settings link.
    return iter((
        GroupedOption("Axial Deadzone", [
            axial_mode,
            NestedOption("axial_settings", display_name="Axial Settings", children=[
                GroupedOption("Left Stick", [left_enabled, left_x, left_y]),
                GroupedOption("Right Stick", [right_enabled, right_x, right_y]),
            ]),
        ]),
        GroupedOption("Radial Deadzone", [
            radial_mode,
            NestedOption("radial_settings", display_name="Radial Settings", children=[
                GroupedOption("Left Stick", [radial_left_enabled, radial_left_inner, radial_left_outer]),
                GroupedOption("Right Stick", [radial_right_enabled, radial_right_inner, radial_right_outer]),
            ]),
        ]),
        GroupedOption("General", [
            NestedOption("advanced_settings", display_name="Advanced Settings", children=[
                protect_kbm, controller_wake, use_native_radial, tps_experimental_native_radial,
            ]),
            ButtonOption("reset_defaults", display_name="Reset to Defaults",
                         description="Restore all deadzone and advanced options to their defaults.",
                         on_press=_reset_to_defaults),
        ]),
    ))

mod.iter_display_options = _display_options
