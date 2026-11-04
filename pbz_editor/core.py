"""Inventory editing with authenticated PBZS IO and sparse JSON text patches."""
from __future__ import annotations
import copy,hashlib,json,os,struct,sys
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'vendor'))
from . import format as fmt

KINDS={'InventoryInfos':'item','AccessoryInfos':'accessory','MainWeaponInfos':'main_weapon',
       'ShaChiWeaponInfo':'shaqi_weapon','MedicineInfos':'medicine'}
LEVELS={'AccessoryInfos':'CurrentLevel','ShaChiWeaponInfo':'ShaChiWeaponLevel','MedicineInfos':'CurrentLevel'}

def stamp():return datetime.now(timezone(timedelta(hours=8))).strftime('%Y.%m.%d-%H.%M.%S')

def encode(value):
    if isinstance(value,Decimal):
        if not value.is_finite():raise ValueError('数值必须有限')
        return str(value)
    if isinstance(value,dict):return '{'+','.join(json.dumps(k,ensure_ascii=False)+':'+encode(v) for k,v in value.items())+'}'
    if isinstance(value,list):return '['+','.join(encode(v) for v in value)+']'
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))

def locate(text,path):
    start,end=fmt.skip_space(text,0),len(text)
    for key in path:
        if isinstance(key,int):start,end,_=list(fmt.elements(text,start))[key]
        else:_,start,end=fmt.members(text,start)[key]
    return start,end

def sparse_patch(text,old,new):
    updates=[];removes=[];adds={};task_arrays=[]
    def walk(a,b,path):
        if a==b:return
        if isinstance(a,dict) and isinstance(b,dict):
            for key in a:
                if key not in b:removes.append(path+(key,))
                else:walk(a[key],b[key],path+(key,))
            for key in b:
                if key not in a:adds.setdefault(path,[]).append((key,b[key]))
        elif isinstance(a,list) and isinstance(b,list):
            keyed=all(isinstance(x,dict) and isinstance(x.get('QuestClass'),str) for x in a+b)
            if keyed and [x['QuestClass'] for x in a]!=[x['QuestClass'] for x in b]:
                old_ids=[x['QuestClass'] for x in a];new_ids=[x['QuestClass'] for x in b]
                if len(set(old_ids))!=len(a) or len(set(new_ids))!=len(b):raise ValueError('任务数组有重复任务ID')
                lookup={x['QuestClass']:x for x in b}
                for i,x in enumerate(a):
                    if x['QuestClass'] in lookup:walk(x,lookup[x['QuestClass']],path+(i,))
                task_arrays.append((path,old_ids,b))
            elif len(a)==len(b):
                for i,(x,y) in enumerate(zip(a,b)):walk(x,y,path+(i,))
            else:updates.append((path,b))
        else:updates.append((path,b))
    walk(old,new,())
    edits=[]
    for path,value in updates:
        left,right=locate(text,path);edits.append((left,right,encode(value)))
    result=fmt.apply_edits(text,edits)
    for path in removes:
        start,_=locate(result,path[:-1]);members=fmt.members(result,start);keys=list(members);index=keys.index(path[-1]);left,_,right=members[path[-1]]
        if index<len(keys)-1:
            comma=fmt.skip_space(result,right)
            if result[comma]!=',':raise ValueError('物品记录分隔符异常')
            right=comma+1
        elif index:
            previous=members[keys[index-1]][2];left=fmt.skip_space(result,previous)
            if result[left]!=',':raise ValueError('物品记录分隔符异常')
        result=result[:left]+result[right:]
    for path,values in adds.items():
        start,end=locate(result,path);members=fmt.members(result,start)
        insertion=(',' if members else '')+','.join(json.dumps(k,ensure_ascii=False)+':'+encode(v) for k,v in values)
        result=result[:end-1]+insertion+result[end-1:]
    for path,old_ids,desired in task_arrays:
        desired_ids=[x['QuestClass'] for x in desired]
        start,_=locate(result,path)
        # Field patches are complete; moves reuse these slices without re-encoding.
        fragments={quest_id:result[left:right] for quest_id,(left,right,_) in zip(old_ids,fmt.elements(result,start))}
        anchors={old_ids[index] for block in SequenceMatcher(a=old_ids,b=desired_ids,autojunk=False).get_matching_blocks()
                 for index in range(block.a,block.a+block.size)}
        for index in reversed(range(len(old_ids))):
            if old_ids[index] in anchors:continue
            start,_=locate(result,path);items=list(fmt.elements(result,start))
            left,right,_=items[index]
            if index<len(items)-1:
                comma=fmt.skip_space(result,right)
                if result[comma]!=',':raise ValueError('任务数组分隔符异常')
                right=comma+1
            elif index:
                left=fmt.skip_space(result,items[index-1][1])
                if result[left]!=',':raise ValueError('任务数组分隔符异常')
            result=result[:left]+result[right:]
        current=[x for x in old_ids if x in anchors]
        for index,value in enumerate(desired):
            if value['QuestClass'] in current:continue
            fragment=fragments[value['QuestClass']] if value['QuestClass'] in fragments else encode(value)
            start,end=locate(result,path);items=list(fmt.elements(result,start))
            if index==len(items):
                at=end-1;insertion=(',' if items else '')+fragment
            else:at=items[index][0];insertion=fragment+','
            result=result[:at]+insertion+result[at:];current.insert(index,value['QuestClass'])
    if fmt.DECODER.decode(result)!=new:raise ValueError('局部修改校验失败，未生成存档')
    return result

class SaveEditor:
    @classmethod
    def open(cls,path,catalog=None,quest_catalog=None):
        if catalog is None:
            from .catalog import Catalog
            catalog=Catalog()
        if quest_catalog is None:
            from .quest_catalog import QuestCatalog
            quest_catalog=QuestCatalog()
        obj=cls();obj.path=Path(path).resolve();obj._source=obj.path.read_bytes();obj.source_sha256=hashlib.sha256(obj._source).hexdigest()
        obj._entries,obj._toc=fmt.read_archive(obj._source);obj._raw={};obj._original={};obj._documents={};obj.catalog=catalog;obj._history=[]
        obj.quest_catalog=quest_catalog;obj.revision=0;obj.reward_operations=[];obj._reward_receipts={}
        for entry in obj._entries:
            raw=fmt.decode_payload(entry,ROOT/'bin'/'1031_pbz_decoder_v1.exe');doc=fmt.DECODER.decode(raw.decode('utf-8'))
            obj._raw[entry['name']]=raw;obj._original[entry['name']]=doc;obj._documents[entry['name']]=copy.deepcopy(doc)
        obj.slots=[e['name'] for e in obj._entries if isinstance(obj._documents[e['name']].get('Inventory'),dict)]
        if not obj.slots:raise ValueError('这个存档没有可编辑的背包槽位')
        current=obj._documents.get('__Advanced__',{}).get('LastContinueSlot')
        obj.current_slot=current if current in obj.slots else obj.slots[0]
        return obj

    def _inventory(self,slot):
        if slot not in self.slots:raise ValueError('请选择包含背包的有效槽位')
        return self._documents[slot]['Inventory']

    def _spec(self,item_id,storage):
        if storage not in KINDS:raise ValueError('不支持该物品存储类型')
        spec=self.catalog.find(item_id,storage)
        if spec is None:
            if not any(item_id in self._inventory(s).get(storage,{}) for s in self.slots):
                raise ValueError('物品ID不在目录或存档中，无法确认其类型')
        return spec

    def list_items(self,slot):
        inv=self._inventory(slot);records=[];seen=set()
        specs=[(s.item_id,s.storage,s) for s in self.catalog.items]
        for storage in KINDS:
            for item_id in inv.get(storage,{}):
                if not self.catalog.find(item_id,storage):specs.append((item_id,storage,None))
        for item_id,storage,spec in specs:
            if (item_id,storage) in seen:continue
            seen.add((item_id,storage));record=inv.get(storage,{}).get(item_id);kind=KINDS[storage]
            quantity=(record or {}).get('CurrentAmount',0) if kind=='item' else (record or {}).get('CurrentTime',0) if kind=='medicine' else int(record is not None)
            owned=record is not None and (quantity>0 if kind=='item' else True)
            equipped=any(item_id==inv.get(k) for k in ('CurrentMainWeaponID','TopMedicine','BottomMedicine')) or any(item_id in inv.get(k,[]) for k in ('AccessoryIDs','MainWeapons','ShaqiWeapons'))
            records.append(dict(item_id=item_id,name=spec.name if spec else item_id,category=spec.category if spec else '未识别物品',storage=storage,kind=kind,
                                quantity=quantity,level=(record or {}).get(LEVELS[storage],0) if storage in LEVELS else None,owned=owned,equipped=equipped,
                                description=getattr(spec,'description','') if spec else '目录中未识别，保留原有字段',record=copy.deepcopy(record)))
        return records

    @staticmethod
    def _number(value,label):
        if type(value) is not int or not 0<=value<=2147483647:raise ValueError(label+'必须是0到2147483647之间的整数')
        return value

    def _commit(self,slot,inv):
        document=copy.deepcopy(self._documents[slot]);document['Inventory']=inv
        self._commit_document(slot,document)

    def _commit_document(self,slot,document,operation=None):
        if document==self._documents[slot]:return
        self._history.append((slot,copy.deepcopy(self._documents[slot]),copy.deepcopy(self.reward_operations),copy.deepcopy(self._reward_receipts)))
        self._documents[slot]=document
        if operation:
            self.reward_operations.append(operation)
            self._reward_receipts.setdefault(slot,set()).update(operation.get('reward_keys',[]))
        self.revision+=1

    def set_item(self,slot,item_id,storage,quantity=None,level=None):
        self._spec(item_id,storage)
        if quantity is not None:self._number(quantity,'数量')
        if level is not None:self._number(level,'等级')
        if storage=='MainWeaponInfos' and level is not None:raise ValueError('主武器等级不保存在背包记录中，此处只编辑拥有状态')
        if quantity==0 and storage!='MedicineInfos':return self.remove_item(slot,item_id,storage)
        inv=copy.deepcopy(self._inventory(slot));pool=inv.setdefault(storage,{})
        old=pool.get(item_id);cycle=self._documents[slot].get('PBZGameSystem',{}).get('CycleCount',0);now=stamp();kind=KINDS[storage]
        if old is None:
            if kind=='item':record=dict(CurrentAmount=quantity if quantity is not None else 1,MaxAmount=quantity if quantity is not None else 1,FirstAddTime=now,DirtyTime=now,AcquiredCycle=cycle)
            elif kind=='accessory':record=dict(CurrentLevel=level or 0,HasUIRedPoint=True,DirtyTime=now,AcquiredCycle=cycle)
            elif kind=='main_weapon':record=dict(DateTime=now,AlreadySnapSoul=0,HasUIRedPoint=True,AcquiredCycle=cycle,FirstAcquiredCycle=cycle)
            elif kind=='shaqi_weapon':record=dict(ShaChiWeaponLevel=level or 0,AcquiredCycle=cycle,FirstAcquiredCycle=cycle,DirtyTime=now,AlreadySnapSoul=False,HasUIRedPoint=True)
            else:record=dict(CurrentLevel=level or 0,AlreadyHasUIRedPoint=False,CurrentTime=quantity if quantity is not None else 1,MaxTime=self.catalog.medicine_capacity(item_id,level or 0),BuffRemainTime=0,DirtyTime=now,AcquiredCycle=cycle)
        else:record=copy.deepcopy(old)
        if kind=='item' and quantity is not None:
            record['MaxAmount']=max(record.get('MaxAmount',0),record.get('CurrentAmount',0),quantity);record['CurrentAmount']=quantity
            if quantity and (old is None or old.get('CurrentAmount',0)==0):record['AcquiredCycle']=cycle
        elif kind=='medicine' and quantity is not None:record['CurrentTime']=quantity
        if kind=='medicine' and old is not None and level is not None and level!=old.get('CurrentLevel',0):
            record['MaxTime']=self.catalog.medicine_capacity(item_id,level)
        if level is not None and storage in LEVELS:record[LEVELS[storage]]=level
        pool[item_id]=record;self._commit(slot,inv)

    def remove_item(self,slot,item_id,storage):
        self._spec(item_id,storage);inv=copy.deepcopy(self._inventory(slot));pool=inv.get(storage,{})
        if item_id not in pool:return
        if storage=='InventoryInfos':
            pool[item_id]['CurrentAmount']=0;self._commit(slot,inv);return
        if inv.get('ForcedLoadoutActive') and (item_id in inv.get('ForcedLoadoutMainWeapons',[]) or item_id in inv.get('ForcedLoadoutShaqiWeapons',[]) or item_id in inv.get('ForcedLoadoutAccessory',[])):
            raise ValueError('该物品用于当前剧情强制装备，请离开强制装备状态后再移除')
        if storage=='MainWeaponInfos' and len(pool)<=1:raise ValueError('至少需要保留一把主武器')
        del pool[item_id]
        if storage=='AccessoryInfos':arrays=('AccessoryIDs','ZlgwSavedAssemblyAccessory','ForcedLoadoutAccessory')
        elif storage=='MainWeaponInfos':arrays=('MainWeapons','ZlgwSavedMainWeapons','ForcedLoadoutMainWeapons')
        elif storage=='ShaChiWeaponInfo':arrays=('ShaqiWeapons','ZlgwSavedShaqiWeapons','ForcedLoadoutShaqiWeapons')
        else:arrays=()
        for key in arrays:
            if isinstance(inv.get(key),list):inv[key]=['None' if value==item_id else value for value in inv[key]]
        if storage=='MedicineInfos':
            for key in ('TopMedicine','BottomMedicine','ZlgwSavedBottomMedicineID','ZlgwSavedTopMedicineID'):
                if inv.get(key)==item_id:inv[key]='None'
        if storage=='MainWeaponInfos':
            equipped=inv.setdefault('MainWeapons',[])
            if inv.get('CurrentMainWeaponID')==item_id or inv.get('CurrentMainWeaponID') not in pool:
                available=next(((i,x) for i,x in enumerate(equipped) if x in pool),None)
                if available is None:
                    fallback=next(iter(pool));index=equipped.index('None') if 'None' in equipped else len(equipped)
                    if index==len(equipped):equipped.append(fallback)
                    else:equipped[index]=fallback
                    available=(index,fallback)
                inv['CurrentWeaponIndex'],inv['CurrentMainWeaponID']=available
            for key in ('ZlgwSavedCurrentMainWeaponID','ForcedLoadoutCurrentMainWeaponID','ForcedLoadoutWeaponID'):
                if inv.get(key)==item_id:inv[key]='None'
        if storage=='ShaChiWeaponInfo':
            values=inv.get('ShaqiWeapons',[]);index=inv.get('CurrentShaqiWeaponIndex',0)
            if not (0<=index<len(values) and values[index] in pool):inv['CurrentShaqiWeaponIndex']=next((i for i,x in enumerate(values) if x in pool),0)
        self._commit(slot,inv)

    def undo(self):
        if self._history:
            slot,document,operations,_=self._history.pop();self._documents[slot]=document
            self.reward_operations=operations
            self._reward_receipts=copy.deepcopy(getattr(self,'_loaded_reward_receipts',{}))
            for operation in operations:self._reward_receipts.setdefault(operation['slot'],set()).update(operation.get('reward_keys',[]))
            self.revision+=1

    def discard(self):
        for slot in self.slots:self._documents[slot]=copy.deepcopy(self._original[slot])
        self._history.clear();self.reward_operations=[];self.revision+=1
        self._reward_receipts=copy.deepcopy(getattr(self,'_loaded_reward_receipts',{}))

    def load_reward_history(self,backup_dir):
        from .reward_history import RewardHistory
        history=RewardHistory(Path(backup_dir))
        receipts={slot:set(history.receipts(self.source_sha256,slot)) for slot in self.slots}
        self._loaded_reward_receipts=copy.deepcopy(receipts)
        for operation in self.reward_operations:receipts.setdefault(operation['slot'],set()).update(operation.get('reward_keys',[]))
        if any(receipts.get(slot,set())!=self._reward_receipts.get(slot,set()) for slot in self.slots):self.revision+=1
        self._reward_receipts=receipts

    def list_quests(self,slot):
        self._inventory(slot)
        from .quests import list_quests
        return list_quests(self._documents[slot],self.quest_catalog)

    def preview_quest(self,slot,quest_id,action,route_id=None,stage_id=None):
        self._inventory(slot)
        from .quests import preview_quest
        preview=preview_quest(self._documents[slot],self.quest_catalog,quest_id,action,route_id,stage_id,self._reward_receipts.get(slot,set()))
        preview.update(slot=slot,source_sha256=self.source_sha256,revision=self.revision)
        return preview

    def apply_quest(self,preview,selected_rewards=None,allow_uncertain=False):
        if preview.get('source_sha256')!=self.source_sha256 or preview.get('revision')!=self.revision:
            raise ValueError('预览已过期，请重新预览后应用')
        slot=preview['slot'];self._inventory(slot)
        if not preview.get('can_apply'):raise ValueError('；'.join(preview.get('warnings',[])) or '此转换尚不支持')
        from .rewards import apply_rewards
        document=copy.deepcopy(preview['_document'])
        grants=apply_rewards(document,preview['rewards'],selected_rewards,allow_uncertain)
        keys=[g['key'] for g in grants if g.get('grant_amount',g.get('quantity',1))>0]
        operation=dict(slot=slot,quest_id=preview['quest_id'],action=preview['action'],reward_keys=keys,
                       cycle=document.get('PBZGameSystem',{}).get('CycleCount'),grants=grants)
        self._commit_document(slot,document,operation if keys else None)

    def changes(self):
        return [dict(change,slot=slot) for slot in self.slots for change in fmt.json_differences(self._original[slot],self._documents[slot])]

    def build(self):
        if not self.changes():return self._source
        toc=bytearray(self._toc);blocks=[];offset=0;expected={}
        for entry in self._entries:
            name=entry['name'];raw=self._raw[name]
            if self._original[name]!=self._documents[name]:
                raw=sparse_patch(raw.decode('utf-8'),self._original[name],self._documents[name]).encode('utf-8')
                block=fmt.seal_block(fmt.pack_uncompressed(raw))
            else:block=entry['block']
            expected[name]=raw;struct.pack_into('<III',toc,entry['toc_pos'],len(raw),offset,len(block));blocks.append(block);offset+=len(block)
        header=b'PBZS\x03\x01'+struct.pack('<HI',len(self._entries),len(toc)+36);toc[:12]=header
        output=header+fmt.seal_block(bytes(toc))+b''.join(blocks);checked,_=fmt.read_archive(output)
        for old,new in zip(self._entries,checked):
            if old['name']!=new['name'] or fmt.decode_payload(new,ROOT/'bin'/'1031_pbz_decoder_v1.exe')!=expected[old['name']]:raise ValueError('写回认证/解码校验失败')
            if self._original[old['name']]==self._documents[old['name']] and old['block']!=new['block']:raise ValueError('未编辑槽位意外变化')
        return output
