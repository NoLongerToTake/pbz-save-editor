"""保存事务的外部奖励收据。pending不是游戏已写入的证明；afterhash匹配才是。"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from uuid import uuid4


def _sha256(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise ValueError('存档指纹须为64位SHA-256十六进制字符串')
    return value.lower()


def _operations(values):
    if not isinstance(values, list):
        raise TypeError('operations须为dict列表')
    result = copy.deepcopy(values)
    for operation in result:
        if not isinstance(operation, dict):
            raise ValueError('每次奖励操作须为对象')
        for name in ('slot', 'quest_id'):
            if not isinstance(operation.get(name), str) or not operation[name]:
                raise ValueError(f'奖励操作缺少{name}')
        keys = operation.get('reward_keys')
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, list) or any(not isinstance(k, str) or not k for k in keys):
            raise ValueError('reward_keys须为完整字符串key列表')
        operation['reward_keys'] = list(dict.fromkeys(keys))
    return result


def _json_extra(value):
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError(f'收据字段无法序列化：{type(value).__name__}')


class RewardHistory:
    """只使用显式base_dir，无AppData、游戏路径或默认配置目录回退。"""

    def __init__(self, base_dir):
        if base_dir is None:
            raise ValueError('必须显式指定外部收据base_dir')
        self.base_dir = Path(base_dir).resolve()

    def _path(self, path):
        path = Path(path).resolve()
        if path.parent != self.base_dir:
            raise ValueError('收据路径必须位于此RewardHistory的base_dir内')
        return path

    def _write(self, path, payload):
        path = self._path(path)
        # 在创建目录前完成序列化，错误参数不留下半份正式收据。
        data = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False,
                          default=_json_extra).encode('utf-8')
        self.base_dir.mkdir(parents=True, exist_ok=True)
        staged = self.base_dir / (path.name + '.' + uuid4().hex + '.tmp')
        with staged.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)

    def _read(self, path):
        payload = json.loads(self._path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(payload, dict) or payload.get('schema') != 'pbz-reward-receipt-v1':
            raise ValueError('不是有效的PBZ奖励收据')
        if payload.get('status') not in ('pending', 'completed'):
            raise ValueError('未知的奖励收据状态')
        payload['before_sha256'] = _sha256(payload.get('before_sha256'))
        payload['after_sha256'] = _sha256(payload.get('after_sha256'))
        payload['operations'] = _operations(payload.get('operations'))
        return payload

    def receipts(self, source_sha256, slot) -> list[str]:
        """仅返回确实匹配当前文件afterhash及槽位的key，pending同样可恢复确认。"""
        source_sha256 = _sha256(source_sha256)
        if not isinstance(slot, str) or not slot:
            raise ValueError('槽位须为非空字符串')
        if not self.base_dir.exists():
            return []
        keys = []
        for path in sorted(self.base_dir.glob('*_奖励收据_v1_*.json')):
            payload = self._read(path)
            if payload['after_sha256'] != source_sha256:
                continue
            for operation in payload['operations']:
                if operation['slot'] == slot:
                    keys.extend(operation['reward_keys'])
        return list(dict.fromkeys(keys))

    def prepare(self, before_sha256, after_sha256, operations) -> Path:
        """在实际保存前持久化pending；调用方保存成功后再finish。"""
        payload = dict(schema='pbz-reward-receipt-v1', status='pending',
                       before_sha256=_sha256(before_sha256), after_sha256=_sha256(after_sha256),
                       operations=_operations(operations), created_at=datetime.now(timezone.utc).isoformat())
        path = self.base_dir / (datetime.now().strftime('%m%d') + '_奖励收据_v1_' + uuid4().hex + '.json')
        self._write(path, payload)
        return path

    def finish(self, path):
        """只确认收据。失败会向上传递，但不能据此推断此前的存档写入失败。"""
        path = self._path(path)
        payload = self._read(path)
        if payload['status'] == 'completed':
            return
        payload.update(status='completed', finished_at=datetime.now(timezone.utc).isoformat())
        self._write(path, payload)
