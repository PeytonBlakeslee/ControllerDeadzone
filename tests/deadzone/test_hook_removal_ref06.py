"""REF-06: characterize the real production hook-removal functions in isolation.

For integration: place in ControllerDeadzone/tests/deadzone/. Tests take the
actual source via AST; no unreal SDK or game install is necessary.
"""
from __future__ import annotations
import ast
import os
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

DEFAULT_SOURCE = Path(__file__).resolve().parents[2] / 'mod' / 'ControllerDeadzone' / '__init__.py'
SOURCE = Path(os.environ.get('DZ_REF06_SOURCE', str(DEFAULT_SOURCE)))
A = ('Game.Player:Foo', 'PRE', 'alpha')
B = ('Game.Player:Bar', 'POST', 'alpha')
C = ('Game.Pawn:Baz', 'POST', 'beta')
FOREIGN = ('Other.Actor:Tick', 'POST', 'alpha')


def load_removal_functions(scope):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'), str(SOURCE))
    names = {'_remove_owned_hook_entry', '_remove_hook', '_remove_all_hooks'}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if not {'_remove_hook', '_remove_all_hooks'} <= {node.name for node in nodes}:
        raise AssertionError('Required production hook removal functions missing')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), scope)
    return scope


class Hooks:
    def __init__(self, entries, ledger):
        self.live = set(entries)
        self.ledger = ledger
        self.events = []
        self.rules = {}
        self.reenter = None

    def remove_hook(self, *entry):
        self.events.append(('remove', entry))
        policy = self.rules.get(entry)
        existed = entry in self.live
        if policy == 'throw_before':
            raise RuntimeError('injected remove error')
        if policy != 'remain':
            self.live.discard(entry)
        if self.reenter is not None:
            call = self.reenter
            self.reenter = None
            call()
        if policy == 'throw_after':
            raise RuntimeError('injected post-remove error')
        if policy == 'replace':
            self.live.add(entry)  # Another owner takes this identifier.
        return None if policy == 'has_throw' else existed and policy != 'remain'

    def has_hook(self, *entry):
        self.events.append(('has', entry))
        if self.rules.get(entry) == 'has_throw':
            raise RuntimeError('has_hook failed')
        return entry in self.live


class HookRemovalContract(unittest.TestCase):
    def rig(self, ledger, live=None):
        data = list(ledger)
        sdk = Hooks(data if live is None else live, data)
        seen = set()
        def warn_once(key, message):
            if key not in seen:
                seen.add(key)
                sdk.events.append(('warn', key, message))
        scope = dict(_hooks=data, _hook_ids={entry: entry[2] for entry in data},
                     _hook_pending_removal=set(), _warn_once=warn_once,
                     unrealsdk=NS(hooks=sdk), Type=object)
        load_removal_functions(scope)
        return scope, sdk

    def test_target_identifier_preserves_forward_order_and_foreign(self):
        scope, sdk = self.rig([A,B,C], [A,B,C,FOREIGN])
        scope['_remove_hook']('alpha')
        self.assertEqual([x[1] for x in sdk.events if x[0]=='remove'], [A,B])
        self.assertEqual(scope['_hooks'], [C])
        self.assertEqual(sdk.live, {C, FOREIGN})

    def test_bulk_cleanup_is_reverse_and_isolated(self):
        scope, sdk = self.rig([A,B,C], [A,B,C,FOREIGN])
        scope['_remove_all_hooks']()
        self.assertEqual([x[1] for x in sdk.events if x[0]=='remove'], [C,B,A])
        self.assertEqual(scope['_hooks'], [])
        self.assertEqual(sdk.live, {FOREIGN})

    def test_absent_record_is_pruned_without_touching_foreign(self):
        scope, sdk = self.rig([A], [FOREIGN])
        scope['_remove_hook']('alpha')
        self.assertEqual(scope['_hooks'], [])
        self.assertEqual(sdk.live, {FOREIGN})
        self.assertEqual([x[0] for x in sdk.events], ['remove','has'])

    def test_duplicate_identical_records_are_retired_once(self):
        scope, sdk = self.rig([A,A])
        scope['_remove_hook']('alpha')
        self.assertEqual([x[1] for x in sdk.events if x[0]=='remove'], [A])
        self.assertEqual(scope['_hooks'], [])

    def test_failed_remove_retains_entry_then_retry_succeeds(self):
        scope, sdk = self.rig([A,B,C])
        sdk.rules[A] = 'throw_before'
        scope['_remove_hook']('alpha')
        self.assertEqual(scope['_hooks'], [A,C])
        self.assertEqual(sdk.live, {A,C})
        self.assertEqual([x[1] for x in sdk.events if x[0]=='remove'], [A,B])
        sdk.rules.clear()
        scope['_remove_hook']('alpha')
        self.assertEqual(scope['_hooks'], [C])
        self.assertEqual(sdk.live, {C})

    def test_remains_registered_retains_record_without_foreign_removal(self):
        scope, sdk = self.rig([A,B], [A,B,FOREIGN])
        sdk.rules[A] = 'remain'
        scope['_remove_all_hooks']()
        self.assertEqual(scope['_hooks'], [A])
        self.assertEqual(sdk.live, {A,FOREIGN})
        self.assertIn(('warn','unhook:alpha','hook removal deferred (alpha): RuntimeError: hook remains registered'), sdk.events)

    def test_exception_after_unhook_keeps_record_until_explicit_retry(self):
        scope, sdk = self.rig([A])
        sdk.rules[A] = 'throw_after'
        scope['_remove_hook']('alpha')
        self.assertEqual(scope['_hooks'], [A])
        self.assertNotIn(A, sdk.live)
        sdk.rules.clear()
        scope['_remove_hook']('alpha')
        self.assertEqual(scope['_hooks'], [])

    def test_external_replacement_does_not_claim_success(self):
        scope, sdk = self.rig([A], [A,FOREIGN])
        sdk.rules[A] = 'replace'
        scope['_remove_all_hooks']()
        self.assertEqual(scope['_hooks'], [])
        self.assertEqual(sdk.live, {A,FOREIGN})
        scope['_remove_all_hooks']()
        self.assertIn(A, sdk.live)

    def test_has_hook_exception_keeps_record(self):
        scope, sdk = self.rig([A])
        sdk.rules[A] = 'has_throw'
        scope['_remove_all_hooks']()
        self.assertEqual(scope['_hooks'], [A])
        self.assertNotIn(A, sdk.live)
        sdk.rules.clear()
        scope['_remove_all_hooks']()
        self.assertEqual(scope['_hooks'], [])

    def test_multiple_same_identifier_failures_warn_once(self):
        scope, sdk = self.rig([A,B])
        sdk.rules[A]=sdk.rules[B]='throw_before'
        scope['_remove_all_hooks']()
        self.assertEqual(scope['_hooks'], [A,B])
        self.assertEqual(len([x for x in sdk.events if x[0]=='warn']),1)

    def test_reentrant_ledger_prune_retains_current_order(self):
        scope, sdk = self.rig([A,B,C])
        sdk.reenter = lambda: scope['_remove_hook']('beta')
        scope['_remove_all_hooks']()
        self.assertEqual(scope['_hooks'], [])
        self.assertEqual([x[1] for x in sdk.events if x[0]=='remove'], [C,C,B,A])

if __name__=='__main__':
    unittest.main()
