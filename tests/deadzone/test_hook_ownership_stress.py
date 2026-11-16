"""Adversarial tests of the actual ControllerDeadzone hook-management functions."""
import random
import unittest
from test_frontend import load_frontend, Object


class Registry:
    def __init__(self):
        self.live = {}
        self.mode = "true"
        self.remove_mode = "normal"
        self.has_count = 0
        self.fail_has_at = set()
        self.foreign = object()
        self.removed = []

    def has_hook(self, *key):
        self.has_count += 1
        if self.has_count in self.fail_has_at:
            raise RuntimeError("injected has failure")
        return key in self.live

    def add_hook(self, p, k, i, callback):
        key = p, k, i
        if self.mode == "before":
            raise RuntimeError("before insert")
        if self.mode == "collision":
            self.live[key] = self.foreign
            return False
        if self.mode != "reject":
            self.live[key] = callback
        if self.mode == "after":
            raise RuntimeError("after insert")
        return self.mode == "true"

    def remove_hook(self, *key):
        if self.remove_mode == "before":
            raise RuntimeError("before remove")
        if self.remove_mode == "remain":
            return False
        old = self.live.pop(key, None)
        if old is not None:
            self.removed.append(old)
        if self.remove_mode == "replace":
            self.live[key] = self.foreign
        if self.remove_mode == "after":
            raise RuntimeError("after remove")
        if self.remove_mode == "none":
            return None
        return old is not None


class OwnershipStress(unittest.TestCase):
    def setUp(self):
        self.m, self.env = load_frontend()
        self.sdk = Registry()
        self.m.unrealsdk.hooks = self.sdk
        self.logical = ("Function", self.m.Type.PRE, "ControllerDeadzoneControl:Axis")
        self.callback = lambda *args: None

    def actual(self):
        p,k,_ = self.logical
        return p,k,self.m._hook_ids[self.logical]

    def invariant(self):
        m = self.m
        self.assertEqual(len(m._hooks), len(set(m._hooks)))
        self.assertEqual(set(m._hooks), set(m._hook_ids))
        self.assertEqual(len(set(m._hook_ids.values())), len(m._hook_ids))
        self.assertTrue(m._hook_pending_removal <= set(m._hooks))

    def test_registration_modes(self):
        for mode, present in (("true", True), ("false", True), ("reject", False),
                              ("before", False), ("after", True)):
            with self.subTest(mode=mode):
                self.setUp(); self.sdk.mode = mode
                self.assertEqual(self.m._add_hook(*self.logical, self.callback), present)
                self.invariant()
                if present:
                    key = self.actual()
                    self.assertIs(self.sdk.live[key], self.callback)
                    self.assertTrue(self.m._add_hook(*self.logical, self.callback))
                    self.assertEqual(len(self.sdk.live), 1)
                    self.m._remove_all_hooks()
                    self.assertFalse(self.sdk.live)
                self.invariant()

    def test_postflight_failure_retains_and_recovers(self):
        self.sdk.fail_has_at = {2}
        self.assertFalse(self.m._add_hook(*self.logical, self.callback))
        key = self.actual()
        self.assertIs(self.sdk.live[key], self.callback)
        self.assertTrue(self.m._add_hook(*self.logical, self.callback))
        self.assertEqual(len(self.sdk.live), 1)
        self.m._remove_all_hooks(); self.invariant()
        self.assertFalse(self.sdk.live)

    def test_preflight_failure_does_not_create_a_record(self):
        self.sdk.fail_has_at = {1}
        self.assertFalse(self.m._add_hook(*self.logical, self.callback))
        self.invariant(); self.assertFalse(self.sdk.live)

    def test_generated_preexisting_collision_is_never_touched(self):
        ident = f"{self.logical[2]}/{self.m._hook_nonce}/1"
        key = self.logical[0], self.logical[1], ident
        self.sdk.live[key] = self.sdk.foreign
        self.assertFalse(self.m._add_hook(*self.logical, self.callback))
        self.m._remove_all_hooks()
        self.assertIs(self.sdk.live[key], self.sdk.foreign)
        self.assertTrue(self.m._add_hook(*self.logical, self.callback))
        self.assertNotEqual(self.actual(), key)
        self.m._remove_all_hooks(); self.assertIs(self.sdk.live[key], self.sdk.foreign)

    def test_exact_id_race_is_api_ambiguity_not_fixed(self):
        # Both insertion outcomes yield False + presence. The API cannot reveal
        # whether this entry is our callback. Do not describe this as safe.
        self.sdk.mode = "collision"
        self.assertTrue(self.m._add_hook(*self.logical, self.callback))
        self.assertIs(self.sdk.live[self.actual()], self.sdk.foreign)
        self.assertEqual(len(self.m._hooks), 1)

    def test_cleanup_modes_and_retry(self):
        for mode in ("normal", "none", "before", "after", "remain", "replace"):
            with self.subTest(mode=mode):
                self.setUp(); self.assertTrue(self.m._add_hook(*self.logical, self.callback))
                key = self.actual(); self.sdk.remove_mode = mode
                self.m._remove_all_hooks(); self.invariant()
                if mode in ("before", "after", "remain"):
                    self.assertIn(self.logical, self.m._hooks)
                    self.sdk.remove_mode = "normal"
                    self.m._remove_all_hooks()
                else:
                    self.assertNotIn(self.logical, self.m._hooks)
                if mode == "replace":
                    self.assertIs(self.sdk.live[key], self.sdk.foreign)
                    self.m._remove_all_hooks()
                    self.assertIs(self.sdk.live[key], self.sdk.foreign)
                else:
                    self.assertNotIn(key, self.sdk.live)
                self.invariant()

    def test_interrupted_removal_is_not_reused_as_active(self):
        self.assertTrue(self.m._add_hook(*self.logical, self.callback))
        key = self.actual()
        self.sdk.remove_mode = "before"
        self.m._remove_all_hooks()
        self.assertIn(self.logical, self.m._hook_pending_removal)
        self.assertFalse(self.m._add_hook(*self.logical, self.callback))
        self.assertEqual(len(self.sdk.live), 1)
        self.assertIs(self.sdk.live[key], self.callback)
        self.sdk.remove_mode = "normal"
        self.m._remove_all_hooks()
        self.assertFalse(self.m._hook_pending_removal)
        self.assertTrue(self.m._add_hook(*self.logical, self.callback))
        self.assertNotEqual(key, self.actual())

    def test_successful_remove_with_replacement_does_not_depend_on_postcheck(self):
        self.assertTrue(self.m._add_hook(*self.logical, self.callback))
        key = self.actual()
        self.sdk.remove_mode = "replace"
        self.sdk.fail_has_at = {self.sdk.has_count + 1}
        self.m._remove_all_hooks()
        self.assertNotIn(self.logical, self.m._hooks)
        self.m._remove_all_hooks()
        self.assertIs(self.sdk.live[key], self.sdk.foreign)

    def test_remove_verification_failure_keeps_record(self):
        self.assertTrue(self.m._add_hook(*self.logical, self.callback))
        self.sdk.remove_mode = "none"  # No positive SDK removal confirmation.
        self.sdk.fail_has_at.add(self.sdk.has_count + 1)
        self.m._remove_all_hooks()
        self.assertIn(self.logical, self.m._hooks)
        self.m._remove_all_hooks(); self.assertFalse(self.m._hooks); self.invariant()

    def test_many_same_function_and_unique_ids(self):
        ids = set()
        for i in range(10000):
            logical = ("Function", self.m.Type.PRE, f"logical-{i}")
            self.assertTrue(self.m._add_hook(*logical, self.callback))
            actual = self.m._hook_ids[logical]
            self.assertNotIn(actual, ids); ids.add(actual)
            self.m._remove_hook(logical[2])
            self.assertFalse(self.m._hooks)
        self.assertEqual(len(ids), 10000); self.assertFalse(self.sdk.live); self.invariant()

    def test_multiple_hooks_share_function_and_stage(self):
        first = self.logical
        second = (first[0], first[1], "other-logical")
        self.assertTrue(self.m._add_hook(*first, self.callback))
        self.sdk.mode = "false"  # Pinned SDK appends but returns False.
        self.assertTrue(self.m._add_hook(*second, self.callback))
        self.assertEqual(len(self.sdk.live), 2)
        self.m._remove_hook(first[2])
        self.assertIn(second, self.m._hooks)
        self.assertEqual(len(self.sdk.live), 1)
        self.m._remove_all_hooks(); self.assertFalse(self.sdk.live); self.invariant()

    def test_real_frontend_enable_disable_cycles(self):
        self.m.unrealsdk.find_object = lambda *args: object()
        identifiers = set()
        for cycle in range(1000):
            self.m._on_enable()
            current = set(self.m._hook_ids.values())
            self.assertTrue(current, f"no lifecycle hooks in cycle {cycle}")
            self.assertFalse(identifiers & current)
            identifiers.update(current)
            self.invariant()
            self.m._on_disable()
            self.assertEqual(self.m._hooks, [])
            self.assertEqual(self.m._hook_ids, {})
            self.assertEqual(self.sdk.live, {})
        self.assertGreaterEqual(len(identifiers), 1000)

    def test_post_insert_exception_and_postflight_failure_retains_until_retry(self):
        self.sdk.mode = "after"
        self.sdk.fail_has_at = {2}
        self.assertFalse(self.m._add_hook(*self.logical, self.callback))
        key = self.actual()
        self.assertIs(self.sdk.live[key], self.callback)
        self.sdk.remove_mode = "normal"
        self.m._remove_all_hooks()
        self.assertFalse(self.m._hooks); self.assertFalse(self.sdk.live)

    def test_replacement_after_registration_cannot_be_identified_by_sdk(self):
        self.assertTrue(self.m._add_hook(*self.logical, self.callback))
        key = self.actual()
        self.sdk.live[key] = self.sdk.foreign
        # Explicit counterexample: SDK's identifier-only remove cannot distinguish
        # this foreign callback from the previously owned registration.
        self.m._remove_all_hooks()
        self.assertIn(self.sdk.foreign, self.sdk.removed)
        self.assertFalse(self.m._hooks)

    def test_actual_runtime_reconciliation_under_backend_switches(self):
        rnd = random.Random(440711)
        native = [False]
        self.m._try_native_axis_hook = lambda: native[0]
        self.m._try_native_key_hook = lambda: native[0]
        self.m._try_native_pre_hook = lambda: native[0]
        self.m._stop_native_axis_hook = lambda: None
        self.m._stop_native_key_hook = lambda: None
        self.m._stop_native_pre_hook = lambda: None
        self.m._stop_native_lifecycle = lambda: None
        names = lambda: {entry[2].rsplit(":",1)[-1] for entry in self.m._hooks}
        for step in range(5000):
            hot,key,radial,native[0] = (bool(rnd.getrandbits(1)) for _ in range(4))
            self.assertTrue(self.m._sync_runtime_hooks(hot,key,radial), f"step={step}")
            native_axis = radial and not hot and native[0]
            expected = set()
            if (hot or radial) and not native_axis: expected.add("Axis")
            if hot: expected.add("PlayerInput")
            if key and not (native_axis and native[0]): expected.add("InputKey")
            if radial and not (native_axis and native[0]): expected.add("Radial")
            self.assertEqual(names(), expected, f"seed=440711 step={step}")
            self.assertEqual(len(self.sdk.live),len(expected), f"seed=440711 step={step}")
            self.invariant()
        self.m._sync_runtime_hooks(False,False,False)
        self.assertFalse(self.sdk.live);self.invariant()

    def test_actual_input_identity_disconnect_and_reconnect(self):
        # Exercise the production identity cache with inert engine-state boundaries.
        self.m._stop_native_lifecycle = lambda: None
        self.m._stop_native_pre_hook = lambda: None
        for step in range(1000):
            pi = Object(1000+step)
            self.m._cache_player_input(pi)
            self.assertEqual(self.m._pi_address,1000+step)
            self.m._cache_player_input(None)
            self.assertIsNone(self.m._pi_address)
            self.invariant()

    def test_randomized_cycles(self):
        for seed in (7, 20261114, 846203):
            with self.subTest(seed=seed):
                self.setUp(); rnd = random.Random(seed); trace = []
                foreign_key = ("Function", self.m.Type.PRE, "foreign")
                self.sdk.live[foreign_key] = self.sdk.foreign
                for step in range(5000):
                    action = rnd.choice(("add", "remove", "retry", "enable", "disable", "travel", "reconnect", "backend"))
                    trace.append(action)
                    try:
                        if action in ("add", "enable", "travel", "reconnect", "backend"):
                            self.sdk.mode = rnd.choice(("true", "false", "reject", "before", "after"))
                            self.m._add_hook(*self.logical, self.callback)
                        else:
                            self.sdk.remove_mode = rnd.choice(("normal", "none", "before", "after", "remain"))
                            self.m._remove_all_hooks()
                        self.invariant()
                        self.assertIs(self.sdk.live[foreign_key], self.sdk.foreign)
                        self.assertNotIn(self.sdk.foreign, self.sdk.removed)
                        self.assertLessEqual(len(self.m._hooks), 1)
                        self.assertLessEqual(len(self.sdk.live), 2)
                    except AssertionError as exc:
                        raise AssertionError(f"seed={seed} step={step} recent={trace[-30:]}") from exc
                self.sdk.remove_mode = "normal"; self.m._remove_all_hooks()
                self.assertEqual(self.sdk.live, {foreign_key: self.sdk.foreign}); self.invariant()


if __name__ == "__main__":
    unittest.main()
