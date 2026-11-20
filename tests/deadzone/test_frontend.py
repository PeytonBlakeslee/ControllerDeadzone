"""Regression checks against the real frontend using a small SDK boundary stub."""
import importlib.util
import math
from pathlib import Path
import sys
import tempfile
import tomllib
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

ROOT = Path(__file__).resolve().parents[2]


class Option:
    def __init__(self, *args, **kwargs):
        self.__dict__.update(kwargs)
        self.identifier = kwargs.get("identifier", args[0] if args else "")
        self.value = kwargs.get("value")
        self.default_value = self.value


class Object:
    def __init__(self, address, **kwargs):
        self.address = address
        self.__dict__.update(kwargs)

    def _get_address(self):
        return self.address


def load_frontend(game="BL2", saved_native=None, settings_dir=None, saved_tps_native=None):
    env = {"pc": None, "hooks": {}, "warnings": []}
    sdk = ModuleType("unrealsdk")
    hooks = ModuleType("unrealsdk.hooks")
    hooks.Type = SimpleNamespace(PRE=0, POST=1, POST_UNCONDITIONAL=2)
    hooks.add_hook = lambda p, k, i, cb: env["hooks"].__setitem__((p, k, i), cb)
    hooks.remove_hook = lambda p, k, i: env["hooks"].pop((p, k, i), None)
    hooks.has_hook = lambda p, k, i: (p, k, i) in env["hooks"]
    sdk.hooks = hooks
    sdk.logging = SimpleNamespace(info=lambda *a: None, warning=env["warnings"].append)
    sdk.find_object = lambda *args: None
    unreal = ModuleType("unrealsdk.unreal")
    unreal.WeakPointer = weakref.ref
    base = ModuleType("mods_base")
    for name in ("BoolOption", "ButtonOption", "GroupedOption", "NestedOption", "SliderOption"):
        setattr(base, name, Option)
    base.get_pc = lambda **kwargs: env["pc"]
    base.SETTINGS_DIR = settings_dir or ROOT / "build" / "test_settings"
    base.EInputEvent = SimpleNamespace(IE_Pressed=0, IE_Released=1, IE_Repeat=2, IE_DoubleClick=3)
    base.Game = SimpleNamespace(TPS="TPS", get_current=lambda: game)
    def build_mod(**kwargs):
        # Match the installed mods_base factory's metadata injection contract.
        if saved_native is not None:
            next(option for option in kwargs["options"] if option.identifier == "use_native_radial").value = saved_native
        if saved_tps_native is not None:
            next(option for option in kwargs["options"] if option.identifier == "tps_experimental_native_radial").value = saved_tps_native
        version = tomllib.loads((ROOT / "mod/ControllerDeadzone/pyproject.toml").read_text())["project"]["version"]
        parser = kwargs.get("version_info_parser", lambda v: tuple(int(x) for x in v.split(".")))
        return SimpleNamespace(save_settings=Mock(), version_info=parser(version), **kwargs)
    base.build_mod = build_mod
    replacements = {"unrealsdk": sdk, "unrealsdk.hooks": hooks,
                    "unrealsdk.unreal": unreal, "mods_base": base}
    mod_dir = ROOT / "mod/ControllerDeadzone"
    spec = importlib.util.spec_from_file_location(
        "deadzone_test_mod", mod_dir / "__init__.py",
        submodule_search_locations=[str(mod_dir)])
    m = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**replacements, "deadzone_test_mod": m}):
        spec.loader.exec_module(m)
    return m, env


def mapping(m, count=4, address=100):
    names = tuple(m.AXIS_INDEX)
    device = Object(address, Name=m.CONTROLLER_SOURCE_NAME,
                    Class=SimpleNamespace(Name="InputDeviceDefinition"),
                    Axes=[SimpleNamespace(AxisName=n, DeadZone=.2, Speed=1.) for n in names])
    logical = ("aStrafe", "aForward", "aTurn", "aLookUp")
    pi = Object(200, Axes=[SimpleNamespace(AxisName=logical[i], InputDeviceHandler=
        SimpleNamespace(SourceDevice=device, SourceIndex=i)) for i in range(count)],
        Bindings=[], bUsingGamepad=True)
    return device, pi


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.m, self.env = load_frontend()

    def test_stable_metadata_imports_with_mods_base_numeric_version_api(self):
        self.assertEqual(self.m.mod.version_info, (1, 8))

    def test_native_notification_uses_supplied_guard_once_without_query(self):
        m = self.m
        device, pi = mapping(m)
        m._enabled = True
        m._pi_ref = weakref.ref(pi)
        m._pi_address = pi.address
        m.use_native_radial.value = True
        m._runtime_radial_left = m._runtime_radial_right = True
        m._runtime_kbm_priority = True
        m._runtime_radial_left_inner = m._runtime_radial_right_inner = .1
        query = Mock(side_effect=AssertionError('notification must not query guard'))
        m._native_radial_module = SimpleNamespace(guard_targets=query)
        apply = Mock()
        m._apply_speed_guard = apply
        m._native_axis_signal(1, True, False, True)
        self.assertEqual(m.active_device, 'kbm')
        query.assert_not_called()
        apply.assert_called_once()
        self.assertTrue(apply.call_args.kwargs['kbm_radial_left'])
        self.assertFalse(apply.call_args.kwargs['kbm_radial_right'])
        m._native_axis_signal(1, True, False, False)
        self.assertEqual(apply.call_count, 1)
        m._native_axis_signal(2, False, False, True)
        self.assertEqual(m.active_device, 'controller')
        self.assertEqual(apply.call_count, 2)
        query.assert_not_called()
        query.side_effect = None
        query.return_value = (True, True)
        m._sync_radial_kbm_guard()
        query.assert_called_once()

    def test_native_guard_notification_keeps_endpoint_and_disabled_safety(self):
        m = self.m
        device, pi = mapping(m)
        m._enabled = True
        m._pi_ref = weakref.ref(pi)
        m.use_native_radial.value = True
        m._runtime_radial_left = m._runtime_radial_right = True
        m._runtime_kbm_priority = True
        m._runtime_radial_left_inner = 1.
        m._runtime_radial_right_inner = .1
        query = Mock(side_effect=AssertionError('unexpected query'))
        m._native_radial_module = SimpleNamespace(guard_targets=query)
        apply = Mock()
        m._apply_speed_guard = apply
        m._native_axis_signal(1, True, False, True)
        self.assertFalse(apply.call_args.kwargs['kbm_radial_left'])
        self.assertEqual(m._kbm_guard_last_requested[0], (True, False))
        m._enabled = False
        m._native_axis_signal(2, False, False, True)
        self.assertEqual(m.active_device, 'kbm')
        self.assertEqual(apply.call_count, 1)
        query.assert_not_called()

    def test_unverified_and_ambiguous_left_sources_are_rejected(self):
        m = self.m
        device, pi = mapping(m)
        rogue, rogue_pi = mapping(m, address=101)
        rogue.Name = "MouseDevice"
        pi.Axes.insert(0, rogue_pi.Axes[0])
        self.assertIs(m._linked_axis_candidates(pi)["XboxTypeS_LeftX"][0], device)
        other, other_pi = mapping(m, address=102)
        pi.Axes.append(other_pi.Axes[0])
        self.assertNotIn("XboxTypeS_LeftX", m._linked_axis_candidates(pi))

    def test_duplicate_same_source_different_logical_axis_is_rejected(self):
        m = self.m
        device, pi = mapping(m)
        pi.Axes.append(SimpleNamespace(AxisName="aTurn", InputDeviceHandler=pi.Axes[0].InputDeviceHandler))
        self.assertNotIn("XboxTypeS_LeftX", m._linked_axis_candidates(pi))

    def test_partial_mapping_recovers_without_another_lifecycle_event(self):
        m = self.m
        m._enabled = True
        m.axial_mode.value = False
        m.radial_mode.value = True
        device, pi = mapping(m, count=0)
        pc = Object(300, PlayerInput=pi)
        self.env["pc"] = pc
        m._discover_native_left = lambda pi: False
        m._discover_native_right = lambda: False
        with patch.object(m.time, "monotonic", return_value=10.):
            m._reconcile_runtime()
        self.assertFalse(m._runtime_radial_left)
        self.assertFalse(m._runtime_radial_right)
        self.assertGreater(m._recovery_deadline, 0)
        self.assertEqual(self.env["warnings"], [])
        logical = ("aStrafe", "aForward", "aTurn", "aLookUp")
        pi.Axes[:] = [SimpleNamespace(AxisName=logical[i], InputDeviceHandler=
            SimpleNamespace(SourceDevice=device, SourceIndex=i)) for i in range(4)]
        with patch.object(m.time, "monotonic", return_value=10.3):
            m._input_recovery_tick(pc, None, None, None)
        self.assertTrue(m._runtime_radial_left and m._runtime_radial_right)
        self.assertEqual(m._recovery_deadline, 0)
        self.assertFalse(any(key[2].startswith("ControllerDeadzoneControl:InputRecovery/") for key in self.env["hooks"]))

    def test_recovery_has_fixed_deadline_and_ignores_remote_controller(self):
        m = self.m
        m._enabled = True
        pc = Object(1)
        self.env["pc"] = pc
        with patch.object(m.time, "monotonic", return_value=1.):
            m._schedule_input_recovery()
        with patch.object(m.time, "monotonic", return_value=2.), patch.object(m, "_reconcile_runtime") as reconcile:
            m._schedule_input_recovery()
            m._input_recovery_tick(Object(2), None, None, None)
            reconcile.assert_not_called()
        self.assertEqual(m._recovery_deadline, 16.)
        with patch.object(m.time, "monotonic", return_value=16.):
            m._input_recovery_tick(pc, None, None, None)
        self.assertEqual(m._recovery_deadline, 0)
        self.assertEqual(len(self.env["warnings"]), 1)

    def test_local_departure_restores_and_disarms_before_recovery(self):
        m = self.m
        device, pi = mapping(m)
        pc = Object(300, PlayerInput=pi)
        self.env["pc"] = pc
        m._enabled = True
        m.radial_mode.value = True
        m.axial_mode.value = False
        with patch.object(m.time, "monotonic", return_value=10.):
            m._reconcile_runtime()
            self.assertTrue(m._runtime_radial_left and m._runtime_radial_right)
            self.assertEqual(device.Axes[0].DeadZone, 0.)
            m._lifecycle_departure(Object(301), None, None, None)
            self.assertTrue(m._runtime_radial_left)
            with patch.object(m, "_native_lifecycle_begin") as suspend:
                m._lifecycle_departure(pc, None, None, None)
                suspend.assert_called_once()
        self.assertIsNone(m._pi_address)
        self.assertFalse(m._runtime_radial_left or m._runtime_radial_right)
        self.assertFalse(m._runtime_left_fallback or m._runtime_right_fallback)
        self.assertTrue(all(row.DeadZone == .2 and row.Speed == 1. for row in device.Axes))
        self.assertFalse(any(any(key[2].startswith(name + "/") for name in (
                                      "ControllerDeadzoneControl:Axis",
                                      "ControllerDeadzoneControl:Radial",
                                      "ControllerDeadzoneControl:PlayerInput"))
                             for key in self.env["hooks"]))
        self.assertGreater(m._recovery_deadline, 0.)
        replacement_device, replacement = mapping(m, address=101)
        replacement.address = 201
        self.env["pc"] = Object(302, PlayerInput=replacement)
        with patch.object(m.time, "monotonic", return_value=10.3):
            m._input_recovery_tick(self.env["pc"], None, None, None)
        self.assertEqual(m._pi_address, 201)
        self.assertTrue(m._runtime_radial_left and m._runtime_radial_right)
        self.assertEqual(m._recovery_deadline, 0.)
        self.assertTrue(all(row.DeadZone == .2 for row in device.Axes))

    def test_radial_hook_failure_reconciles_owned_speed_without_foreign_overwrite(self):
        # A failed required radial hook must release an existing zeroed Speed
        # field, but never overwrite a different value set by another writer.
        for current, expected in ((0.0, 1.0), (0.7, 0.7)):
            with self.subTest(current=current):
                m, _env = load_frontend()
                device, pi = mapping(m)
                m._enabled = True
                m._pi_ref = weakref.ref(pi)
                m._pi_address = pi.address
                m._speed_owner_pi = pi.address
                physical = "XboxTypeS_LeftX"
                device.Axes[0].Speed = current
                m._speed_targets[physical] = (weakref.ref(device), 0, device.address, 1.0)
                m._speed_applied.add(physical)

                settings = [False] * 14
                settings[8] = True    # request left radial
                settings[10] = 0.1    # not a 100% deadzone; Speed should restore
                hook_registration = Mock(side_effect=lambda hot, key, radial: not radial)
                with (patch.object(m, "_apply_native",
                                   return_value=(True, False, "native-linked", "off", True)),
                      patch.object(m, "_fallback_needs", return_value=(False, False, False)),
                      patch.object(m, "_sync_runtime_hooks", hook_registration),
                      patch.object(m, "_apply_native_left", return_value=(True, "off")),
                      patch.object(m, "_native_requested", return_value=False),
                      patch.object(m, "_set_active_device")):
                    m._reconcile_runtime(tuple(settings))

                self.assertEqual(hook_registration.call_count, 2)
                self.assertFalse(m._runtime_radial_left)
                self.assertEqual(device.Axes[0].Speed, expected)
                self.assertNotIn(physical, m._speed_targets)
                self.assertNotIn(physical, m._speed_applied)

    def test_required_fallback_hook_failures_suspend_all_dependent_processing(self):
        cases = (
            # axial, endpoint, radial Python, radial with native axis/pre but
            # Python InputKey fallback: each required hook is independently fatal.
            ("axial-player", False, False, "PlayerInput", False),
            ("axial-key", False, False, "InputKey", False),
            ("endpoint-player", False, True, "PlayerInput", False),
            ("endpoint-key", False, True, "InputKey", False),
            ("radial-python-key", True, False, "InputKey", False),
            ("radial-native-key", True, False, "InputKey", True),
            ("radial-python-axis", True, False, "Axis", False),
        )
        for name, radial, endpoint, blocked, native in cases:
            with self.subTest(mode=name):
                m, env = load_frontend()
                device, pi = mapping(m)
                env["pc"] = Object(300, PlayerInput=pi)
                m._pi_ref = weakref.ref(pi)
                m._pi_address = pi.address
                m._enabled = True
                settings = [False] * 14
                settings[8 if radial else 0] = True
                settings[3] = 1.0 if endpoint else 0.2
                settings[4] = 0.2
                settings[10] = 0.1
                settings[11] = 1.0
                # Simulate an already-owned zero Speed for endpoint guard.
                if endpoint:
                    device.Axes[0].Speed = 0.0
                    key = "XboxTypeS_LeftX"
                    m._speed_owner_pi = pi.address
                    m._speed_targets[key] = (weakref.ref(device), 0, device.address, 1.0)
                    m._speed_applied.add(key)
                target_ids = {
                    "PlayerInput": "ControllerDeadzoneControl:PlayerInput",
                    "InputKey": "ControllerDeadzoneControl:InputKey",
                    "Axis": "ControllerDeadzoneControl:Axis",
                }
                sdk_hooks = m.unrealsdk.hooks
                add_hook = sdk_hooks.add_hook
                def refuse(path, kind, identifier, callback):
                    if identifier.startswith(target_ids[blocked] + "/"):
                        return False
                    return add_hook(path, kind, identifier, callback)
                sdk_hooks.add_hook = refuse
                with (patch.object(m, "_apply_native", return_value=(
                          not not radial, True,
                          "native-linked" if radial else ("100%-guard" if endpoint else "fallback"),
                          "off", True)),
                      patch.object(m, "_apply_native_left", return_value=(True, "off")) as release,
                      patch.object(m, "_native_requested", return_value=native),
                      patch.object(m, "_try_native_axis_hook", return_value=native),
                      patch.object(m, "_try_native_key_hook", return_value=False),
                      patch.object(m, "_try_native_pre_hook", return_value=native)):
                    m._reconcile_runtime(tuple(settings))
                self.assertIn(blocked if blocked != "Axis" else "InputAxis",
                              m._runtime_hook_issue or "")
                self.assertFalse(m._runtime_left_fallback)
                self.assertFalse(m._runtime_right_fallback)
                self.assertFalse(m._runtime_radial_left)
                self.assertFalse(m._runtime_radial_right)
                self.assertFalse(m._runtime_protect)
                release.assert_called_once()
                self.assertFalse(any(any(key[2].startswith(name + "/") for name in (
                    "ControllerDeadzoneControl:Axis",
                    "ControllerDeadzoneControl:InputKey",
                    "ControllerDeadzoneControl:PlayerInput",
                    "ControllerDeadzoneControl:Radial"))
                    for key in env["hooks"]))
                if endpoint:
                    self.assertEqual(device.Axes[0].Speed, 1.0)
                    self.assertNotIn("XboxTypeS_LeftX", m._speed_applied)
                self.assertTrue(env["warnings"])

    def test_foreign_fixed_id_coexists_with_randomized_input_hook(self):
        m = self.m
        device, pi = mapping(m)
        self.env["pc"] = Object(300, PlayerInput=pi)
        m._pi_ref = weakref.ref(pi)
        m._pi_address = pi.address
        m._enabled = True
        settings = [False] * 14
        settings[0], settings[3], settings[4] = True, 0.2, 0.2
        foreign_key = (m.VIEWPORT_KEY, m.Type.PRE,
                       "ControllerDeadzoneControl:InputKey")
        foreign_callback = object()
        self.env["hooks"][foreign_key] = foreign_callback
        with (patch.object(m, "_apply_native", return_value=(False, True, "fallback", "off", True)),
              patch.object(m, "_apply_native_left", return_value=(True, "off")),
              patch.object(m, "_native_requested", return_value=False)):
            m._reconcile_runtime(tuple(settings))
        self.assertIsNone(m._runtime_hook_issue)
        self.assertIs(self.env["hooks"][foreign_key], foreign_callback)
        self.assertNotIn(foreign_key[2], m._hook_ids.values())
        self.assertTrue(m._runtime_left_fallback)
        m._remove_all_hooks()
        self.assertIs(self.env["hooks"][foreign_key], foreign_callback)

    def test_required_fallback_hooks_recover_when_registration_recovers(self):
        m = self.m
        device, pi = mapping(m)
        self.env["pc"] = Object(300, PlayerInput=pi)
        m._pi_ref = weakref.ref(pi)
        m._pi_address = pi.address
        m._enabled = True
        settings = [False] * 14
        settings[0], settings[3], settings[4] = True, 0.2, 0.2
        hook_add = m.unrealsdk.hooks.add_hook
        m.unrealsdk.hooks.add_hook = lambda p,k,i,cb: (
            False if i.startswith("ControllerDeadzoneControl:PlayerInput/")
            else hook_add(p,k,i,cb))
        with (patch.object(m, "_apply_native", return_value=(False, True, "fallback", "off", True)),
              patch.object(m, "_apply_native_left", return_value=(True, "off")),
              patch.object(m, "_native_requested", return_value=False)):
            m._reconcile_runtime(tuple(settings))
            self.assertIn("PlayerInput", m._runtime_hook_issue or "")
            m.unrealsdk.hooks.add_hook = hook_add
            m._reconcile_runtime(tuple(settings))
        self.assertIsNone(m._runtime_hook_issue)
        self.assertTrue(m._runtime_left_fallback)
        self.assertTrue(m._runtime_protect)
        self.assertTrue(any(k[2].startswith("ControllerDeadzoneControl:PlayerInput/")
                            for k in self.env["hooks"]))

    def test_failed_external_rebuild_disarms_without_modifying_input_fields(self):
        m = self.m
        device, pi = mapping(m)
        pc = Object(300, PlayerInput=pi)
        self.env["pc"] = pc
        m._enabled = True
        m._pi_ref = weakref.ref(pi)
        m._pi_address = pi.address
        m._runtime_radial_left = m._runtime_radial_right = True
        m._runtime_protect = True
        before = [(axis.DeadZone, axis.Speed) for axis in device.Axes]
        self.assertTrue(m.notify_input_rebuild_failed(pc, pi))
        self.assertFalse(m._runtime_radial_left or m._runtime_radial_right)
        self.assertFalse(m._runtime_protect)
        self.assertEqual(
            [(axis.DeadZone, axis.Speed) for axis in device.Axes], before)
        with patch.object(m, "_apply_native", side_effect=AssertionError(
                "partial input table must not be reconciled")):
            m._reconcile_runtime()
            self.assertFalse(m.notify_input_rebuilt(pc, pi))
        self.assertTrue(m._failed_rebuild_is_current(pi))

        replacement_device, replacement = mapping(m, address=101)
        replacement.address = 201
        new_pc = Object(301, PlayerInput=replacement)
        self.env["pc"] = new_pc
        self.assertFalse(m.notify_input_rebuild_failed(pc, pi))
        self.assertTrue(m.notify_input_rebuilt(new_pc, replacement))
        self.assertFalse(m._failed_rebuild_is_current(replacement))

    def test_remote_failed_rebuild_does_not_suspend_local_radial(self):
        m = self.m
        device, pi = mapping(m)
        pc = Object(300, PlayerInput=pi)
        self.env["pc"] = pc
        m._enabled = True
        m._pi_ref = weakref.ref(pi)
        m._pi_address = pi.address
        m._runtime_radial_left = True
        foreign_device, foreign = mapping(m, address=101)
        foreign.address = 201
        self.assertFalse(m.notify_input_rebuild_failed(
            Object(301, PlayerInput=foreign), foreign))
        self.assertTrue(m._runtime_radial_left)
        self.assertIsNone(m._failed_rebuild_pi)

    def test_native_registration_failure_never_skips_validation(self):
        m = self.m
        m._pi_address = 200
        native = SimpleNamespace(start_lifecycle_hooks=Mock(return_value=False), lifecycle_resume=Mock())
        m._native_radial_module = native
        self.assertFalse(m._arm_native_lifecycle())
        native.lifecycle_resume.assert_not_called()

    def test_hook_registration_and_cleanup_follow_sdk_state(self):
        m = self.m
        hooks = m.unrealsdk.hooks
        key = ("path", m.Type.PRE, "owned")
        original_add = hooks.add_hook
        def partial_add(*args):
            original_add(*args)
            raise RuntimeError("after registration")
        hooks.add_hook = partial_add
        self.assertTrue(m._add_hook(*key, lambda *args: None))
        self.assertIn(key, m._hooks)
        hooks.remove_hook = lambda *args: None
        m._remove_hook("owned")
        self.assertIn(key, m._hooks)
        actual = (key[0], key[1], m._hook_ids[key])
        self.assertIn(actual, self.env["hooks"])
        hooks.remove_hook = lambda *args: self.env["hooks"].pop(tuple(args), None)
        m._remove_all_hooks()
        self.assertNotIn(key, m._hooks)
        self.assertNotIn(actual, self.env["hooks"])

    def test_hook_missing_or_foreign_is_not_claimed(self):
        m = self.m
        hooks = m.unrealsdk.hooks
        key = ("path", m.Type.PRE, "owned")
        hooks.add_hook = lambda *args: None
        self.assertFalse(m._add_hook(*key, lambda *args: None))
        self.assertFalse(m._hooks)
        self.env["hooks"][key] = object()
        hooks.add_hook = lambda p,k,i,cb: self.env["hooks"].__setitem__((p,k,i),cb)
        self.assertTrue(m._add_hook(*key, lambda *args: None))
        m._remove_all_hooks()
        self.assertIn(key, self.env["hooks"])

    def test_removed_owned_hook_is_reinstalled(self):
        m = self.m
        key = ("path", m.Type.PRE, "owned")
        self.assertTrue(m._add_hook(*key, lambda *args: None))
        actual = (key[0], key[1], m._hook_ids[key])
        self.env["hooks"].pop(actual)
        self.assertTrue(m._add_hook(*key, lambda *args: None))
        self.assertEqual(m._hooks.count(key), 1)
        self.assertIn((key[0], key[1], m._hook_ids[key]), self.env["hooks"])
        self.assertNotEqual(actual[2], m._hook_ids[key])

    def test_retained_python_hooks_yield_to_native_owners(self):
        m = self.m
        m._enabled = True
        m._runtime_radial_left = True
        m._runtime_protect = True
        m._native_axis_hook_live = m._native_key_hook_live = m._native_pre_hook_live = True
        native = SimpleNamespace(axis_event=Mock(), key_event=Mock(), apply_cached_radial=Mock())
        m._native_radial_module = native
        m._capture_axis(None, None, None, None)
        m._input_key(None, None, None, None)
        m._apply_radial(None, None, None, None)
        native.axis_event.assert_not_called()
        native.key_event.assert_not_called()
        native.apply_cached_radial.assert_not_called()

    def test_python_fallback_and_mixed_device_guard(self):
        m = self.m
        pi = Object(200, bUsingGamepad=True)
        m._set_active_device("controller")
        self.assertTrue(m._fallback_controller_allowed(pi))
        m._set_active_device("kbm")
        self.assertFalse(m._fallback_controller_allowed(pi))
        m._set_active_device("controller")
        m._kbm_keys_down.add("W")
        self.assertFalse(m._fallback_controller_allowed(pi))
        m._kbm_keys_down.clear()
        pi.bUsingGamepad = False
        self.assertFalse(m._fallback_controller_allowed(pi))
        self.assertEqual(m._custom_value(.9, .8, m.GAIN_LEFT_X, 1.0), 0.0)
        self.assertEqual(m._native_safe_deadzone(1.0), .999)

    def test_axial_and_radial_modes_remain_exclusive(self):
        m = self.m
        m.axial_mode.value = m.radial_mode.value = True
        settings = m._settings_tuple()
        self.assertFalse(settings[8] or settings[9])
        m.axial_mode.value = False
        m.radial_left_enabled.value = True
        m.radial_right_enabled.value = False
        settings = m._settings_tuple()
        self.assertFalse(settings[0] or settings[1])
        self.assertTrue(settings[8])
        self.assertFalse(settings[9])

    def test_deferred_native_resume_keeps_python_pre(self):
        m = self.m
        m._enabled = True
        m._pi_address = 200
        m.use_native_radial.value = True
        m._runtime_radial_left = True
        m._native_axis_hook_live = True
        m._native_radial_module = SimpleNamespace(start_preprocess_hook=Mock(return_value=True),
            stop_preprocess_hook=Mock(), preprocess_hook_active=Mock(return_value=False),
            start_lifecycle_hooks=Mock(return_value=True), lifecycle_resume=Mock(return_value=False))
        self.assertFalse(m._try_native_pre_hook())
        self.assertFalse(m._native_pre_hook_live)
        self.assertGreater(m._recovery_deadline, 0)

    def test_reset_does_not_discard_native_owned_fields(self):
        m = self.m
        native = SimpleNamespace(reset=Mock(), clear_owned_axis_fields=Mock())
        m._native_radial_module = native
        m._reset_native_radial()
        native.reset.assert_called_once()
        native.clear_owned_axis_fields.assert_not_called()

    def test_failed_speed_restore_keeps_baseline_and_repairs_later(self):
        m = self.m
        device, pi = mapping(m)
        physical = "XboxTypeS_LeftX"
        device.Axes[0].Speed = 0.
        m._speed_targets[physical] = (weakref.ref(device), 0, 100, 1.)
        m._speed_applied.add(physical)
        native = SimpleNamespace(restore_owned_axis_field=Mock(return_value=-1),
                                 relinquish_owned_axis_field=Mock())
        m._native_radial_module = native
        with patch.object(m.time, "monotonic", return_value=1.):
            m._restore_speed_axis(physical)
        self.assertEqual(len(m._pending_field_restores), 1)
        self.assertEqual(device.Axes[0].Speed, 0.)
        native.restore_owned_axis_field.return_value = 2
        with patch.object(m.time, "monotonic", return_value=1.3):
            m._retry_field_restores()
        self.assertEqual(device.Axes[0].Speed, 1.)
        self.assertEqual(m._pending_field_restores, {})

    def test_pending_restore_preserves_external_writer(self):
        m = self.m
        device, pi = mapping(m)
        device.Axes[0].Speed = .7
        m._pending_field_restores[(100, 0, "XboxTypeS_LeftX", "Speed")] = (weakref.ref(device), 1., 0., 0.)
        m._retry_field_restores()
        self.assertEqual(device.Axes[0].Speed, .7)
        self.assertFalse(m._pending_field_restores)

    def test_cleanup_repairs_after_disable_and_unregisters_itself(self):
        m = self.m
        device, pi = mapping(m)
        device.Axes[0].Speed = 0.
        with patch.object(m.time, "monotonic", return_value=1.):
            m._queue_field_restore(weakref.ref(device), 0, 100, "XboxTypeS_LeftX", "Speed", 1., 0.)
        self.assertFalse(m._enabled)
        with patch.object(m.time, "monotonic", return_value=1.3):
            m._field_cleanup_tick(None, None, None, None)
        self.assertEqual(device.Axes[0].Speed, 1.)
        self.assertEqual(m._field_cleanup_deadline, 0)
        self.assertFalse(self.env["hooks"])

    def test_expired_weak_identity_is_relinquished_without_raw_access(self):
        m = self.m
        release = Mock()
        m._native_radial_module = SimpleNamespace(relinquish_owned_axis_field=release)
        m._pending_field_restores[(100, 0, "XboxTypeS_LeftX", "Speed")] = (lambda: None, 1., 0., 0.)
        m._retry_field_restores()
        release.assert_called_once_with(100, 0, 0, 0)
        self.assertFalse(m._pending_field_restores)

    def test_native_partial_frame_is_not_filtered_again(self):
        m = self.m
        device, pi = mapping(m)
        m._enabled = True
        m._runtime_radial_left = True
        m._pi_address = 200
        m._native_pre_skip_once = True
        m._apply_radial(pi, None, None, None)  # no accumulators exist: a second filter would fail
        self.assertFalse(m._native_pre_skip_once)
        self.assertEqual(self.env["warnings"], [])

    def test_nonfinite_source_read_and_cas_are_rejected(self):
        m = self.m
        device, pi = mapping(m)
        row = device.Axes[0]
        row.Speed = float("nan")
        with self.assertRaises(ValueError):
            m._source_field_read(row, 100, 0, "XboxTypeS_LeftX", "Speed")
        self.assertFalse(m._source_field_cas(row, 100, 0, "XboxTypeS_LeftX", "Speed", float("nan"), 0.))
        self.assertFalse(m._source_field_cas(row, 100, 0, "XboxTypeS_LeftX", "Speed", 1., 0.))
        self.assertTrue(math.isnan(row.Speed))

    def test_settings_keys_and_reset_preserve_python_backend_default(self):
        m = self.m
        ids = [o.identifier for o in m._stored_options]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("use_native_radial", ids)
        self.assertIn("tps_experimental_native_radial", ids)
        m.tps_experimental_native_radial.value = True
        m.use_native_radial.value = True
        m.radial_mode.value = True
        m.axial_mode.value = False
        m._reset_to_defaults()
        self.assertFalse(m.use_native_radial.value)
        self.assertFalse(m.tps_experimental_native_radial.value)
        self.assertFalse(m.axial_mode.value)
        self.assertTrue(m.radial_mode.value)
        self.assertEqual(m.radial_left_inner.value, 10)
        self.assertEqual(m.radial_right_inner.value, 10)
        m.mod.save_settings.assert_called_once()

    def test_reset_fallback_values_match_all_options_without_default_attribute(self):
        m, _ = load_frontend("TPS", saved_native=True, saved_tps_native=True)
        expected = [option.default_value for option in m._stored_options]
        for option in m._stored_options:
            del option.default_value
        m._reset_to_defaults()
        self.assertEqual([option.value for option in m._stored_options], expected)
        self.assertFalse(m.tps_experimental_native_radial.value)

    def test_radial_endpoints_and_diagonal_curve(self):
        f = self.m._radial_multiplier
        self.assertEqual(f(1., 1., 1., 1.), 0)
        self.assertEqual(f(.1, 0., .1, 1.), 0)
        self.assertAlmostEqual(f(.5, 0., .1, 1.), (0.4 / .9) / .5)
        self.assertAlmostEqual(f(1., 1., .1, 1.), 1 / math.sqrt(2))

    def test_matching_native_version_is_loaded_and_configured(self):
        m = self.m
        m.use_native_radial.value = True
        fake = SimpleNamespace(apply_radial=Mock(), backend_version=lambda: "1.0.5")
        m.deadzone_backend = fake
        with patch.dict(sys.modules, {"deadzone_test_mod": m}), patch.object(m, "_configure_native_runtime") as configure:
            m._load_native_radial()
        self.assertIs(m._native_radial_module, fake)
        configure.assert_called_once()
        self.assertEqual(self.env["warnings"], [])

    def test_stale_native_version_retains_python_fallback(self):
        m = self.m
        m.use_native_radial.value = True
        fake = SimpleNamespace(apply_radial=Mock(), backend_version=lambda: "1.0.3")
        m.deadzone_backend = fake
        with patch.dict(sys.modules, {"deadzone_test_mod": m}):
            m._load_native_radial()
        self.assertTrue(m._native_radial_attempted)
        self.assertIsNone(m._native_radial_module)
        self.assertTrue(any("version does not match" in warning for warning in self.env["warnings"]))

    def test_tps_persisted_native_setting_cannot_load_or_register_backend(self):
        m, env = load_frontend("TPS", saved_native=True)
        self.assertTrue(m.IS_TPS)
        self.assertTrue(m.use_native_radial.value)
        self.assertTrue(m.use_native_radial.is_hidden)
        self.assertFalse(m._native_requested())
        m._load_native_radial()
        self.assertFalse(m._native_radial_attempted)
        self.assertIsNone(m._native_radial_module)
        m._native_radial_changed(m.use_native_radial, True)
        self.assertFalse(m._load_native_radial_on_next_frame[0])
        m._on_enable()
        self.assertIsNone(m._native_radial_module)
        self.assertFalse(m._native_radial_attempted)
        self.assertFalse(m._native_hot_available())
        m._on_disable()
        self.assertTrue(m.use_native_radial.value)

    def test_tps_opt_in_is_separate_visible_and_persistent(self):
        m, env = load_frontend("TPS", saved_native=True, saved_tps_native=True)
        self.assertTrue(m.use_native_radial.is_hidden)
        self.assertFalse(m.tps_experimental_native_radial.is_hidden)
        self.assertTrue(m._native_requested())
        self.assertIn(m.tps_experimental_native_radial, m._stored_options)
        m._native_radial_changed(m.tps_experimental_native_radial, True)
        self.assertTrue(m._load_native_radial_on_next_frame[0])
        m.tps_experimental_native_radial.value = False
        self.assertFalse(m._native_requested())
        bl2, _ = load_frontend("BL2", saved_native=False, saved_tps_native=True)
        self.assertTrue(bl2.tps_experimental_native_radial.is_hidden)
        self.assertFalse(bl2._native_requested())

    def test_tps_opt_in_loads_matching_backend_for_radial_processing(self):
        m, env = load_frontend("TPS", saved_native=False, saved_tps_native=True)
        native = SimpleNamespace(apply_radial=Mock(), backend_version=lambda: "1.0.5")
        m.deadzone_backend = native
        m._runtime_radial_left = True
        with patch.dict(sys.modules, {"deadzone_test_mod": m}), patch.object(m, "_configure_native_runtime") as configure:
            m._load_native_radial()
        self.assertIs(m._native_radial_module, native)
        self.assertTrue(m._native_hot_available())
        configure.assert_called_once()
        m.tps_experimental_native_radial.value = False
        self.assertFalse(m._native_hot_available())

    def test_tps_native_configuration_failure_does_not_select_backend(self):
        m, env = load_frontend("TPS", saved_tps_native=True)
        m.deadzone_backend = SimpleNamespace(apply_radial=Mock(), backend_version=lambda: "1.0.5")
        with patch.dict(sys.modules, {"deadzone_test_mod": m}), patch.object(m, "_configure_native_runtime", side_effect=RuntimeError("failed")):
            m._load_native_radial()
        self.assertIsNone(m._native_radial_module)
        self.assertTrue(m._native_radial_attempted)
        self.assertFalse(m._native_hot_available())
        self.assertTrue(env["warnings"])

    def test_tps_switch_back_arms_python_hooks_before_native_cleanup(self):
        m, env = load_frontend("TPS", saved_tps_native=True)
        m._enabled=True; m._runtime_radial_left=True
        events=[]
        m._add_hook=lambda *args: events.append("python") or True
        m._stop_native_lifecycle=lambda:events.append("lifecycle")
        m._stop_native_pre_hook=lambda:events.append("pre")
        m._stop_native_key_hook=lambda:events.append("key")
        m._stop_native_axis_hook=lambda:events.append("axis")
        m._native_radial_changed(m.tps_experimental_native_radial, False)
        self.assertEqual(events[:3],["python"]*3)
        self.assertEqual(events[3:],["lifecycle","pre","key","axis"])

    def test_tps_guards_native_registration_and_retains_engine_fields(self):
        m, env = load_frontend("TPS", saved_native=True)
        native = SimpleNamespace(start_axis_hook=Mock(), start_key_hook=Mock(),
                                 start_preprocess_hook=Mock())
        m._native_radial_module = native
        m._runtime_radial_left = True
        self.assertFalse(m._try_native_axis_hook())
        self.assertFalse(m._try_native_key_hook())
        self.assertFalse(m._try_native_pre_hook())
        native.start_axis_hook.assert_not_called()
        native.start_key_hook.assert_not_called()
        native.start_preprocess_hook.assert_not_called()
        m._native_radial_module = None
        row = SimpleNamespace(DeadZone=.2)
        self.assertEqual(m._source_field_read(row, 100, 0, 'XboxTypeS_LeftX', 'DeadZone'), .2)
        self.assertTrue(m._source_field_cas(row, 100, 0, 'XboxTypeS_LeftX', 'DeadZone', .2, .3))
        self.assertEqual(row.DeadZone, .3)
        self.assertFalse(m._source_field_cas(row, 100, 0, 'XboxTypeS_LeftX', 'DeadZone', .2, .4))

    def test_bl2_native_selection_is_unchanged(self):
        m, env = load_frontend("BL2", saved_native=True)
        self.assertFalse(m.IS_TPS)
        self.assertFalse(m.use_native_radial.is_hidden)
        self.assertTrue(m._native_requested())
        m._native_radial_changed(m.use_native_radial, True)
        self.assertTrue(m._load_native_radial_on_next_frame[0])

    def test_tps_metadata(self):
        metadata = tomllib.loads((ROOT / 'mod/ControllerDeadzone/pyproject.toml').read_text())
        self.assertEqual(metadata['tool']['sdkmod']['supported_games'], ['BL2', 'TPS'])

    def test_tps_mode_stick_independence_and_kbm_safeguards(self):
        self.m, self.env = load_frontend('TPS', saved_native=True)
        self.test_axial_and_radial_modes_remain_exclusive()
        self.test_python_fallback_and_mixed_device_guard()
        self.test_radial_endpoints_and_diagonal_curve()
        self.test_hook_registration_and_cleanup_follow_sdk_state()
        self.test_pending_restore_preserves_external_writer()

    def test_game_settings_paths_and_native_preference_remain_separate(self):
        bl2_root = ROOT / 'build' / 'uncreated_bl2_settings'
        tps_root = ROOT / 'build' / 'uncreated_tps_settings'
        bl2, _ = load_frontend('BL2', saved_native=True, settings_dir=bl2_root)
        tps, _ = load_frontend('TPS', saved_native=True, settings_dir=tps_root)
        self.assertEqual(bl2.mod.settings_file, bl2_root / 'ControllerDeadzone.json')
        self.assertEqual(tps.mod.settings_file, tps_root / 'ControllerDeadzone.json')
        self.assertNotEqual(bl2.mod.settings_file, tps.mod.settings_file)
        self.assertTrue(bl2.use_native_radial.value and tps.use_native_radial.value)
        self.assertTrue(bl2._native_requested())
        self.assertFalse(tps._native_requested())

    def test_migration_keeps_existing_settings_and_legacy_file(self):
        migration = self.m
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            old = root / '3ControllerDeadzoneControl.json'
            new = root / 'ControllerDeadzone.json'
            old.write_text('{"use_native_radial": true}', encoding='utf-8')
            migration.migrate_settings(root)
            self.assertEqual(new.read_text(encoding='utf-8'), old.read_text(encoding='utf-8'))
            new.write_text('{"use_native_radial": false}', encoding='utf-8')
            migration.migrate_settings(root)
            self.assertIn('false', new.read_text(encoding='utf-8'))
            self.assertIn('true', old.read_text(encoding='utf-8'))


if __name__ == "__main__":
    unittest.main()
