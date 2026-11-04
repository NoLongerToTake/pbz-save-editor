import hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pbz_editor import services
from pbz_editor.core import SaveEditor
from pbz_editor.reward_history import RewardHistory
from test_core import SLOT,archive,CatalogStub
from test_quests_core import Q,QuestStub

class QuestSavingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'save.sav';self.backups=self.root/'备份'
        self.original=archive({'__Advanced__':b'{"LastContinueSlot":"ManualSave1"}','ManualSave1':SLOT})
        self.source.write_bytes(self.original)
        self.editor=SaveEditor.open(self.source,CatalogStub(),quest_catalog=QuestStub())
        self.editor.apply_quest(self.editor.preview_quest('ManualSave1',Q,'complete',route_id='good'))
        self.key=Q+'/final/AC_Drink'
    def test_save_records_receipts_and_carries_them_through_inventory_edits(self):
        result=services.save_editor(self.editor,self.backups)
        self.assertEqual(result['backup_path'].read_bytes(),self.original)
        self.assertIn(self.key,RewardHistory(self.backups).receipts(result['output_sha256'],'ManualSave1'))
        reopened=SaveEditor.open(self.source,CatalogStub(),quest_catalog=QuestStub())
        reopened.load_reward_history(self.backups)
        reopened.set_item('ManualSave1','I_01007','InventoryInfos',quantity=3)
        result2=services.save_editor(reopened,self.backups)
        self.assertNotEqual(result2['output_sha256'],result['output_sha256'])
        self.assertIn(self.key,RewardHistory(self.backups).receipts(result2['output_sha256'],'ManualSave1'))
    def test_failed_receipt_finalization_reports_saved_file_and_pending_is_recoverable(self):
        with patch.object(RewardHistory,'finish',side_effect=OSError('test finalization error')):
            result=services.save_editor(self.editor,self.backups)
        self.assertIn('history_warning',result)
        self.assertNotEqual(self.source.read_bytes(),self.original)
        self.assertIn(self.key,RewardHistory(self.backups).receipts(result['output_sha256'],'ManualSave1'))
    def test_failed_file_replace_does_not_claim_unwritten_reward(self):
        with patch.object(services,'_atomic_write',side_effect=OSError('locked test file')):
            with self.assertRaises(OSError):services.save_editor(self.editor,self.backups)
        self.assertEqual(self.source.read_bytes(),self.original)
        self.assertEqual(RewardHistory(self.backups).receipts(hashlib.sha256(self.original).hexdigest(),'ManualSave1'),[])

if __name__=='__main__':unittest.main()
