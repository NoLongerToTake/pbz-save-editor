import json,sys,tempfile,unittest,struct
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'vendor')]
from pbz_editor import format as fmt
try:
    from pbz_editor.core import SaveEditor
except ImportError:
    SaveEditor=None

def archive(payloads):
    blocks=[fmt.seal_block(fmt.pack_uncompressed(raw)) for raw in payloads.values()]
    names=list(payloads);toc_size=12+sum(2+len(n.encode())+2+12 for n in names)+36
    header=b'PBZS\x03\x01'+struct.pack('<HI',len(names),toc_size);toc=bytearray(header);offset=0
    for name,block in zip(names,blocks):
        encoded=name.encode();toc+=struct.pack('<H',len(encoded))+encoded+b'\x02\x01'+struct.pack('<III',len(payloads[name]),offset,len(block));offset+=len(block)
    return header+fmt.seal_block(bytes(toc))+b''.join(blocks)

SLOT=b'''{ "PBZGameSystem":{"CycleCount":1},"Precise":1.234567890123456789, "Inventory":{
"InventoryInfos":{"I_01007":{"CurrentAmount":1,"MaxAmount":1,"FirstAddTime":"2026.01.01-00.00.00","DirtyTime":"2026.01.01-00.00.00","AcquiredCycle":0},"UnknownMod":{"CurrentAmount":4,"Extra":12.00000000000000001}},
"AccessoryInfos":{"AC_Attack":{"CurrentLevel":3,"AcquiredCycle":0,"Custom":0.1234567890123456789}},"AccessoryIDs":["AC_Attack","None"],
"MainWeaponInfos":{"Tang":{"DateTime":"2026.01.01-00.00.00","AlreadySnapSoul":2},"Double":{"DateTime":"2026.01.01-00.00.00"}},"MainWeapons":["Tang","Double"],"CurrentMainWeaponID":"Tang","CurrentWeaponIndex":0,
"ShaChiWeaponInfo":{"Axe":{"ShaChiWeaponLevel":5}},"ShaqiWeapons":["Axe","None"],"CurrentShaqiWeaponIndex":0,
"MedicineInfos":{"Hulu":{"CurrentLevel":2,"CurrentTime":2,"MaxTime":3,"BuffRemainTime":0}},"TopMedicine":"Hulu","BottomMedicine":"None"},"Narrative":{"Unrelated":true},"Player":{"Dust":100}}'''

class CatalogStub:
    def __init__(self):
        self.items=[SimpleNamespace(item_id=i,name=n,storage=s,kind=k,category=c,description='') for i,n,s,k,c in [
          ('I_01007','金创草','InventoryInfos','item','剧情物品'),('R_01014','文稿','InventoryInfos','item','书籍·书页'),
          ('Book_XiongFu','案卷','InventoryInfos','item','书籍·整册'),('AC_Attack','饰品','AccessoryInfos','accessory','饰品'),
          ('AC_Drink','香薰药盒','AccessoryInfos','accessory','饰品'),('Tang','刀','MainWeaponInfos','main_weapon','主武器'),
          ('Double','双刀','MainWeaponInfos','main_weapon','主武器'),('Axe','斧','ShaChiWeaponInfo','shaqi_weapon','杀气武器'),
          ('Hulu','葫芦','MedicineInfos','medicine','药品')]]
    def find(self,item_id,storage=None):return next((x for x in self.items if x.item_id==item_id and (storage is None or x.storage==storage)),None)
    def medicine_capacity(self,item_id,level):
        return (2,2,2,3,3,3,4)[level]

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SaveEditor,'存档编辑核心尚未实现')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'PBZSaveGame.sav'
        self.original=archive({'__Advanced__':b'{"LastContinueSlot":"ManualSave1"}','ManualSave1':SLOT,'AutoSave0':SLOT,'__Achievements__':b'{"Untouched":true}'})
        self.path.write_bytes(self.original);self.editor=SaveEditor.open(self.path,CatalogStub())
    def payload(self,output,slot='ManualSave1'):
        entries,_=fmt.read_archive(output);entry=next(e for e in entries if e['name']==slot)
        return fmt.decode_payload(entry,ROOT/'bin'/'1031_pbz_decoder_v1.exe')
    def test_open_and_noop_are_exact(self):
        self.assertEqual(self.editor.current_slot,'ManualSave1');self.assertEqual(self.editor.build(),self.original)
        self.assertEqual(self.path.read_bytes(),self.original)
    def test_new_medicine_uses_native_capacity_and_level_change_updates_capacity(self):
        self.editor.remove_item('ManualSave1','Hulu','MedicineInfos')
        self.editor.set_item('ManualSave1','Hulu','MedicineInfos',quantity=1,level=2)
        record=self.editor._documents['ManualSave1']['Inventory']['MedicineInfos']['Hulu']
        self.assertEqual((record['CurrentTime'],record['MaxTime']),(1,2))
        self.editor.set_item('ManualSave1','Hulu','MedicineInfos',quantity=1,level=6)
        self.assertEqual(self.editor._documents['ManualSave1']['Inventory']['MedicineInfos']['Hulu']['MaxTime'],4)
    def test_add_books_and_story_item_clear_only_selected_slot(self):
        self.editor.set_item('ManualSave1','R_01014','InventoryInfos',quantity=1)
        self.editor.set_item('ManualSave1','Book_XiongFu','InventoryInfos',quantity=1)
        self.editor.remove_item('ManualSave1','I_01007','InventoryInfos')
        output=self.editor.build();text=self.payload(output).decode();obj=fmt.DECODER.decode(text)
        self.assertEqual(obj['Inventory']['InventoryInfos']['R_01014']['CurrentAmount'],1)
        self.assertEqual(obj['Inventory']['InventoryInfos']['Book_XiongFu']['CurrentAmount'],1)
        self.assertEqual(obj['Inventory']['InventoryInfos']['I_01007']['CurrentAmount'],0)
        self.assertIn('1.234567890123456789',text);self.assertIn('12.00000000000000001',text)
        old,_=fmt.read_archive(self.original);new,_=fmt.read_archive(output)
        self.assertEqual([a['name'] for a,b in zip(old,new) if a['block']!=b['block']],['ManualSave1'])
    def test_quantity_preserves_unknown_fields_and_increases_history(self):
        self.editor.set_item('ManualSave1','UnknownMod','InventoryInfos',quantity=12)
        inv=fmt.DECODER.decode(self.payload(self.editor.build()).decode())['Inventory']['InventoryInfos']
        self.assertEqual(inv['UnknownMod']['CurrentAmount'],12);self.assertEqual(inv['UnknownMod']['MaxAmount'],12)
        self.assertEqual(str(inv['UnknownMod']['Extra']),'12.00000000000000001')
    def test_remove_accessory_clears_equipment(self):
        self.editor.remove_item('ManualSave1','AC_Attack','AccessoryInfos')
        inv=fmt.DECODER.decode(self.payload(self.editor.build()).decode())['Inventory']
        self.assertNotIn('AC_Attack',inv['AccessoryInfos']);self.assertNotIn('AC_Attack',inv['AccessoryIDs'])
    def test_remove_weapon_selects_existing_and_cannot_remove_last(self):
        self.editor.remove_item('ManualSave1','Tang','MainWeaponInfos')
        inv=fmt.DECODER.decode(self.payload(self.editor.build()).decode())['Inventory']
        self.assertEqual(inv['CurrentMainWeaponID'],'Double');self.assertNotIn('Tang',inv['MainWeapons'])
        with self.assertRaises(ValueError):self.editor.remove_item('ManualSave1','Double','MainWeaponInfos')
    def test_undo_and_discard(self):
        self.editor.set_item('ManualSave1','AC_Drink','AccessoryInfos',level=0)
        self.editor.undo();self.assertEqual(self.editor.build(),self.original)
        self.editor.remove_item('ManualSave1','I_01007','InventoryInfos');self.editor.discard();self.assertEqual(self.editor.changes(),[])
    def test_invalid_amount_leaves_no_changes(self):
        with self.assertRaises(ValueError):self.editor.set_item('ManualSave1','I_01007','InventoryInfos',quantity=-1)
        self.assertEqual(self.editor.changes(),[])
    def test_edit_all_equipment_native_fields(self):
        self.editor.set_item('ManualSave1','AC_Attack','AccessoryInfos',level=4)
        self.editor.set_item('ManualSave1','Axe','ShaChiWeaponInfo',level=6)
        self.editor.set_item('ManualSave1','Hulu','MedicineInfos',quantity=5,level=3)
        inv=fmt.DECODER.decode(self.payload(self.editor.build()).decode())['Inventory']
        self.assertEqual(inv['AccessoryInfos']['AC_Attack']['CurrentLevel'],4)
        self.assertEqual(inv['ShaChiWeaponInfo']['Axe']['ShaChiWeaponLevel'],6)
        self.assertEqual(inv['MedicineInfos']['Hulu']['CurrentTime'],5)
        self.assertEqual(inv['MedicineInfos']['Hulu']['MaxTime'],3)

if __name__=='__main__':unittest.main()
