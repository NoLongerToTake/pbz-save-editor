"""任务目录：独立核对本机图、原生路线与奖励阶段，不启动窗口。"""
import copy
import importlib.util
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def quest_id(stem, folder='Simple'):
    return f'/Game/PBZstory/PBZQuest/{folder}/{stem}.{stem}_C'


def graph_fixture():
    # FPackageIndex=export_index+1; deliberately different from BranchID.
    return [
        dict(export_index=0, object_name='Default__Example_C', properties={
            'FlowLocTextQuestName': {'Key': 'title'}, 'QuestType': 'News'}),
        dict(export_index=3, object_name='PBZQuestBranch_17', properties={
            'ID': 'A', 'DestinationState': 9, 'QuestTasks': [2],
            'FlowLocTarget': {'Key': 'branch'}, 'BelongToWorld': 'XiGu'}),
        dict(export_index=1, object_name='BPT_HasTag_C_0', properties={'Tags': ['quest.a']}),
        dict(export_index=6, object_name='PBZQuestState_0', properties={
            'ID': 'start', 'Branches': [4, 10]}),
        dict(export_index=8, object_name='PBZQuestState_3', properties={
            'ID': 'done', 'StateNodeType': 'Success'}),
        dict(export_index=9, object_name='PBZQuestBranch_1', properties={
            'ID': 'optional'}),
        dict(export_index=10, object_name='QuestTemplate', properties={
            'QuestStartState': 7, 'States': [7, 9], 'Branches': [4, 10]}),
    ]


class QuestBuildTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'tools/build_quests.py'
        self.assertTrue(path.is_file(), '任务目录构建器尚未实现')
        spec = importlib.util.spec_from_file_location('test_build_quests', path)
        self.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.builder)

    def example(self):
        return self.builder.build_quest(
            'ProjectPBZ/Content/PBZstory/PBZQuest/Simple/Example.uasset',
            graph_fixture(), {('Default', 'title'): '示例任务', ('Default', 'branch'): '采药'})

    def test_package_indices_resolve_to_native_ids_and_tasks(self):
        q = self.example()
        self.assertEqual(q['id'], quest_id('Example'))
        self.assertEqual(q['name'], '示例任务')
        self.assertEqual(q['start_state_id'], 'start')
        self.assertEqual(q['branches'][0]['id'], 'A')
        self.assertEqual(q['branches'][0]['name'], '采药')
        self.assertEqual(q['branches'][0]['destination_state_id'], 'done')
        self.assertEqual(q['branches'][0]['task_schema']['count'], 1)
        self.assertEqual(q['branches'][0]['task_schema']['tasks'][0]['properties']['Tags'], ['quest.a'])
        self.assertEqual(q['states'][0]['branches'], ['A', 'optional'])
        self.assertEqual(q['states'][1]['completion'], 'EQuestCompletion::QC_Succeded')
        self.assertEqual(q['routes'], [], '图里出现分支不等于观察到了成功路线')

    def test_unresolved_positive_reference_is_not_silently_a_branch_id(self):
        exports = graph_fixture()
        exports[3]['properties']['Branches'] = [99]
        with self.assertRaisesRegex(ValueError, '99'):
            self.builder.build_quest('ProjectPBZ/Content/PBZstory/PBZQuest/Simple/Example.uasset', exports, {})

    def test_blueprint_default_uses_derived_schema_before_base_fields(self):
        # SanMianHunDun/XiaoCui/YongShengDaoZhu have one extra derived field.
        from tools.build_catalog import Reader
        name = b'Default__Example_C'
        export_offset = 86 + len(name)
        serial_offset = export_offset + 72
        raw = struct.pack('<HiI', 0x0301, 42, 0)  # skip subclass field, read base ID
        data = bytearray(serial_offset) + raw
        header = [0] * 15
        header[1], header[8], header[9] = serial_offset, export_offset, serial_offset
        struct.pack_into('<15I', data, 0, *header)
        struct.pack_into('<I', data, 60, 1)
        data[84:86] = bytes([0, len(name)])
        data[86:86 + len(name)] = name
        struct.pack_into('<QQII', data, export_offset, 0, len(raw), 0, 0)
        prop = lambda name: SimpleNamespace(index=0, array_dim=1, name=name, type=('Int',))
        schemas = {'PBZQuest': SimpleNamespace(props=[prop('ID')], super=None),
                   'Example_C': SimpleNamespace(props=[prop('UberGraphFrame')], super='PBZQuest')}
        try:
            exports = self.builder.package_exports(bytes(data), Reader, schemas, {})
        except IndexError:
            self.fail('CDO 错用 PBZQuest 基类 schema，漏掉派生蓝图字段偏移')
        self.assertEqual(exports[0]['properties'], {'ID': 42})

    def test_native_success_set_and_zero_progress_are_preserved(self):
        q = self.example()
        record = dict(QuestClass=q['id'], QuestCompletion='EQuestCompletion::QC_Succeded',
                      QuestBranches=[dict(BranchID='A', BranchType='EBranchType::BranchCompleted',
                                          TasksProgress=[0], DirtyTime='0001.01.01-00.00.00'),
                                     dict(BranchID='optional', BranchType='EBranchType::None')],
                      CompletedBranches=['A'], ReachedStateNames=[])
        original = copy.deepcopy(record)
        self.builder.add_observation(q, record, {'path': 'native/AutoSave0.json', 'sha256': 'a' * 64})
        self.assertEqual(q['routes'][0]['branches'], ['A'])
        self.assertEqual(q['routes'][0]['template']['QuestBranches'][0]['TasksProgress'], [0])
        self.assertEqual(q['template'], original)
        self.assertEqual(record, original)
        self.builder.add_observation(q, record, {'path': 'native/AutoSave1.json', 'sha256': 'b' * 64})
        self.assertEqual(len(q['routes']), 1)
        self.assertEqual(len(q['routes'][0]['source']['observations']), 2)

    def test_success_set_permutations_are_one_route_without_invented_stage_order(self):
        q = self.example()
        record = dict(QuestClass=q['id'], QuestCompletion='EQuestCompletion::QC_Succeded',
                      QuestBranches=[dict(BranchID='A', BranchType='EBranchType::BranchCompleted', TasksProgress=[0]),
                                     dict(BranchID='optional', BranchType='EBranchType::BranchCompleted')],
                      CompletedBranches=['optional', 'A'], ReachedStateNames=[])
        self.builder.add_observation(q, record, {'path': 'native/first.json'})
        record['CompletedBranches'] = ['A', 'optional']
        self.builder.add_observation(q, record, {'path': 'native/second.json'})
        self.assertEqual(len(q['routes']), 1, 'Set 不同排列不能变成不同成功路线')
        self.assertFalse(q['routes'][0]['stage_order_verified'])
        self.assertEqual(q['routes'][0]['stage_order'], [])

    def test_linear_graph_verifies_order_independent_of_success_set_order(self):
        q = self.example()
        q['states'][0]['branches'] = ['A']
        q['states'].append(dict(id='middle', branches=['optional'], completion='EQuestCompletion::QC_Started'))
        q['branches'][0]['destination_state_id'] = 'middle'
        q['branches'][1]['destination_state_id'] = 'done'
        record = dict(QuestClass=q['id'], QuestCompletion='EQuestCompletion::QC_Succeded',
                      QuestBranches=[dict(BranchID='A', BranchType='EBranchType::BranchCompleted', TasksProgress=[0]),
                                     dict(BranchID='optional', BranchType='EBranchType::BranchCompleted')],
                      CompletedBranches=['optional', 'A'], ReachedStateNames=[])
        self.builder.add_observation(q, record, {'path': 'native/first.json'})
        route = q['routes'][0]
        self.assertTrue(route['stage_order_verified'])
        self.assertEqual(route['stage_order'], ['A', 'optional'])
        self.assertEqual(route['stage_order_source']['kind'], 'linear_state_graph')

    def test_failed_record_does_not_create_a_success_route(self):
        q = self.example()
        record = dict(QuestClass=q['id'], QuestCompletion='EQuestCompletion::QC_Failed',
                      QuestBranches=[], CompletedBranches=['A'], ReachedStateNames=[])
        self.builder.add_observation(q, record, {'path': 'native/failed.json'})
        self.assertEqual(q['routes'], [])

    def bumie_native_history(self):
        payload = json.loads((ROOT / 'data/quests.json').read_text(encoding='utf-8'))
        q = next(q for q in payload['quests'] if q['id'] == quest_id('BuMieXiaDan'))
        record = copy.deepcopy(q['routes'][0]['template'])
        source = copy.deepcopy(q['routes'][0]['source']['observations'][0])
        return q, record, source

    def test_bumie_reviewed_history_orders_stages_independently_of_both_arrays(self):
        q, record, source = self.bumie_native_history()
        record['CompletedBranches'] = ['1', 'S1']
        record['QuestBranches'].reverse()
        original = copy.deepcopy(record)
        result = self.builder.stage_order_evidence(q, record, source)
        self.assertTrue(result['stage_order_verified'])
        self.assertEqual(result['stage_order'], ['S1', '1'])
        self.assertEqual(result['stage_order_source']['kind'], 'native_branch_dirty_time')
        self.assertEqual(result['stage_order_source']['reviewed_evidence']['path'],
                         'docs/1104_不灭侠胆证据_v2.json')
        self.assertEqual(result['stage_order_source']['branch_dirty_times'],
                         {'S1': '2026.10.31-00.57.30', '1': '2026.10.31-01.06.40'})
        self.assertEqual(record, original)

    def test_bumie_unreviewed_or_invalid_history_does_not_enable_stage_jumps(self):
        for case in ('missing_time', 'same_time', 'reverse_time', 'incomplete_branch', 'subset', 'failed'):
            with self.subTest(case=case):
                q, record, source = self.bumie_native_history()
                if case == 'missing_time':
                    record['QuestBranches'][0].pop('DirtyTime')
                elif case == 'same_time':
                    record['QuestBranches'][1]['DirtyTime'] = record['QuestBranches'][0]['DirtyTime']
                elif case == 'reverse_time':
                    record['QuestBranches'][0]['DirtyTime'] = '2026.10.31-02.00.00'
                elif case == 'incomplete_branch':
                    record['QuestBranches'][0]['BranchType'] = 'EBranchType::BranchStarted'
                elif case == 'subset':
                    record['CompletedBranches'] = ['1']
                else:
                    record['QuestCompletion'] = 'EQuestCompletion::QC_Failed'
                result = self.builder.stage_order_evidence(q, record, source)
                self.assertFalse(result['stage_order_verified'])
                self.assertEqual(result['stage_order'], [])


class QuestCatalogTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'pbz_editor/quest_catalog.py').is_file(), '自包含任务目录尚未实现')
        from pbz_editor.quest_catalog import QuestCatalog
        self.catalog_type = QuestCatalog
        self.catalog = QuestCatalog()

    def test_full_class_lookup_does_not_match_basename(self):
        q = self.catalog.find(quest_id('JianXiYiShi'))
        self.assertEqual(q['name'], '见习医师')
        self.assertIsNone(self.catalog.find('JianXiYiShi_C'))
        self.assertIsNone(self.catalog.find('/missing'))

    def test_all_local_production_subquest_folders_are_present(self):
        counts = {folder: sum(f'/{folder}/' in q['id'] for q in self.catalog.quests)
                  for folder in ('Simple', 'Collect', 'Branch', 'Quest_Branchline')}
        self.assertEqual(counts, dict(Simple=36, Collect=10, Branch=15, Quest_Branchline=1))
        for q in self.catalog.quests:
            self.assertFalse(any(f'/{folder}/' in q['id'] for folder in ('Main', 'Tutorial', 'Demo')))
            self.assertTrue(q['branches'])
            self.assertTrue(q['source']['sha256'])
        for stem in ('QiShengHeLai', 'SanMianHunDun', 'XueRouHua', 'XiGuShenChu'):
            self.assertIsNotNone(self.catalog.find(quest_id(stem, 'Branch')))

    def test_nanyan_success_excludes_optional_branch_and_export_order(self):
        q = self.catalog.find(quest_id('NanYanZhiYin'))
        self.assertEqual(q['routes'][0]['branches'], ['1', '2', '3', '4', '5'])
        self.assertIn('S1', [b['id'] for b in q['branches']])
        branch = next(b for b in q['branches'] if b['id'] == '1')
        self.assertEqual(branch['destination_state_id'], 'NanYanZhiYin_PBZQuestState_1')
        self.assertTrue(q['routes'][0]['source']['observations'])

    def test_stage_orders_are_explicit_graph_or_reviewed_history_evidence(self):
        for stem in ('JianXiYiShi', 'JiuSiFuShang'):
            route = self.catalog.find(quest_id(stem))['routes'][0]
            self.assertTrue(route['stage_order_verified'])
            self.assertEqual(route['stage_order'], ['1', '2'])
            self.assertEqual(route['stage_order_source']['kind'], 'linear_state_graph')
        route = self.catalog.find(quest_id('NanYanZhiYin'))['routes'][0]
        self.assertTrue(route['stage_order_verified'])
        self.assertEqual(route['stage_order'], ['1', '2', '3', '4', '5'])
        self.assertEqual(route['stage_order_source']['kind'], 'native_branch_dirty_time')
        for stem in ('WuXiangWang', 'BaiXue'):
            route = self.catalog.find(quest_id(stem))['routes'][0]
            self.assertFalse(route['stage_order_verified'])
            self.assertEqual(route['stage_order'], [])

    def test_alternative_route_does_not_complete_both_outcomes(self):
        q = self.catalog.find(quest_id('GuiTu'))
        self.assertIn(['1', '2', '3-A', '4'], [r['branches'] for r in q['routes']])
        for route in q['routes']:
            self.assertFalse({'3-A', '3-B'} <= set(route['branches']))

    def test_bumie_catalog_exposes_only_reviewed_stage_order_and_native_zero_progress(self):
        q = self.catalog.find(quest_id('BuMieXiaDan'))
        self.assertEqual(len(q['routes']), 1)
        route = q['routes'][0]
        self.assertTrue(route['stage_order_verified'])
        self.assertEqual(route['stage_order'], ['S1', '1'])
        self.assertEqual(len(route['source']['observations']), 7)
        self.assertEqual(q['task_schema']['1']['count'], 6)
        branch = next(b for b in route['template']['QuestBranches'] if b['BranchID'] == '1')
        self.assertEqual(branch['TasksProgress'], [0, 0, 0, 0, 0, 0])
        native = next(b for b in q['branches'] if b['id'] == '1')['source']['properties']
        self.assertTrue(native['bNeedCount'])
        self.assertEqual(native['CompleteTime'], 4)

    def test_bumie_final_reward_has_progress_graph_association_not_name_or_boss_drop(self):
        q = self.catalog.find(quest_id('BuMieXiaDan'))
        self.assertEqual(len(q['rewards']), 1)
        reward = q['rewards'][0]
        self.assertEqual((reward['item_id'], reward['quantity'], reward['stage_id'],
                          reward['category'], reward['certainty']),
                         ('TU_19006', 1, '1', 'quest_final', 'verified'))
        self.assertEqual(reward['name'], '不灭侠胆')
        self.assertEqual(reward['storage'], 'InventoryInfos')
        self.assertEqual(reward['route_ids'], [q['routes'][0]['id']])
        source = reward['source']
        self.assertEqual(source['row'], 'BuMieXiaDan_1_完成任务')
        self.assertEqual(source['kind'], 'reviewed_quest_flow')
        self.assertEqual(len(source['flow_evidence']), 6)
        for flow in source['flow_evidence']:
            self.assertEqual(flow['reward']['properties']['ItemIDAndCount']['entries'], {'TU_19006': 1})
            self.assertEqual(flow['condition']['properties']['MatchCount'], 3)
            tags = flow['condition']['properties']['Tags']
            self.assertEqual(set(tags), {f'pz.不灭侠胆.任务进度{i}' for i in range(1, 7)})
            self.assertEqual(flow['condition']['properties']['Connections']['entries']['True']['NodeGuid'],
                             flow['reward']['properties']['NodeGuid'])
        self.assertNotIn('I_06006', [r['item_id'] for r in q['rewards']])

    def test_unobserved_tasks_remain_previewable_without_invented_success(self):
        q = self.catalog.find(quest_id('MiaoShouKongKong', 'Collect'))
        self.assertTrue(q['branches'])
        self.assertEqual(q['routes'], [])
        self.assertTrue(any('自动成功' in n for n in q['notes']))

    def test_success_templates_keep_zero_task_progress(self):
        q = self.catalog.find(quest_id('JianXiYiShi'))
        branch = next(b for b in q['routes'][0]['template']['QuestBranches'] if b['BranchID'] == '1')
        self.assertEqual(branch['TasksProgress'], [0, 0, 0])
        self.assertEqual(q['task_schema']['1']['count'], 3)
        self.assertEqual(q['task_schema']['1']['observed_progress'][0]['value'], [0, 0, 0])

    def test_rewards_have_verified_stage_event_and_catalog_item(self):
        from pbz_editor.catalog import Catalog
        items = Catalog()
        expected = [('JianXiYiShi', 'AC_Drink', '2', 'quest_final'),
                    ('JiuSiFuShang', 'AC_BreakDrink', '2', 'quest_final'),
                    ('WuXiangWang', 'AC_UltimateBreak', '1', 'quest_final'),
                    ('BaiXue', 'AC_Bleed', '3', 'quest_final'),
                    ('NanYanZhiYin', 'R_01014', '4', 'stage')]
        keys = []
        for stem, item_id, stage, category in expected:
            q = self.catalog.find(quest_id(stem))
            matching = [r for r in q['rewards'] if r['item_id'] == item_id]
            self.assertEqual(len(matching), 1)
            reward = matching[0]
            self.assertEqual((reward['stage_id'], reward['category'], reward['certainty']),
                             (stage, category, 'verified'))
            self.assertEqual(reward['source']['event'], '完成任务')
            self.assertIn(reward['source']['row'], reward['source']['stage_evidence'])
            self.assertEqual(reward['name'], items.find(item_id, reward['storage']).name)
            self.assertEqual((reward['quantity'], reward['level']), (1, 0))
            self.assertTrue(reward['route_ids'])
            keys.append(reward['key'])
        self.assertEqual(len(keys), len(set(keys)))

    def test_nanyan_dust_preserves_base_and_multipliers_as_unknown_preview(self):
        from pbz_editor.rewards import build_reward_preview
        q = self.catalog.find(quest_id('NanYanZhiYin'))
        matches = [r for r in q['rewards'] if r['item_id'] == 'Player.Dust']
        self.assertEqual(len(matches), 1, '已核实的445基础碎屑不能从真实奖励预览静默遗漏')
        spec = matches[0]
        self.assertEqual((spec['storage'], spec['kind'], spec['quantity'], spec['certainty']),
                         ('Player.Dust', 'dust', 445, 'unknown'))
        self.assertEqual((spec['stage_id'], spec['category'], spec['base_quantity']), ('4', 'stage', 445))
        self.assertEqual(spec['cycle_multipliers'], [1.0, 10.899999618530273, 17.450000762939453,
                                                    24.0, 30.540000915527344, 37.09000015258789,
                                                    43.630001068115234])
        self.assertIn('取整', spec['reason'])
        self.assertIn('所有周目', spec['reason'])
        self.assertEqual(spec['source']['row'], 'NanYanZhiYin_4_完成任务')
        doc = {'Player': {'Dust': 100}, 'PBZGameSystem': {'CycleCount': 3}}
        row = build_reward_preview(doc, [spec])[0]
        self.assertFalse(row['default_selected'])
        self.assertEqual(row['grant_amount'], 0)
        self.assertEqual(row['base_quantity'], 445)
        self.assertEqual(row['cycle_multipliers'], spec['cycle_multipliers'])
        self.assertEqual(doc['Player']['Dust'], 100)

    def test_catalog_validates_duplicates_without_access_to_game_assets(self):
        row = self.catalog.quests[0]
        with patch.object(Path, 'read_text', return_value=json.dumps({'quests': [row, row]})):
            with self.assertRaisesRegex(ValueError, '重复|Duplicate'):
                self.catalog_type('only-catalog.json')


if __name__ == '__main__':
    unittest.main()
