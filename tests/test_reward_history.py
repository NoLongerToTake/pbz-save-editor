"""收据只在项目内临时夹具写入，绝不调用默认用户配置路径。"""
from decimal import Decimal
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BEFORE, AFTER, OTHER = 'a' * 64, 'b' * 64, 'c' * 64


def operations():
    return [{'slot': 'ManualSave1', 'quest_id': '/Game/fixture.Quest_C',
             'reward_keys': ['quest:cycle2:first', 'quest:cycle2:second']},
            {'slot': 'AutoSave0', 'quest_id': '/Game/other.Quest_C', 'reward_keys': ['other:first']}]


class RewardHistoryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('pbz_editor.reward_history'),
                             '保存指纹关联的外部收据尚未实现')
        self.api = importlib.import_module('pbz_editor.reward_history')
        self.temp = tempfile.TemporaryDirectory(prefix='reward-fixture-', dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'custom-history'
        self.history = self.api.RewardHistory(self.base)

    def test_read_missing_history_does_not_create_files_or_claim_reward_unreceived(self):
        self.assertEqual(self.history.receipts(AFTER, 'ManualSave1'), [])
        self.assertFalse(self.base.exists())

    def test_pending_receipt_matches_only_after_fingerprint_and_exact_slot(self):
        path = self.history.prepare(BEFORE, AFTER, operations())
        self.assertTrue(path.is_file())
        self.assertTrue(path.resolve().is_relative_to(self.base.resolve()))
        self.assertEqual(self.history.receipts(AFTER, 'ManualSave1'), ['quest:cycle2:first', 'quest:cycle2:second'])
        self.assertEqual(self.history.receipts(AFTER, 'AutoSave0'), ['other:first'])
        self.assertEqual(self.history.receipts(BEFORE, 'ManualSave1'), [])
        self.assertEqual(self.history.receipts(OTHER, 'ManualSave1'), [])
        self.assertEqual(self.history.receipts(AFTER, 'ManualSave10'), [])
        raw = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual((raw['before_sha256'], raw['after_sha256']), (BEFORE, AFTER))

    def test_finish_and_reload_preserve_matching_pending_results(self):
        path = self.history.prepare(BEFORE, AFTER, operations())
        self.history.finish(path)
        restored = self.api.RewardHistory(self.base)
        self.assertEqual(restored.receipts(AFTER, 'ManualSave1'), ['quest:cycle2:first', 'quest:cycle2:second'])
        self.history.finish(path)  # repeated confirmation is safe
        self.assertEqual(restored.receipts(BEFORE, 'ManualSave1'), [])

    def test_failed_finish_leaves_recoverable_receipt_for_already_written_save(self):
        save = self.root / 'fixture.sav'
        original, saved = b'original fixture', b'already written save fixture'
        save.write_bytes(original)
        before, after = hashlib.sha256(original).hexdigest(), hashlib.sha256(saved).hexdigest()
        path = self.history.prepare(before, after, operations())
        self.assertEqual(self.history.receipts(hashlib.sha256(save.read_bytes()).hexdigest(), 'ManualSave1'), [])
        save.write_bytes(saved)
        with patch.object(self.api.os, 'replace', side_effect=PermissionError('receipt locked')):
            with self.assertRaises(PermissionError):
                self.history.finish(path)
        restored = self.api.RewardHistory(self.base)
        self.assertEqual(restored.receipts(hashlib.sha256(save.read_bytes()).hexdigest(), 'ManualSave1'),
                         ['quest:cycle2:first', 'quest:cycle2:second'])
        self.assertEqual(save.read_bytes(), saved)
        restored.finish(path)
        self.assertEqual(restored.receipts(after, 'AutoSave0'), ['other:first'])

    def test_prepare_and_finish_never_write_outside_explicit_base(self):
        sentinel = self.root / 'unrelated.json'
        sentinel.write_text('{"keep":true}', encoding='utf-8')
        self.history.prepare(BEFORE, AFTER, operations())
        with self.assertRaises(ValueError):
            self.history.finish(sentinel)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), '{"keep":true}')
        self.assertEqual({p.name for p in self.root.iterdir()}, {'unrelated.json', 'custom-history'})

    def test_invalid_prepare_arguments_fail_before_creating_history(self):
        for before, after, ops in [('bad', AFTER, operations()), (BEFORE, 'bad', operations()),
                                   (BEFORE, AFTER, [{'slot': 'ManualSave1', 'quest_id': 'q', 'reward_keys': [7]}]),
                                   (BEFORE, AFTER, [{'quest_id': 'q', 'reward_keys': ['key']}])]:
            with self.subTest(before=before, after=after, ops=ops):
                with self.assertRaises((ValueError, TypeError)):
                    self.history.prepare(before, after, ops)
                self.assertFalse(self.base.exists())

    def test_str_reward_keys_are_whole_keys_and_exact_task_cycle_identity_survives(self):
        ops = [{'slot': 'ManualSave1', 'quest_id': 'q', 'reward_keys': 'quest:cycle2:first'}]
        self.history.prepare(BEFORE, AFTER, ops)
        self.assertEqual(self.history.receipts(AFTER, 'ManualSave1'), ['quest:cycle2:first'])

    def test_multiple_receipts_deduplicate_keys_and_ignore_other_fingerprints(self):
        self.history.prepare(BEFORE, AFTER, operations())
        self.history.prepare(OTHER, AFTER, operations())
        self.history.prepare(AFTER, OTHER, [{'slot': 'ManualSave1', 'quest_id': 'q', 'reward_keys': ['wrong']}])
        self.assertEqual(self.history.receipts(AFTER, 'ManualSave1'), ['quest:cycle2:first', 'quest:cycle2:second'])

    def test_custom_base_cannot_finish_another_histories_receipt(self):
        path = self.api.RewardHistory(self.root / 'second-history').prepare(BEFORE, AFTER, operations())
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.history.finish(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.base.exists())

    def test_decimal_operation_details_do_not_block_receipt_preparation(self):
        ops = operations()
        ops[0]['changes'] = [{'before': Decimal('1.000000000000000000001'), 'after': Decimal('2.000000000000000000001')}]
        path = self.history.prepare(BEFORE, AFTER, ops)
        self.assertIn('2.000000000000000000001', path.read_text(encoding='utf-8'))
        self.assertEqual(self.history.receipts(AFTER, 'ManualSave1'), ['quest:cycle2:first', 'quest:cycle2:second'])

    def test_unrelated_json_is_not_treated_as_a_receipt(self):
        self.base.mkdir()
        (self.base / 'unrelated.json').write_text('{"reward_keys":["forged"]}', encoding='utf-8')
        self.assertEqual(self.history.receipts(AFTER, 'ManualSave1'), [])


if __name__ == '__main__':
    unittest.main()
