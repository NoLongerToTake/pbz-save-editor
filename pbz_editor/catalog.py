"""只读物品目录；运行时仅需标准库和随包 items.json。"""
from dataclasses import dataclass, fields
import json
from pathlib import Path


@dataclass(frozen=True)
class ItemSpec:
    item_id: str
    name: str
    category: str
    storage: str
    kind: str
    description: str = ''


class Catalog:
    def __init__(self, path=None):
        path = Path(path) if path is not None else Path(__file__).resolve().parents[1] / 'data' / 'items.json'
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
        names = {field.name for field in fields(ItemSpec)}
        self.items = [ItemSpec(**{key: value for key, value in row.items() if key in names})
                      for row in payload['items']]
        self._by_storage = {}
        self._by_id = {}
        self._medicine_data = {row['item_id']: row.get('metadata', {}).get('native_data', {})
                               for row in payload['items'] if row['storage'] == 'MedicineInfos'}
        for item in self.items:
            key = (item.item_id, item.storage)
            if key in self._by_storage:
                raise ValueError(f'Duplicate catalog item: {key}')
            self._by_storage[key] = item
            self._by_id.setdefault(item.item_id, item)

    def find(self, item_id, storage=None):
        if storage is None:
            return self._by_id.get(item_id)
        return self._by_storage.get((item_id, storage))

    def medicine_capacity(self, item_id, level):
        data = self._medicine_data.get(item_id, {})
        levels = data.get('MedicineLevelData', [])
        if type(level) is not int or not 0 <= level <= len(levels):
            raise ValueError(f'此药品等级须为 0 到 {len(levels)}')
        entry = data.get('DefaultMedicineData', {}) if level == 0 else levels[level - 1]
        capacity = entry.get('UseTimes')
        if type(capacity) is not int or capacity < 0:
            raise ValueError('目录缺少此药品的容量数据，无法新增或变更等级')
        return capacity
