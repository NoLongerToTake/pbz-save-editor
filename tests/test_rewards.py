"""奖励合同测试：纯内存文档，不读取或写入正式存档。"""
import copy
from decimal import Decimal, localcontext
import importlib
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def reward(key='quest:accessory', **changes):
    row = dict(key=key, item_id='AC_Bleed', name='雪与血',
               storage='AccessoryInfos', kind='accessory', quantity=1, level=0,
               stage_id='3', route_ids=['success'], certainty='verified',
               source={'table': 'fixture'}, category='quest_final')
    row.update(changes)
    return row


def document():
    return {'PBZGameSystem': {'CycleCount': 2}, 'Player': {'Dust': 10},
            'Inventory': {'InventoryInfos': {}, 'AccessoryInfos': {},
                          'MainWeaponInfos': {}, 'ShaChiWeaponInfo': {},
                          'MedicineInfos': {}, 'AccessoryIDs': ['None']},
            'Narrative': {'Untouched': True}}


class RewardTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('pbz_editor.rewards'),
                             '奖励预览和应用尚未实现')
        self.api = importlib.import_module('pbz_editor.rewards')
        self.doc = document()

    def preview(self, rows, receipts=()):
        return self.api.build_reward_preview(self.doc, rows, receipts)

    def apply(self, rows, keys=None, allow=False):
        return self.api.apply_rewards(self.doc, rows, keys, allow)

    def test_preview_is_read_only_and_preserves_spec_and_evidence(self):
        spec = reward(extra={'proof': [1]})
        original, source = copy.deepcopy(self.doc), copy.deepcopy(spec)
        row = self.preview([spec])[0]
        self.assertEqual(self.doc, original)
        self.assertEqual(spec, source)
        self.assertFalse(row['owned'])
        self.assertEqual((row['current_amount'], row['grant_amount']), (0, 1))
        self.assertTrue(row['default_selected'])
        self.assertFalse(row['requires_confirmation'])
        self.assertTrue(row['reason'])
        row['extra']['proof'].append(2)
        self.assertEqual(spec['extra']['proof'], [1])

    def test_defaults_only_verified_missing_unique_equipment_with_stage_evidence(self):
        rows = [reward('missing'), reward('unknown', certainty='unknown'),
                reward('random', certainty='random'), reward('world', category='world_pickup'),
                reward('drop', category='boss_drop'), reward('no-stage', stage_id=None, route_ids=[]),
                reward('count', storage='InventoryInfos', kind='item', item_id='I_01007', quantity=3),
                reward('dust', storage='Player.Dust', kind='dust', item_id='Dust', quantity=4)]
        self.assertEqual([r['key'] for r in self.preview(rows) if r['default_selected']], ['missing'])

    def test_missing_evidence_never_automatically_selects_unique_equipment(self):
        for source in (None, {}, '', []):
            with self.subTest(source=source):
                row = self.preview([reward(source=source)])[0]
                self.assertFalse(row['default_selected'])
                self.assertTrue(row['requires_confirmation'])

    def test_existing_unique_equipment_is_never_downgraded_or_rewritten(self):
        fixtures = [('AccessoryInfos', 'AC_Bleed', 'accessory', {'CurrentLevel': 9, 'Custom': 17}),
                    ('MainWeaponInfos', 'Tang', 'main_weapon', {'AlreadySnapSoul': 5, 'Custom': 18}),
                    ('ShaChiWeaponInfo', 'Axe', 'shaqi_weapon', {'ShaChiWeaponLevel': 8, 'Custom': 19}),
                    ('MedicineInfos', 'Hulu', 'medicine', {'CurrentLevel': 6, 'CurrentTime': 0, 'MaxTime': 4})]
        specs = []
        for storage, item_id, kind, record in fixtures:
            self.doc['Inventory'][storage][item_id] = record
            specs.append(reward(storage, storage=storage, item_id=item_id, kind=kind))
        old = copy.deepcopy(self.doc)
        rows = self.preview(specs)
        self.assertTrue(all(r['owned'] and not r['default_selected'] for r in rows))
        self.assertEqual(self.apply(rows, [r['key'] for r in rows], True), [])
        self.assertEqual(self.doc, old)

    def test_new_equipment_uses_native_fields_and_does_not_equip_or_mark_game_json(self):
        specs = [reward(), reward('main', storage='MainWeaponInfos', item_id='Tang', kind='main_weapon'),
                 reward('shaqi', storage='ShaChiWeaponInfo', item_id='Axe', kind='shaqi_weapon', level=2)]
        changes = self.apply(self.preview(specs))
        self.assertEqual([x['key'] for x in changes], ['quest:accessory', 'main', 'shaqi'])
        inv = self.doc['Inventory']
        self.assertEqual(inv['AccessoryInfos']['AC_Bleed']['CurrentLevel'], 0)
        self.assertEqual(inv['MainWeaponInfos']['Tang']['FirstAcquiredCycle'], 2)
        self.assertEqual(inv['MainWeaponInfos']['Tang']['AlreadySnapSoul'], 0)
        self.assertEqual(inv['ShaChiWeaponInfo']['Axe']['ShaChiWeaponLevel'], 2)
        self.assertFalse(inv['ShaChiWeaponInfo']['Axe']['AlreadySnapSoul'])
        self.assertEqual(inv['AccessoryIDs'], ['None'])
        self.assertEqual(set(self.doc), {'PBZGameSystem', 'Player', 'Inventory', 'Narrative'})

    def test_empty_selection_grants_nothing(self):
        old = copy.deepcopy(self.doc)
        self.assertEqual(self.apply(self.preview([reward()]), []), [])
        self.assertEqual(self.doc, old)

    def test_count_history_unknown_even_when_missing_or_acquired_this_cycle(self):
        spec = reward('count', storage='InventoryInfos', kind='item', item_id='I_01007', quantity=3)
        for record in (None, {'CurrentAmount': 0, 'MaxAmount': 8, 'AcquiredCycle': 2},
                       {'CurrentAmount': 5, 'MaxAmount': 8, 'AcquiredCycle': 2}):
            with self.subTest(record=record):
                self.doc = document()
                if record is not None:
                    self.doc['Inventory']['InventoryInfos']['I_01007'] = record
                row = self.preview([spec])[0]
                self.assertFalse(row['default_selected'])
                self.assertTrue(row['requires_confirmation'])
                self.assertEqual(row['grant_amount'], 3)
                before = copy.deepcopy(self.doc)
                with self.assertRaises(ValueError):
                    self.apply([row], ['count'])
                self.assertEqual(self.doc, before)

    def test_confirmed_count_adds_only_delta_and_preserves_history_and_custom_fields(self):
        self.doc['Inventory']['InventoryInfos']['I_01007'] = {
            'CurrentAmount': 5, 'MaxAmount': 10, 'FirstAddTime': 'old',
            'AcquiredCycle': 0, 'Extra': Decimal('1.00000000000000000001')}
        rows = self.preview([reward('count', storage='InventoryInfos', kind='item', item_id='I_01007', quantity=3)])
        changes = self.apply(rows, ['count'], True)
        record = self.doc['Inventory']['InventoryInfos']['I_01007']
        self.assertEqual((record['CurrentAmount'], record['MaxAmount']), (8, 10))
        self.assertEqual((record['FirstAddTime'], record['AcquiredCycle']), ('old', 0))
        self.assertEqual(record['Extra'], Decimal('1.00000000000000000001'))
        self.assertEqual((changes[0]['before'], changes[0]['after'], changes[0]['grant_amount']), (5, 8, 3))

    def test_unknown_and_random_cannot_be_granted_even_with_confirmation(self):
        for certainty in ('unknown', 'random'):
            with self.subTest(certainty=certainty):
                before = copy.deepcopy(self.doc)
                rows = self.preview([reward(certainty=certainty)])
                self.assertEqual(rows[0]['grant_amount'], 0)
                with self.assertRaises(ValueError):
                    self.apply(rows, ['quest:accessory'], True)
                self.assertEqual(self.doc, before)

    def test_receipt_keys_work_as_strings_and_prevent_automatic_regrant(self):
        spec = reward()
        for receipts in (['quest:accessory'], ('quest:accessory',), 'quest:accessory'):
            with self.subTest(receipts=receipts):
                row = self.preview([spec], receipts)[0]
                self.assertFalse(row['default_selected'])
                self.assertEqual(row['grant_amount'], 0)
                self.assertEqual(self.apply([row]), [])

    def test_receipt_of_other_task_or_cycle_does_not_prove_this_reward_granted(self):
        row = self.preview([reward('quest:cycle2:accessory')], ['quest:cycle1:accessory', 'other:accessory'])[0]
        self.assertTrue(row['default_selected'])

    def test_all_selected_rewards_are_validated_before_changing_document(self):
        good = reward()
        bad = reward('bad', storage='Player.Dust', item_id='Dust', kind='dust', quantity=2147483647)
        before = copy.deepcopy(self.doc)
        with self.assertRaises(ValueError):
            self.apply(self.preview([good, bad]), ['quest:accessory', 'bad'], True)
        self.assertEqual(self.doc, before)

    def test_unknown_selected_key_aborts_whole_batch(self):
        before = copy.deepcopy(self.doc)
        with self.assertRaises(ValueError):
            self.apply(self.preview([reward()]), ['quest:accessory', 'not-in-preview'], True)
        self.assertEqual(self.doc, before)

    def test_duplicate_keys_are_rejected_instead_of_granted_twice(self):
        before = copy.deepcopy(self.doc)
        with self.assertRaises(ValueError):
            self.apply(self.preview([reward(), reward()]), ['quest:accessory'], True)
        self.assertEqual(self.doc, before)
        rows = self.preview([reward()])
        self.apply(rows, ['quest:accessory', 'quest:accessory'])
        self.assertEqual(len(self.doc['Inventory']['AccessoryInfos']), 1)

    def test_repeated_targets_accumulate_and_check_combined_overflow_atomically(self):
        specs = [reward(k, storage='InventoryInfos', kind='item', item_id='I_01007', quantity=2)
                 for k in ('first', 'second')]
        self.doc['Inventory']['InventoryInfos']['I_01007'] = {'CurrentAmount': 2147483644, 'MaxAmount': 2147483644}
        before = copy.deepcopy(self.doc)
        with self.assertRaises(ValueError):
            self.apply(self.preview(specs), ['first', 'second'], True)
        self.assertEqual(self.doc, before)
        self.doc['Inventory']['InventoryInfos']['I_01007']['CurrentAmount'] = 1
        self.apply(self.preview(specs), ['first', 'second'], True)
        self.assertEqual(self.doc['Inventory']['InventoryInfos']['I_01007']['CurrentAmount'], 5)

    def test_invalid_integer_rewards_cannot_be_applied(self):
        for quantity in (-1, 0, True, 1.5, 2147483648):
            with self.subTest(quantity=quantity):
                old = copy.deepcopy(self.doc)
                rows = self.preview([reward(quantity=quantity)])
                with self.assertRaises(ValueError):
                    self.apply(rows, ['quest:accessory'], True)
                self.assertEqual(self.doc, old)

    def test_quantity_limit_allows_exact_upper_bound_but_rejects_negative_existing_amount(self):
        spec = reward('count', storage='InventoryInfos', kind='item', item_id='I_01007', quantity=2147483647)
        self.apply(self.preview([spec]), ['count'], True)
        self.assertEqual(self.doc['Inventory']['InventoryInfos']['I_01007']['CurrentAmount'], 2147483647)
        self.doc['Inventory']['InventoryInfos']['I_01007']['CurrentAmount'] = -1
        old = copy.deepcopy(self.doc)
        with self.assertRaises(ValueError):
            self.apply(self.preview([spec]), ['count'], True)
        self.assertEqual(self.doc, old)

    def test_new_medicine_uses_catalog_capacity_and_validates_before_any_grant(self):
        med = reward('medicine', storage='MedicineInfos', kind='medicine', item_id='Hulu', level=3, quantity=2)
        self.apply(self.preview([med]))
        record = self.doc['Inventory']['MedicineInfos']['Hulu']
        self.assertEqual((record['CurrentLevel'], record['CurrentTime'], record['MaxTime']), (3, 2, 3))
        self.assertEqual(record['BuffRemainTime'], 0)
        self.doc = document()
        bad = dict(med, item_id='not-a-medicine', key='bad')
        old = copy.deepcopy(self.doc)
        with self.assertRaises(ValueError):
            self.apply(self.preview([reward(), bad]), ['quest:accessory', 'bad'], True)
        self.assertEqual(self.doc, old)

    def test_medicine_does_not_silently_truncate_quantity_over_capacity(self):
        med = reward('medicine', storage='MedicineInfos', kind='medicine', item_id='Hulu', level=0, quantity=3)
        old = copy.deepcopy(self.doc)
        with self.assertRaises(ValueError):
            self.apply(self.preview([med]), ['medicine'], True)
        self.assertEqual(self.doc, old)

    def test_dust_requires_explicit_selection_and_confirmation(self):
        spec = reward('dust', storage='Player.Dust', item_id='Dust', kind='dust', quantity=7)
        row = self.preview([spec])[0]
        self.assertFalse(row['default_selected'])
        self.assertTrue(row['requires_confirmation'])
        self.assertEqual(self.apply([row]), [])
        with self.assertRaises(ValueError):
            self.apply([row], ['dust'])
        self.apply([row], ['dust'], True)
        self.assertEqual(self.doc['Player']['Dust'], 17)

    def test_decimal_dust_retains_fraction_and_precision_even_in_small_decimal_context(self):
        self.doc['Player']['Dust'] = Decimal('10.000000000000000000000000000001')
        spec = reward('dust', storage='Player.Dust', item_id='Dust', kind='dust', quantity=7)
        with localcontext() as ctx:
            ctx.prec = 6
            self.apply(self.preview([spec]), ['dust'], True)
        self.assertIsInstance(self.doc['Player']['Dust'], Decimal)
        self.assertEqual(self.doc['Player']['Dust'], Decimal('17.000000000000000000000000000001'))

    def test_dust_missing_or_unconfirmed_type_is_never_created_or_coerced(self):
        spec = reward('dust', storage='Player.Dust', item_id='Dust', kind='dust', quantity=7)
        for player in ({}, None, {'Dust': True}, {'Dust': '10'}, {'Dust': 1.5}, {'Dust': Decimal('NaN')},
                       {'Dust': Decimal('Infinity')}, {'Dust': -1}):
            with self.subTest(player=player):
                self.doc = document()
                if player is None:
                    del self.doc['Player']
                else:
                    self.doc['Player'] = player
                old = copy.deepcopy(self.doc)
                with self.assertRaises(ValueError):
                    self.apply(self.preview([spec]), ['dust'], True)
                self.assertEqual(repr(self.doc), repr(old))

    def test_stale_preview_is_rechecked_against_document(self):
        rows = self.preview([reward()])
        self.doc['Inventory']['AccessoryInfos']['AC_Bleed'] = {'CurrentLevel': 8, 'Custom': 'keep'}
        old = copy.deepcopy(self.doc)
        self.assertEqual(self.apply(rows), [])
        self.assertEqual(self.doc, old)

    def test_rebuilt_preview_replaces_old_derived_validation_results(self):
        spec = reward('dust', storage='Player.Dust', item_id='Dust', kind='dust', quantity=7)
        self.doc['Player'] = {}
        old_rows = self.preview([spec])
        self.assertFalse(old_rows[0]['can_grant'])
        self.doc['Player']['Dust'] = 10
        rows = self.preview(old_rows)
        self.assertTrue(rows[0]['can_grant'])
        self.apply(rows, ['dust'], True)
        self.assertEqual(self.doc['Player']['Dust'], 17)

    def test_empty_selection_is_noop_even_with_unselectable_duplicate_preview_rows(self):
        old = copy.deepcopy(self.doc)
        rows = self.preview([reward(certainty='unknown'), reward(certainty='unknown')])
        self.assertEqual(self.apply(rows, []), [])
        self.assertEqual(self.doc, old)


if __name__ == '__main__':
    unittest.main()
