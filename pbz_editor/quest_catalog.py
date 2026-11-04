"""自包含的只读支线目录；不加载游戏、资产提取器或存档。"""
import json
from pathlib import Path


class QuestCatalog:
    def __init__(self, path=None):
        path = Path(path) if path is not None else Path(__file__).resolve().parents[1] / 'data/quests.json'
        self.payload = json.loads(path.read_text(encoding='utf-8-sig'))
        self.quests = self.payload['quests']
        self._by_id = {}
        for quest in self.quests:
            quest_id = quest['id']
            if quest_id in self._by_id:
                raise ValueError(f'重复任务 QuestClass: {quest_id}')
            self._by_id[quest_id] = quest

    def find(self, quest_id):
        return self._by_id.get(quest_id)
