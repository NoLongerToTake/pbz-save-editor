import unittest
from pbz_editor.quest_ui import format_quest_preview

class RewardExplanationTests(unittest.TestCase):
    def test_unknown_dust_keeps_base_quantity_and_rounding_reason_visible(self):
        preview=dict(action='complete',flow=[],changes=[],warnings=[],can_apply=True,
                     rewards=[dict(name='碎屑',item_id='Player.Dust',quantity=445,grant_amount=0,
                                   certainty='unknown',reason='奖励未核实',
                                   certainty_reason='基础碎屑445；最终取整规则未核实，不补发')])
        text=format_quest_preview(preview,dict(id='quest',name='支线',record_present=True))
        self.assertIn('445',text)
        self.assertIn('取整规则未核实',text)
        self.assertIn('不补发',text)

if __name__=='__main__':unittest.main()
