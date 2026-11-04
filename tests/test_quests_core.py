import copy,json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pbz_editor.core import SaveEditor,sparse_patch
from pbz_editor import format as fmt
from test_core import SLOT,archive,CatalogStub

Q='/Game/PBZstory/PBZQuest/Simple/Test.Test_C'
NEW='/Game/PBZstory/PBZQuest/Simple/New.New_C'
OTHER='/Game/PBZstory/PBZQuest/Simple/Other.Other_C'
MAIN='/Game/PBZstory/PBZQuest/Main/Main.Main_C'

class QuestStub:
    def __init__(self):
        self.quests=[dict(id=q,name=name,region='XiGu',branches=[dict(id=x,name='阶段'+x) for x in ['1','2','S1']],
                         routes=[dict(id='good',name='成功',branches=['1','2'],stage_order=['1','2'],stage_order_verified=True)],notes=[],
                         rewards=[dict(key=q+'/final/AC_Drink',item_id='AC_Drink',name='香薰药盒',storage='AccessoryInfos',kind='accessory',quantity=1,level=0,stage_id='2',route_ids=['good'],certainty='verified',category='quest_final',source='test evidence')])
                     for q,name in [(Q,'测试支线'),(NEW,'未接取支线')]]
    def find(self,q):return next((x for x in self.quests if x['id']==q),None)

class QuestCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'save.sav'
        quest=dict(QuestClass=Q,QuestBranches=[dict(BranchID=x,BranchType='EBranchType::'+t,DirtyTime='old',Extra=9) for x,t in [('1','BranchCompleted'),('2','BranchLocked'),('S1','BranchCompleted')]],QuestCompletion='EQuestCompletion::QC_Failed',CompletedBranches=['1','S1'],ReachedStateNames=[])
        self.other_text='{"QuestClass":'+json.dumps(OTHER)+',"QuestBranches":[],"QuestCompletion":"EQuestCompletion::QC_Started","ModValue":1.23000000000000001}'
        narrative='{"Unrelated":true,"TrackedQuestPath":'+json.dumps(Q)+',"TrackedBranchID":"2","QuestGameTimes":[{"QuestClass":'+json.dumps(Q)+',"GameTime":99},{"QuestClass":'+json.dumps(MAIN)+',"GameTime":100}],"NarrativeSavedQuests":['+json.dumps(quest)+','+self.other_text+']}'
        self.raw=SLOT.replace(b'"Narrative":{"Unrelated":true}',b'"Narrative":'+narrative.encode())
        self.source=archive({'__Advanced__':b'{"LastContinueSlot":"ManualSave1"}','ManualSave1':self.raw,'AutoSave0':self.raw,'__Achievements__':b'{"value":1}'})
        self.path.write_bytes(self.source)
        self.editor=SaveEditor.open(self.path,CatalogStub(),quest_catalog=QuestStub())
    def target(self):return next(q for q in self.editor._documents['ManualSave1']['Narrative']['NarrativeSavedQuests'] if q['QuestClass']==Q)
    def test_list_merges_catalog_and_native_unknown(self):
        rows={q['id']:q for q in self.editor.list_quests('ManualSave1')}
        self.assertEqual(rows[Q]['status'],'failed');self.assertEqual(rows[NEW]['status'],'unaccepted')
        self.assertIn(OTHER,rows);self.assertNotIn(MAIN,rows)
    def test_complete_and_reward_are_one_undo_with_unchanged_other_blocks(self):
        preview=self.editor.preview_quest('ManualSave1',Q,'complete',route_id='good')
        self.editor.apply_quest(preview)
        self.assertEqual(self.target()['CompletedBranches'],['1','2'])
        self.assertEqual(self.target()['QuestBranches'][2]['BranchType'],'EBranchType::None')
        self.assertIn('AC_Drink',self.editor._documents['ManualSave1']['Inventory']['AccessoryInfos'])
        output=self.editor.build();old,_=fmt.read_archive(self.source);new,_=fmt.read_archive(output)
        self.assertEqual([a['name'] for a,b in zip(old,new) if a['block']!=b['block']],['ManualSave1'])
        raw=fmt.decode_payload(new[1],ROOT/'bin/1031_pbz_decoder_v1.exe').decode()
        self.assertIn(self.other_text,raw);self.assertIn('1.234567890123456789',raw)
        self.editor.undo();self.assertEqual(self.editor.build(),self.source)
    def test_partial_stage_does_not_offer_final_reward(self):
        p=self.editor.preview_quest('ManualSave1',Q,'stage',route_id='good',stage_id='1')
        self.assertFalse(p['rewards']);self.editor.apply_quest(p)
        self.assertEqual(self.target()['QuestCompletion'],'EQuestCompletion::QC_Started')
        self.assertEqual(self.target()['CompletedBranches'],['1'])
        self.assertEqual(self.target()['QuestBranches'][1]['BranchType'],'EBranchType::BranchActivating')
    def test_unordered_completion_set_cannot_be_used_as_stage_sequence(self):
        self.editor.quest_catalog.quests[0]['routes'][0]['stage_order_verified']=False
        p=self.editor.preview_quest('ManualSave1',Q,'stage',route_id='good',stage_id='1')
        self.assertFalse(p['can_apply'])
        with self.assertRaises(ValueError):self.editor.apply_quest(p)
        self.assertFalse(self.editor.changes())
        self.assertTrue(self.editor.preview_quest('ManualSave1',Q,'complete',route_id='good')['can_apply'])
    def test_reset_removes_only_this_record_and_its_timer_and_tracking(self):
        self.editor.apply_quest(self.editor.preview_quest('ManualSave1',Q,'reset'))
        n=self.editor._documents['ManualSave1']['Narrative']
        self.assertEqual([q['QuestClass'] for q in n['NarrativeSavedQuests']],[OTHER])
        self.assertEqual([q['QuestClass'] for q in n['QuestGameTimes']],[MAIN])
        self.assertEqual(n['TrackedQuestPath'],'None')
        data=self.editor.build();entries,_=fmt.read_archive(data)
        raw=fmt.decode_payload(entries[1],ROOT/'bin/1031_pbz_decoder_v1.exe').decode()
        self.assertIn(self.other_text,raw)
        self.editor.undo();self.assertEqual(self.editor.build(),self.source)
    def test_create_unaccepted_then_failure_keeps_rewards_and_world_untouched(self):
        original_inventory=copy.deepcopy(self.editor._documents['ManualSave1']['Inventory'])
        self.editor.apply_quest(self.editor.preview_quest('ManualSave1',NEW,'fail'))
        new=next(q for q in self.editor._documents['ManualSave1']['Narrative']['NarrativeSavedQuests'] if q['QuestClass']==NEW)
        self.assertEqual(new['QuestCompletion'],'EQuestCompletion::QC_Failed')
        self.assertEqual(self.editor._documents['ManualSave1']['Inventory'],original_inventory)
        entries,_=fmt.read_archive(self.editor.build())
        self.assertIn(self.other_text,fmt.decode_payload(entries[1],ROOT/'bin/1031_pbz_decoder_v1.exe').decode())
    def test_stale_preview_and_bad_reward_apply_leave_no_partial_commit(self):
        p=self.editor.preview_quest('ManualSave1',Q,'complete',route_id='good')
        self.editor.set_item('ManualSave1','I_01007','InventoryInfos',quantity=2)
        snapshot=copy.deepcopy(self.editor._documents)
        with self.assertRaises(ValueError):self.editor.apply_quest(p)
        self.assertEqual(self.editor._documents,snapshot)
        p=self.editor.preview_quest('ManualSave1',Q,'complete',route_id='good')
        with self.assertRaises(ValueError):self.editor.apply_quest(p,selected_rewards=['nonexistent'])
        self.assertEqual(self.editor._documents,snapshot)

class SparseTaskArrayTests(unittest.TestCase):
    def test_insert_remove_preserve_unedited_record_lexemes(self):
        text='{"Narrative":{"NarrativeSavedQuests":[ {"QuestClass":"A","x":1.2300},\n { "QuestClass" : "B" , "x" : 12.00000000000000001 } ]}}'
        old=fmt.DECODER.decode(text);new=copy.deepcopy(old)
        new['Narrative']['NarrativeSavedQuests'].pop(0)
        result=sparse_patch(text,old,new)
        self.assertIn('{ "QuestClass" : "B" , "x" : 12.00000000000000001 }',result)
        final=copy.deepcopy(new);final['Narrative']['NarrativeSavedQuests'].append({'QuestClass':'C','x':2})
        result=sparse_patch(result,new,final)
        self.assertIn('{ "QuestClass" : "B" , "x" : 12.00000000000000001 }',result)

if __name__=='__main__':unittest.main()
