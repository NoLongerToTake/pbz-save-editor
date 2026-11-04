"""奖励预览与内存应用；不导入 core，不接触存档文件或用户配置。"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from .catalog import Catalog

MAX_INT = 2147483647
KINDS = {'InventoryInfos': 'item', 'AccessoryInfos': 'accessory',
         'MainWeaponInfos': 'main_weapon', 'ShaChiWeaponInfo': 'shaqi_weapon',
         'MedicineInfos': 'medicine'}
UNIQUE = frozenset(KINDS) - {'InventoryInfos'}


def _integer(value, label, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INT:
        raise ValueError(f'{label}须为{minimum}到{MAX_INT}之间的整数')
    return value


def _keys(values):
    # str 本身是一个完整 key，不能拆成单字符集合。
    if isinstance(values, str):
        values = [values]
    result = set(values)
    if any(not isinstance(key, str) or not key for key in result):
        raise ValueError('奖励key须为非空字符串')
    return result


def _dust(value):
    if type(value) is int:
        return _integer(value, '碎屑存量')
    if isinstance(value, Decimal) and value.is_finite() and 0 <= value <= MAX_INT:
        return value
    raise ValueError('Player.Dust必须已存在，且为有效的整数或Decimal数值')


def _add(current, amount):
    if isinstance(current, Decimal):
        # 不依赖调用者的 Decimal 上下文，保留原记录全部小数位。
        with localcontext() as ctx:
            ctx.prec = max(len(current.as_tuple().digits), len(str(amount))) + max(0, -current.as_tuple().exponent) + 2
            result = current + amount
    else:
        result = current + amount
    if result > MAX_INT:
        raise ValueError('奖励累加后超过2147483647上界')
    return result


def _inspect(document, spec, receipts):
    row = copy.deepcopy(spec)
    for field in ('validation_error', 'medicine_capacity'):
        row.pop(field, None)
    row.update(owned=False, current_amount=0, grant_amount=0, default_selected=False,
               requires_confirmation=False, reason='', can_grant=False,
               receipt_confirmed=spec.get('key') in receipts, already_owned=False,
               history_known=False)
    try:
        key, storage = spec.get('key'), spec.get('storage')
        if not isinstance(key, str) or not key:
            raise ValueError('奖励缺少唯一key')
        if not isinstance(document, dict):
            raise ValueError('目标槽位document不是对象')
        if storage not in KINDS and storage != 'Player.Dust':
            raise ValueError('无法确认奖励存储类型')
        if not isinstance(spec.get('item_id'), str) or not spec['item_id']:
            raise ValueError('奖励缺少物品ID')
        if storage in KINDS and spec.get('kind') != KINDS[storage]:
            raise ValueError('奖励kind与存储类型不一致')
        amount = _integer(spec.get('quantity'), '奖励数量', 1)
        level = _integer(spec.get('level', 0), '奖励等级')
        if storage in UNIQUE - {'MedicineInfos'} and amount != 1:
            raise ValueError('唯一装备数量须为1')

        record = None
        if storage == 'Player.Dust':
            player = document.get('Player')
            if not isinstance(player, dict) or 'Dust' not in player:
                raise ValueError('原槽位不存在Player.Dust，不能新增或猜测')
            current = _dust(player['Dust'])
            row.update(current_amount=current, owned=current > 0)
        else:
            inventory = document.get('Inventory')
            if not isinstance(inventory, dict):
                raise ValueError('目标槽位缺少原生Inventory对象')
            pool = inventory.get(storage, {})
            if not isinstance(pool, dict):
                raise ValueError('原物品池不是对象，不能覆盖')
            if spec['item_id'] in pool:
                record = pool[spec['item_id']]
                if not isinstance(record, dict):
                    raise ValueError('原物品记录不是对象，不能覆盖')
            if storage == 'InventoryInfos':
                current = _integer(record.get('CurrentAmount') if record is not None else 0, '物品存量')
                if record is not None:
                    _integer(record.get('MaxAmount', current), '物品历史数量')
                row.update(current_amount=current, owned=current > 0)
            elif storage == 'MedicineInfos':
                current = _integer(record.get('CurrentTime', 0), '药品次数') if record is not None else 0
                row.update(current_amount=current, owned=record is not None, already_owned=record is not None)
            else:
                current = int(record is not None)
                row.update(current_amount=current, owned=record is not None, already_owned=record is not None)

        if spec.get('certainty') != 'verified':
            raise ValueError('奖励未核实或含随机内容，不能发放')
        if row['receipt_confirmed']:
            row.update(history_known=True, reason='匹配当前保存指纹和槽位的收据已确认此key，不重复发放')
            return row
        if row['already_owned']:
            row['reason'] = '已持有该唯一装备，保留强化和全部原字段；持有状态不证明任务已发奖'
            return row

        if storage == 'MedicineInfos':
            capacity = _integer(Catalog().medicine_capacity(spec['item_id'], level), '药品容量')
            if amount > capacity:
                raise ValueError('奖励药品次数超过目录容量，不能截断或猜测')
            row['medicine_capacity'] = capacity
        if storage in ('InventoryInfos', 'Player.Dust'):
            _add(current, amount)
            row.update(requires_confirmation=True,
                       reason='无匹配收据，领取历史不明；需明确选择并确认后按固定数量增加')
        else:
            # 目录证实具体阶段或路线后，才允许自动选中缺失的唯一装备。
            associated = bool(spec.get('stage_id') or spec.get('route_ids'))
            automatic = associated and bool(spec.get('source')) and spec.get('category') in ('quest_final', 'stage')
            row.update(default_selected=automatic, requires_confirmation=not automatic,
                       reason='已核实阶段/路线的缺失唯一装备，可补入' if automatic else
                              '阶段关联或自动结算依据不足，需明确选择并确认')
        row.update(can_grant=True, grant_amount=amount)
    except (ValueError, TypeError) as exc:
        row['reason'] = str(exc)
        row['validation_error'] = str(exc)
    return row


def build_reward_preview(document: dict, rewards: list[dict], receipts=()) -> list[dict]:
    """不修改输入。收据由调用方先按文件指纹和槽位筛选；缺失收据不等于未领。"""
    received = _keys(receipts)
    return [_inspect(document, spec, received) for spec in rewards]


def _stamp():
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y.%m.%d-%H.%M.%S')


def _grant(document, row):
    storage, item_id, amount = row['storage'], row['item_id'], row['grant_amount']
    before, level = row['current_amount'], row.get('level', 0)
    if storage == 'Player.Dust':
        after = _add(_dust(document['Player']['Dust']), amount)
        document['Player']['Dust'] = after
    else:
        cycle = _integer(document.get('PBZGameSystem', {}).get('CycleCount', 0), '周目')
        now = _stamp()
        pool = document['Inventory'].setdefault(storage, {})
        old = pool.get(item_id)
        if storage == 'InventoryInfos':
            after = _add(before, amount)
            record = copy.deepcopy(old) if old is not None else dict(
                FirstAddTime=now, AcquiredCycle=cycle)
            record.update(CurrentAmount=after, MaxAmount=max(record.get('MaxAmount', 0), after), DirtyTime=now)
            if before == 0:
                record['AcquiredCycle'] = cycle
        elif storage == 'AccessoryInfos':
            after = 1
            record = dict(CurrentLevel=level, HasUIRedPoint=True, DirtyTime=now, AcquiredCycle=cycle)
        elif storage == 'MainWeaponInfos':
            after = 1
            record = dict(DateTime=now, AlreadySnapSoul=0, HasUIRedPoint=True,
                          AcquiredCycle=cycle, FirstAcquiredCycle=cycle)
        elif storage == 'ShaChiWeaponInfo':
            after = 1
            record = dict(ShaChiWeaponLevel=level, AcquiredCycle=cycle, FirstAcquiredCycle=cycle,
                          DirtyTime=now, AlreadySnapSoul=False, HasUIRedPoint=True)
        else:
            after = amount
            record = dict(CurrentLevel=level, AlreadyHasUIRedPoint=False, CurrentTime=amount,
                          MaxTime=row['medicine_capacity'], BuffRemainTime=0, DirtyTime=now, AcquiredCycle=cycle)
        pool[item_id] = record
    return dict(key=row['key'], item_id=item_id, storage=storage, kind=row['kind'],
                name=row.get('name', item_id), grant_amount=amount, quantity=amount,
                before=before, after=after, level=level)


def apply_rewards(document: dict, preview: list[dict], selected_keys: list[str] | None = None,
                  allow_uncertain: bool = False) -> list[dict]:
    """全部选择验证及增量计算完成后，才提交到传入的临时document。"""
    selected = (_keys(row.get('key') for row in preview if row.get('default_selected'))
                if selected_keys is None else _keys(selected_keys))
    if not selected:
        return []
    by_key = {}
    for row in preview:
        key = row.get('key')
        if not isinstance(key, str) or not key or key in by_key:
            raise ValueError('奖励预览key缺失或重复，不能确定选择目标')
        by_key[key] = row
    if selected - by_key.keys():
        raise ValueError('选择包含预览之外的奖励key')
    received = {row['key'] for row in preview if row.get('receipt_confirmed')}

    # 先校验全部选择，之后在私有工作文档上计算累加结果和同目标间的溢出。
    checked = []
    for original in preview:
        if original['key'] not in selected:
            continue
        row = _inspect(document, original, received)
        if row.get('validation_error'):
            raise ValueError(row['validation_error'])
        if not row['can_grant']:
            continue  # 原有装备/匹配收据：不重写，也不产生虚假的增量记录。
        if row['requires_confirmation'] and (selected_keys is None or not allow_uncertain):
            raise ValueError('领取历史或阶段关联不明，必须明确选择并启用allow_uncertain确认')
        checked.append(original)
    if not checked:
        return []
    working = copy.deepcopy(document)
    changes = []
    for original in checked:
        row = _inspect(working, original, received)
        if row.get('validation_error'):
            raise ValueError(row['validation_error'])
        if row['can_grant']:
            changes.append(_grant(working, row))
    # Inventory/Player之外的任务、NPC和场景数据保持原对象，不写自定义标记。
    for section in {'Player' if change['storage'] == 'Player.Dust' else 'Inventory' for change in changes}:
        document[section] = working[section]
    return changes
