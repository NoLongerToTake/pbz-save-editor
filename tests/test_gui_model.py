"""GUI helpers; these tests do not replace save-format integration tests."""
import unittest

from pbz_editor.gui import build_item_rows, filter_items, parse_edit_values, format_change


class GuiModelTests(unittest.TestCase):
    def test_catalog_merge_keeps_unknown_records_and_storage_identity(self):
        from types import SimpleNamespace
        catalog = [SimpleNamespace(item_id="A", name="书页", category="书页",
                                   storage="InventoryInfos", kind="item", description="")]
        records = [dict(item_id="A", name="书页", category="书页", storage="InventoryInfos",
                        kind="item", quantity=2, level=None, owned=True),
                   dict(item_id="A", name="未知饰品", category="未识别物品", storage="AccessoryInfos",
                        kind="accessory", quantity=1, level=3, owned=True)]
        rows = build_item_rows(catalog, records)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["quantity"], 2)
        self.assertEqual(rows[1]["storage"], "AccessoryInfos")
        self.assertTrue(rows[0]["owned"])

    def test_unowned_catalog_row_and_search(self):
        from types import SimpleNamespace
        rows = build_item_rows([SimpleNamespace(item_id="BOOK_01", name="剑谱残页", category="书页",
            storage="InventoryInfos", kind="item", description="")], [])
        self.assertFalse(rows[0]["owned"])
        self.assertEqual(filter_items(rows, "全部分类", "book_01", False), rows)
        self.assertEqual(filter_items(rows, "书页", "剑谱", True), [])
        self.assertEqual(filter_items(rows, "饰品", "", False), [])

    def test_integer_validation_rejects_bad_and_negative_values(self):
        self.assertEqual(parse_edit_values("1", "3", "accessory"), (1, 3))
        self.assertEqual(parse_edit_values("0", "", "item"), (0, None))
        for quantity, level in [("-1", ""), ("1.5", ""), ("", ""), ("1", "-1")]:
            with self.subTest(quantity=quantity, level=level):
                with self.assertRaises(ValueError):
                    parse_edit_values(quantity, level, "accessory")
        # Disabled level input must never leak an old equipment level into an item.
        self.assertEqual(parse_edit_values("2", "5", "item"), (2, None))

    def test_preview_does_not_truncate_data(self):
        change = {"slot": "ManualSave1", "path": "InventoryInfos.A", "before": None,
                  "after": {"Quantity": 5, "Name": "剧情道具"}}
        slot, path, before, after = format_change(change)
        self.assertEqual(slot, "ManualSave1")
        self.assertEqual(path, "InventoryInfos.A")
        self.assertEqual(before, "null")
        self.assertIn("剧情道具", after)
        self.assertIn("5", after)

    def test_native_equipment_quantity_and_level_rules(self):
        self.assertEqual(parse_edit_values("1", "8", "main_weapon"), (1, None))
        self.assertEqual(parse_edit_values("0", "2", "accessory"), (0, 2))
        self.assertEqual(parse_edit_values("5", "2", "medicine"), (5, 2))
        for kind in ("accessory", "main_weapon", "shaqi_weapon"):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    parse_edit_values("2", "", kind)


if __name__ == "__main__":
    unittest.main()
