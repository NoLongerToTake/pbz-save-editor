"""目录接口与本机表证据的直接测试；以 python -B 运行，避免写缓存。"""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class CatalogTests(unittest.TestCase):
    def test_medicine_capacity_uses_default_then_one_based_upgrade_levels(self):
        Catalog, _ = self.api()
        catalog = Catalog()
        self.assertEqual([catalog.medicine_capacity('Hulu', level) for level in (0, 2, 3, 6)], [2, 2, 3, 4])
        self.assertEqual(catalog.medicine_capacity('Medicine_ResistanceElement', 0), 3)
        self.assertEqual(catalog.medicine_capacity('Medicine_ResistanceElement', 1), 4)
        with self.assertRaises(ValueError):
            catalog.medicine_capacity('Hulu', 7)

    def api(self):
        from pbz_editor.catalog import Catalog, ItemSpec
        return Catalog, ItemSpec

    def test_contract_defaults(self):
        _, ItemSpec = self.api()
        item = ItemSpec('a', '中文', '材料/药材', 'InventoryInfos', 'item')
        self.assertEqual(item.description, '')

    def test_storage_qualified_lookup_and_unknown(self):
        Catalog, _ = self.api()
        rows = [dict(item_id='shared', name='甲', category='饰品',
                     storage='AccessoryInfos', kind='accessory', metadata={'native': 1}),
                dict(item_id='shared', name='乙', category='主武器',
                     storage='MainWeaponInfos', kind='main_weapon')]
        with patch.object(Path, 'read_text', return_value=json.dumps({'version': 1, 'items': rows})):
            catalog = Catalog(Path('unused.json'))
        self.assertEqual(len(catalog.items), 2)
        self.assertEqual(catalog.find('shared', 'MainWeaponInfos').name, '乙')
        self.assertIsNone(catalog.find('shared', 'InventoryInfos'))
        self.assertIsNone(catalog.find('not_a_real_id'))
        self.assertIn(catalog.find('shared').name, ('甲', '乙'))

    def test_default_catalog_has_all_storage_types_and_categories(self):
        Catalog, _ = self.api()
        catalog = Catalog()
        self.assertGreaterEqual(len(catalog.items), 640)
        self.assertEqual({x.storage for x in catalog.items}, {
            'InventoryInfos', 'AccessoryInfos', 'MainWeaponInfos', 'ShaChiWeaponInfo', 'MedicineInfos'})
        self.assertTrue({'书籍', '书页', '剧情道具', '材料/药材', '记忆花瓣', '完整花',
                         '饰品', '主武器', '杀气武器', '药品'} <= {x.category for x in catalog.items})
        self.assertEqual(catalog.find('F_06004').name, '其一：儿时的老树')
        self.assertEqual(catalog.find('R_01014').category, '书页')
        self.assertEqual(catalog.find('AC_Bleed', 'AccessoryInfos').name, '雪与血')

    def test_all_rows_have_provenance_and_inventory_is_complete(self):
        self.api()
        data = json.loads((ROOT / 'data/items.json').read_text(encoding='utf-8'))
        inventory = [x for x in data['items'] if x['storage'] == 'InventoryInfos']
        self.assertEqual(len(inventory), 640)
        self.assertEqual(len({(x['item_id'], x['storage']) for x in data['items']}), len(data['items']))
        for row in data['items']:
            self.assertIn('source_table', row['metadata'])
            self.assertIn('source_row', row['metadata'])
            self.assertIn('name_key', row['metadata'])
            if not row['metadata']['name_verified']:
                self.assertEqual(row['name'], row['item_id'])
        for table in data['sources']:
            self.assertEqual(table['unread_bytes'], 0)
            self.assertEqual(len(table['sha256']), 64)
            source_rows = []
            for item in data['items']:
                if item['metadata']['source_table'] == table['table']:
                    source_rows.append(item['metadata']['source_row'])
                    source_rows.extend(alias['source_row'] for alias in item['metadata'].get('source_alias_rows', []))
            self.assertEqual(len(source_rows), table['rows'])
            self.assertEqual(len(set(source_rows)), table['rows'])

    def test_names_match_referenced_chinese_localization(self):
        self.api()
        data = json.loads((ROOT / 'data/items.json').read_text(encoding='utf-8'))
        source = Path(data['localization']['path'])
        if not source.exists():
            self.skipTest('本机源本地化不随便携包分发')
        localization = {(row['namespace'], row['key']): row['text']
                        for row in json.loads(source.read_text(encoding='utf-8'))}
        for row in data['items']:
            metadata = row['metadata']
            if metadata['name_verified']:
                self.assertEqual(row['name'], localization[(metadata['name_namespace'], metadata['name_key'])])
        self.assertEqual(len(data['unmapped_names']), 27)
        self.assertEqual({row['item_id'] for row in data['unmapped_categories']}, {'R_06001', 'TU_19003'})

    def test_reader_empty_and_invariant_ftext_and_map(self):
        path = ROOT / 'tools/build_catalog.py'
        self.assertTrue(path.is_file(), '构建工具尚未实现')
        spec = importlib.util.spec_from_file_location('catalog_builder_test', path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        reader = builder.Reader(bytes.fromhex('00000000ff00000000'), [], {}, {})
        self.assertEqual(reader.value(('Text',)), '')
        raw = struct.pack('<Ibii', 0, -1, 1, 4) + b'abc\0'
        self.assertEqual(builder.Reader(raw, [], {}, {}).value(('Text',)), 'abc')
        raw = struct.pack('<iiii', 0, 1, 7, 9)
        self.assertEqual(builder.Reader(raw, [], {}, {}).value(('Map', ('Int',), ('Int',))), {7: 9})

    def test_inventory_category_uses_fields_not_id(self):
        spec = importlib.util.spec_from_file_location('category_builder', ROOT / 'tools/build_catalog.py')
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        self.assertEqual(builder.inventory_category({'ItemId': 'B_fake', 'ItemDetailType': 'NarrativeItem'}), '剧情道具')
        self.assertEqual(builder.inventory_category({'ItemId': 'R_fake', 'ItemType': 'BookPage'}), '书页')
        self.assertEqual(builder.inventory_category({'ItemId': 'F_99999'}), '未识别物品')

    def test_duplicate_native_id_preserves_alias_row_evidence(self):
        spec = importlib.util.spec_from_file_location('alias_builder', ROOT / 'tools/build_catalog.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        def record(row):
            return dict(item_id='HuoLiuXing', storage='ShaChiWeaponInfo', name='火流星',
                        kind='shaqi_weapon', category='杀气武器', metadata={'source_row': row})
        result = builder.merge_alias_rows([record('火流星'), record('火流星_0')])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['metadata']['source_alias_rows'][0]['source_row'], '火流星_0')


if __name__ == '__main__':
    unittest.main()
