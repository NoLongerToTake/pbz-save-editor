"""Headless contract tests: never construct a Tk interpreter or touch save files."""
import copy
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from pbz_editor import gui

try:
    from pbz_editor import quest_ui as ui
except ImportError:
    ui = None


QUEST = {
    "id": "/Game/PBZstory/PBZQuest/Simple/BaiXue.BaiXue_C", "name": "白雪儿",
    "status": "active", "status_label": "进行中", "current_stage": "1",
    "record_present": True, "region": "村落", "notes": [],
    "routes": [{"id": "good", "name": "相助", "branches": ["2", "1"],
                "stage_order_verified": True, "stage_order": ["1", "2"]},
               {"id": "other", "name": "另一结局", "branches": ["3"]}],
    "branches": [{"id": "1", "name": "初遇", "description": "与白雪儿交谈"},
                 {"id": "2", "name": "归还", "description": "交还信物"}],
}
UNKNOWN = dict(id="/Game/PBZstory/PBZQuest/Collect/Mod.Mod_C", name="未识别任务",
               status="unknown", status_label="未知", current_stage="?",
               record_present=True, region="", notes=[], routes=[], branches=[])


def reward(key, **extra):
    row = dict(key=key, item_id=key, name="奖励" + key, storage="InventoryInfos",
               kind="item", quantity=2, level=0, stage_id="2", route_ids=["good"],
               certainty="verified", source="本地表", category="stage", owned=False,
               current_amount=0, grant_amount=2, default_selected=False,
               requires_confirmation=False, reason="可补发")
    row.update(extra)
    return row


class EditorDouble:
    """New API boundary double; all state and writes stay in memory."""
    def __init__(self):
        self.revision = 3
        self.source_sha256 = "source-hash"
        self.quests = [copy.deepcopy(QUEST), copy.deepcopy(UNKNOWN)]
        self.calls = []
        self.applied = []
        self.preview_error = None
        self.can_apply = True
        self.pending = []

    def list_quests(self, slot):
        return copy.deepcopy(self.quests)

    def preview_quest(self, slot, quest_id, action, route_id=None, stage_id=None):
        if self.preview_error:
            raise ValueError(self.preview_error)
        self.calls.append((slot, quest_id, action, route_id, stage_id))
        return dict(slot=slot, quest_id=quest_id, action=action,
                    source_sha256=self.source_sha256, revision=self.revision,
                    route_id=route_id, stage_id=stage_id,
                    changes=[dict(slot=slot, path="Quest.Q", before="active", after=action)],
                    rewards=[reward("fixed", default_selected=True),
                             reward("dust", storage="Player.Dust", requires_confirmation=True),
                             reward("random", certainty="random", default_selected=True),
                             reward("unknown", certainty="unknown")],
                    warnings=[] if self.can_apply else ["缺少真实成功路线"],
                    flow=[dict(id="2", name="归还", description="交还信物", before="进行中", after="已完成")],
                    can_apply=self.can_apply)

    def apply_quest(self, preview, selected_rewards=None, allow_uncertain=False):
        self.applied.append((copy.deepcopy(preview), selected_rewards, allow_uncertain))
        self.pending.extend(preview["changes"])
        self.revision += 1


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ui, "任务页及无窗口模型尚未实现")
        self.editor = EditorDouble()
        self.model = ui.QuestModel(self.editor, "ManualSave1")
        self.model.refresh()
        self.model.select_quest(QUEST["id"])

    def test_all_five_statuses_and_chinese_or_full_id_search(self):
        rows = [dict(QUEST, id=str(i), status=status) for i, status in enumerate(
            ("unaccepted", "active", "completed", "failed", "unknown"))]
        self.assertEqual(len(ui.filter_quests(rows)), 5)
        for i, status in enumerate(("unaccepted", "active", "completed", "failed", "unknown")):
            self.assertEqual([r["id"] for r in ui.filter_quests(rows, status)], [str(i)])
        self.assertEqual(ui.filter_quests([QUEST], query=" 白雪 "), [QUEST])
        self.assertEqual(ui.filter_quests([QUEST], query="simple/baixue"), [QUEST])
        self.assertEqual(ui.filter_quests([QUEST], "failed", "白雪"), [])

    def test_preview_forwards_selected_route_and_branch_ids(self):
        self.model.select_route("good")
        self.model.select_stage("2")
        preview = self.model.preview("stage")
        self.assertEqual(self.editor.calls[-1], ("ManualSave1", QUEST["id"], "stage", "good", "2"))
        self.assertEqual(preview["revision"], 3)
        self.assertEqual(self.model.selected_rewards, {"fixed"})
        text = ui.format_quest_preview(preview, QUEST)
        self.assertIn("归还", text)
        self.assertIn("完成", text)
        self.assertIn("Quest.Q", text)

    def test_unknown_without_routes_can_fail_or_reset_but_cannot_complete_stage(self):
        self.model.select_quest(UNKNOWN["id"])
        for action in ("fail", "reset"):
            preview = self.model.preview(action)
            self.assertTrue(preview["can_apply"])
            self.assertEqual(self.editor.calls[-1][3:], (None, None))
        self.editor.can_apply = False
        preview = self.model.preview("stage")
        self.assertFalse(preview["can_apply"])
        self.assertIn("缺少真实成功路线", ui.format_quest_preview(preview, UNKNOWN))
        with self.assertRaises(ValueError):
            self.model.apply()
        self.assertEqual(self.editor.applied, [])

    def test_reward_keys_and_uncertain_confirmation_are_independent(self):
        self.model.preview("rewards")
        self.model.toggle_reward("dust")
        with self.assertRaisesRegex(ValueError, "确认补发领取情况不明项"):
            self.model.apply()
        self.assertEqual(self.editor.applied, [])
        self.model.apply(allow_uncertain=True)
        self.assertEqual(self.editor.applied[0][1:], (["fixed", "dust"], True))

    def test_any_selected_reward_does_not_implicitly_allow_uncertain(self):
        self.model.preview("complete")
        self.model.apply()
        self.assertEqual(self.editor.applied[0][1:], (["fixed"], False))

    def test_unknown_and_random_rewards_are_unselectable_even_with_confirmation(self):
        self.model.preview("rewards")
        for key in ("unknown", "random"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.model.toggle_reward(key)
        self.model.selected_rewards.clear()
        self.model.apply(allow_uncertain=True)
        self.assertEqual(self.editor.applied[0][1], [])

    def test_uncertain_never_default_selected_even_if_flagged_by_preview(self):
        original = self.editor.preview_quest
        def preview(*args, **kwargs):
            result = original(*args, **kwargs)
            result["rewards"][1]["default_selected"] = True
            return result
        self.editor.preview_quest = preview
        self.model.preview("rewards")
        self.assertEqual(self.model.selected_rewards, {"fixed"})

    def test_input_change_refresh_and_revision_change_invalidate_preview(self):
        mutations = [lambda: self.model.select_route("other"),
                     lambda: self.model.select_stage("2"),
                     lambda: self.model.select_quest(UNKNOWN["id"]),
                     lambda: self.model.refresh(),
                     lambda: setattr(self.editor, "revision", 4),
                     lambda: setattr(self.model, "slot", "AutoSave0"),
                     lambda: setattr(self.editor, "source_sha256", "new-source")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.setUp()
                self.model.preview("complete")
                mutate()
                with self.assertRaises(ValueError):
                    self.model.apply()
                self.assertEqual(self.editor.applied, [])

    def test_preview_value_error_keeps_original_pending_and_clears_old_preview(self):
        self.editor.pending = [{"path": "Inventory.Keep"}]
        self.model.preview("complete")
        self.editor.preview_error = "缺少路线证据"
        with self.assertRaisesRegex(ValueError, "缺少路线证据"):
            self.model.preview("stage")
        self.assertIsNone(self.model.current_preview)
        self.assertEqual(self.editor.pending, [{"path": "Inventory.Keep"}])

    def test_refresh_drops_stage_when_order_evidence_is_no_longer_available(self):
        self.model.select_route("good")
        self.model.select_stage("2")
        self.editor.quests[0]["routes"][0]["stage_order_verified"] = False
        self.model.refresh()
        self.assertIsNone(self.model.stage_id)


class CoreConnectionTests(unittest.TestCase):
    def test_real_core_preview_apply_reward_and_undo_stay_in_memory(self):
        from pathlib import Path
        from pbz_editor.core import SaveEditor
        from test_core import archive, CatalogStub
        self.assertIsNotNone(ui)
        spec = copy.deepcopy(QUEST)
        spec["rewards"] = [reward("unique-key", item_id="AC_Drink", storage="AccessoryInfos",
                                   kind="accessory", quantity=1)]
        catalog = SimpleNamespace(quests=[spec], find=lambda key: spec if key == spec["id"] else None)
        document = dict(Inventory={"AccessoryInfos": {}}, PBZGameSystem={"CycleCount": 1},
                        Narrative={"NarrativeSavedQuests": [dict(QuestClass=QUEST["id"],
                            QuestCompletion="EQuestCompletion::QC_Started", CompletedBranches=[],
                            QuestBranches=[dict(BranchID="1", BranchType="EBranchType::BranchActivating"),
                                           dict(BranchID="2", BranchType="EBranchType::None")])]})
        source = archive({"ManualSave1": json.dumps(document, ensure_ascii=False).encode()})
        with patch.object(Path, "read_bytes", return_value=source), \
             patch.object(Path, "write_bytes", side_effect=AssertionError("禁止写存档")), \
             patch.object(gui.tk, "Tk", side_effect=AssertionError("禁止启动Tk")):
            editor = SaveEditor.open("headless-fixture.sav", CatalogStub(), quest_catalog=catalog)
            model = ui.QuestModel(editor, "ManualSave1")
            model.refresh()
            model.select_quest(QUEST["id"])
            model.select_route("good")
            preview = model.preview("complete")
            self.assertEqual(editor.changes(), [])
            self.assertIn("初遇", ui.format_quest_preview(preview, model.quest))
            self.assertEqual(model.selected_rewards, {"unique-key"})
            model.apply()
            changed = editor._documents["ManualSave1"]
            self.assertEqual(changed["Narrative"]["NarrativeSavedQuests"][0]["QuestCompletion"],
                             "EQuestCompletion::QC_Succeded")
            self.assertIn("AC_Drink", changed["Inventory"]["AccessoryInfos"])
            self.assertTrue(editor.changes())
            editor.undo()
            self.assertEqual(editor.changes(), [])
            self.assertEqual(editor.build(), source)


class Variable:
    def __init__(self, master=None, value="", **kwargs):
        self.value = value
        self.callbacks = []
    def get(self):
        return self.value
    def set(self, value):
        self.value = value
        for callback in self.callbacks:
            callback(None, None, None)
    def trace_add(self, mode, callback):
        self.callbacks.append(callback)


class Widget:
    """Small native-widget substitute; never creates Tcl/Tk or a desktop surface."""
    def __init__(self, parent=None, **kwargs):
        self.parent, self.options = parent, kwargs
        self.children, self.pages, self.items = [], [], {}
        self.selected, self.bindings, self.text = [], {}, ""
        if isinstance(parent, Widget):
            parent.children.append(self)
    def configure(self, *args, **kwargs):
        self.options.update(kwargs)
    config = configure
    def cget(self, key):
        return self.options.get(key, "normal")
    def grid(self, **kwargs):
        self.grid_options = kwargs
    def pack(self, **kwargs):
        pass
    def columnconfigure(self, *args, **kwargs):
        pass
    rowconfigure = columnconfigure
    heading = columnconfigure
    column = columnconfigure
    def add(self, child, **kwargs):
        self.pages.append((child, kwargs))
    def bind(self, event, callback):
        self.bindings[event] = callback
    def insert(self, *args, **kwargs):
        if "values" in kwargs:
            self.items[kwargs["iid"]] = kwargs
        elif len(args) == 2:
            self.text += str(args[1])
    def delete(self, *args):
        self.items.clear()
        self.text = ""
        self.selected = []
    def get_children(self):
        return tuple(self.items)
    def selection(self):
        return tuple(self.selected)
    def selection_set(self, value):
        self.selected = [value] if isinstance(value, (str, int)) else list(value)
    def curselection(self):
        return ()
    def see(self, *args):
        pass
    def set(self, *args):
        pass
    def yview(self, *args):
        pass
    xview = yview
    def title(self, value):
        self.window_title = value
    def geometry(self, value):
        pass
    minsize = columnconfigure
    def theme_names(self):
        return []
    def identify_row(self, y):
        return str(y)


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ui, "任务页尚未实现")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(gui.tk, "Tk", side_effect=AssertionError("禁止启动Tk")))
        # QuestPanel's base was bound before ttk constructors are replaced.
        for name in ("__init__", "columnconfigure", "rowconfigure", "grid", "pack", "configure"):
            self.stack.enter_context(patch.object(ui.QuestPanel.__bases__[0], name, getattr(Widget, name)))
        for module in (gui.tk,):
            for name in ("StringVar", "BooleanVar"):
                self.stack.enter_context(patch.object(module, name, Variable))
            for name in ("Text", "Listbox"):
                self.stack.enter_context(patch.object(module, name, Widget))
        for name in ("Frame", "Label", "Button", "Entry", "Combobox", "Treeview", "Scrollbar",
                     "Checkbutton", "Radiobutton", "Panedwindow", "Notebook", "Progressbar", "Style"):
            self.stack.enter_context(patch.object(gui.ttk, name, Widget))
        self.stack.enter_context(patch.object(gui.font, "nametofont", return_value=Widget()))
        self.editor = EditorDouble()
        self.refresh_count = 0
        self.host = SimpleNamespace(editor=self.editor, slot="ManualSave1", busy=False,
                                    root=Widget(), status_var=Variable(), _refresh=self.refresh_host)
        self.panel = ui.QuestPanel(Widget(), self.host)
        self.panel.refresh()
        self.panel.model.select_quest(QUEST["id"])
        self.panel._render_selection()

    def refresh_host(self):
        self.refresh_count += 1
        self.panel.refresh()

    def test_panel_success_refreshes_shared_pending_without_extra_approval(self):
        self.panel._preview("complete")
        self.panel._apply()
        self.assertEqual(self.refresh_count, 1)
        self.assertEqual(len(self.editor.pending), 1)
        self.assertIn("暂存", self.host.status_var.get())
        self.assertIsNone(self.panel.model.current_preview)

    def test_confirmation_is_explicit_and_reset_when_inputs_change(self):
        self.panel._preview("rewards")
        self.panel.model.toggle_reward("dust")
        self.panel._apply()
        self.assertEqual(self.editor.applied, [])
        self.assertIn("确认补发领取情况不明项", self.host.status_var.get())
        self.panel.confirm_var.set(True)
        self.panel._apply()
        self.assertEqual(self.editor.applied[0][2], True)
        self.assertFalse(self.panel.confirm_var.get())

    def test_busy_and_can_apply_disable_apply_but_fail_does_not_need_route(self):
        self.panel.model.select_quest(UNKNOWN["id"])
        self.panel._render_selection()
        self.assertEqual(self.panel.action_buttons["fail"].cget("state"), "normal")
        self.assertEqual(self.panel.action_buttons["reset"].cget("state"), "normal")
        self.editor.can_apply = False
        self.panel._preview("stage")
        self.assertEqual(self.panel.apply_button.cget("state"), "disabled")
        self.assertIn("缺少真实成功路线", self.panel.preview_text.text)
        self.panel.set_busy(True)
        self.panel._preview("fail")
        self.panel._apply()
        self.assertEqual(len(self.editor.calls), 1)
        self.assertEqual(self.editor.applied, [])
        self.assertEqual(self.panel.action_buttons["fail"].cget("state"), "disabled")

    def test_unaccepted_preconditions_and_no_reset_for_absent_record(self):
        self.editor.quests[0].update(status="unaccepted", record_present=False)
        self.panel.refresh()
        self.assertIn("前置未核实", self.panel.preview_text.text)
        self.assertEqual(self.panel.action_buttons["reset"].cget("state"), "disabled")

    def test_preview_error_is_inline_and_preserves_pending(self):
        self.editor.pending = [{"path": "keep"}]
        self.editor.preview_error = "未核实路线"
        self.panel._preview("complete")
        self.assertIn("未核实路线", self.panel.preview_text.text)
        self.assertEqual(self.editor.pending, [{"path": "keep"}])
        self.assertEqual(self.panel.apply_button.cget("state"), "disabled")

    def test_stages_follow_verified_order_not_completed_branch_set(self):
        self.panel.model.select_route("good")
        self.panel._render_selection()
        self.assertEqual([v for v in self.panel.stage_choices.values() if v is not None], ["1", "2"])
        self.assertEqual(self.panel.stage_box.cget("state"), "readonly")
        self.panel.model.select_route("other")
        self.panel._render_selection()
        self.assertEqual([v for v in self.panel.stage_choices.values() if v is not None], [])
        self.assertEqual(self.panel.stage_box.cget("state"), "disabled")
        self.assertEqual(self.panel.action_buttons["stage"].cget("state"), "disabled")
        self.assertEqual(self.panel.action_buttons["complete"].cget("state"), "normal")
        self.assertIn("阶段顺序未核实", self.panel.preview_text.text)

    def test_notebook_and_shared_changes_remain_outside_tabs(self):
        window = object.__new__(gui.EditorWindow)
        window.root = Widget()
        for name in ("path", "backup", "slot", "search", "owned", "quantity",
                     "quantity_label", "level", "description", "hint", "status", "changes"):
            setattr(window, name + "_var", Variable())
        window.editor, window.slot, window.busy = None, "", False
        window.rows, window.visible_rows, window.pending_changes = [], {}, []
        window._make_ui()
        self.assertEqual([options["text"] for _, options in window.notebook.pages], ["背包", "支线任务"])
        self.assertIs(window.quest_panel.host, window)
        self.assertIsNot(window.change_tree.parent.parent.parent, window.notebook)
        self.assertTrue(any(child.options.get("text") == ui.SCOPE_HINT for child in window.root.children),
                        "任务范围提示须在切换背包页后仍然可见")
        window._refresh()
        window.busy = True
        window._update_controls()
        self.assertTrue(window.quest_panel.busy)


class SaveConnectionTests(unittest.TestCase):
    def window(self):
        from pathlib import Path
        window = object.__new__(gui.EditorWindow)
        window.root = Widget()
        window.busy, window.slot = False, "ManualSave1"
        window.settings = {}
        window.catalog = SimpleNamespace(items=[])
        window.status_var, window.path_var = Variable(), Variable()
        window.slot_var, window.backup_var = Variable(), Variable(value="backups")
        window.slot_box = Widget()
        window.settings_store = SimpleNamespace(save=lambda settings: None)
        window.refreshes = 0
        def refresh():
            window.refreshes += 1
        window._refresh = refresh
        window.editor = SimpleNamespace(path=Path("memory.sav"), source_sha256="source",
                                        slots=["ManualSave1"], current_slot="ManualSave1")
        window.editor.changes = lambda: [{"slot": "ManualSave1", "path": "Quest.Q", "before": 0, "after": 1}]
        return window

    def test_save_uses_service_receipts_and_preserves_read_written_path(self):
        window = self.window()
        calls = []
        def save_editor(editor, backup_dir, destination=None):
            calls.append((editor, backup_dir, destination))
            return {"output_path": "memory.sav", "backup_path": "memory.bak"}
        window.services = SimpleNamespace(save_editor=save_editor)
        window.editor.build = lambda: self.fail("GUI must leave build/write/history transaction to service")
        window._text_dialog = lambda *a, **k: True
        window._read_written = lambda result, settings: (result, settings)
        outcomes = []
        window._finish_write = lambda result, slot, title: outcomes.append((result, slot)) or True
        window._submit = lambda label, work, done: done(work())
        window._save()
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], window.editor)
        self.assertIsNone(calls[0][2])
        self.assertEqual(outcomes[0][0][0]["backup_path"], "memory.bak")

    def test_history_load_precedes_refresh_and_failure_keeps_editor_loaded(self):
        window = self.window()
        editor = window.editor
        events = []
        def load_history(path):
            events.append("history")
            self.assertEqual(path, "backups")
            raise ValueError("收据损坏")
        editor.load_reward_history = load_history
        window._refresh = lambda: events.append("refresh")
        with patch.object(gui.messagebox, "showwarning") as warning:
            window._adopt_editor(editor)
        self.assertEqual(events, ["history", "refresh"])
        self.assertIs(window.editor, editor)
        self.assertIn("已载入", window.status_var.get())
        self.assertIn("收据", window.status_var.get())
        self.assertEqual(warning.call_count, 1)

    def test_saved_history_warning_is_shown_as_saved_and_reopen_refreshes(self):
        window = self.window()
        result = dict(output_path="memory.sav", backup_path="memory.bak", history_warning="收据写入失败")
        with patch.object(gui.messagebox, "showwarning") as warning, patch.object(gui.messagebox, "showinfo"):
            ok = window._finish_write((result, window.editor, "", "", {}), "ManualSave1", "保存完成")
        self.assertTrue(ok)
        self.assertEqual(window.refreshes, 1)
        self.assertEqual(warning.call_count, 1)
        self.assertIn("已保存", warning.call_args.args[1])
        self.assertIn("收据写入失败", warning.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
