"""Quest page and headless presentation model. Importing never starts Tk."""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import ttk


SCOPE_HINT = "仅修改任务记录与奖励；NPC、剧情和地图暂不处理"
STATUS_LABELS = {"all": "全部状态", "unaccepted": "未接取", "active": "进行中",
                 "completed": "已完成", "failed": "已失败", "unknown": "未知状态"}
ACTIONS = {"complete": "完成任务", "stage": "完成阶段", "fail": "标记失败",
           "reset": "回退未接取", "rewards": "仅补发奖励"}


def filter_quests(rows, status="all", query=""):
    query = query.strip().casefold()
    return [row for row in rows
            if (status == "all" or row.get("status") == status)
            and (not query or query in str(row.get("name", "")).casefold()
                 or query in str(row.get("id", "")).casefold())]


def _value(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def reward_explanation(row):
    parts = [str(row.get('reason') or '')]
    if row.get('certainty_reason'):
        parts.append(str(row['certainty_reason']))
    return '；'.join(dict.fromkeys(part for part in parts if part))


def format_quest_preview(preview, quest):
    """Render only public preview fields, never the core's private document."""
    lines = [f"操作：{ACTIONS[preview['action']]}", f"任务：{quest.get('name', quest['id'])}"]
    for key, label in (("route_id", "路线"), ("stage_id", "阶段")):
        if preview.get(key) is not None:
            lines.append(f"{label}：{preview[key]}")
    if preview["action"] == "fail":
        lines.append("标记任务记录失败并停止活动分支，不模拟失败剧情。")
    elif preview["action"] == "reset":
        lines.append("回退任务记录为未接取，保留已获得奖励；不保证世界事件重放。")
    if not quest.get("record_present"):
        lines.append("前置未核实：当前槽位没有此任务记录。")
    lines += ["", "流程预览："]
    flow = preview.get("flow", [])
    for row in flow:
        label = f"{row.get('name') or row['id']}（{row['id']}）"
        before, after = row.get("before", "—"), row.get("after", "—")
        lines.append(f"{label}：{before} → {after}")
        if row.get("description"):
            lines.append(f"  {row['description']}")
        if row.get("destination_state_id") is not None:
            lines.append(f"  → 状态 {row['destination_state_id']}")
    if not flow:
        lines.append("暂无已核实流程；仍可查看当前任务记录。")
    if preview.get('rewards'):
        lines += ['', '奖励依据（实际补发以勾选为准）：']
        for row in preview['rewards']:
            lines.append(f"{row.get('name', row.get('item_id', ''))}（{row.get('item_id', '')}）："
                         f"表内/基础数量 {row.get('quantity', '未查明')}，本次可补 {row.get('grant_amount', 0)}")
            lines.append(reward_explanation(row))
    lines += ["", "任务记录差异："]
    for change in preview.get("changes", []):
        lines.append(f"{change.get('path', '')}\n  {_value(change.get('before'))} → {_value(change.get('after'))}")
    if not preview.get("changes"):
        lines.append("无任务记录差异。")
    if preview.get("warnings"):
        lines += ["", "说明：", *map(str, preview["warnings"])]
    if not preview.get("can_apply"):
        lines.append("此预览暂不可应用。")
    return "\n".join(lines)


class QuestModel:
    """Own selections and bind a preview to the exact editor/input revision."""
    def __init__(self, editor=None, slot=""):
        self.editor, self.slot = editor, slot
        self.rows = []
        self.quest_id = None
        self.route_id = None
        self.stage_id = None
        self.invalidate()

    @property
    def quest(self):
        return next((row for row in self.rows if row["id"] == self.quest_id), None)

    def invalidate(self):
        self.current_preview = None
        self.selected_rewards = set()
        self._binding = None

    def refresh(self):
        self.invalidate()
        self.rows = self.editor.list_quests(self.slot) if self.editor is not None and self.slot else []
        if not self.quest:
            self.quest_id = None
            self.route_id = self.stage_id = None
        else:
            if self.route_id not in [r["id"] for r in self.quest.get("routes", [])]:
                self.route_id = None
            route = next((r for r in self.quest.get("routes", []) if r["id"] == self.route_id), {})
            order = route.get("stage_order", []) if route.get("stage_order_verified") is True else []
            if self.stage_id not in order:
                self.stage_id = None
        return self.rows

    def select_quest(self, quest_id):
        if quest_id == self.quest_id:
            return
        self.invalidate()
        self.quest_id = quest_id
        self.route_id = self.stage_id = None
        if self.quest:
            routes = self.quest.get("routes", [])
            if len(routes) == 1:
                self.route_id = routes[0]["id"]

    def select_route(self, route_id):
        if route_id != self.route_id:
            self.invalidate()
            self.route_id = route_id
            self.stage_id = None

    def select_stage(self, stage_id):
        if stage_id != self.stage_id:
            self.invalidate()
            self.stage_id = stage_id

    def _signature(self):
        return (id(self.editor), self.slot, self.quest_id, self.route_id, self.stage_id,
                getattr(self.editor, "revision", None), getattr(self.editor, "source_sha256", None))

    @property
    def preview_is_current(self):
        return self.current_preview is not None and self._binding == self._signature()

    def preview(self, action):
        self.invalidate()
        if self.editor is None or not self.slot or not self.quest:
            raise ValueError("请先载入存档并选择任务。")
        if action not in ACTIONS:
            raise ValueError("不支持的任务操作。")
        route_id = self.route_id if action in ("complete", "stage", "rewards") else None
        stage_id = self.stage_id if action in ("stage", "rewards") else None
        result = self.editor.preview_quest(self.slot, self.quest_id, action,
                                           route_id=route_id, stage_id=stage_id)
        self.current_preview = result
        self._binding = self._signature()
        self.selected_rewards = {r["key"] for r in result.get("rewards", [])
                                 if r.get("certainty") == "verified" and r.get("default_selected")
                                 and not r.get("requires_confirmation")}
        return result

    def toggle_reward(self, key):
        if not self.preview_is_current:
            raise ValueError("请重新生成预览。")
        row = next((row for row in self.current_preview.get("rewards", []) if row["key"] == key), None)
        if row is None or row.get("certainty") != "verified":
            raise ValueError("奖励未核实或为随机结果，不能补发。")
        if key in self.selected_rewards:
            self.selected_rewards.remove(key)
        else:
            self.selected_rewards.add(key)

    @property
    def needs_confirmation(self):
        return bool(self.current_preview and any(
            row["key"] in self.selected_rewards and row.get("requires_confirmation")
            for row in self.current_preview.get("rewards", [])))

    def apply(self, allow_uncertain=False):
        if not self.preview_is_current:
            raise ValueError("预览已失效，请重新预览后应用。")
        if not self.current_preview.get("can_apply"):
            raise ValueError("；".join(self.current_preview.get("warnings", [])) or "此预览暂不可应用。")
        if self.needs_confirmation and not allow_uncertain:
            raise ValueError("请勾选“确认补发领取情况不明项”。")
        # Preserve the preview order and pass keys, never item IDs or whole rows.
        selected = [row["key"] for row in self.current_preview.get("rewards", [])
                    if row["key"] in self.selected_rewards]
        result = self.editor.apply_quest(self.current_preview, selected_rewards=selected,
                                         allow_uncertain=bool(allow_uncertain))
        self.invalidate()
        return result


class QuestPanel(ttk.Frame):
    def __init__(self, parent, host):
        super().__init__(parent, padding=(6, 4))
        self.host = host
        self.busy = bool(host.busy)
        self.model = QuestModel(host.editor, host.slot)
        self.visible_rows = {}
        self.reward_rows = {}
        self.status_var = tk.StringVar(master=self, value=STATUS_LABELS["all"])
        self.search_var = tk.StringVar(master=self)
        self.route_var = tk.StringVar(master=self)
        self.stage_var = tk.StringVar(master=self)
        self.detail_var = tk.StringVar(master=self, value="请先载入存档并选择任务。")
        self.confirm_var = tk.BooleanVar(master=self, value=False)
        self._make_ui()
        self.search_var.trace_add("write", lambda *_: self._render_rows())
        self._sync_controls()

    def _make_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        body = ttk.Panedwindow(self, orient="horizontal")
        body.grid(row=0, column=0, sticky="nsew")
        left, right = ttk.Frame(body), ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(left, weight=2)
        body.add(right, weight=3)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        filters = ttk.Frame(left)
        filters.grid(row=0, column=0, sticky="ew")
        ttk.Label(filters, text="任务状态").pack(side="left")
        self.status_box = ttk.Combobox(filters, textvariable=self.status_var, width=12,
                                       values=list(STATUS_LABELS.values()), state="readonly")
        self.status_box.pack(side="left", padx=6)
        self.status_box.bind("<<ComboboxSelected>>", lambda _e: self._render_rows())
        search = ttk.Frame(left)
        search.grid(row=1, column=0, sticky="ew", pady=5)
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="中文名 / ID").grid(row=0, column=0, padx=(0, 5))
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew")
        table = ttk.Frame(left)
        table.grid(row=2, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.quest_tree = ttk.Treeview(table, columns=("name", "status", "stage", "id"),
                                      show="headings", selectmode="browse", height=8)
        for key, label, width in (("name", "任务", 130), ("status", "状态", 70),
                                  ("stage", "当前阶段", 100), ("id", "完整任务 ID", 300)):
            self.quest_tree.heading(key, text=label)
            self.quest_tree.column(key, width=width, minwidth=60)
        self.quest_tree.grid(row=0, column=0, sticky="nsew")
        self._scrollbars(table, self.quest_tree)
        self.quest_tree.bind("<<TreeviewSelect>>", self._select_quest)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)
        ttk.Label(right, textvariable=self.detail_var, wraplength=620).grid(row=0, column=0, sticky="w")
        choices = ttk.Frame(right)
        choices.grid(row=1, column=0, sticky="ew", pady=4)
        choices.columnconfigure(1, weight=1)
        choices.columnconfigure(3, weight=1)
        ttk.Label(choices, text="路线").grid(row=0, column=0)
        self.route_box = ttk.Combobox(choices, textvariable=self.route_var, width=18, state="readonly")
        self.route_box.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Label(choices, text="阶段").grid(row=0, column=2)
        self.stage_box = ttk.Combobox(choices, textvariable=self.stage_var, width=18, state="readonly")
        self.stage_box.grid(row=0, column=3, sticky="ew", padx=4)
        self.route_box.bind("<<ComboboxSelected>>", self._choose_route)
        self.stage_box.bind("<<ComboboxSelected>>", self._choose_stage)
        actions = ttk.Frame(right)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        self.action_buttons = {}
        for index, (action, label) in enumerate(ACTIONS.items()):
            button = ttk.Button(actions, text=label, command=lambda a=action: self._preview(a))
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0, 4), pady=2)
            actions.columnconfigure(index % 3, weight=1)
            self.action_buttons[action] = button
        self.preview_tabs = ttk.Notebook(right)
        self.preview_tabs.grid(row=3, column=0, sticky="nsew")
        preview_frame = ttk.Frame(self.preview_tabs)
        self.preview_tabs.add(preview_frame, text="流程 / 修改预览")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.preview_text = tk.Text(preview_frame, height=7, wrap="word", state="disabled")
        self.preview_text.grid(row=0, column=0, sticky="nsew")
        pbar = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview_text.yview)
        pbar.grid(row=0, column=1, sticky="ns")
        self.preview_text.configure(yscrollcommand=pbar.set)
        rewards_tab = ttk.Frame(self.preview_tabs)
        self.preview_tabs.add(rewards_tab, text="奖励预览 / 勾选")
        rewards_tab.columnconfigure(0, weight=1)
        rewards_tab.rowconfigure(1, weight=1)
        ttk.Label(rewards_tab, text="点选框或按空格勾选；未核实/随机项不可选").grid(
            row=0, column=0, sticky="w", pady=(2, 2))
        rewards = ttk.Frame(rewards_tab)
        rewards.grid(row=1, column=0, sticky="nsew")
        rewards.columnconfigure(0, weight=1)
        rewards.rowconfigure(0, weight=1)
        self.reward_tree = ttk.Treeview(rewards, columns=("selected", "name", "id", "current", "grant", "reason"),
                                       show="headings", height=3, selectmode="browse")
        for key, label, width in (("selected", "选择", 45), ("name", "奖励", 110),
                                  ("id", "物品 ID", 120), ("current", "现有", 50),
                                  ("grant", "补发", 50), ("reason", "领取/证据说明", 230)):
            self.reward_tree.heading(key, text=label)
            self.reward_tree.column(key, width=width, minwidth=40)
        self.reward_tree.grid(row=0, column=0, sticky="nsew")
        self._scrollbars(rewards, self.reward_tree)
        self.reward_tree.bind("<Button-1>", self._click_reward)
        self.reward_tree.bind("<space>", self._toggle_reward)
        bottom = ttk.Frame(right)
        bottom.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        self.confirm_box = ttk.Checkbutton(bottom, text="确认补发领取情况不明项",
                                           variable=self.confirm_var, command=self._sync_controls)
        self.confirm_box.pack(side="left")
        self.apply_button = ttk.Button(bottom, text="应用预览到修改清单", command=self._apply)
        self.apply_button.pack(side="right")

    @staticmethod
    def _scrollbars(parent, tree):
        ybar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)

    def refresh(self):
        # Catalog/read failures intentionally propagate to the host's error handler.
        self.model.editor, self.model.slot = self.host.editor, self.host.slot
        self.model.refresh()
        self._render_rows()

    def set_busy(self, busy):
        self.busy = bool(busy)
        self._sync_controls()

    def _render_rows(self):
        status = next((key for key, label in STATUS_LABELS.items() if label == self.status_var.get()), "all")
        rows = filter_quests(self.model.rows, status, self.search_var.get())
        selected_id = self.model.quest_id
        self.quest_tree.delete(*self.quest_tree.get_children())
        self.visible_rows = {}
        for index, row in enumerate(rows):
            iid = str(index)
            self.visible_rows[iid] = row
            label = row.get("status_label") or STATUS_LABELS.get(row.get("status"), "未知状态")
            if not row.get("record_present"):
                label += "（前置未核实）"
            self.quest_tree.insert("", "end", iid=iid, values=(row.get("name", row["id"]), label,
                                   row.get("current_stage", "—"), row["id"]))
        selected = next((iid for iid, row in self.visible_rows.items() if row["id"] == selected_id), None)
        if selected is None and rows:
            selected = "0"
        self.model.select_quest(self.visible_rows[selected]["id"] if selected is not None else None)
        if selected is not None:
            self.quest_tree.selection_set(selected)
            self.quest_tree.see(selected)
        self._render_selection()

    def _select_quest(self, _event=None):
        if self.busy or self.host.busy:
            return
        selection = self.quest_tree.selection()
        row = self.visible_rows.get(selection[0]) if selection else None
        quest_id = row["id"] if row else None
        if quest_id != self.model.quest_id:
            self.model.select_quest(quest_id)
            self._render_selection()

    def _render_selection(self):
        quest = self.model.quest
        self.confirm_var.set(False)
        self.model.invalidate()
        self._set_text("")
        self._render_rewards()
        self.route_choices = {"不指定路线": None}
        self.stage_choices = {"不指定阶段": None}
        if quest:
            text = f"{quest.get('name', '')} ｜ {quest.get('status_label', '未知状态')} ｜ {quest.get('current_stage', '—')}\n{quest['id']}"
            if not quest.get("record_present"):
                text += "\n前置未核实：当前槽位没有任务记录。"
            notes = quest.get("notes", [])
            if notes:
                text += "\n" + "；".join(map(str, notes))
            if not quest.get("routes"):
                text += "\n暂无已核实成功路线；可查看记录及预览失败/回退。"
            self.detail_var.set(text.split('\n', 1)[0])
            for route in quest.get("routes", []):
                self.route_choices[f"{route.get('name') or route['id']}（{route['id']}）"] = route["id"]
            branches = {branch["id"]: branch for branch in quest.get("branches", [])}
            for branch_id in self._stage_order():
                branch = branches.get(branch_id, {})
                self.stage_choices[f"{branch.get('name') or branch_id}（{branch_id}）"] = branch_id
            if not self._stage_order():
                text += "\n阶段顺序未核实，不能完成阶段；请选择有阶段顺序证据的路线。"
            self._set_text("当前任务记录：\n" + text +
                           "\n\n选择操作生成预览，检查流程和奖励后应用。\n"
                           "失败仅修改记录，不模拟失败剧情；回退保留奖励，不保证世界事件重放。")
        else:
            self.detail_var.set("请先载入存档并选择任务。")
        for box, variable, choices, current in (
                (self.route_box, self.route_var, self.route_choices, self.model.route_id),
                (self.stage_box, self.stage_var, self.stage_choices, self.model.stage_id)):
            box.configure(values=list(choices))
            variable.set(next((label for label, value in choices.items() if value == current), next(iter(choices))))
        self._sync_controls()

    def _choose_route(self, _event=None):
        if self.busy or self.host.busy:
            return
        self.model.select_route(self.route_choices.get(self.route_var.get()))
        self._render_selection()

    def _stage_order(self):
        quest = self.model.quest or {}
        route = next((row for row in quest.get("routes", []) if row["id"] == self.model.route_id), {})
        order = route.get("stage_order")
        return order if route.get("stage_order_verified") is True and isinstance(order, list) else []

    def _choose_stage(self, _event=None):
        if self.busy or self.host.busy:
            return
        self.model.select_stage(self.stage_choices.get(self.stage_var.get()))
        self._render_selection()

    def _set_text(self, text):
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state="disabled")

    def _preview(self, action):
        if self.busy or self.host.busy:
            return
        self.confirm_var.set(False)
        try:
            preview = self.model.preview(action)
            self._set_text(format_quest_preview(preview, self.model.quest))
            self.host.status_var.set("预览已生成，尚未改变暂存；检查奖励后应用。")
        except Exception as error:
            self.model.invalidate()
            self._set_text(f"无法预览：{error}\n原暂存修改仍保留。")
            self.host.status_var.set(f"无法预览：{error}；原暂存修改仍保留。")
        self._render_rewards()
        self._sync_controls()

    def _render_rewards(self):
        self.reward_tree.delete(*self.reward_tree.get_children())
        self.reward_rows = {}
        preview = self.model.current_preview
        for index, row in enumerate(preview.get("rewards", []) if preview else []):
            iid = str(index)
            self.reward_rows[iid] = row
            verified = row.get("certainty") == "verified"
            mark = ("☑" if row["key"] in self.model.selected_rewards else "☐") if verified else "—"
            reason = reward_explanation(row)
            if not verified:
                reason = ("随机奖励不可选；" if row.get("certainty") == "random" else "奖励未核实，不可选；") + reason
            elif row.get("requires_confirmation"):
                reason = "领取情况不明，需勾选确认；" + reason
            self.reward_tree.insert("", "end", iid=iid, values=(mark, row.get("name", ""),
                row.get("item_id", ""), row.get("current_amount", "—"), row.get("grant_amount", 0), reason))

    def _click_reward(self, event):
        if self.reward_tree.identify_column(event.x) == "#1":
            iid = self.reward_tree.identify_row(event.y)
            return self._toggle_reward(iid=iid)

    def _toggle_reward(self, _event=None, iid=None):
        if self.busy or self.host.busy:
            return "break"
        selection = self.reward_tree.selection()
        iid = iid if iid is not None else (selection[0] if selection else None)
        row = self.reward_rows.get(iid)
        if row:
            try:
                self.model.toggle_reward(row["key"])
            except ValueError as error:
                self.host.status_var.set(str(error))
            self._render_rewards()
            self.reward_tree.selection_set(iid)
            self._sync_controls()
        return "break"

    def _apply(self):
        if self.busy or self.host.busy:
            return
        try:
            self.model.apply(allow_uncertain=bool(self.confirm_var.get()))
        except Exception as error:
            self.host.status_var.set(f"无法应用：{error}；原暂存修改仍保留。")
            self._sync_controls()
            return
        self.confirm_var.set(False)
        self.host._refresh()
        self.host.status_var.set("任务与选中奖励已暂存到统一修改清单，尚未写入文件。")

    def _sync_controls(self):
        loaded = self.host.editor is not None and bool(self.host.slot) and not (self.busy or self.host.busy)
        selected = loaded and self.model.quest is not None
        self.status_box.configure(state="readonly" if loaded else "disabled")
        self.search_entry.configure(state="normal" if loaded else "disabled")
        self.route_box.configure(state="readonly" if selected and self.model.quest.get("routes") else "disabled")
        self.stage_box.configure(state="readonly" if selected and self._stage_order() else "disabled")
        for action, button in self.action_buttons.items():
            enabled = selected and (action != "reset" or self.model.quest.get("record_present"))
            if action == "stage":
                enabled = enabled and bool(self._stage_order())
            button.configure(state="normal" if enabled else "disabled")
        current = selected and self.model.preview_is_current
        self.confirm_box.configure(state="normal" if current and self.model.needs_confirmation else "disabled")
        enabled = (current and self.model.current_preview.get("can_apply")
                   and (not self.model.needs_confirmation or self.confirm_var.get()))
        self.apply_button.configure(state="normal" if enabled else "disabled")
