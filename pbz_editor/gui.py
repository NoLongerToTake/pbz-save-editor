"""Native, offline Tk UI. Importing this module never opens or writes a save.

Only run() imports the runtime modules. All workers return through a queue;
widgets, variables and dialogs are exclusively accessed on the Tk thread.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from queue import Empty, Queue
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

from .quest_ui import QuestPanel, SCOPE_HINT


ALL_CATEGORIES = "全部分类"
APP_ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = ("书籍", "书页", "剧情道具", "材料/药材", "记忆花瓣/完整花",
              "饰品", "主武器", "杀气武器", "药品", "未识别物品")
LEVEL_KINDS = {"accessory", "shaqi_weapon", "medicine"}
OWNERSHIP_KINDS = {"accessory", "main_weapon", "shaqi_weapon"}


def build_item_rows(specs, records):
    """Merge by both native storage and ID; retain unknown native records."""
    rows = {}
    for spec in specs:
        row = {key: getattr(spec, key, "") for key in
               ("item_id", "name", "category", "storage", "kind", "description")}
        row.update(quantity=0, level=None, owned=False, equipped=False, record=None)
        rows[(row["storage"], row["item_id"])] = row
    for record in records:
        key = (record["storage"], record["item_id"])
        row = rows.get(key, {"description": ""}).copy()
        row.update(record)
        rows[key] = row
    return list(rows.values())


def filter_items(rows, category=ALL_CATEGORIES, query="", owned_only=True):
    query = query.strip().casefold()
    return [row for row in rows
            if (category == ALL_CATEGORIES or row.get("category") == category)
            and (not owned_only or row.get("owned", False))
            and (not query or query in str(row.get("name", "")).casefold()
                 or query in str(row.get("item_id", "")).casefold())]


def parse_edit_values(quantity, level, kind):
    def integer(value, label):
        value = value.strip()
        if not value or not value.isascii() or not value.isdecimal():
            raise ValueError(f"{label}请输入大于或等于 0 的整数。")
        return int(value)
    amount = integer(quantity, "数量")
    if kind in OWNERSHIP_KINDS and amount not in (0, 1):
        raise ValueError("此类装备数量只能是 0（移除）或 1（拥有）。")
    rank = integer(level, "等级") if kind in LEVEL_KINDS and level.strip() else None
    return amount, rank


def format_change(change):
    return (str(change.get("slot", "—")), str(change.get("path", "")),
            json.dumps(change.get("before"), ensure_ascii=False, default=str),
            json.dumps(change.get("after"), ensure_ascii=False, default=str))


def _path(value):
    if not str(value).strip():
        raise ValueError("路径不能为空。")
    return Path(str(value).strip().strip('"')).expanduser().absolute()


class EditorWindow:
    def __init__(self, root, catalog_class, editor_class, services):
        self.root = root
        self.Catalog = catalog_class
        self.SaveEditor = editor_class
        self.services = services
        self.catalog = None
        self.editor = None
        self.slot = ""
        self.rows = []
        self.visible_rows = {}
        self.pending_changes = []
        self.settings = {}
        self.settings_store = services.SettingsStore(APP_ROOT)
        self.busy = False
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pbz-file")
        self.results = Queue()
        self.path_var = tk.StringVar()
        self.backup_var = tk.StringVar()
        self.slot_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.owned_var = tk.BooleanVar(value=True)
        self.quantity_var = tk.StringVar(value="1")
        self.quantity_label_var = tk.StringVar(value="数量")
        self.level_var = tk.StringVar()
        self.description_var = tk.StringVar(value="选择物品后，可在下方添加、修改或移除。")
        self.hint_var = tk.StringVar()
        self.status_var = tk.StringVar(value="正在查找本机存档…")
        self.changes_var = tk.StringVar(value="待修改：0 项（仅暂存在内存）")
        self._make_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(80, self._drain_results)
        self.root.after_idle(self._startup)

    def _make_ui(self):
        root = self.root
        root.title("PBZ 存档编辑器 1.0 Beta · 离线")
        root.geometry("1160x820")
        root.minsize(860, 650)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            font.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=28)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        paths = ttk.Frame(root, padding=(12, 10, 12, 4))
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        ttk.Label(paths, text="存档路径").grid(row=0, column=0, padx=(0, 8))
        self.path_box = ttk.Combobox(paths, textvariable=self.path_var)
        self.path_box.grid(row=0, column=1, sticky="ew")
        self.path_box.bind("<Return>", lambda _e: self._open_path())
        self.path_box.bind("<<ComboboxSelected>>", lambda _e: self._open_path())
        self.browse_button = ttk.Button(paths, text="浏览…", command=self._browse_save)
        self.browse_button.grid(row=0, column=2, padx=6)
        self.open_button = ttk.Button(paths, text="载入", command=self._open_path)
        self.open_button.grid(row=0, column=3)
        self.discover_button = ttk.Button(paths, text="重新查找", command=self._discover)
        self.discover_button.grid(row=0, column=4, padx=(6, 0))
        ttk.Label(paths, text="备份目录").grid(row=1, column=0, pady=(8, 0), padx=(0, 8))
        self.backup_entry = ttk.Entry(paths, textvariable=self.backup_var)
        self.backup_entry.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        self.backup_browse = ttk.Button(paths, text="选择…", command=self._browse_backup)
        self.backup_browse.grid(row=1, column=2, padx=6, pady=(8, 0))
        self.restore_button = ttk.Button(paths, text="恢复备份…", command=self._restore)
        self.restore_button.grid(row=1, column=3, columnspan=2, sticky="ew", pady=(8, 0))
        toolbar = ttk.Frame(root, padding=(12, 6))
        toolbar.grid(row=1, column=0, sticky="ew")
        ttk.Label(toolbar, text="编辑槽位").pack(side="left")
        self.slot_box = ttk.Combobox(toolbar, width=24, textvariable=self.slot_var, state="readonly")
        self.slot_box.pack(side="left", padx=8)
        self.slot_box.bind("<<ComboboxSelected>>", self._switch_slot)
        self.save_button = ttk.Button(toolbar, text="预览并保存", command=self._save)
        self.save_button.pack(side="right")
        self.save_as_button = ttk.Button(toolbar, text="另存为…", command=lambda: self._save(save_as=True))
        self.save_as_button.pack(side="right", padx=6)
        self.backup_button = ttk.Button(toolbar, text="立即备份", command=self._backup)
        self.backup_button.pack(side="right", padx=6)
        ttk.Label(toolbar, text="编辑仅暂存；保存前会备份源文件").pack(side="left", padx=8)
        pane = ttk.Panedwindow(root, orient="vertical")
        pane.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        self.notebook = ttk.Notebook(pane)
        pane.add(self.notebook, weight=4)
        upper = ttk.Panedwindow(self.notebook, orient="horizontal")
        self.notebook.add(upper, text="背包")
        left = ttk.Frame(upper)
        upper.add(left, weight=1)
        ttk.Label(left, text="物品分类").pack(anchor="w", pady=(0, 6))
        category_frame = ttk.Frame(left)
        category_frame.pack(fill="both", expand=True)
        self.category_list = tk.Listbox(category_frame, width=18, exportselection=False,
                                       relief="flat", highlightthickness=1)
        self.category_list.pack(side="left", fill="both", expand=True)
        category_scroll = ttk.Scrollbar(category_frame, orient="vertical", command=self.category_list.yview)
        category_scroll.pack(side="right", fill="y")
        self.category_list.configure(yscrollcommand=category_scroll.set)
        self.category_list.bind("<<ListboxSelect>>", lambda _e: self._render_items())
        self._set_categories([])
        right = ttk.Frame(upper, padding=(10, 0, 0, 0))
        upper.add(right, weight=5)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1, minsize=130)
        search = ttk.Frame(right)
        search.grid(row=0, column=0, sticky="ew")
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="搜索中文名 / ID").grid(row=0, column=0, padx=(0, 8))
        self.search_entry = ttk.Entry(search, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.owned_radio = ttk.Radiobutton(search, text="已有", variable=self.owned_var,
                                          value=True, command=self._render_items)
        self.owned_radio.grid(row=0, column=2, padx=(10, 4))
        self.all_radio = ttk.Radiobutton(search, text="全部（可添加）", variable=self.owned_var,
                                        value=False, command=self._render_items)
        self.all_radio.grid(row=0, column=3)
        self.search_var.trace_add("write", lambda *_: self._render_items())
        ttk.Label(right, textvariable=self.hint_var, foreground="#986100").grid(
            row=1, column=0, sticky="w", pady=(4, 4))
        table = ttk.Frame(right)
        table.grid(row=2, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.item_tree = ttk.Treeview(table, columns=("name", "id", "quantity", "level", "state"),
                                     show="headings", selectmode="browse")
        for key, title, width, anchor in (("name", "中文名", 200, "w"), ("id", "物品 ID", 190, "w"),
                                         ("quantity", "数量", 70, "e"), ("level", "等级", 60, "e"),
                                         ("state", "状态", 95, "center")):
            self.item_tree.heading(key, text=title)
            self.item_tree.column(key, width=width, minwidth=50, anchor=anchor)
        self.item_tree.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(table, orient="vertical", command=self.item_tree.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar = ttk.Scrollbar(table, orient="horizontal", command=self.item_tree.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.item_tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.item_tree.bind("<<TreeviewSelect>>", self._select_item)
        self.item_tree.bind("<Double-1>", lambda _e: self.quantity_entry.focus_set())
        ttk.Label(right, textvariable=self.description_var, wraplength=760).grid(
            row=3, column=0, sticky="w", pady=5)
        edit = ttk.Frame(right)
        edit.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(edit, textvariable=self.quantity_label_var).pack(side="left")
        self.quantity_entry = ttk.Entry(edit, textvariable=self.quantity_var, width=10)
        self.quantity_entry.pack(side="left", padx=(6, 16))
        ttk.Label(edit, text="等级").pack(side="left")
        self.level_entry = ttk.Entry(edit, textvariable=self.level_var, width=8)
        self.level_entry.pack(side="left", padx=6)
        self.add_button = ttk.Button(edit, text="添加", command=lambda: self._edit_item(True))
        self.add_button.pack(side="left", padx=(10, 4))
        self.modify_button = ttk.Button(edit, text="修改", command=self._edit_item)
        self.modify_button.pack(side="left", padx=4)
        self.remove_button = ttk.Button(edit, text="移除", command=self._remove_item)
        self.remove_button.pack(side="left", padx=4)
        self.quest_panel = QuestPanel(self.notebook, self)
        self.notebook.add(self.quest_panel, text="支线任务")
        lower = ttk.Frame(pane)
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(1, weight=1)
        pane.add(lower, weight=1)
        pending_bar = ttk.Frame(lower)
        pending_bar.grid(row=0, column=0, sticky="ew", pady=(4, 6))
        ttk.Label(pending_bar, textvariable=self.changes_var).pack(side="left")
        self.discard_button = ttk.Button(pending_bar, text="放弃全部暂存", command=self._discard)
        self.discard_button.pack(side="right")
        self.undo_button = ttk.Button(pending_bar, text="撤销上一步", command=self._undo)
        self.undo_button.pack(side="right", padx=6)
        pending_table = ttk.Frame(lower)
        pending_table.grid(row=1, column=0, sticky="nsew")
        pending_table.rowconfigure(0, weight=1)
        pending_table.columnconfigure(0, weight=1)
        self.change_tree = ttk.Treeview(pending_table, columns=("slot", "path", "before", "after"),
                                       show="headings", height=5, selectmode="browse")
        for key, title, width in (("slot", "槽位", 130), ("path", "修改位置", 300),
                                  ("before", "原值", 220), ("after", "暂存值", 220)):
            self.change_tree.heading(key, text=title)
            self.change_tree.column(key, width=width, minwidth=80)
        self.change_tree.grid(row=0, column=0, sticky="nsew")
        cy = ttk.Scrollbar(pending_table, orient="vertical", command=self.change_tree.yview)
        cy.grid(row=0, column=1, sticky="ns")
        cx = ttk.Scrollbar(pending_table, orient="horizontal", command=self.change_tree.xview)
        cx.grid(row=1, column=0, sticky="ew")
        self.change_tree.configure(yscrollcommand=cy.set, xscrollcommand=cx.set)
        self.change_tree.bind("<Double-1>", self._show_change)
        ttk.Label(root, text=SCOPE_HINT, foreground="#986100").grid(
            row=3, column=0, sticky="w", padx=12, pady=(4, 0))
        footer = ttk.Frame(root, padding=(12, 4, 12, 10))
        footer.grid(row=4, column=0, sticky="ew")
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=90)
        self.progress.pack(side="right")
        ttk.Label(footer, textvariable=self.status_var, wraplength=900).pack(side="left")
        root.bind("<Control-s>", lambda _e: self._save())
        root.bind("<Control-z>", lambda _e: self._undo())
        self._update_controls()

    def _submit(self, label, work, done, failed=None):
        if self.busy or self.closed:
            return
        self.busy = True
        self.status_var.set(label)
        self.progress.start(12)
        self._update_controls()

        def worker():
            try:
                result = work()
            except Exception as error:
                self.results.put((False, error, done, failed))
            else:
                self.results.put((True, result, done, failed))
        self.executor.submit(worker)

    def _drain_results(self):
        if self.closed:
            return
        try:
            ok, result, done, failed = self.results.get_nowait()
        except Empty:
            pass
        else:
            self.busy = False
            self.progress.stop()
            self._update_controls()
            if ok:
                try:
                    done(result)
                except Exception as error:
                    self._error("更新界面失败", error)
            elif failed:
                failed(result)
            else:
                self._error("操作失败", result)
        self.root.after(80, self._drain_results)

    def _error(self, title, error):
        self.status_var.set(f"{title}：{error}")
        messagebox.showerror(title, str(error), parent=self.root)

    def _startup(self):
        def work():
            warning = ""
            try:
                settings = self.settings_store.load()
            except Exception as error:
                settings, warning = {}, f"设置读取失败：{error}"
            catalog = self.Catalog()
            try:
                paths = self.services.discover_saves()
            except Exception as error:
                paths = []
                warning += ("\n" if warning else "") + f"自动查找失败，可手动选择存档：{error}"
            remembered = str(settings.get("save_path") or "").strip()
            if remembered:
                previous = Path(remembered)
                if previous.is_file():
                    paths = [previous] + [path for path in paths if Path(path).resolve() != previous.resolve()]
            return catalog, settings, paths, str(self.services.default_backup_dir(APP_ROOT)), warning

        def done(result):
            self.catalog, self.settings, paths, default_backup, warning = result
            self.backup_var.set(str(self.settings.get("backup_dir") or default_backup))
            self.path_box.configure(values=[str(path) for path in paths])
            if warning:
                messagebox.showwarning("设置提示", warning, parent=self.root)
            if paths:
                self.path_var.set(str(paths[0]))
                self._open_path()
            else:
                self.status_var.set("未找到存档，请手动选择。")
                messagebox.showinfo("未找到存档", "未发现 PBZ 存档，请选择存档文件。", parent=self.root)
                self._browse_save()
        self._submit("正在读取物品目录并查找本机存档…", work, done)

    def _discover(self):
        if self.busy:
            return
        if self.catalog is None:
            self._startup()
            return

        def done(paths):
            self.path_box.configure(values=[str(path) for path in paths])
            if paths:
                self.path_var.set(str(paths[0]))
                self._open_path()
            else:
                self.status_var.set("未找到存档，请手动选择。")
                messagebox.showinfo("未找到存档", "请选择 PBZ 存档文件。", parent=self.root)
                self._browse_save()
        self._submit("正在重新查找存档…", self.services.discover_saves, done)

    def _browse_save(self):
        if self.busy:
            return
        value = filedialog.askopenfilename(parent=self.root, title="选择 PBZ 存档",
                    filetypes=[("PBZ 存档", "*.sav"), ("所有文件", "*.*")])
        if value:
            self.path_var.set(value)
            self._open_path()

    def _browse_backup(self):
        if self.busy:
            return
        value = filedialog.askdirectory(parent=self.root, title="选择备份目录")
        if value:
            self.backup_var.set(value)
            self._remember_settings()

    def _remember_settings(self):
        settings = dict(self.settings, backup_dir=self.backup_var.get().strip(),
                        save_path=str(self.editor.path) if self.editor else self.path_var.get().strip())

        def done(_):
            self.settings = settings
            self.status_var.set("备份目录已记住。")
        self._submit("正在保存备份目录设置…", lambda: self.settings_store.save(settings), done)

    def _resolve_pending(self, action):
        if self.busy:
            return
        if self.editor is None or not self.editor.changes():
            action()
            return
        choice = messagebox.askyesnocancel("有未保存修改",
            "当前有未保存修改。\n\n是：预览并保存后继续\n否：放弃这些修改后继续\n取消：留在当前存档/槽位",
            parent=self.root)
        if choice is True:
            self._save(on_success=action)
        elif choice is False:
            action()

    def _open_path(self):
        if self.busy:
            return
        try:
            path = _path(self.path_var.get())
        except ValueError as error:
            self._error("路径无效", error)
            return
        if self.catalog is None:
            self._error("物品目录未就绪", "请点击“重新查找”重试初始化。")
            return

        def open_now():
            self._submit("正在验证并读取存档…",
                         lambda: self.SaveEditor.open(path, catalog=self.catalog), self._adopt_editor,
                         failed=self._open_failed)
        self._resolve_pending(open_now)
        if not self.busy and self.editor is not None:
            self.path_var.set(str(self.editor.path))

    def _open_failed(self, error):
        if self.editor is not None:
            self.path_var.set(str(self.editor.path))
        self._error("读取失败，原暂存修改仍保留", error)

    def _adopt_editor(self, editor, preferred_slot=None):
        self.editor = editor
        self.path_var.set(str(editor.path))
        slots = list(editor.slots)
        self.slot = preferred_slot if preferred_slot in slots else editor.current_slot
        if self.slot not in slots:
            self.slot = slots[0] if slots else ""
        self.slot_box.configure(values=slots)
        self.slot_var.set(self.slot)
        history_warning = ""
        if hasattr(editor, "load_reward_history"):
            try:
                editor.load_reward_history(self.backup_var.get())
            except Exception as error:
                history_warning = f"奖励收据读取失败：{error}"
        self._refresh()
        self.status_var.set(f"已载入：{editor.path} ｜ 源 SHA-256：{editor.source_sha256[:16]}…")
        self.settings = dict(self.settings, save_path=str(editor.path), backup_dir=self.backup_var.get().strip())
        try:
            self.settings_store.save(self.settings)
        except Exception as error:
            self.status_var.set(f"已载入存档，但路径设置未记住：{error}")
        if history_warning:
            self.status_var.set(self.status_var.get() + f" ｜ {history_warning}")
            messagebox.showwarning("存档已载入，奖励收据读取失败",
                f"存档已载入，当前暂存仍可继续编辑。\n{history_warning}", parent=self.root)

    def _switch_slot(self, _event=None):
        requested = self.slot_var.get()
        self.slot_var.set(self.slot)
        if self.busy or not self.editor or requested == self.slot:
            return

        def switch():
            try:
                self.editor.discard()
                self.slot = requested
                self.slot_var.set(requested)
                self._refresh()
            except Exception as error:
                self._error("切换槽位失败", error)
        self._resolve_pending(switch)

    def _set_categories(self, rows):
        previous = self._category()
        old_state = self.category_list.cget("state")
        self.category_list.configure(state="normal")
        present = {str(row.get("category") or "未识别物品") for row in rows}
        values = [ALL_CATEGORIES] + [category for category in CATEGORIES if category in present]
        values += sorted(present - set(values))
        self.category_list.delete(0, "end")
        for category in values:
            self.category_list.insert("end", category)
        self.category_list.selection_set(values.index(previous) if previous in values else 0)
        self.category_list.configure(state=old_state)

    def _category(self):
        selection = self.category_list.curselection()
        return self.category_list.get(selection[0]) if selection else ALL_CATEGORIES

    def _refresh(self, selected_key=None):
        if self.editor is None:
            self.rows = []
            self.pending_changes = []
        else:
            records = self.editor.list_items(self.slot) if self.slot else []
            self.rows = build_item_rows(self.catalog.items, records)
            self.pending_changes = self.editor.changes()
        self._set_categories(self.rows)
        self._render_items(selected_key)
        self.change_tree.delete(*self.change_tree.get_children())
        for index, change in enumerate(self.pending_changes):
            self.change_tree.insert("", "end", iid=str(index), values=format_change(change))
        self.changes_var.set(f"待修改：{len(self.pending_changes)} 项（仅暂存在内存；双击查看原值/暂存值）")
        self.root.title(f"{'* ' if self.pending_changes else ''}PBZ 存档编辑器 1.0 Beta · 离线")
        if hasattr(self, "quest_panel"):
            self.quest_panel.refresh()
        self._update_controls()

    def _render_items(self, selected_key=None):
        if not hasattr(self, "item_tree"):
            return
        if selected_key is None:
            old = self._selected()
            selected_key = (old["storage"], old["item_id"]) if old else None
        self.item_tree.delete(*self.item_tree.get_children())
        self.visible_rows = {}
        category = self._category()
        self.hint_var.set("书籍 / 剧情物品仅改变持有物，不会自动完成任务或场景事件。"
                          if any(word in category for word in ("书", "剧情")) else "")
        rows = filter_items(self.rows, category, self.search_var.get(), self.owned_var.get())
        for index, row in enumerate(rows):
            iid = str(index)
            self.visible_rows[iid] = row
            state = "已装备" if row.get("equipped") else ("持有" if row.get("owned") else "未持有")
            self.item_tree.insert("", "end", iid=iid, values=(row.get("name", ""), row["item_id"],
                row.get("quantity", 0), row.get("level") if row.get("level") is not None else "—", state))
            if (row["storage"], row["item_id"]) == selected_key:
                self.item_tree.selection_set(iid)
                self.item_tree.see(iid)
        self._select_item()

    def _selected(self):
        selection = self.item_tree.selection()
        return self.visible_rows.get(selection[0]) if selection else None

    def _select_item(self, _event=None):
        row = self._selected()
        if row:
            self.quantity_label_var.set("使用次数" if row.get("kind") == "medicine" else
                                        "拥有（0/1）" if row.get("kind") in OWNERSHIP_KINDS else "数量")
            quantity = row.get("quantity", 0)
            self.quantity_var.set(str(quantity if row.get("owned") else max(1, quantity or 0)))
            self.level_var.set(str(row["level"]) if row.get("level") is not None else "")
            detail = f"{row.get('name', '')} ｜ {row['item_id']} ｜ {row['storage']}"
            if row.get("description"):
                detail += f"\n{row['description']}"
            if row.get("kind") == "main_weapon":
                detail += "\n主武器没有可编辑等级；最后一把主武器不可移除。"
            if any(word in str(row.get("category", "")) for word in ("书", "剧情")):
                detail += "\n仅改变持有物，不会自动完成任务或场景事件。"
            self.description_var.set(detail)
        else:
            self.description_var.set("请选择物品。添加未持有物品时，请切换到“全部（可添加）”。")
        self._update_controls()

    def _update_controls(self):
        if hasattr(self, "quest_panel"):
            self.quest_panel.set_busy(self.busy)
        loaded = self.editor is not None and bool(self.slot) and not self.busy
        selected = self._selected() if hasattr(self, "item_tree") else None
        for widget in (self.browse_button, self.open_button, self.discover_button,
                       self.backup_browse, self.path_box, self.backup_entry):
            widget.configure(state="disabled" if self.busy else "normal")
        self.slot_box.configure(state="readonly" if loaded else "disabled")
        for widget in (self.search_entry, self.owned_radio, self.all_radio):
            widget.configure(state="normal" if loaded else "disabled")
        self.category_list.configure(state="normal" if loaded else "disabled")
        self.quantity_entry.configure(state="normal" if loaded and selected else "disabled")
        self.level_entry.configure(state="normal" if loaded and selected and
            selected.get("kind") in LEVEL_KINDS else "disabled")
        for widget, enabled in ((self.add_button, loaded and selected and not selected.get("owned")),
                                (self.modify_button, loaded and selected and selected.get("owned")),
                                (self.remove_button, loaded and selected and selected.get("owned")),
                                (self.save_button, loaded and bool(self.pending_changes)),
                                (self.save_as_button, loaded), (self.restore_button, loaded),
                                (self.backup_button, loaded),
                                (self.undo_button, loaded and bool(self.pending_changes)),
                                (self.discard_button, loaded and bool(self.pending_changes))):
            widget.configure(state="normal" if enabled else "disabled")

    def _edit_item(self, adding=False):
        row = self._selected()
        if self.busy or not self.editor or not row:
            return
        try:
            quantity, level = parse_edit_values(self.quantity_var.get(), self.level_var.get(), row["kind"])
            if adding and quantity == 0:
                raise ValueError("添加物品时，数量须大于 0。")
            self.editor.set_item(self.slot, row["item_id"], row["storage"], quantity=quantity, level=level)
            self._refresh((row["storage"], row["item_id"]))
            self.status_var.set("修改已暂存到内存，尚未写入文件。")
        except Exception as error:
            self._error("无法暂存修改", error)

    def _remove_item(self):
        row = self._selected()
        if self.busy or not self.editor or not row:
            return
        effect = ("数量归零，历史记录保留。" if row.get("kind") == "item" else
                  "同时解除该物品的装备状态。")
        if not messagebox.askyesno("移除物品", f"暂存移除：{row.get('name', '')}（{row['item_id']}）？\n"
                f"{effect}\n实际写入前仍可撤销。", parent=self.root):
            return
        try:
            self.editor.remove_item(self.slot, row["item_id"], row["storage"])
            self._refresh()
            self.status_var.set("移除已暂存，尚未写入文件。")
        except Exception as error:
            self._error("无法移除", error)

    def _undo(self):
        if self.busy or not self.editor:
            return
        try:
            self.editor.undo()
            self._refresh()
            self.status_var.set("已撤销上一步暂存修改。")
        except Exception as error:
            self._error("无法撤销", error)

    def _discard(self):
        if self.busy or not self.editor or not self.pending_changes:
            return
        if messagebox.askyesno("放弃全部暂存", "放弃当前文件的所有未保存修改？文件不会被写入。", parent=self.root):
            try:
                self.editor.discard()
                self._refresh()
                self.status_var.set("已放弃全部暂存修改。")
            except Exception as error:
                self._error("无法放弃暂存", error)

    def _text_dialog(self, title, text, confirm=False):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("880x570")
        dialog.minsize(620, 400)
        dialog.transient(self.root)
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame = ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        body = tk.Text(frame, wrap="word", padx=8, pady=8, font=("Microsoft YaHei UI", 10))
        body.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=body.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        body.configure(yscrollcommand=scrollbar.set)
        body.insert("1.0", text)
        body.configure(state="disabled")
        accepted = [False]

        def accept():
            accepted[0] = True
            dialog.destroy()
        buttons = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        buttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons, text="取消" if confirm else "关闭", command=dialog.destroy).pack(side="right")
        if confirm:
            ttk.Button(buttons, text="确认备份并写入", command=accept).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.grab_set()
        self.root.wait_window(dialog)
        return accepted[0]

    def _show_change(self, _event=None):
        selection = self.change_tree.selection()
        if selection:
            change = self.pending_changes[int(selection[0])]
            slot, path, before, after = format_change(change)
            self._text_dialog("修改详情", f"槽位：{slot}\n位置：{path}\n\n原值：\n{before}\n\n暂存值：\n{after}")

    def _backup(self):
        if self.busy or not self.editor:
            return
        try:
            backup_dir = _path(self.backup_var.get())
        except ValueError as error:
            self._error("备份目录无效", error)
            return
        source, expected = self.editor.path, self.editor.source_sha256
        settings = dict(self.settings, backup_dir=str(backup_dir), save_path=str(source))

        def work():
            result = self.services.create_backup(source, backup_dir, expected)
            settings_error = ""
            try:
                self.settings_store.save(settings)
            except Exception as error:
                settings_error = str(error)
            return result, settings_error

        def done(outcome):
            result, settings_error = outcome
            self.settings = settings
            detail = f"备份位置：{result['backup_path']}\n\n备份的是磁盘原档，不包含尚未保存的暂存修改。"
            if settings_error:
                detail += f"\n路径设置未记住：{settings_error}"
            self.status_var.set(f"已备份：{result['backup_path']}")
            messagebox.showinfo("备份完成", detail, parent=self.root)
        self._submit("正在备份磁盘原档并核对内容…", work, done)

    def _save(self, save_as=False, on_success=None):
        if self.busy or not self.editor:
            return
        source = Path(self.editor.path)
        destination = None
        if save_as:
            value = filedialog.asksaveasfilename(parent=self.root, title="另存为 PBZ 存档",
                initialdir=str(source.parent), initialfile=f"{source.stem}_编辑{source.suffix}",
                defaultextension=".sav", filetypes=[("PBZ 存档", "*.sav"), ("所有文件", "*.*")])
            if not value:
                return
            destination = _path(value)
        try:
            backup_dir = _path(self.backup_var.get())
        except ValueError as error:
            self._error("备份目录无效", error)
            return
        changes = self.editor.changes()
        if not changes and not save_as:
            self.status_var.set("没有待保存的修改。")
            if on_success:
                on_success()
            return
        preview = [f"源文件：{source}", f"输出文件：{destination or source}", f"备份目录：{backup_dir}",
                   f"待修改：{len(changes)} 项", "", "确认后先构建并校验，通过后备份源文件，再写入输出文件。",
                   "源文件已被外部修改时会拒绝写入。另存将以输出文件作为后续编辑源。", ""]
        for index, change in enumerate(changes, 1):
            slot, path, before, after = format_change(change)
            preview.append(f"{index}. [{slot}] {path}\n原值：{before}\n改为：{after}\n")
        if not self._text_dialog("保存前预览", "\n".join(preview), confirm=True):
            return
        editor, slot = self.editor, self.slot
        settings = dict(self.settings, backup_dir=str(backup_dir), save_path=str(destination or source))

        def work():
            result = self.services.save_editor(editor, backup_dir, destination=destination)
            return self._read_written(result, settings)

        def done(result):
            if self._finish_write(result, slot, "保存完成") and on_success:
                on_success()
        self._submit("正在构建、验证、备份并保存…", work, done)

    def _read_written(self, result, settings):
        # Writing already succeeded: a re-open/settings error must not say it failed.
        reopened, read_error, settings_error = None, "", ""
        try:
            reopened = self.SaveEditor.open(Path(result["output_path"]), catalog=self.catalog)
        except Exception as error:
            read_error = str(error)
        try:
            self.settings_store.save(settings)
        except Exception as error:
            settings_error = str(error)
        return result, reopened, read_error, settings_error, settings

    def _finish_write(self, outcome, slot, title):
        result, reopened, read_error, settings_error, settings = outcome
        detail = f"输出文件：{result['output_path']}\n备份位置：{result['backup_path']}"
        history_warning = result.get("history_warning")
        if history_warning:
            detail = f"文件已保存。\n{detail}\n\n奖励收据提示：{history_warning}"
        self.settings = settings
        if reopened is None:
            self.editor = None
            self.slot = ""
            self.slot_var.set("")
            self.path_var.set(str(result["output_path"]))
            self._refresh()
            self.status_var.set("文件已写入，但重新读取失败，请重新载入。")
            messagebox.showwarning("文件已写入，需重新载入", detail + f"\n\n重新读取失败：{read_error}", parent=self.root)
            return False
        self._adopt_editor(reopened, slot)
        if settings_error:
            detail += f"\n\n备份目录设置未记住：{settings_error}"
        if history_warning:
            self.status_var.set(f"文件已保存；奖励收据提示：{history_warning}")
            messagebox.showwarning("文件已保存，奖励收据需检查", detail, parent=self.root)
        else:
            messagebox.showinfo(title, detail, parent=self.root)
        return True

    def _restore(self):
        if self.busy or not self.editor:
            return

        def choose():
            value = filedialog.askopenfilename(parent=self.root, title="选择需要恢复的 PBZ 备份",
                filetypes=[("存档和备份", "*.sav *.bak"), ("所有文件", "*.*")])
            if not value:
                return
            try:
                backup_file = _path(value)
                backup_dir = _path(self.backup_var.get())
            except ValueError as error:
                self._error("路径无效", error)
                return
            source, expected = Path(self.editor.path), self.editor.source_sha256
            slot = self.slot

            def validate():
                restored = self.SaveEditor.open(backup_file, catalog=self.catalog)
                return restored.build(), list(restored.slots)

            def validated(result):
                data, slots = result
                text = (f"已验证备份：{backup_file}\n恢复到：{source}\n本次备份目录：{backup_dir}\n\n"
                        f"备份包含槽位：{', '.join(map(str, slots))}\n\n"
                        "恢复会用备份整体替换当前存档文件，包括所有槽位。\n"
                        "当前文件会先备份；未保存的暂存修改将在恢复成功后放弃。")
                if not self._text_dialog("恢复备份前预览", text, confirm=True):
                    self.status_var.set("已取消恢复，当前暂存修改仍保留。")
                    return
                settings = dict(self.settings, backup_dir=str(backup_dir))

                def write():
                    result = self.services.write_with_backup(source, data, backup_dir,
                        expected_sha256=expected)
                    return self._read_written(result, settings)
                self._submit("备份验证已通过，正在备份当前文件并恢复…", write,
                    lambda outcome: self._finish_write(outcome, slot, "恢复完成"))
            self._submit("正在验证备份存档，尚未写入…", validate, validated)
        self._resolve_pending(choose)

    def _close(self):
        if self.busy:
            messagebox.showinfo("操作进行中", "请等待当前读取或写入结束后关闭窗口。", parent=self.root)
            return

        def close():
            self.closed = True
            self.executor.shutdown(wait=False)
            self.root.destroy()
        self._resolve_pending(close)


def run():
    """Application entry point used by the launcher / portable build."""
    root = tk.Tk()
    root.withdraw()
    try:
        from .catalog import Catalog
        from .core import SaveEditor
        from . import services
        EditorWindow(root, Catalog, SaveEditor, services)
    except Exception as error:
        messagebox.showerror("PBZ 存档编辑器无法启动", str(error), parent=root)
        root.destroy()
        raise
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    run()
