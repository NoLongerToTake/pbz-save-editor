r"""从本机已提取资产或只读游戏块构建目录，不导入会回写证据的旧脚本。

运行：python -B tools/build_catalog.py --workspace <evidence-workspace>
运行时目录不依赖本工具。重建复用本机 usmap_io、映射和取块 helper，
不安装任何依赖；默认且唯一写入 data/items.json。
"""
import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


class Reader:
    """Unversioned properties；补全旧 Reader 的 FText、Map 和原生颜色。"""
    def __init__(self, data, names, structs, enums):
        self.data, self.names, self.structs, self.enums = data, names, structs, enums
        self.pos = 0

    def take(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError(f'Invalid read at {self.pos}: {size}')
        result = self.data[self.pos:self.pos + size]
        self.pos += size
        return result

    def number(self, fmt):
        return struct.unpack('<' + fmt, self.take(struct.calcsize('<' + fmt)))[0]

    def name(self):
        index, number = self.number('I'), self.number('I')
        return self.names[index] + (f'_{number - 1}' if number else '')

    def string(self):
        size = self.number('i')
        raw = self.take(size if size >= 0 else -size * 2)
        return raw.decode('utf-8' if size >= 0 else 'utf-16-le').rstrip('\0')

    def value(self, kind):
        tag = kind[0]
        simple = {'Bool': '?', 'Int': 'i', 'UInt32': 'I', 'UInt64': 'Q', 'Int64': 'q',
                  'Byte': 'B', 'Float': 'f', 'Double': 'd', 'Object': 'i'}
        if tag in simple:
            return self.number(simple[tag])
        if tag == 'Name':
            return self.name()
        if tag == 'Str':
            return self.string()
        if tag == 'Enum':
            value = self.value(kind[1])
            return self.enums[kind[2]].get(value, value)
        if tag in ('Array', 'Set'):
            count = self.number('i')
            if not 0 <= count <= len(self.data):
                raise ValueError(f'Invalid array count: {count}')
            return [self.value(kind[1]) for _ in range(count)]
        if tag == 'Map':
            removed, count = self.number('i'), self.number('i')
            if removed != 0 or not 0 <= count <= len(self.data):
                raise ValueError(f'Unsupported map header: {removed}, {count}')
            result = {}
            for _ in range(count):
                key = self.value(kind[1])
                result[key] = self.value(kind[2])
            return result
        if tag == 'Text':
            self.number('I')  # FText flags
            history = self.number('b')
            if history == -1:
                return self.string() if self.number('i') else ''
            if history == 0:
                namespace, key, source = self.string(), self.string(), self.string()
                return {'namespace': namespace, 'key': key, 'source': source}
            raise ValueError(f'Unsupported FText history {history} at {self.pos}')
        if tag == 'SoftObject':
            return [self.name(), self.name(), self.string()]
        if tag == 'Struct':
            name = kind[1]
            if name in ('Vector', 'Vector2D'):
                return [self.number('d') for _ in range(3 if name == 'Vector' else 2)]
            if name == 'LinearColor':
                return [self.number('f') for _ in range(4)]
            if name == 'DateTime':
                return self.number('Q')
            if name == 'GameplayTagContainer':
                return [self.name() for _ in range(self.number('i'))]
            return self.object(name)
        raise ValueError(f'Unsupported property {kind} at {self.pos}')

    @lru_cache(maxsize=None)
    def schema(self, name):
        entry = self.structs[name]
        fields = [(prop.name, prop.type) for prop in sorted(entry.props, key=lambda p: p.index)
                  for _ in range(prop.array_dim)]
        return fields + (self.schema(entry.super) if entry.super else [])

    def object(self, name):
        fields, fragments = self.schema(name), []
        while True:
            raw = self.number('H')
            fragments.append((raw & 127, bool(raw & 128), raw >> 9))
            if raw & 256:
                break
        bits = sum(count for _, zero, count in fragments if zero)
        width = 0 if bits == 0 else 1 if bits <= 8 else 2 if bits <= 16 else (bits + 31) // 32 * 4
        mask = int.from_bytes(self.take(width), 'little')
        index = bit = 0
        result = {}
        for skip, has_zero, count in fragments:
            index += skip
            for _ in range(count):
                key, kind = fields[index]
                zero = has_zero and bool((mask >> bit) & 1)
                if has_zero:
                    bit += 1
                result[key] = None if zero else self.value(kind)
                index += 1
        return result


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def read_table(data, row_struct, structs, enums):
    header = struct.unpack_from('<15I', data)
    count = struct.unpack_from('<I', data, 60)[0]
    pos = 76 + count * 10
    names = []
    for index in range(count):
        hi, low = data[76 + count * 8 + index * 2:76 + count * 8 + index * 2 + 2]
        length, width = ((hi & 127) << 8) | low, 2 if hi & 128 else 1
        names.append(data[pos:pos + length * width].decode('utf-16-le' if width == 2 else 'utf-8'))
        pos += length * width
    serial, size = struct.unpack_from('<QQ', data, header[8])
    raw = data[header[1] + serial:header[1] + serial + size]
    reader = Reader(raw, names, structs, enums)
    reader.object('DataTable')
    if reader.number('I') != 0:
        raise ValueError('Nonzero DataTable object GUID flag')
    count = reader.number('i')
    rows = []
    for _ in range(count):
        row_name = reader.name()
        rows.append((row_name, reader.object(row_struct)))
    unread = len(raw) - reader.pos
    if unread:
        raise ValueError(f'{row_struct}: {unread} unread bytes')
    return rows, unread


def inventory_category(row):
    item_type = row.get('ItemType')
    if item_type in {'Book', 'BookPage', 'Letter', 'MemoryFlower', 'MemoryFlowerPetal'}:
        return {'Book': '书籍', 'BookPage': '书页', 'Letter': '剧情道具',
                'MemoryFlower': '完整花', 'MemoryFlowerPetal': '记忆花瓣'}[item_type]
    return {'NarrativeItem': '剧情道具', 'Material': '材料/药材', 'Cooking': '材料/药材',
            'Treasures': '珍宝', 'AddAttribute': '能力提升道具',
            'AccessoryFragment': '饰品碎片'}.get(row.get('ItemDetailType'), '未识别物品')


def merge_alias_rows(items):
    """同表重复 ItemId 保留行证据，但只向编辑器暴露一项原生 ID。"""
    result, by_key = [], {}
    for item in items:
        key = (item['item_id'], item['storage'])
        previous = by_key.get(key)
        if previous is None:
            by_key[key] = item
            result.append(item)
            continue
        if any(previous[field] != item[field] for field in ('name', 'category', 'kind')):
            raise ValueError(f'Conflicting duplicate ID: {key}')
        previous['metadata'].setdefault('source_alias_rows', []).append(item['metadata'])
    return result


def build(workspace):
    workspace = Path(workspace)
    base = workspace / 'drafts/1031_pbz_tools_v2'
    mapping_module = load_module('pbz_catalog_usmap', base / 'usmap_io.py')
    mapping_path = base / 'usmap-package/ProjectPBZ-5.7.4-301524-full.usmap'
    mapping = mapping_module.read(mapping_path)
    structs = {item.name: item for item in mapping.structs}
    enums = {item.name: dict(item.entries) for item in mapping.enums}
    loc_path = base / '1031_中文文本_v1.json'
    localized = {(row['namespace'], row['key']): row['text']
                 for row in json.loads(loc_path.read_text(encoding='utf-8'))}
    asset_index = None
    chunk_helper = None
    tables = [
        ('DT_InventoryData', 'PBZItemDataStruct', 'InventoryInfos', 'item', None, 'ItemName',
         workspace / 'content/1103_PBZ见习医师_v2/evidence/DT_InventoryData.uasset'),
        ('DT_Accessory', 'AccessoryData', 'AccessoryInfos', 'accessory', '饰品', 'AccessoryName', base / 'DT_Accessory.uasset'),
        ('DT_WeaponData', 'WeaponData', 'MainWeaponInfos', 'main_weapon', '主武器', 'WeaponName', base / 'DT_WeaponData.uasset'),
        ('DT_ShaqiWeaponData', 'ShaqiWeaponData', 'ShaChiWeaponInfo', 'shaqi_weapon', '杀气武器', 'WeaponName', base / 'DT_ShaqiWeaponData.uasset'),
        ('DT_MedicineTable', 'PBZMedicineDataStruct', 'MedicineInfos', 'medicine', '药品', 'Name', base / 'DT_MedicineTable.uasset'),
    ]
    items, sources, missing, unresolved_categories = [], [], [], []
    for table, row_struct, storage, kind, category, name_field, path in tables:
        asset_path = f'ProjectPBZ/Content/PBZContent/DataTable/UI/{table}.uasset'
        if path.exists():
            data, origin = path.read_bytes(), str(path)
        else:
            if asset_index is None:
                asset_index = json.loads((workspace / 'drafts/1101_pbz_tools_v3/1101_资产索引_v1.json').read_text(encoding='utf-8'))
                chunk_helper = load_module('pbz_catalog_chunk_reader', base / '1031_read_asset_v1.py')
            data = chunk_helper.get_chunk(asset_index[asset_path])
            origin = asset_path
        rows, unread = read_table(data, row_struct, structs, enums)
        sources.append(dict(table=table, origin=origin, row_struct=row_struct,
                            rows=len(rows), unread_bytes=unread, sha256=hashlib.sha256(data).hexdigest()))
        for row_name, row in rows:
            item_id = row.get('ItemId')
            # Never infer an English save ID from a Chinese table row label.
            if not item_id or item_id == 'None':
                raise ValueError(f'Missing native ItemId: {table}/{row_name}')
            else:
                id_source = 'ItemId'
            actual_name_field = name_field
            if kind == 'item' and row.get('ItemType') in {'BookPage', 'MemoryFlowerPetal', 'MemoryFlower'}:
                if not (row.get(name_field) or {}).get('Key'):
                    actual_name_field = 'Subhead'
            text = row.get(actual_name_field) or {}
            key, namespace = text.get('Key', ''), text.get('Namespace', '')
            name = localized.get((namespace, key)) if key else None
            verified = bool(name)
            if not verified:
                missing.append(dict(item_id=item_id, storage=storage, namespace=namespace, key=key, source_row=row_name))
                name = item_id
            item_category = category or inventory_category(row)
            if item_category == '未识别物品':
                unresolved_categories.append(dict(item_id=item_id, storage=storage, source_row=row_name))
            descriptions = []
            description_keys = {}
            for field in ('ItemDescription', 'FunctionalDescription', 'IPDescription', 'AccessoryDesc', 'IPDesc', 'EquipDescription', 'Desc', 'Simple', 'SimpleDesc'):
                reference = row.get(field)
                if isinstance(reference, dict) and reference.get('Key'):
                    description_keys[field] = reference
                    value = localized.get((reference.get('Namespace', ''), reference['Key']))
                    if value and value not in descriptions:
                        descriptions.append(value)
            metadata = dict(source_table=table, source_row=row_name, id_source=id_source,
                            name_field=actual_name_field,
                            name_namespace=namespace, name_key=key, name_verified=verified,
                            description_keys=description_keys,
                            classification={field: row.get(field) for field in
                                            ('ItemType', 'ItemDetailType', 'MaterialType', 'PBZItemTypeTag')},
                            native_data={field: value for field, value in row.items() if field in {
                                'IntegrationItemID', 'IntegrationItemNum', 'ItemLevel', 'WeaponLevel',
                                'MedicineLevel', 'MaxRefineLevel', 'AccessoryLevel', 'AccessoryType',
                                'UseMedicineType', 'DefaultMedicineData', 'MedicineLevelData',
                                'WeaponUpLevelDatas', 'ShaChiWeaponUpLevelData', 'WeaponSnapSoulData',
                                'ShaChiWeaponSnapSoulData', 'ConsumeMaterials'}})
            items.append(dict(item_id=item_id, name=name, category=item_category, storage=storage,
                              kind=kind, description='\n\n'.join(descriptions), metadata=metadata))
    items = merge_alias_rows(items)
    return dict(version=1, items=items, sources=sources,
                localization=dict(path=str(loc_path), sha256=hashlib.sha256(loc_path.read_bytes()).hexdigest()),
                schema=dict(path=str(mapping_path), sha256=hashlib.sha256(mapping_path.read_bytes()).hexdigest()),
                metadata_note='native_data 是游戏表原始参数，不是存档记录模板；不得据此推断主武器存档等级。',
                unmapped_names=missing, unmapped_categories=unresolved_categories,
                category_counts=dict(Counter(item['category'] for item in items)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='只读重建比较，不写文件')
    args = parser.parse_args()
    payload = build(args.workspace)
    target = ROOT / 'data/items.json'
    if args.check:
        if json.loads(target.read_text(encoding='utf-8')) != payload:
            raise SystemExit('目录与本机资产不一致')
    else:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(items=len(payload['items']), category_counts=payload['category_counts'],
                          unmapped_names_count=len(payload['unmapped_names']),
                          unmapped_names=[item['item_id'] for item in payload['unmapped_names']],
                          unmapped_categories=payload['unmapped_categories']), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
