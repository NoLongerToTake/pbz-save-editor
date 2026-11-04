"""Quest review regressions, using synthetic saves and local receipts only."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pbz_editor import format as fmt
from pbz_editor.core import SaveEditor, sparse_patch
from pbz_editor.quest_catalog import QuestCatalog
from pbz_editor.reward_history import RewardHistory
from test_core import SLOT, CatalogStub, archive
from test_quests_core import NEW, OTHER, Q, QuestStub


class QuestReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quest-review-', dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'fixture.sav'
        self.path.write_bytes(archive({'ManualSave1': SLOT}))
        self.editor = SaveEditor.open(self.path, CatalogStub(), quest_catalog=QuestStub())
        self.spec = self.editor.quest_catalog.find(Q)

    def _editor_with_retained_quest(self, extra_records=()):
        first = json.dumps(dict(QuestClass=Q, QuestCompletion='EQuestCompletion::QC_Started',
                                QuestBranches=[], CompletedBranches=[], ReachedStateNames=[]))
        retained = ('{ "QuestClass" : ' + json.dumps(OTHER) + ', "QuestBranches" : [], '
                    '"QuestCompletion" : "EQuestCompletion::QC_Started", "Value" : 1e+02 }')
        records = [first, retained] + [json.dumps(record) for record in extra_records]
        narrative = '{"NarrativeSavedQuests":[' + ',\n'.join(records) + ']}'
        raw = SLOT.replace(b'"Narrative":{"Unrelated":true}', b'"Narrative":' + narrative.encode())
        self.path.write_bytes(archive({'ManualSave1': raw, 'AutoSave0': raw}))
        return SaveEditor.open(self.path, CatalogStub(), quest_catalog=QuestStub()), retained

    def test_complete_excludes_stage_rewards_outside_selected_success_branches(self):
        base = self.spec['rewards'][0]
        self.spec['rewards'] = [
            dict(base, key='success-first', stage_id='1', route_ids=[], category='stage'),
            dict(base, key='success-last', item_id='AC_Last', stage_id='2', route_ids=[], category='stage'),
            dict(base, key='failure-stage', item_id='AC_Failure', stage_id='S1', route_ids=[], category='stage'),
        ]
        partial = self.editor.preview_quest('ManualSave1', Q, 'stage', route_id='good', stage_id='1')
        self.assertEqual([r['key'] for r in partial['rewards']], ['success-first'])
        preview = self.editor.preview_quest('ManualSave1', Q, 'complete', route_id='good')
        self.assertEqual([r['key'] for r in preview['rewards']], ['success-first', 'success-last'])
        self.editor.apply_quest(preview)
        document = self.editor._documents['ManualSave1']
        self.assertEqual(document['Narrative']['NarrativeSavedQuests'][0]['CompletedBranches'], ['1', '2'])
        self.assertEqual(set(document['Inventory']['AccessoryInfos']), {'AC_Attack', 'AC_Drink', 'AC_Last'})

    def test_final_stage_offers_route_only_final_reward_but_not_intermediate_stage(self):
        self.spec['rewards'] = [dict(self.spec['rewards'][0], key='route-final', stage_id=None)]
        partial = self.editor.preview_quest('ManualSave1', Q, 'stage', route_id='good', stage_id='1')
        self.assertEqual(partial['rewards'], [])
        self.editor.apply_quest(partial)
        self.assertNotIn('AC_Drink', self.editor._documents['ManualSave1']['Inventory']['AccessoryInfos'])
        preview = self.editor.preview_quest('ManualSave1', Q, 'stage', route_id='good', stage_id='2')
        self.assertEqual([r['key'] for r in preview['rewards']], ['route-final'])
        self.assertTrue(preview['rewards'][0]['default_selected'])
        self.editor.apply_quest(preview)
        document = self.editor._documents['ManualSave1']
        self.assertEqual(document['Narrative']['NarrativeSavedQuests'][0]['QuestCompletion'],
                         'EQuestCompletion::QC_Succeded')
        self.assertIn('AC_Drink', document['Inventory']['AccessoryInfos'])

    def test_rewards_selected_physician_stage_filters_without_changing_quest_document(self):
        quest_id = '/Game/PBZstory/PBZQuest/Simple/JianXiYiShi.JianXiYiShi_C'
        record = dict(QuestClass=quest_id, QuestCompletion='EQuestCompletion::QC_Started',
                      CompletedBranches=['1'], ReachedStateNames=[], QuestBranches=[
                          dict(BranchID='1', BranchType='EBranchType::BranchCompleted'),
                          dict(BranchID='2', BranchType='EBranchType::BranchActivating')])
        editor, _ = self._editor_with_retained_quest([record])
        editor.quest_catalog = QuestCatalog()
        before = copy.deepcopy(editor._documents['ManualSave1'])
        partial = editor.preview_quest('ManualSave1', quest_id, 'rewards', stage_id='1')
        self.assertNotIn('AC_Drink', [r['item_id'] for r in partial['rewards']])
        self.assertEqual(partial['changes'], [])
        self.assertEqual(partial['_document'], before)
        editor.apply_quest(partial)
        self.assertEqual(editor._documents['ManualSave1'], before)

        final = editor.preview_quest('ManualSave1', quest_id, 'rewards', stage_id='2')
        self.assertEqual([r['item_id'] for r in final['rewards']], ['AC_Drink'])
        self.assertTrue(final['rewards'][0]['default_selected'])
        self.assertEqual(final['changes'], [])
        self.assertEqual(final['_document'], before)
        whole_route = editor.preview_quest('ManualSave1', quest_id, 'rewards')
        self.assertEqual([r['item_id'] for r in whole_route['rewards']], ['AC_Drink'])
        editor.apply_quest(final)
        after = editor._documents['ManualSave1']
        self.assertIn('AC_Drink', after['Inventory']['AccessoryInfos'])
        self.assertEqual({k: v for k, v in after.items() if k != 'Inventory'},
                         {k: v for k, v in before.items() if k != 'Inventory'})

    def test_rewards_selected_stage_uses_prefix_and_final_reward_eligibility(self):
        editor = self.editor
        spec = editor.quest_catalog.find(Q)
        base = spec['rewards'][0]
        spec['rewards'] = [
            dict(base, key='first', stage_id='1', category='stage'),
            dict(base, key='last', stage_id='2', category='stage'),
            dict(base, key='failed', stage_id='S1', route_ids=[], category='stage'),
            dict(base, key='route-final', stage_id=None),
        ]
        before = copy.deepcopy(editor._documents['ManualSave1'])
        for stage_id, expected in [('1', ['first']), ('2', ['first', 'last', 'route-final'])]:
            with self.subTest(stage_id=stage_id):
                preview = editor.preview_quest('ManualSave1', Q, 'rewards', route_id='good', stage_id=stage_id)
                self.assertEqual([r['key'] for r in preview['rewards']], expected)
                self.assertEqual(preview['_document'], before)
                self.assertEqual(preview['changes'], [])

    def test_rewards_selected_stage_rejects_invalid_or_unverified_stage(self):
        before = copy.deepcopy(self.editor._documents)
        for stage_id in ('', 'S1', 'missing'):
            with self.subTest(stage_id=stage_id):
                with self.assertRaisesRegex(ValueError, '阶段'):
                    self.editor.preview_quest('ManualSave1', Q, 'rewards', route_id='good', stage_id=stage_id)
        self.spec['routes'][0]['stage_order_verified'] = False
        preview = self.editor.preview_quest('ManualSave1', Q, 'rewards', route_id='good', stage_id='1')
        self.assertFalse(preview['can_apply'])
        self.assertEqual(preview['rewards'], [])
        self.assertTrue(any('阶段先后尚未核实' in warning for warning in preview['warnings']))
        with self.assertRaisesRegex(ValueError, '尚未核实'):
            self.editor.apply_quest(preview)
        self.assertEqual(self.editor._documents, before)

    def test_equal_length_quest_id_replacement_preserves_retained_record_text(self):
        retained = '{ "QuestClass" : "B", "Value" : 1e+02, "Extra" : true }'
        text = '{"Narrative":{"NarrativeSavedQuests":[{"QuestClass":"A","Value":1},\n' + retained + ']}}'
        old = fmt.DECODER.decode(text)
        new = copy.deepcopy(old)
        records = new['Narrative']['NarrativeSavedQuests']
        records.pop(0)
        records.append({'QuestClass': 'C', 'Value': 3})
        result = sparse_patch(text, old, new)
        self.assertEqual(fmt.DECODER.decode(result), new)
        self.assertIn(retained, result)

    def test_task_reordering_preserves_unedited_record_text(self):
        first = '{"QuestClass":"A","Value":1}'
        retained = '{ "QuestClass" : "B", "Value" : 1e+02 }'
        text = '{"NarrativeSavedQuests":[' + first + ',\n' + retained + ']}'
        old = fmt.DECODER.decode(text)
        new = copy.deepcopy(old)
        new['NarrativeSavedQuests'].reverse()
        result = sparse_patch(text, old, new)
        self.assertEqual(fmt.DECODER.decode(result), new)
        self.assertIn(first, result)
        self.assertIn(retained, result)

    def test_moved_existing_record_uses_updated_original_fragment(self):
        original = '{ "QuestClass" : "B", "Value" : 1e+02, "Changed" : false }'
        text = '{"NarrativeSavedQuests":[{"QuestClass":"A"},' + original + ']}'
        old = fmt.DECODER.decode(text)
        new = copy.deepcopy(old)
        new['NarrativeSavedQuests'].reverse()
        new['NarrativeSavedQuests'][0]['Changed'] = True
        result = sparse_patch(text, old, new)
        self.assertEqual(fmt.DECODER.decode(result), new)
        self.assertIn('{ "QuestClass" : "B", "Value" : 1e+02, "Changed" : true }', result)

    def test_reset_then_complete_builds_reordered_tasks_without_rewriting_other_quest(self):
        editor, retained = self._editor_with_retained_quest()
        editor.apply_quest(editor.preview_quest('ManualSave1', Q, 'reset'), selected_rewards=[])
        editor.apply_quest(editor.preview_quest('ManualSave1', Q, 'complete', route_id='good'), selected_rewards=[])
        entries, _ = fmt.read_archive(editor.build())
        raw = fmt.decode_payload(entries[0], ROOT / 'bin/1031_pbz_decoder_v1.exe').decode()
        records = fmt.DECODER.decode(raw)['Narrative']['NarrativeSavedQuests']
        self.assertEqual([record['QuestClass'] for record in records], [OTHER, Q])
        self.assertEqual(records[1]['QuestCompletion'], 'EQuestCompletion::QC_Succeded')
        self.assertIn(retained, raw)
        self.assertEqual(entries[1]['block'], editor._entries[1]['block'])

    def test_reset_add_complete_stage_remove_builds_moved_and_new_tasks(self):
        removed = '/Game/PBZstory/PBZQuest/Simple/Removed.Removed_C'
        editor, retained = self._editor_with_retained_quest([dict(QuestClass=removed, QuestBranches=[])])
        for quest_id, action, options in [
            (Q, 'reset', {}),
            (NEW, 'complete', dict(route_id='good')),
            (Q, 'complete', dict(route_id='good')),
            (Q, 'stage', dict(route_id='good', stage_id='1')),
            (removed, 'reset', {}),
        ]:
            editor.apply_quest(editor.preview_quest('ManualSave1', quest_id, action, **options), selected_rewards=[])
        entries, _ = fmt.read_archive(editor.build())
        raw = fmt.decode_payload(entries[0], ROOT / 'bin/1031_pbz_decoder_v1.exe').decode()
        records = fmt.DECODER.decode(raw)['Narrative']['NarrativeSavedQuests']
        self.assertEqual([record['QuestClass'] for record in records], [OTHER, NEW, Q])
        self.assertEqual(records[2]['QuestCompletion'], 'EQuestCompletion::QC_Started')
        self.assertEqual(records[2]['CompletedBranches'], ['1'])
        self.assertIn(retained, raw)
        self.assertEqual(entries[1]['block'], editor._entries[1]['block'])

    def test_receipt_reload_expires_preview_and_keeps_staged_reward_keys(self):
        accessory_key = self.spec['rewards'][0]['key']
        self.spec['rewards'].append(dict(
            key='fixed-three-items', item_id='I_01007', name='金创草', storage='InventoryInfos',
            kind='item', quantity=3, level=0, stage_id='2', route_ids=['good'],
            certainty='verified', category='quest_final', source='test evidence'))
        self.editor.apply_quest(self.editor.preview_quest('ManualSave1', Q, 'complete', route_id='good'))
        preview = self.editor.preview_quest('ManualSave1', Q, 'rewards', route_id='good')
        item = next(r for r in preview['rewards'] if r['key'] == 'fixed-three-items')
        self.assertTrue(item['requires_confirmation'])
        self.assertEqual(item['grant_amount'], 3)
        before_document = copy.deepcopy(self.editor._documents)
        before_operations = copy.deepcopy(self.editor.reward_operations)
        before_revision = self.editor.revision

        history_dir = self.root / 'receipts'
        RewardHistory(history_dir).prepare('a' * 64, self.editor.source_sha256, [
            dict(slot='ManualSave1', quest_id=Q, reward_keys=['fixed-three-items'])])
        self.editor.load_reward_history(history_dir)
        self.assertEqual(self.editor._reward_receipts['ManualSave1'], {accessory_key, 'fixed-three-items'})
        self.assertEqual(self.editor.reward_operations, before_operations)
        with self.assertRaisesRegex(ValueError, '过期'):
            self.editor.apply_quest(preview, selected_rewards=['fixed-three-items'], allow_uncertain=True)
        self.assertEqual(self.editor.revision, before_revision + 1)
        self.assertEqual(self.editor._documents, before_document)

        fresh = self.editor.preview_quest('ManualSave1', Q, 'rewards', route_id='good')
        item = next(r for r in fresh['rewards'] if r['key'] == 'fixed-three-items')
        self.assertTrue(item['receipt_confirmed'])
        self.assertFalse(item['can_grant'])
        self.editor.load_reward_history(history_dir)
        self.assertEqual(self.editor.revision, fresh['revision'])
        self.editor.apply_quest(fresh, selected_rewards=['fixed-three-items'], allow_uncertain=True)
        self.assertEqual(self.editor._documents, before_document)
        self.assertEqual(self.editor.reward_operations, before_operations)

    def test_undo_keeps_loaded_receipts_and_rebuilds_remaining_reward_keys(self):
        accessory_key = self.spec['rewards'][0]['key']
        item_key = 'fixed-three-items'
        self.spec['rewards'].append(dict(
            key=item_key, item_id='I_01007', name='金创草', storage='InventoryInfos',
            kind='item', quantity=3, level=0, stage_id='2', route_ids=['good'],
            certainty='verified', category='quest_final', source='test evidence'))
        self.editor.apply_quest(self.editor.preview_quest('ManualSave1', Q, 'complete', route_id='good'),
                                selected_rewards=[accessory_key])
        before_edit = copy.deepcopy(self.editor._documents)
        remaining_operations = copy.deepcopy(self.editor.reward_operations)
        self.editor.set_item('ManualSave1', 'I_01007', 'InventoryInfos', quantity=2)

        history_dir = self.root / 'receipts'
        RewardHistory(history_dir).prepare('a' * 64, self.editor.source_sha256, [
            dict(slot='ManualSave1', quest_id=Q, reward_keys=[item_key])])
        self.editor.load_reward_history(history_dir)
        self.editor.undo()
        preview = self.editor.preview_quest('ManualSave1', Q, 'rewards', route_id='good')
        self.editor.apply_quest(preview, selected_rewards=[item_key], allow_uncertain=True)
        self.assertEqual(self.editor._documents['ManualSave1']['Inventory']['InventoryInfos']
                         ['I_01007']['CurrentAmount'], 1)
        self.assertEqual(self.editor._documents, before_edit)
        self.assertEqual(self.editor.reward_operations, remaining_operations)
        self.assertEqual(self.editor._loaded_reward_receipts['ManualSave1'], {item_key})
        self.assertEqual(self.editor._reward_receipts['ManualSave1'], {item_key, accessory_key})
        self.assertTrue(next(r for r in preview['rewards'] if r['key'] == item_key)['receipt_confirmed'])

        self.editor.undo()
        self.assertNotIn('AC_Drink', self.editor._documents['ManualSave1']['Inventory']['AccessoryInfos'])
        self.assertEqual(self.editor.reward_operations, [])
        self.assertEqual(self.editor._reward_receipts['ManualSave1'], {item_key})
        preview = self.editor.preview_quest('ManualSave1', Q, 'rewards', route_id='good')
        rows = {row['key']: row for row in preview['rewards']}
        self.assertTrue(rows[item_key]['receipt_confirmed'])
        self.assertFalse(rows[item_key]['can_grant'])
        self.assertFalse(rows[accessory_key]['receipt_confirmed'])
        self.assertTrue(rows[accessory_key]['can_grant'])


if __name__ == '__main__':
    unittest.main()
