r"""只读本机游戏资产及旧 JSON，唯一输出 data/quests.json。

python -B tools/build_quests.py --workspace <evidence-workspace>
python -B tools/build_quests.py --check
不导入旧补丁，不解码/写入正式存档，不启动窗口；运行时不依赖本工具。
"""
import argparse
from collections import Counter
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import struct
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_FOLDERS = {'Simple', 'Branch', 'Collect', 'Quest_Branchline'}
SUCCESS = 'EQuestCompletion::QC_Succeded'
BUMIE_ID = '/Game/PBZstory/PBZQuest/Simple/BuMieXiaDan.BuMieXiaDan_C'
BUMIE_EVIDENCE = 'docs/1104_不灭侠胆证据_v2.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def native_class(asset_path):
    relative = asset_path.split('/Content/', 1)[1].removesuffix('.uasset')
    return f'/Game/{relative}.{Path(relative).name}_C'


def package_exports(data, reader_type, structs, enums):
    """读取 Zen export，FPackageIndex 以 export_index+1 解析，绝不当 BranchID。"""
    header = struct.unpack_from('<15I', data)
    count = struct.unpack_from('<I', data, 60)[0]
    pos = 76 + count * 10
    names = []
    for index in range(count):
        hi, low = data[76 + count * 8 + index * 2:76 + count * 8 + index * 2 + 2]
        length, width = ((hi & 127) << 8) | low, 2 if hi & 128 else 1
        names.append(data[pos:pos + length * width].decode('utf-16-le' if width == 2 else 'utf-8'))
        pos += length * width
    entries = []
    for index, offset in enumerate(range(header[8], header[9], 72)):
        serial, size, name, number = struct.unpack_from('<QQII', data, offset)
        object_name = names[name] + (f'_{number - 1}' if number else '')
        raw = data[header[1] + serial:header[1] + serial + size]
        entries.append(dict(export_index=index, object_name=object_name, raw=raw))

    def decode(entry, kind):
        reader = reader_type(entry['raw'], names, structs, enums)
        properties = reader.object(kind)
        if entry['raw'][reader.pos:] not in (b'', b'\0\0\0\0'):
            raise ValueError(f"Unread export bytes: {entry['object_name']}: {len(entry['raw']) - reader.pos}")
        return dict(export_index=entry['export_index'], object_name=entry['object_name'],
                    properties=properties, struct=kind, unread_bytes=0)

    exports = []
    task_refs = set()
    for entry in entries:
        name = entry['object_name']
        kind = ('PBZQuestBranch' if name.startswith('PBZQuestBranch') else
                'PBZQuestState' if name.startswith('PBZQuestState') else
                name.removeprefix('Default__') if name.startswith('Default__') else
                'Quest' if name == 'QuestTemplate' else None)
        if kind:
            parsed = decode(entry, kind)
            exports.append(parsed)
            task_refs.update(parsed['properties'].get('QuestTasks') or [])
    for ref in sorted(task_refs):
        if ref <= 0 or ref > len(entries):
            raise ValueError(f'Unsupported QuestTask reference: {ref}')
        entry = entries[ref - 1]
        # BPT_*_C instance names carry the native class followed by _<instance>.
        kind = entry['object_name'].rsplit('_', 1)[0]
        if kind not in structs:
            raise ValueError(f'Unknown QuestTask class: {kind}')
        exports.append(decode(entry, kind))
    return sorted(exports, key=lambda e: e['export_index'])


def build_quest(asset_path, exports, localized):
    by_ref = {entry['export_index'] + 1: entry for entry in exports}

    def resolve(ref, prefix=None):
        if ref is None or ref == 0:
            return None
        entry = by_ref.get(ref)
        if entry is None or (prefix and not entry['object_name'].startswith(prefix)):
            raise ValueError(f'{asset_path}: unresolved FPackageIndex {ref} ({prefix})')
        return entry

    def text(reference):
        if not reference:
            return ''
        key = reference.get('Key', '')
        return localized.get((reference.get('Namespace', 'Default'), key), '')

    def native_id(ref, prefix):
        entry = resolve(ref, prefix)
        return entry['properties']['ID'] if entry else None

    defaults = next(e['properties'] for e in exports if e['object_name'].startswith('Default__'))
    graph = next((e['properties'] for e in exports if e['object_name'] == 'QuestTemplate'), {})
    qid = native_class(asset_path)
    notes, branches, states = [], [], []
    name = text(defaults.get('FlowLocTextQuestName'))
    if not name:
        name = qid.rsplit('/', 1)[-1].split('.')[0]
        notes.append('本机中文库未找到任务名，保留原生资产名。')
    for entry in exports:
        p = entry['properties']
        source = dict(export_index=entry['export_index'], object_name=entry['object_name'], properties=p)
        if entry['object_name'].startswith('PBZQuestBranch'):
            tasks = [copy.deepcopy(resolve(ref)) for ref in (p.get('QuestTasks') or [])]
            task_schema = dict(count=len(tasks), tasks=tasks, observed_progress=[])
            branches.append(dict(id=p['ID'], name=text(p.get('FlowLocTarget')) or p['ID'],
                                 description=text(p.get('FlowLocDesc')),
                                 destination_state_id=native_id(p.get('DestinationState'), 'PBZQuestState'),
                                 region=p.get('BelongToWorld') or p.get('AcceptBelongToWorld') or '',
                                 task_schema=task_schema, source=source))
        elif entry['object_name'].startswith('PBZQuestState'):
            node_type = p.get('StateNodeType') or 'Regular'
            completion = {'Success': SUCCESS, 'Failure': 'EQuestCompletion::QC_Failed',
                          'Fail': 'EQuestCompletion::QC_Failed'}.get(node_type, 'EQuestCompletion::QC_Started')
            states.append(dict(id=p['ID'], branches=[native_id(ref, 'PBZQuestBranch') for ref in (p.get('Branches') or [])],
                               completion=completion, node_type=node_type, source=source))
    if len({b['id'] for b in branches}) != len(branches):
        raise ValueError(f'Duplicate BranchID: {asset_path}')
    if len({s['id'] for s in states}) != len(states):
        raise ValueError(f'Duplicate state ID: {asset_path}')
    return dict(id=qid, name=name, description=text(defaults.get('FlowLocTextQuestDesc')),
                region=' / '.join(dict.fromkeys(b['region'] for b in branches if b['region'])),
                quest_type=defaults.get('QuestType'), branches=branches, states=states,
                start_state_id=native_id(graph.get('QuestStartState'), 'PBZQuestState'),
                routes=[], rewards=[], notes=notes, template=None,
                task_schema={b['id']: b['task_schema'] for b in branches},
                source=dict(asset=asset_path, defaults=defaults, graph=graph,
                            parsed_exports=len(exports), unread_bytes=0), observations=[])


def stage_order_evidence(quest, record, source):
    """Set 不能证明顺序；仅唯一线性图或逐项审过的历史时间可证明。"""
    members = set(record['CompletedBranches'])
    branches = {b['id']: b for b in quest['branches']}
    states = {s['id']: s for s in quest['states']}
    state_id, order, visited = quest['start_state_id'], [], set()
    while state_id in states and state_id not in visited:
        visited.add(state_id)
        state = states[state_id]
        if state['completion'] == SUCCESS:
            if set(order) == members:
                return dict(stage_order_verified=True, stage_order=order, stage_order_source=dict(
                    kind='linear_state_graph', start_state_id=quest['start_state_id'],
                    success_state_id=state_id, asset=quest['source']['asset'],
                    rule='从原生起始状态到成功状态，每一步均只有一个分支，无并行或跳线'))
            break
        outgoing = state['branches']
        if len(outgoing) != 1 or outgoing[0] not in branches:
            break
        branch = branches[outgoing[0]]
        order.append(branch['id'])
        state_id = branch['destination_state_id']
    # Histories are reviewed individually. BuMie additionally binds the exact
    # observed route/times and source hashes to its quest/flow evidence file.
    reviewed = ('/Simple/NanYanZhiYin.NanYanZhiYin_C', '/Simple/GuiTu.GuiTu_C')
    bumie = quest['id'] == BUMIE_ID
    evidence_data = (ROOT / BUMIE_EVIDENCE).read_bytes() if bumie else None
    evidence = json.loads(evidence_data) if bumie else None
    reviewed_bumie = bumie and record.get('QuestCompletion') == SUCCESS and members == {'S1', '1'} and any(
        source.get('path') == o['path'] and source.get('sha256') == o['sha256']
        for o in evidence['stage_order']['observations'])
    if quest['id'].endswith(reviewed) or reviewed_bumie:
        records = {b['BranchID']: b for b in record.get('QuestBranches', [])}
        times = {}
        for branch_id in members:
            branch = records.get(branch_id, {})
            value = branch.get('DirtyTime', '')
            if branch.get('BranchType') != 'EBranchType::BranchCompleted':
                break
            try:
                stamp = datetime.strptime(value, '%Y.%m.%d-%H.%M.%S')
            except ValueError:
                break
            if stamp.year <= 1:
                break
            times[branch_id] = value
        ordered = sorted(times, key=times.get)
        if (len(times) == len(members) and len(set(times.values())) == len(members)
                and (not bumie or (times == evidence['stage_order']['dirty_times']
                                    and ordered == evidence['stage_order']['order']))):
            result = dict(stage_order_verified=True, stage_order=ordered, stage_order_source=dict(
                kind='native_branch_dirty_time', observation=source, branch_dirty_times=times,
                rule='仅此已观察成功历史：已完成分支 DirtyTime 均有效、互异；未声称所有路线的固定顺序'))
            if bumie:
                result['stage_order_source']['reviewed_evidence'] = dict(
                    path=BUMIE_EVIDENCE, sha256=digest(evidence_data))
            return result
    return dict(stage_order_verified=False, stage_order=[], stage_order_source=dict(
        kind='unverified', reason='CompletedBranches 是 Set；图含并行/跳线或缺少可核对的历史时间顺序'))


def add_observation(quest, record, source):
    """仅 QC_Succeded + 原生 CompletedBranches 集合构成成功证据。"""
    if record.get('QuestClass') != quest['id']:
        raise ValueError('Observation QuestClass mismatch')
    reference = dict(source, json_path='Narrative.NarrativeSavedQuests',
                     quest_class=quest['id'], completion=record.get('QuestCompletion'))
    quest['observations'].append(reference)
    if quest['template'] is None:
        quest['template'] = copy.deepcopy(record)
    for branch in record.get('QuestBranches', []):
        schema = quest['task_schema'].get(branch['BranchID'])
        if schema is not None and 'TasksProgress' in branch:
            value = branch['TasksProgress']
            if len(value) != schema['count']:
                raise ValueError(f"{quest['id']}/{branch['BranchID']}: TasksProgress size mismatch")
            if not any(o['value'] == value for o in schema['observed_progress']):
                schema['observed_progress'].append(dict(value=copy.deepcopy(value), source=reference))
    if record.get('QuestCompletion') != SUCCESS:
        return
    route = record.get('CompletedBranches')
    known = {b['id'] for b in quest['branches']}
    if not isinstance(route, list) or not route or not set(route) <= known or len(set(route)) != len(route):
        quest['notes'].append('成功样本缺少完整可核对的 CompletedBranches，不支持此样本自动成功。')
        return
    for existing in quest['routes']:
        if set(existing['branches']) == set(route):
            existing['source']['observations'].append(reference)
            return
    order_evidence = stage_order_evidence(quest, record, reference)
    quest['routes'].append(dict(id='observed_' + digest(json.dumps(sorted(route), ensure_ascii=False).encode())[:12],
                                name='已观察成功路线 ' + str(len(quest['routes']) + 1),
                                branches=copy.deepcopy(route), template=copy.deepcopy(record),
                                **order_evidence,
                                source=dict(kind='native_success_record', observations=[reference],
                                            collection_type='Set<Name>',
                                            order='原生排列仅保留来源，不表示阶段先后；阶段须看 stage_order_verified')))
    if len(quest['routes']) == 1:
        quest['template'] = copy.deepcopy(record)


# Deliberately reviewed one by one. Other debug-table reward rows are evidence
# to inspect, not permission to mass-associate quest rewards or infer stages.
REVIEWED_REWARDS = [
    ('JianXiYiShi', '2', 'AC_Drink', 'Xigu.AC_Drink', 'quest_final',
     'content/1103_PBZ见习医师_v2/1103_奖励证据_v1.json'),
    ('JiuSiFuShang', '2', 'AC_BreakDrink', 'FuJiZhiLin.AC_BreakDrink', 'quest_final',
     'drafts/1031_pbz_tools_v2/1031_饰品编号证据_v1.json'),
    ('WuXiangWang', '1', 'AC_UltimateBreak', 'PangZhen.AC_UltimateBreak', 'quest_final',
     'content/1101_PBZ花与勿相忘_v4/1101_勿相忘奖励证据_v1.json'),
    ('BaiXue', '3', 'AC_Bleed', 'HouJie.001.A2_05002', 'quest_final',
     'content/1101_PBZ白雪儿修复_v3/1101_白雪儿奖励证据_v1.json'),
    ('NanYanZhiYin', '4', 'R_01014', 'Xigu.R_01014', 'stage',
     'content/1102_PBZ难言之隐_v5/1102_难言之隐奖励证据_v1.json'),
]


def add_reviewed_rewards(quests, stage_rows, loot_rows, items, workspace, table_sources):
    by_id = {q['id']: q for q in quests}
    by_loot = dict(loot_rows)
    for stem, branch, item_id, loot_id, category, old_evidence in REVIEWED_REWARDS:
        qid = f'/Game/PBZstory/PBZQuest/Simple/{stem}.{stem}_C'
        quest = by_id[qid]
        row_name = f'{stem}_{branch}_完成任务'
        stage = dict(stage_rows)[row_name]
        soft = stage['QuestClass']
        if f'{soft[0]}.{soft[1]}' != qid or stage['BranchID'] != branch or stage['State'] != '完成任务':
            raise ValueError(f'Wrong native stage mapping: {row_name}')
        if loot_id not in stage['LootItemIDs']:
            raise ValueError(f'Unverified stage loot association: {row_name}/{loot_id}')
        loot = by_loot[loot_id]
        if loot['LootItemID'] != loot_id or len(loot['EntryDatas']) != 1:
            raise ValueError(f'Ambiguous loot alternatives: {loot_id}')
        entry = loot['EntryDatas'][0]
        storage, field, id_field, kind = (('AccessoryInfos', 'Accessories', 'AccessoryID', 'accessory')
                                        if item_id.startswith('AC_') else
                                        ('InventoryInfos', 'LootItems', 'ItemId', 'item'))
        reward_rows = [r for r in entry[field] if r[id_field] == item_id]
        if len(reward_rows) != 1 or reward_rows[0].get('Probability') != 1.0:
            raise ValueError(f'Unverified deterministic reward: {loot_id}/{item_id}')
        native = reward_rows[0]
        if kind == 'item' and (native['MinAmount'], native['MaxAmount']) != (1, 1):
            raise ValueError(f'Unexpected quantity: {loot_id}/{item_id}')
        item = next(i for i in items if i['item_id'] == item_id and i['storage'] == storage)
        evidence = workspace / old_evidence
        evidence_data = evidence.read_bytes()
        if item_id.encode() not in evidence_data:
            raise ValueError(f'Old evidence does not identify {item_id}')
        quest['rewards'].append(dict(
            key=f'{qid}:{branch}:{storage}:{item_id}', item_id=item_id, name=item['name'],
            storage=storage, kind=kind, quantity=1, level=0, stage_id=branch,
            route_ids=[r['id'] for r in quest['routes'] if branch in r['branches']],
            certainty='verified', category=category,
            source=dict(row=row_name, event='完成任务', stage_evidence={row_name: stage},
                        stage_table=table_sources['stages'], loot_table=table_sources['loot'],
                        loot_id=loot_id, loot_data=loot, item_source=item['metadata'],
                        previous_evidence=dict(path=old_evidence, sha256=digest(evidence_data)),
                        association='原生任务阶段表的完整 QuestClass、BranchID、完成事件与 LootItemID 逐项匹配')))
        if stem == 'NanYanZhiYin':
            dust_rows = entry['LootDusts']
            if len(dust_rows) != 1 or (dust_rows[0]['MinDust'], dust_rows[0]['MaxDust'],
                                       dust_rows[0]['Probability']) != (445, 445, 1.0):
                raise ValueError('难言之隐基础碎屑与已核实证据不一致')
            dust = dust_rows[0]
            reason = '基础碎屑445及周目倍率已核实，最终乘法取整未核实；445不是所有周目的最终值，本版本仅预览，不补发。'
            dust_source = copy.deepcopy(quest['rewards'][-1]['source'])
            dust_source.update(dust_entry=copy.deepcopy(dust), reason=reason)
            quest['rewards'].append(dict(
                key=f'{qid}:{branch}:Player.Dust:Player.Dust', item_id='Player.Dust',
                name='碎屑（445为基础值）', storage='Player.Dust', kind='dust',
                quantity=445, base_quantity=445, cycle_multipliers=copy.deepcopy(dust['CycleDustMultipliers']),
                level=0, stage_id=branch, route_ids=[r['id'] for r in quest['routes'] if branch in r['branches']],
                certainty='unknown', category='stage', source=dust_source,
                reason=reason, certainty_reason=reason))


def add_bumie_reward(quest, stage_rows, loot_rows, items, table_sources, helper, index):
    """仅适配已逐项核对的侠胆进度发奖链；资产变化须重新审证据。"""
    data = (ROOT / BUMIE_EVIDENCE).read_bytes()
    evidence = json.loads(data)

    def require(ok, reason):
        if not ok:
            raise ValueError(f'不灭侠胆证据不一致: {reason}')

    require(evidence['quest_id'] == quest['id'] == BUMIE_ID, 'QuestClass')
    require(evidence['quest_source'] == quest['source'], '任务原生图')
    stages = dict(stage_rows)
    for row, properties in evidence['stage_rows'].items():
        require(stages.get(row) == properties, row)
    require(evidence['table_sources'] == table_sources, '阶段/掉落表哈希')
    native = next(b for b in quest['branches'] if b['id'] == '1')['source']['properties']
    require(native.get('bNeedCount') is True and native.get('CompleteTime') == 4, '阶段1计数')
    tags = {tag for task in quest['task_schema']['1']['tasks'] for tag in task['properties']['Tags']}
    require(tags == {f'pz.不灭侠胆.任务进度{i}' for i in range(1, 7)}, '六项任务标签')
    reward = evidence['reward']
    require((reward['item_id'], reward['quantity'], reward['stage_id'], reward['category']) ==
            ('TU_19006', 1, '1', 'quest_final'), '奖励身份/数量/阶段')
    loot = dict(loot_rows).get(reward['loot_row_name'])
    require(loot == reward['loot_row'], '辅助掉落行')
    require(loot['EntryDatas'][0]['LootItems'] == [dict(
        ItemId='TU_19006', MinAmount=1, MaxAmount=1, Probability=1.0)], '辅助掉落行数量')
    flows = reward['flow_evidence']
    require(len(flows) == 6, '六个记忆流程')
    for i, flow in enumerate(flows, 1):
        require(flow['asset'].endswith(f'/BPF_BuMieXiaDan_Memory_{i}.uasset'), '流程身份')
        require(digest(helper.get_chunk(index[flow['asset']])) == flow['sha256'], flow['asset'])
        condition, grant, progress = (flow[key]['properties'] for key in ('condition', 'reward', 'progress'))
        require(set(condition['Tags']) == tags and condition['MatchCount'] == 3, '发奖进度条件')
        require(condition['Connections']['entries']['True']['NodeGuid'] == grant['NodeGuid'], '条件至发奖连接')
        require(grant['ItemIDAndCount']['entries'] == {'TU_19006': 1}, '实际发放节点')
        require(grant['Connections']['entries']['获得物品UI关闭后']['NodeGuid'] == progress['NodeGuid'],
                '发奖至进度连接')
        require(progress['AddTags'] == [f'pz.不灭侠胆.任务进度{i}'], '本项进度写入')
    wolf = evidence['wolf_leader_flow']
    require(digest(helper.get_chunk(index[wolf['asset']])) == wolf['sha256'], '狼头领流程哈希')
    finish, begin = (wolf[key]['properties'] for key in ('finish_s1', 'begin_1'))
    require(finish['Optional Start from ID'] == 'S1' and begin['Optional Start from ID'] == '1'
            and finish['Quest'] == begin['Quest']
            and finish['Connections']['entries']['Out']['NodeGuid'] == begin['NodeGuid'], 'S1至1流程连接')
    item = next(i for i in items if i['item_id'] == 'TU_19006' and i['storage'] == 'InventoryInfos')
    row_name = 'BuMieXiaDan_1_完成任务'
    quest['rewards'].append(dict(
        key=f'{BUMIE_ID}:1:InventoryInfos:TU_19006', item_id='TU_19006', name=item['name'],
        storage='InventoryInfos', kind='item', quantity=1, level=0, stage_id='1',
        route_ids=[r['id'] for r in quest['routes'] if set(r['branches']) == {'S1', '1'}],
        certainty='verified', category='quest_final', source=dict(
            kind='reviewed_quest_flow', row=row_name, event='任务进度条件满足后发放',
            stage_evidence={row_name: stages[row_name]}, stage_table=table_sources['stages'],
            flow_evidence=copy.deepcopy(flows), loot_id=reward['loot_row_name'], loot_data=loot,
            loot_table=table_sources['loot'], item_source=item['metadata'],
            reviewed_evidence=dict(path=BUMIE_EVIDENCE, sha256=digest(data)),
            association=reward['association'])))
    quest['notes'].append('S1 发放的破碎的“侠胆”为流程道具；最终奖励仅列“不灭侠胆”×1。')


def build(workspace):
    # The companion loader has no import-time writes and supplies the verified
    # FText/Map reader; legacy quest/patch modules are never imported.
    import importlib.util
    spec = importlib.util.spec_from_file_location('pbz_quest_asset_reader', ROOT / 'tools/build_catalog.py')
    companion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(companion)
    workspace = Path(workspace)
    base = workspace / 'drafts/1031_pbz_tools_v2'
    mapping_path = base / 'usmap-package/ProjectPBZ-5.7.4-301524-full.usmap'
    usmap = companion.load_module('pbz_quests_usmap', base / 'usmap_io.py')
    mapping = usmap.read(mapping_path)
    structs = {s.name: s for s in mapping.structs}
    enums = {e.name: dict(e.entries) for e in mapping.enums}
    index_path = workspace / 'drafts/1101_pbz_tools_v3/1101_资产索引_v1.json'
    index = json.loads(index_path.read_text(encoding='utf-8'))
    helper = companion.load_module('pbz_quests_chunks', base / '1031_read_asset_v1.py')
    loc_path = base / '1031_中文文本_v1.json'
    localized = {(r['namespace'], r['key']): r['text'] for r in json.loads(loc_path.read_text(encoding='utf-8'))}
    quests, excluded = [], []
    for asset_path, chunk in sorted(index.items()):
        if '/PBZQuest/' not in asset_path or not asset_path.endswith('.uasset'):
            continue
        folder = asset_path.split('/PBZQuest/', 1)[1].split('/')[0]
        if folder not in PRODUCTION_FOLDERS:
            excluded.append(dict(asset=asset_path, reason=f'排除 {folder} 类别'))
            continue
        data = helper.get_chunk(chunk)
        exports = package_exports(data, companion.Reader, structs, enums)
        quest = build_quest(asset_path, exports, localized)
        quest['source'].update(sha256=digest(data), chunk_index=chunk)
        quests.append(quest)
    by_id = {q['id']: q for q in quests}
    observation_sources, unknown = [], []
    samples = workspace / 'drafts/1102_pbz_tools_v5'
    for path in sorted(samples.glob('*.json')):
        data = path.read_bytes()
        document = json.loads(data)
        if not isinstance(document, dict):
            continue
        records = document.get('Narrative', {}).get('NarrativeSavedQuests', [])
        if not records:
            continue
        source = dict(path=path.relative_to(workspace).as_posix(), sha256=digest(data))
        observation_sources.append(source)
        for record in records:
            quest = by_id.get(record['QuestClass'])
            if quest:
                add_observation(quest, record, source)
            elif not any(f'/{folder}/' in record['QuestClass'] for folder in ('Main', 'Tutorial')):
                unknown.append(dict(quest_class=record['QuestClass'], source=source))
    table_sources = {}
    tables = {}
    for key, asset, row_struct in [
        ('stages', 'ProjectPBZ/Content/PBZContent/Blueprint/FlowGraph/DebugUI/FlowQuest/FlowQuestBranchStateDataTable.uasset', 'FlowQuestBranchStateRow'),
        ('loot', 'ProjectPBZ/Content/PBZContent/DataTable/UI/DT_LevelLootDataTable.uasset', 'LevelLootTableData'),
    ]:
        # Use the exact indexed path; do not guess an alternate table if absent.
        if asset not in index:
            matches = [p for p in index if p.endswith('/' + Path(asset).name)]
            if len(matches) != 1:
                raise ValueError(f'Ambiguous/missing table: {asset}')
            asset = matches[0]
        data = helper.get_chunk(index[asset])
        rows, unread = companion.read_table(data, row_struct, structs, enums)
        tables[key] = rows
        table_sources[key] = dict(asset=asset, sha256=digest(data), rows=len(rows), unread_bytes=unread)
    items = json.loads((ROOT / 'data/items.json').read_text(encoding='utf-8'))['items']
    add_reviewed_rewards(quests, tables['stages'], tables['loot'], items, workspace, table_sources)
    add_bumie_reward(by_id[BUMIE_ID], tables['stages'], tables['loot'], items, table_sources, helper, index)
    for quest in quests:
        if not quest['routes']:
            quest['notes'].append('未观察到可核对的原生成功路线，当前不支持自动成功；可预览任务图。')
        if not quest['rewards']:
            quest['notes'].append('尚未逐项核实奖励与阶段关联，本目录不自动补发奖励。')
        if quest['routes']:
            quest['notes'].append('路线仅修改任务记录；不代表 NPC、剧情事件或地图状态同步。')
            if any(not route['stage_order_verified'] for route in quest['routes']):
                quest['notes'].append('成功分支集合已观察，但阶段先后未核实；仅支持完成终点，不支持按集合排列跳阶段。')
    return dict(version=2, quests=quests, sources=dict(
        asset_index=dict(path=index_path.relative_to(workspace).as_posix(), sha256=digest(index_path.read_bytes())),
        schema=dict(path=mapping_path.relative_to(workspace).as_posix(), sha256=digest(mapping_path.read_bytes())),
        localization=dict(path=loc_path.relative_to(workspace).as_posix(), sha256=digest(loc_path.read_bytes())),
        observations=observation_sources, tables=table_sources),
        excluded=excluded, unknown_observed_quests=unknown,
        counts=dict(quests=len(quests), folders=dict(Counter(q['id'].split('/PBZQuest/')[1].split('/')[0] for q in quests)),
                    with_success_routes=sum(bool(q['routes']) for q in quests),
                    with_verified_stage_order=sum(any(r['stage_order_verified'] for r in q['routes']) for q in quests),
                    routes=sum(len(q['routes']) for q in quests), rewards=sum(len(q['rewards']) for q in quests)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='只读重建比较，不写目录')
    args = parser.parse_args()
    payload = build(args.workspace)
    target = ROOT / 'data/quests.json'
    if args.check:
        if json.loads(target.read_text(encoding='utf-8')) != payload:
            raise SystemExit('任务目录与本机证据不一致')
    else:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(payload['counts'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
