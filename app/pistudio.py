import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
import webbrowser
from tkinter import ttk, filedialog, messagebox, simpledialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import piengine as E

APP = E.APP
VERSION = E.VERSION

DARK = {"bg": "#0e1116", "panel": "#141a22", "panel2": "#1a212b", "line": "#232b37",
        "ink": "#e8eef5", "ink2": "#9dabbd", "ink3": "#6d7a8c", "sel": "#243145",
        "ok": "#43c06a", "warn": "#d8a13a", "err": "#f06055", "sunken": "#0a0d12",
        "user": "#2b4a7d", "ai": "#1d2635", "tool": "#1a2430"}
LIGHT = {"bg": "#eceff4", "panel": "#ffffff", "panel2": "#f4f6fa", "line": "#dde3ec",
         "ink": "#0f1722", "ink2": "#48566a", "ink3": "#76839a", "sel": "#dce4f2",
         "ok": "#2f9e52", "warn": "#b8860b", "err": "#d94135", "sunken": "#e4e8ef",
         "user": "#dbe6fb", "ai": "#f2f5fa", "tool": "#eef2f8"}

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

# 面板注册表：界面布局的唯一事实来源（标题 / 停靠区 / 是否默认展开）。
# 新增面板只需在此登记 + 实现 panel_<pid>()，布局菜单、右键移动、重置布局会自动跟上。
PANEL_DEFS = [
    # pid, 标题, 停靠区("main" 主区 / "bottom" 底部), 默认打开
    ("chat", "对话", "main", True),
    ("tools", "工具", "main", True),
    ("skills", "技能", "main", False),
    ("plugins", "插件", "main", False),
    ("mcp", "MCP", "main", False),
    ("models", "模型", "main", True),
    ("changes", "变更", "main", True),
    ("settings", "设置", "main", True),
    ("runtime", "运行时", "bottom", True),
    ("console", "控制台", "bottom", True),
    ("about", "关于", "bottom", True),
]
PANEL_TITLES = {p: t for p, t, _w, _d in PANEL_DEFS}
PANEL_DOCKS = {p: w for p, _t, w, _d in PANEL_DEFS}
PANEL_DEFAULT = {p: bool(d) for p, _t, _w, d in PANEL_DEFS}


def fmt_n(n):
    n = int(n or 0)
    return ("%.1fk" % (n / 1000.0)) if n >= 1000 else str(n)


def ts(t):
    return time.strftime("%H:%M:%S", time.localtime(t or time.time()))


def day_of(t):
    d = time.localtime(t)
    n = time.localtime()
    y = time.localtime(time.time() - 86400)
    if (d.tm_year, d.tm_yday) == (n.tm_year, n.tm_yday):
        return "今天"
    if (d.tm_year, d.tm_yday) == (y.tm_year, y.tm_yday):
        return "昨天"
    return time.strftime("%Y-%m-%d", d)


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = E.load_config()
        self.sessions = E.load_sessions()
        if not self.sessions:
            self.sessions = [E.new_session()]
        self.ui = E.load_ui()
        self.panel_titles = dict(PANEL_TITLES)
        self.panel_dock = dict(PANEL_DOCKS)
        self.panel_default = dict(PANEL_DEFAULT)
        self.q = queue.Queue()
        self.busy = False
        self.cancel = None
        self.frames = {}
        self.dock_of = {}
        self.tool_forms = {}
        self.allow_session = set()
        self.approve_result = False
        self.approve_ev = None
        self.approved_args = None
        self.qa_result = None          # 问答卡返回值（None=跳过 / dict=答案）
        self.qa_ev = None
        self._qa_state = []            # 当前问答卡每题的选择变量（自检可直接读写）
        self._qa_payload = None
        self.var_qa = tk.BooleanVar(value=bool((self.cfg.get("prefs") or {}).get("qa_first")))
        self.var_qamax = tk.IntVar(value=int((self.cfg.get("prefs") or {}).get("qa_max") or 4))
        self.pending_files = []
        self._msg_marks = []
        self._cp_rows = []
        self._click_pos = "1.0"
        self.stream_buf = ""
        self.stream_open = False
        self._s_dirty = False          # 正文流有未画出的增量
        self._flush_job = None         # 合帧定时器
        self._stream_mark = None       # 正文流起始位置
        self._reason_buf = ""          # 推理流缓冲（只追加，不重排）
        self._reason_shown = 0
        self._raw_shown = 0
        self._stream_mode = "md"       # md → 超过阈值后 raw 追加
        self._turn_mark = None         # 本轮开始时的文本位置（回合内局部重绘用）
        self._turn_msg_start = 0
        self._stale = set()            # 不可见面板被跳过刷新的标记（切回该标签时补刷）
        self._pump_job = None          # 队列泵定时器句柄
        self._boot_job = None          # 启动探测定时器句柄
        self._tick_job = None          # 状态栏秒表定时器句柄
        self._t0 = 0.0
        self._follow = True
        self.rt = {"turn": "空闲", "step": 0, "max": 0, "tokens": {}, "tools": [], "elapsed": 0, "model": ""}
        self.cur = self.sessions[0]["id"]
        self.tool_out = "尚未运行工具。"
        self.probe_res = None
        root.title("%s %s" % (APP, VERSION))
        root.geometry("1360x860")
        root.minsize(1040, 640)
        self.center()
        self.build_style()
        self.build_menu()
        self.build_topbar()
        self.build_status()
        self.build_root()
        self.build_sidebar()
        self.build_docks()
        self.bind_keys()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_sessions()
        self.render_chat()
        self.refresh_tools()
        self.refresh_runtime()
        self.refresh_changes()
        self.refresh_ctx()
        self._pump_job = self.root.after(60, self.pump)
        self._boot_job = self.root.after(200, self.boot)

    def center(self):
        self.root.update_idletasks()
        w, h = 1360, 860
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))

    def palette(self):
        return DARK if self.ui.get("theme", "dark") == "dark" else LIGHT

    def session(self):
        for s in self.sessions:
            if s["id"] == self.cur:
                return s
        self.cur = self.sessions[0]["id"]
        return self.sessions[0]

    def build_style(self):
        p = self.palette()
        acc = self.ui.get("accent", "#5b8cff")
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        self.root.configure(bg=p["bg"])
        st.configure(".", background=p["panel"], foreground=p["ink"], fieldbackground=p["sunken"],
                     bordercolor=p["line"], lightcolor=p["panel"], darkcolor=p["panel"], font=(FONT, 9))
        for name, cfg in (
            ("TFrame", {"background": p["bg"]}),
            ("Card.TFrame", {"background": p["panel"]}),
            ("TLabel", {"background": p["bg"], "foreground": p["ink"]}),
            ("Card.TLabel", {"background": p["panel"], "foreground": p["ink"]}),
            ("Dim.TLabel", {"background": p["panel"], "foreground": p["ink2"]}),
            ("Head.TLabel", {"background": p["panel"], "foreground": p["ink"], "font": (FONT, 11, "bold")}),
            ("TButton", {"background": p["panel2"], "foreground": p["ink"], "padding": (10, 5), "borderwidth": 0}),
            ("Acc.TButton", {"background": acc, "foreground": "#ffffff", "padding": (12, 6), "borderwidth": 0}),
            ("Danger.TButton", {"background": p["err"], "foreground": "#ffffff", "padding": (12, 6), "borderwidth": 0}),
            ("TEntry", {"fieldbackground": p["sunken"], "foreground": p["ink"], "insertcolor": p["ink"]}),
            ("TCombobox", {"fieldbackground": p["sunken"], "background": p["panel2"], "foreground": p["ink"],
                           "arrowcolor": p["ink2"]}),
            ("TCheckbutton", {"background": p["bg"], "foreground": p["ink"]}),
            ("Card.TCheckbutton", {"background": p["panel"], "foreground": p["ink"]}),
            ("TRadiobutton", {"background": p["bg"], "foreground": p["ink"]}),
            ("Card.TRadiobutton", {"background": p["panel"], "foreground": p["ink"]}),
            ("TNotebook", {"background": p["bg"], "borderwidth": 0, "tabmargins": (4, 4, 4, 0)}),
            ("TNotebook.Tab", {"background": p["panel2"], "foreground": p["ink2"], "padding": (14, 6)}),
            ("TPanedwindow", {"background": p["bg"]}),
            ("TScrollbar", {"background": p["panel2"], "troughcolor": p["bg"]}),
            ("TSeparator", {"background": p["line"]}),
            ("TLabelframe", {"background": p["panel"], "foreground": p["ink2"]}),
            ("TLabelframe.Label", {"background": p["panel"], "foreground": p["ink2"]}),
        ):
            st.configure(name, **cfg)
        st.map("TNotebook.Tab", background=[("selected", p["panel"]), ("active", p["sel"])],
               foreground=[("selected", acc)])
        st.map("Acc.TButton", background=[("active", acc), ("pressed", acc)])
        st.map("TButton", background=[("active", p["sel"])])
        st.configure("Treeview", background=p["panel"], fieldbackground=p["panel"], foreground=p["ink"],
                     rowheight=22, borderwidth=0)
        st.configure("Treeview.Heading", background=p["panel2"], foreground=p["ink2"], borderwidth=0)
        st.map("Treeview", background=[("selected", p["sel"])], foreground=[("selected", p["ink"])])

    def build_menu(self):
        m = tk.Menu(self.root)
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="新建会话", accelerator="Ctrl+N", command=self.act_new)
        fm.add_command(label="打开工作区目录…", command=self.pick_workspace)
        fm.add_command(label="导出当前会话 (Markdown)…", command=self.act_export)
        fm.add_separator()
        fm.add_command(label="退出", command=self.on_close)
        m.add_cascade(label="文件", menu=fm)
        e = tk.Menu(m, tearoff=0)
        e.add_command(label="运行真实自检", accelerator="F5", command=self.act_selftest)
        e.add_command(label="探测模型端点", command=lambda: self.do_probe())
        e.add_command(label="列出可用工具", command=self.act_list_tools)
        e.add_separator()
        e.add_command(label="打开数据目录", command=lambda: self.open_path(E.HOME))
        e.add_command(label="打开运行日志", command=lambda: self.open_path(E.LOG_PATH))
        m.add_cascade(label="工具与诊断", menu=e)
        g = tk.Menu(m, tearoff=0)
        g.add_command(label="治理中心（契约 / 计划 / 审批 / 接口）…", command=self.act_governance)
        g.add_command(label="文件管理…", command=self.act_files)
        g.add_separator()
        g.add_command(label="开关自动确认（写操作免审批）", command=self.act_toggle_auto)
        m.add_cascade(label="治理", menu=g)
        x = tk.Menu(m, tearoff=0)
        x.add_command(label="技能库…", command=lambda: self.show_panel("skills"))
        x.add_command(label="插件基座…", command=lambda: self.show_panel("plugins"))
        x.add_command(label="MCP 服务器…", command=lambda: self.show_panel("mcp"))
        x.add_separator()
        x.add_command(label="重载 MCP（重新连接全部服务器）", command=self.mcp_reload_async)
        x.add_command(label="重载插件基座", command=self.reload_plugins_async)
        x.add_separator()
        x.add_checkbutton(label="问答（QA）：先问后做", variable=self.var_qa, command=self.toggle_qa)
        x.add_command(label="试一次问答卡（演示选项界面）", command=self.qa_demo_card)
        x.add_command(label="问答状态 / 用法（/qa）", command=lambda: self.slash("/qa"))
        x.add_separator()
        x.add_command(label="新建示例插件 + 技能", command=self.act_plugin_example)
        x.add_command(label="打开技能目录", command=self.act_open_skill_dir)
        x.add_command(label="打开插件根目录", command=self.act_open_plugin_dir)
        x.add_command(label="打开 mcp.json", command=self.act_open_mcp_cfg)
        m.add_cascade(label="扩展", menu=x)
        v = tk.Menu(m, tearoff=0)
        v.add_command(label="切换明暗主题", accelerator="Ctrl+T", command=self.toggle_theme)
        v.add_command(label="重置面板布局", command=self.reset_layout)
        v.add_separator()
        for _pid, _title, _w, _d in PANEL_DEFS:          # 由面板注册表自动生成，新增面板无需改这里
            v.add_command(label="打开「%s」面板" % _title,
                          command=lambda p=_pid: self.show_panel(p))
        m.add_cascade(label="视图", menu=v)
        h = tk.Menu(m, tearoff=0)
        h.add_command(label="快捷键", command=lambda: self.show_panel("about"))
        h.add_command(label="关于 " + APP, command=lambda: self.show_panel("about"))
        m.add_cascade(label="帮助", menu=h)
        self.root.config(menu=m)

    def build_root(self):
        self.outer = ttk.Panedwindow(self.root, orient="horizontal")
        self.outer.pack(fill="both", expand=True)

    def build_topbar(self):
        bar = tk.Frame(self.root, bg=self.palette()["panel"], height=40)
        bar.pack(fill="x", side="top", before=None)
        bar.pack_propagate(False)
        p = self.palette()
        tk.Label(bar, text=APP, bg=p["panel"], fg=self.ui.get("accent", "#5b8cff"),
                 font=(FONT, 11, "bold")).pack(side="left", padx=(10, 12))
        tk.Label(bar, text="工作区", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left")
        self.var_ws = tk.StringVar(value=self.cfg.get("workspace") or "")
        tk.Entry(bar, textvariable=self.var_ws, bg=p["sunken"], fg=p["ink"], relief="flat",
                 insertbackground=p["ink"], width=34, font=(MONO, 9)).pack(side="left", padx=6, ipady=3)
        ttk.Button(bar, text="选择…", command=self.pick_workspace).pack(side="left")
        tk.Label(bar, text="模型", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left", padx=(14, 0))
        self.var_model = tk.StringVar(value=(self.cfg.get("provider") or {}).get("model") or "")
        self.cmb_model = ttk.Combobox(bar, textvariable=self.var_model, width=26,
                                      values=(self.cfg.get("provider") or {}).get("models") or [])
        self.cmb_model.pack(side="left", padx=6)
        self.cmb_model.bind("<<ComboboxSelected>>", lambda e: self.apply_model())
        ttk.Button(bar, text="应用", command=self.apply_model).pack(side="left")
        self.lbl_conn = tk.Label(bar, text="● 未探测", bg=p["panel"], fg=p["ink3"], font=(FONT, 9))
        self.lbl_conn.pack(side="left", padx=12)
        ttk.Button(bar, text="主题", command=self.toggle_theme).pack(side="right", padx=8)
        ttk.Button(bar, text="网页版", command=self.act_start_web).pack(side="right")
        ttk.Button(bar, text="设置", command=lambda: self.show_panel("settings")).pack(side="right", padx=8)

    def build_sidebar(self):
        p = self.palette()
        side = tk.Frame(self.root, bg=p["panel"], width=250)
        self.outer.add(side, weight=0)
        side.pack_propagate(False)
        head = tk.Frame(side, bg=p["panel"])
        head.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(head, text="会话", bg=p["panel"], fg=p["ink"], font=(FONT, 10, "bold")).pack(side="left")
        ttk.Button(head, text="＋", width=3, command=self.act_new).pack(side="right")
        self.var_search = tk.StringVar()
        ent = tk.Entry(side, textvariable=self.var_search, bg=p["sunken"], fg=p["ink"], relief="flat",
                       insertbackground=p["ink"], font=(FONT, 9))
        ent.pack(fill="x", padx=8, ipady=3)
        self.var_search.trace_add("write", lambda *a: self.refresh_sessions())
        wrap = tk.Frame(side, bg=p["panel"])
        wrap.pack(fill="both", expand=True, padx=(4, 0), pady=6)
        self.tree = ttk.Treeview(wrap, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_pick_session)
        self.tree.bind("<Button-3>", self.on_session_menu)
        self.sess_menu = tk.Menu(self.root, tearoff=0)
        self.sess_menu.add_command(label="重命名", command=self.act_rename)
        self.sess_menu.add_command(label="复制", command=self.act_dup)
        self.sess_menu.add_command(label="导出 Markdown", command=self.act_export)
        self.sess_menu.add_separator()
        self.sess_menu.add_command(label="删除", command=self.act_delete)
        foot = tk.Frame(side, bg=p["panel"])
        foot.pack(fill="x", padx=8, pady=6)
        ttk.Button(foot, text="自检", command=self.act_selftest).pack(side="left")
        ttk.Button(foot, text="工具", command=lambda: self.show_panel("tools")).pack(side="left", padx=4)
        ttk.Button(foot, text="日志", command=lambda: self.show_panel("console")).pack(side="left")
        foot2 = tk.Frame(side, bg=p["panel"])
        foot2.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(foot2, text="技能", command=lambda: self.show_panel("skills")).pack(side="left")
        ttk.Button(foot2, text="插件", command=lambda: self.show_panel("plugins")).pack(side="left", padx=4)
        ttk.Button(foot2, text="MCP", command=lambda: self.show_panel("mcp")).pack(side="left")

    def build_docks(self):
        self.vsplit = ttk.Panedwindow(self.outer, orient="vertical")
        self.outer.add(self.vsplit, weight=1)
        self.nb_main = ttk.Notebook(self.vsplit)
        self.nb_bottom = ttk.Notebook(self.vsplit)
        self.vsplit.add(self.nb_main, weight=4)
        self.vsplit.add(self.nb_bottom, weight=1)
        self.nb_main.bind("<Button-3>", self.on_tab_menu)
        self.nb_bottom.bind("<Button-3>", self.on_tab_menu)
        self.nb_main.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        self.nb_bottom.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        self.tab_menu = tk.Menu(self.root, tearoff=0)
        self.tab_menu.add_command(label="移到主区", command=lambda: self.move_current("main"))
        self.tab_menu.add_command(label="移到底部", command=lambda: self.move_current("bottom"))
        self.tab_menu.add_separator()
        self.tab_menu.add_command(label="关闭面板", command=lambda: self.close_current())
        # 默认布局：按注册表顺序打开 default 面板；技能/插件/MCP 等扩展面板按需打开，
        # 避免首屏标签过多（仍可用「扩展」菜单或右键“移到”随时启用）。
        for pid, title, where, default in PANEL_DEFS:
            if default:
                self.add_panel(pid, title, where)

    def add_panel(self, pid, title, where):
        builder = getattr(self, "panel_" + pid)
        parent = self.nb_main if where == "main" else self.nb_bottom
        first = not parent.winfo_children()
        f = builder(parent)
        parent.add(f, text=title)
        self.frames[pid] = f
        self.dock_of[pid] = where
        if first:
            parent.select(f)

    def build_status(self):
        p = self.palette()
        acc = self.ui.get("accent", "#5b8cff")
        self.status = tk.Frame(self.root, bg=p["panel"], height=28)
        self.status.pack(fill="x", side="bottom")
        self.status.pack_propagate(False)
        # 左：状态文字（就绪 / 正在通讯 / 执行工具 / 完成…）
        self.lbl_status = tk.Label(self.status, text="就绪", bg=p["panel"], fg=p["ink2"],
                                   font=(FONT, 9), anchor="w")
        self.lbl_status.pack(side="left", padx=(10, 10))
        # 中：本轮进度栏位（生成时才出现）：进度条 + 「第 x/y 步 · 用时 · 工具数」
        self.pb_turn = ttk.Progressbar(self.status, mode="determinate", length=120)
        self.lbl_turn = tk.Label(self.status, text="", bg=p["panel"], fg=acc, font=(FONT, 9, "bold"))
        # 右：上下文占用 / 模型 · tokens
        self.lbl_usage = tk.Label(self.status, text="", bg=p["panel"], fg=p["ink3"], font=(MONO, 9))
        self.lbl_usage.pack(side="right", padx=10)
        self.lbl_ctxbar = tk.Label(self.status, text="", bg=p["panel"], fg=p["ink3"], font=(MONO, 9))
        self.lbl_ctxbar.pack(side="right", padx=(0, 12))

    def _turn_chips(self, show):
        """生成中显示本轮进度栏位；空闲时收起。"""
        try:
            if show:
                self.pb_turn.pack(side="left", padx=(0, 8))
                self.lbl_turn.pack(side="left")
            else:
                self.pb_turn.pack_forget()
                self.lbl_turn.pack_forget()
        except Exception:
            pass

    def _update_turn_chip(self):
        if not self.busy:
            return
        try:
            el = time.time() - (self._t0 or time.time())
            self.lbl_turn.configure(text="第 %s/%s 步 · %.0fs · %s 次工具" % (
                self.rt.get("step", 0), self.rt.get("max", 0), el, len(self.rt.get("tools") or [])))
        except Exception:
            pass

    def _tick(self):
        if not self.busy:
            return
        self._update_turn_chip()
        try:
            self._tick_job = self.root.after(1000, self._tick)
        except Exception:
            pass

    def bind_keys(self):
        r = self.root
        r.bind("<Control-n>", lambda e: self.act_new())
        r.bind("<Control-t>", lambda e: self.toggle_theme())
        r.bind("<Control-s>", lambda e: self.save_all())
        r.bind("<Control-f>", lambda e: self.show_find())
        r.bind("<Control-o>", lambda e: self.act_attach())
        r.bind("<F5>", lambda e: self.act_selftest())
        r.bind("<Escape>", lambda e: self.stop_turn())
        r.bind("<Control-Return>", lambda e: self.on_send())

    def panel_chat(self, parent):
        p = self.palette()
        wrap = tk.Frame(parent, bg=p["bg"])
        self.find_bar = tk.Frame(wrap, bg=p["panel2"])
        tk.Label(self.find_bar, text="查找", bg=p["panel2"], fg=p["ink2"], font=(FONT, 9)).pack(side="left", padx=8)
        self.var_find = tk.StringVar()
        ent = tk.Entry(self.find_bar, textvariable=self.var_find, bg=p["sunken"], fg=p["ink"], relief="flat",
                       insertbackground=p["ink"], font=(FONT, 9), width=30)
        ent.pack(side="left", padx=4, pady=4, ipady=2)
        ent.bind("<Return>", lambda e: self.find_next())
        ttk.Button(self.find_bar, text="下一个", command=self.find_next).pack(side="left", padx=2)
        ttk.Button(self.find_bar, text="关闭", command=self.hide_find).pack(side="left", padx=2)
        self.lbl_find = tk.Label(self.find_bar, text="", bg=p["panel2"], fg=p["ink3"], font=(FONT, 8))
        self.lbl_find.pack(side="left", padx=8)
        mid = tk.Frame(wrap, bg=p["bg"])
        self.chat_text = tk.Text(mid, wrap="word", relief="flat", bg=p["bg"], fg=p["ink"],
                                 insertbackground=p["ink"], font=(FONT, 10), padx=14, pady=10,
                                 spacing1=1, spacing3=3, state="disabled", cursor="arrow")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.chat_text.pack(side="left", fill="both", expand=True)
        self.install_tags()
        self.chat_text.bind("<Button-3>", self.on_chat_menu)
        self.chat_text.bind("<Control-f>", lambda e: self.show_find())
        self.chat_menu = tk.Menu(self.root, tearoff=0)
        self.chat_menu.add_command(label="复制全部对话", command=self.act_copy_all)
        self.chat_menu.add_separator()
        self.chat_menu.add_command(label="回滚到这条之前（丢弃之后的全部内容）", command=lambda: self.chat_rollback(False))
        self.chat_menu.add_command(label="重发这条消息（丢弃之后的回答）", command=lambda: self.chat_rollback(True))
        self.chat_menu.add_separator()
        self.chat_menu.add_command(label="打开「变更」面板", command=lambda: self.show_panel("changes"))
        box = tk.Frame(wrap, bg=p["panel"], height=132)
        box.pack(fill="x", side="bottom")
        box.pack_propagate(False)
        mid.pack(fill="both", expand=True)
        self.txt_input = tk.Text(box, height=3, relief="flat", bg=p["sunken"], fg=p["ink"],
                                 insertbackground=p["ink"], font=(FONT, 10), padx=8, pady=6)
        self.txt_input.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.txt_input.bind("<Return>", self.on_return)
        self.txt_input.bind("<Shift-Return>", lambda e: None)
        bar = tk.Frame(box, bg=p["panel"])
        bar.pack(fill="x", padx=8, pady=(0, 8))
        self.btn_send = ttk.Button(bar, text="发送", style="Acc.TButton", command=self.on_send)
        self.btn_send.pack(side="left")
        ttk.Button(bar, text="＋附件", command=self.act_attach).pack(side="left", padx=6)
        ttk.Button(bar, text="清空对话", command=self.act_clear).pack(side="left")
        ttk.Button(bar, text="导出", command=self.act_export).pack(side="left", padx=6)
        self.lbl_attach = tk.Label(bar, text="", bg=p["panel"], fg=self.ui.get("accent", "#5b8cff"),
                                   font=(FONT, 8))
        self.lbl_attach.pack(side="left", padx=4)
        self.lbl_hint = tk.Label(bar, text="Enter 发送 · Shift+Enter 换行 · /help 命令",
                                 bg=p["panel"], fg=p["ink3"], font=(FONT, 8))
        self.lbl_hint.pack(side="right")
        return wrap

    def show_find(self):
        f = self.alive("find_bar")
        if f is not None:
            f.pack(fill="x", side="top")
            for w in f.winfo_children():
                if isinstance(w, tk.Entry):
                    w.focus_set()
                    break

    def hide_find(self):
        f = self.alive("find_bar")
        if f is not None:
            f.pack_forget()

    def find_next(self):
        t = self.alive("chat_text")
        if t is None:
            return
        q = (getattr(self, "var_find", None).get() if getattr(self, "var_find", None) else "")
        if not q:
            return
        t.tag_remove("find", "1.0", "end")
        start = getattr(self, "_find_pos", "1.0")
        pos = t.search(q, start, nocase=True, stopindex="end")
        if not pos:
            pos = t.search(q, "1.0", nocase=True, stopindex="end")
        if not pos:
            self.lbl_find.configure(text="无匹配")
            return
        end = "%s+%dc" % (pos, len(q))
        t.tag_configure("find", background="#d8a13a", foreground="#101010")
        t.tag_add("find", pos, end)
        t.see(pos)
        self._find_pos = end
        self.lbl_find.configure(text="找到于 " + pos)

    def on_chat_menu(self, e):
        t = self.alive("chat_text")
        if t is None:
            return
        try:
            self._click_pos = t.index("@%d,%d" % (e.x, e.y))
        except Exception:
            self._click_pos = "1.0"
        self.chat_menu.tk_popup(e.x_root, e.y_root)

    def msg_index_at(self, pos):
        marks = getattr(self, "_msg_marks", [])
        best = -1
        try:
            pl = int(str(pos).split(".")[0])
        except Exception:
            return -1
        for idx, mp in enumerate(marks):
            try:
                ml = int(str(mp).split(".")[0])
            except Exception:
                continue
            if ml <= pl:
                best = idx
        return best

    def chat_rollback(self, resend):
        s = self.session()
        i = self.msg_index_at(getattr(self, "_click_pos", "1.0"))
        if i < 0 or i >= len(s["messages"]):
            self.toast("没有定位到具体消息")
            return
        m = s["messages"][i]
        label = "丢弃这条之后的全部内容？" if not resend else "丢弃这条之后的回答，并重新发送这条消息？"
        if not messagebox.askyesno(APP, label + "\n\n[%s] %s" % (m.get("role"), str(m.get("content", ""))[:160])):
            return
        content = str(m.get("content", ""))
        s["messages"] = s["messages"][:i]
        s["updated"] = time.time()
        self.save_all()
        self.render_chat()
        self.refresh_changes()
        if resend:
            self.txt_input.delete("1.0", "end")
            self.txt_input.insert("1.0", content)
            self.on_send()
        else:
            self.toast("已回滚到第 %d 条之前" % i)

    def act_copy_all(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.chat_text.get("1.0", "end-1c"))
            self.toast("已复制全部对话到剪贴板")
        except Exception as e:
            messagebox.showerror(APP, str(e))

    def act_attach(self):
        files = filedialog.askopenfilenames(title="选择要附加的文本文件")
        if not files:
            return
        self.pending_files = list(getattr(self, "pending_files", [])) + list(files)
        self.lbl_attach.configure(text="已附加 %d 个文件" % len(self.pending_files))
        self.toast("已附加：%s" % "；".join(os.path.basename(f) for f in files))

    def drain_attachments(self):
        files = getattr(self, "pending_files", [])
        self.pending_files = []
        lbl = self.alive("lbl_attach")
        if lbl is not None:
            lbl.configure(text="")
        chunks = []
        for f in files:
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read(120000)
                chunks.append("【附件 %s · %d 字符】\n```\n%s\n```" % (f, len(body), body))
            except Exception as e:
                chunks.append("【附件 %s 读取失败：%s】" % (f, e))
        return "\n\n".join(chunks)

    def install_tags(self):
        p = self.palette()
        t = self.chat_text
        t.tag_configure("user_h", foreground=self.ui.get("accent", "#5b8cff"), font=(FONT, 9, "bold"),
                        spacing1=8)
        t.tag_configure("user", foreground=p["ink"], background=p["user"], lmargin1=8, lmargin2=8, rmargin=40)
        t.tag_configure("ai_h", foreground=p["ok"], font=(FONT, 9, "bold"), spacing1=8)
        t.tag_configure("ai", foreground=p["ink"], background=p["ai"], lmargin1=8, lmargin2=8, rmargin=40)
        t.tag_configure("tool_h", foreground=p["warn"], font=(MONO, 9, "bold"), spacing1=6)
        t.tag_configure("tool", foreground=p["ink2"], background=p["tool"], font=(MONO, 9), lmargin1=18,
                        lmargin2=18, rmargin=30)
        t.tag_configure("dim", foreground=p["ink3"], font=(FONT, 8))
        t.tag_configure("err", foreground=p["err"], font=(FONT, 9, "bold"))
        t.tag_configure("code", font=(MONO, 9), foreground=p["ink2"], background=p["sunken"], lmargin1=18,
                        lmargin2=18, rmargin=30)
        t.tag_configure("codebar", font=(MONO, 8), foreground=p["ink3"], lmargin1=18)
        t.tag_configure("mdh", font=(FONT, 10, "bold"), foreground=p["ink"], spacing1=6, lmargin1=8)
        t.tag_configure("mdli", font=(FONT, 9), foreground=p["ink"], background=p["ai"], lmargin1=20, lmargin2=30)
        t.tag_configure("icode", font=(MONO, 9), foreground=self.ui.get("accent", "#5b8cff"),
                        background=p["sunken"])
        t.tag_configure("bold", font=(FONT, 9, "bold"), foreground=p["ink"], background=p["ai"])

    def panel_tools(self, parent):
        p = self.palette()
        wrap = tk.Frame(parent, bg=p["bg"])
        left = tk.Frame(wrap, bg=p["panel"], width=280)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="本机真实工具", bg=p["panel"], fg=p["ink"], font=(FONT, 10, "bold")).pack(
            anchor="w", padx=10, pady=(8, 2))
        tk.Label(left, text="直接点击运行，结果来自真实执行", bg=p["panel"], fg=p["ink3"],
                 font=(FONT, 8)).pack(anchor="w", padx=10)
        flt = tk.Frame(left, bg=p["panel"])
        flt.pack(fill="x", padx=8, pady=(6, 0))
        self.var_toolgrp = tk.StringVar(value="全部")
        self.cmb_toolgrp = ttk.Combobox(flt, textvariable=self.var_toolgrp, width=9, state="readonly")
        self.cmb_toolgrp.pack(side="left")
        self.cmb_toolgrp.bind("<<ComboboxSelected>>", lambda e: self.refresh_tools())
        self.var_toolkw = tk.StringVar()
        ent = tk.Entry(flt, textvariable=self.var_toolkw, bg=p["sunken"], fg=p["ink"], relief="flat",
                       insertbackground=p["ink"], font=(FONT, 9))
        ent.pack(side="left", fill="x", expand=True, padx=(6, 0), ipady=2)
        self.var_toolkw.trace_add("write", lambda *a: self.refresh_tools())
        self.lbl_tools_n = tk.Label(left, text="", bg=p["panel"], fg=p["ink3"], font=(FONT, 8))
        self.lbl_tools_n.pack(anchor="w", padx=10, pady=(2, 0))
        self.lst_tools = tk.Listbox(left, relief="flat", bg=p["sunken"], fg=p["ink"], font=(MONO, 9),
                                    selectbackground=p["sel"], activestyle="none", highlightthickness=0)
        self.lst_tools.pack(fill="both", expand=True, padx=8, pady=6)
        self.lst_tools.bind("<<ListboxSelect>>", self.on_pick_tool)
        right = tk.Frame(wrap, bg=p["panel"])
        right.pack(side="left", fill="both", expand=True)
        self.lbl_tool = tk.Label(right, text="选择一个工具", bg=p["panel"], fg=p["ink"],
                                 font=(FONT, 10, "bold"), anchor="w")
        self.lbl_tool.pack(fill="x", padx=10, pady=(8, 0))
        self.lbl_tool_desc = tk.Label(right, text="", bg=p["panel"], fg=p["ink3"], font=(FONT, 8),
                                      anchor="w", justify="left", wraplength=760)
        self.lbl_tool_desc.pack(fill="x", padx=10)
        self.form = tk.Frame(right, bg=p["panel"])
        self.form.pack(fill="x", padx=10, pady=6)
        bar = tk.Frame(right, bg=p["panel"])
        bar.pack(fill="x", padx=10)
        ttk.Button(bar, text="运行", style="Acc.TButton", command=self.run_selected_tool).pack(side="left")
        ttk.Button(bar, text="清空输出", command=lambda: self.set_tool_out("")).pack(side="left", padx=6)
        self.btn_run = bar.winfo_children()[0]
        out = tk.Frame(right, bg=p["bg"])
        out.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.txt_tool = tk.Text(out, relief="flat", bg=p["sunken"], fg=p["ink"], font=(MONO, 9),
                                wrap="word", padx=8, pady=6)
        sb = ttk.Scrollbar(out, orient="vertical", command=self.txt_tool.yview)
        self.txt_tool.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt_tool.pack(side="left", fill="both", expand=True)
        self.txt_tool.insert("1.0", self.tool_out)
        return wrap

    def panel_models(self, parent):
        p = self.palette()
        wrap = tk.Frame(parent, bg=p["bg"])
        top = tk.Frame(wrap, bg=p["panel"])
        top.pack(fill="x")
        tk.Label(top, text="模型端点（OpenAI 兼容）", bg=p["panel"], fg=p["ink"],
                 font=(FONT, 10, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
        grid = tk.Frame(top, bg=p["panel"])
        grid.pack(fill="x", padx=10, pady=4)
        prov = self.cfg.get("provider") or {}
        self.var_base = tk.StringVar(value=prov.get("base_url") or "")
        self.var_key = tk.StringVar(value=prov.get("api_key") or "")
        self.var_mdl = tk.StringVar(value=prov.get("model") or "")
        prof_names = [x.get("name") or "未命名" for x in (self.cfg.get("profiles") or [])]
        self.var_profile = tk.StringVar(value=prov.get("name") or (prof_names[0] if prof_names else ""))
        rows = [("配置档案", "PROFILE"), ("Base URL", self.var_base), ("API Key", self.var_key),
                ("模型", self.var_mdl)]
        for i, (lab, var) in enumerate(rows):
            tk.Label(grid, text=lab, bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).grid(row=i, column=0,
                                                                                       sticky="w", pady=3)
            if var == "PROFILE":
                self.cmb_profile = ttk.Combobox(grid, textvariable=self.var_profile, values=prof_names,
                                                width=34, state="readonly")
                self.cmb_profile.grid(row=i, column=1, sticky="w", padx=8, pady=3)
                self.cmb_profile.bind("<<ComboboxSelected>>", lambda e: self.apply_profile())
                ttk.Button(grid, text="应用档案", command=self.apply_profile).grid(row=i, column=2,
                                                                                 sticky="w")
                ttk.Button(grid, text="把当前填的存为档案", command=self.save_profile).grid(row=i, column=3,
                                                                                      sticky="w", padx=6)
                continue
            ent = tk.Entry(grid, textvariable=var, bg=p["sunken"], fg=p["ink"], relief="flat",
                           insertbackground=p["ink"], font=(MONO, 9), width=70,
                           show="•" if lab == "API Key" else "")
            ent.grid(row=i, column=1, sticky="we", padx=8, pady=3, ipady=3)
            if lab == "API Key":
                self.ent_key = ent
                ttk.Checkbutton(grid, text="显示", command=self.toggle_key).grid(row=i, column=2, sticky="w")
            if lab == "模型":
                self.ent_model = ent
                tk.Label(grid, text="可手填，也可在下面双击选", bg=p["panel"], fg=p["ink3"],
                         font=(FONT, 8)).grid(row=i, column=2, columnspan=2, sticky="w")
        grid.columnconfigure(1, weight=1)
        bar = tk.Frame(top, bg=p["panel"])
        bar.pack(fill="x", padx=10, pady=(4, 6))
        ttk.Button(bar, text="探测端点", style="Acc.TButton", command=lambda: self.do_probe(True)).pack(side="left")
        ttk.Button(bar, text="应用并保存", command=self.apply_model).pack(side="left", padx=6)
        ttk.Button(bar, text="从已安装的 NmoIAIgenT 导入", command=self.import_host).pack(side="left")
        ttk.Button(bar, text="探测本机常用端口", command=self.detect_local).pack(side="left", padx=6)
        ttk.Button(bar, text="压缩上下文", command=self.act_compact).pack(side="left")
        self.lbl_probe = tk.Label(top, text="尚未探测", bg=p["panel"], fg=p["ink3"], font=(FONT, 9),
                                  anchor="w", justify="left", wraplength=980)
        self.lbl_probe.pack(fill="x", padx=10, pady=(0, 4))
        self.lbl_meta = tk.Label(top, text="", bg=p["panel"], fg=p["ink2"], font=(MONO, 9), anchor="w",
                                 justify="left", wraplength=980)
        self.lbl_meta.pack(fill="x", padx=10, pady=(0, 8))
        mid = tk.Frame(wrap, bg=p["bg"])
        mid.pack(fill="both", expand=True, padx=10, pady=8)
        tk.Label(mid, text="端点返回的模型（双击即选中并应用）", bg=p["bg"], fg=p["ink2"],
                 font=(FONT, 9)).pack(anchor="w")
        self.lst_models = tk.Listbox(mid, relief="flat", bg=p["sunken"], fg=p["ink"], font=(MONO, 9),
                                     selectbackground=p["sel"], highlightthickness=0)
        self.lst_models.pack(fill="both", expand=True, pady=4)
        self.lst_models.bind("<Double-Button-1>", self.on_pick_model)
        self.lst_models.bind("<<ListboxSelect>>", lambda e: self.show_model_meta())
        ttk.Button(mid, text="使用选中模型", command=self.on_pick_model).pack(anchor="w")
        tk.Label(mid, text="配置文件：%s（含密钥，权限已限定为当前用户）｜ 事件流：%s" % (
            E.CONFIG_PATH, E.EVENTS_PATH), bg=p["bg"], fg=p["ink3"], font=(FONT, 8)).pack(anchor="w", pady=(8, 0))
        self.show_model_meta()
        return wrap

    def show_model_meta(self):
        lbl = self.alive("lbl_meta")
        if lbl is None:
            return
        mid = (self.var_mdl.get() or "").strip()
        meta = (self.cfg.get("provider") or {}).get("model_meta") or {}
        m = meta.get(mid) or E.model_meta(self.cfg, mid)
        parts = ["当前模型 %s" % (mid or "（未选）"),
                 "窗口 %s tokens（%s）" % (m.get("contextWindow"), m.get("contextWindowSource")),
                 "最大输出 %s" % (m.get("maxOutput") or "未标注"),
                 "推理模型 %s" % ("是" if m.get("reasoning") else "否"),
                 "已知模型 %d 个" % len(meta)]
        st = E.ctx_state(self.cfg, self.session())
        parts.append("本会话已用 %s / %s" % (st["used"], st["limit"]))
        if (self.cfg.get("prefs") or {}).get("sandbox", True):
            parts.append("沙箱 开")
        lbl.configure(text=" · ".join(parts))

    def apply_profile(self):
        name = self.var_profile.get()
        prof = next((x for x in (self.cfg.get("profiles") or []) if (x.get("name") or "") == name), None)
        if not prof:
            return
        self.var_base.set(prof.get("base_url") or "")
        self.var_key.set(prof.get("api_key") or "")
        self.var_mdl.set(prof.get("model") or "")
        self.toast("已载入档案：" + name + "（点「应用并保存」生效）")

    def save_profile(self):
        name = simpledialog.askstring(APP, "档案名称（如：公司内网 vLLM）",
                                      initialvalue=self.var_profile.get() or "自定义")
        if not name:
            return
        profs = self.cfg.setdefault("profiles", [])
        item = {"name": name, "kind": "openai", "base_url": self.var_base.get().strip(),
                "api_key": self.var_key.get().strip(), "model": self.var_mdl.get().strip()}
        for i, x in enumerate(profs):
            if (x.get("name") or "") == name:
                profs[i] = item
                break
        else:
            profs.append(item)
        self.cfg["profiles"] = profs
        E.save_config(self.cfg)
        self.var_profile.set(name)
        if self.alive("cmb_profile") is not None:
            self.cmb_profile.configure(values=[x.get("name") for x in profs])
        self.toast("已保存档案：" + name)

    def panel_runtime(self, parent):
        p = self.palette()
        wrap = tk.Frame(parent, bg=p["panel"])
        head = tk.Frame(wrap, bg=p["panel"])
        head.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(head, text="运行时", bg=p["panel"], fg=p["ink"], font=(FONT, 10, "bold")).pack(side="left")
        tk.Label(head, text="本轮字段 · 实时", bg=p["panel"], fg=p["ink3"], font=(FONT, 8)).pack(side="left", padx=8)
        self.lbl_rt_tools = tk.Label(head, text="", bg=p["panel"], fg=p["ink3"], font=(MONO, 9))
        self.lbl_rt_tools.pack(side="right")
        # 字段栏位：label 在上、值在下，成列对齐（不再是一行挤爆的 k=v 串）
        grid = tk.Frame(wrap, bg=p["panel"])
        grid.pack(fill="x", padx=10, pady=(4, 4))
        self._rtf = {}
        for i, (key, name) in enumerate((("turn", "状态"), ("step", "步数"), ("elapsed", "用时"),
                                         ("tools_n", "工具调用"), ("tin", "输入 tokens"), ("tout", "输出 tokens"),
                                         ("treason", "思维 tokens"), ("ttotal", "合计 tokens"))):
            cell = tk.Frame(grid, bg=p["panel"])
            cell.grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 28), pady=3)
            tk.Label(cell, text=name, bg=p["panel"], fg=p["ink3"], font=(FONT, 8)).pack(anchor="w")
            lv = tk.Label(cell, text="—", bg=p["panel"], fg=p["ink"], font=(MONO, 11, "bold"))
            lv.pack(anchor="w")
            self._rtf[key] = lv
        ctxrow = tk.Frame(wrap, bg=p["panel"])
        ctxrow.pack(fill="x", padx=10, pady=(2, 6))
        tk.Label(ctxrow, text="上下文", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left")
        self.pb_ctx = ttk.Progressbar(ctxrow, mode="determinate", maximum=100, length=190)
        self.pb_ctx.pack(side="left", padx=8)
        self.lbl_ctx = tk.Label(ctxrow, text="尚未统计", bg=p["panel"], fg=p["ink3"], font=(MONO, 9))
        self.lbl_ctx.pack(side="left")
        ttk.Button(ctxrow, text="压缩上下文", command=self.act_compact).pack(side="left", padx=10)
        self.lbl_cost = tk.Label(ctxrow, text="", bg=p["panel"], fg=p["ink3"], font=(MONO, 9))
        self.lbl_cost.pack(side="right")
        cols = ("name", "ok", "ms", "args")
        self.tv_runs = ttk.Treeview(wrap, columns=cols, show="headings", height=8)
        for c, t, w in (("name", "工具", 110), ("ok", "结果", 60), ("ms", "耗时", 70), ("args", "参数 / 输出", 700)):
            self.tv_runs.heading(c, text=t)
            self.tv_runs.column(c, width=w, anchor="w")
        self.tv_runs.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.tv_runs.bind("<Double-Button-1>", self.on_pick_run)
        self.refresh_ctx()
        return wrap

    def panel_changes(self, parent):
        p = self.palette()
        wrap = tk.Frame(parent, bg=p["panel"])
        bar = tk.Frame(wrap, bg=p["panel"])
        bar.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(bar, text="文件变更（代理写入前自动留检查点）", bg=p["panel"], fg=p["ink"],
                 font=(FONT, 10, "bold")).pack(side="left")
        self.lbl_cp = tk.Label(bar, text="", bg=p["panel"], fg=p["ink3"], font=(FONT, 8))
        self.lbl_cp.pack(side="right")
        cols = ("tool", "path", "size", "ts", "state")
        self.tv_cp = ttk.Treeview(wrap, columns=cols, show="headings", height=7)
        for c, t, w in (("tool", "工具", 80), ("path", "文件", 520), ("size", "字节", 110), ("ts", "时间", 80),
                        ("state", "状态", 90)):
            self.tv_cp.heading(c, text=t)
            self.tv_cp.column(c, width=w, anchor="w")
        self.tv_cp.pack(fill="x", padx=10, pady=(0, 6))
        self.tv_cp.bind("<<TreeviewSelect>>", self.on_pick_cp)
        act = tk.Frame(wrap, bg=p["panel"])
        act.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Button(act, text="回滚选中", style="Danger.TButton", command=self.act_rollback_one).pack(side="left")
        ttk.Button(act, text="回滚全部", style="Danger.TButton", command=self.act_rollback_all).pack(side="left", padx=6)
        ttk.Button(act, text="刷新", command=self.refresh_changes).pack(side="left", padx=6)
        ttk.Button(act, text="清空列表", command=self.act_clear_cp).pack(side="left")
        self.txt_diff = tk.Text(wrap, relief="flat", bg=p["sunken"], fg=p["ink"], font=(MONO, 9),
                                wrap="none", padx=8, pady=6)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_diff.yview)
        self.txt_diff.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt_diff.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.txt_diff.tag_configure("add", foreground=p["ok"])
        self.txt_diff.tag_configure("del", foreground=p["err"])
        self.txt_diff.tag_configure("hunk", foreground=self.ui.get("accent", "#5b8cff"))
        self.refresh_changes()
        return wrap

    def refresh_changes(self):
        tv = self.alive("tv_cp")
        if tv is None:
            return
        if not self.visible("changes"):        # 未选中该标签：标记待刷，切回时补一次
            self._stale.add("changes")
            return
        tv.delete(*tv.get_children())
        self._cp_rows = E.checkpoints(self.session(), include_undone=True)
        for c in reversed(self._cp_rows):
            tv.insert("", "end", iid=c["id"], values=(c["tool"], c["path"], "%d → %d" % (
                c["bytes_before"], c["bytes_after"]), ts(c["ts"]), "已回滚" if c.get("undone") else "生效中"))
        lbl = self.alive("lbl_cp")
        if lbl is not None:
            live = len([c for c in self._cp_rows if not c.get("undone")])
            lbl.configure(text="共 %d 个检查点 · %d 个可回滚" % (len(self._cp_rows), live))

    def on_pick_cp(self, e=None):
        tv = self.alive("tv_cp")
        w = self.alive("txt_diff")
        if tv is None or w is None:
            return
        sel = tv.selection()
        if not sel:
            return
        cp = next((c for c in self._cp_rows if c["id"] == sel[0]), None)
        if not cp:
            return
        w.delete("1.0", "end")
        head = "%s  %s\n%s\n%s\n\n" % (cp["tool"], cp["path"], ts(cp["ts"]),
                                       "已回滚" if cp.get("undone") else "生效中")
        w.insert("end", head, "hunk")
        for ln in (cp.get("diff") or "").splitlines():
            tag = "add" if ln.startswith("+") else ("del" if ln.startswith("-") else
                                                    ("hunk" if ln.startswith("@") else ""))
            w.insert("end", ln + "\n", tag)
        w.see("1.0")

    def act_rollback_one(self):
        tv = self.alive("tv_cp")
        if tv is None or not tv.selection():
            messagebox.showinfo(APP, "请先在列表里选中一个检查点。")
            return
        cid = tv.selection()[0]
        cp = next((c for c in self._cp_rows if c["id"] == cid), None)
        if not cp:
            return
        if not messagebox.askyesno(APP, "把\n%s\n还原到这次改动之前？" % cp["path"]):
            return
        res = E.rollback(self.session(), cid)
        self.save_all()
        self.refresh_changes()
        self.toast("；".join("%s %s" % ("✔" if r["ok"] else "✘", r["text"]) for r in res))
        self.append_system("回滚：" + "；".join(r["text"] for r in res))

    def act_rollback_all(self):
        cps = E.checkpoints(self.session())
        if not cps:
            messagebox.showinfo(APP, "当前会话没有可回滚的变更。")
            return
        if not messagebox.askyesno(APP, "回滚当前会话的全部 %d 个变更？\n（新建的文件会被删除）" % len(cps)):
            return
        res = E.rollback(self.session())
        self.save_all()
        self.refresh_changes()
        self.toast("已回滚 %d 个变更" % len(res))
        self.append_system("已回滚 %d 个变更：\n%s" % (len(res), "\n".join(
            "%s %s — %s" % ("✔" if r["ok"] else "✘", r["path"], r["text"]) for r in res)))

    def act_clear_cp(self):
        if not messagebox.askyesno(APP, "只清空列表（不还原文件）？"):
            return
        self.session()["checkpoints"] = []
        self.save_all()
        self.refresh_changes()

    def act_compact(self):
        s = self.session()
        if len(s.get("messages") or []) < 3:
            self.toast("消息太少，无需压缩")
            return
        if not messagebox.askyesno(APP, "调用模型把较早的历史压成摘要？这会消耗一次请求。"):
            return
        self.append_system("正在压缩上下文…")

        def work():
            try:
                r = E.compact_session(self.cfg, s, lambda k, d: self.q.put((k, d)))
            except Exception as e:
                r = {"ok": False, "text": "%s: %s" % (type(e).__name__, e)}
            self.q.put(("compacted", r))
        threading.Thread(target=work, daemon=True).start()

    def refresh_ctx(self):
        lbl = self.alive("lbl_ctx")
        pb = self.alive("pb_ctx")
        cl = self.alive("lbl_cost")
        st = E.ctx_state(self.cfg, self.session())
        cb = self.alive("lbl_ctxbar")
        if cb is not None:
            cb.configure(text="上下文 %.0f%% · %s/%s tokens" % (
                st["ratio"] * 100, fmt_n(st["used"]), fmt_n(st["limit"])))
        if lbl is None:
            return
        used = st["used"]
        src = "实测" if used else "待统计"
        lbl.configure(text="%s / %s tokens（%.0f%% · 窗口来源：%s · %s）" % (
            used, st["limit"], st["ratio"] * 100, st["source"], src))
        if pb is not None:
            pb["value"] = min(100.0, st["ratio"] * 100)
        if cl is not None:
            s = self.session()
            c = E.cost(self.cfg, s.get("usage_total") or {})
            cl.configure(text=("累计 %s tokens · 估算花费 %s 元" % (
                (s.get("usage_total") or {}).get("total_tokens", 0), c)) if c is not None else
                ("累计 %s tokens · 未设置单价" % (s.get("usage_total") or {}).get("total_tokens", 0)))

    def panel_console(self, parent):
        p = self.palette()
        wrap = tk.Frame(parent, bg=p["panel"])
        bar = tk.Frame(wrap, bg=p["panel"])
        bar.pack(fill="x", padx=10, pady=6)
        self.var_lv = tk.StringVar(value="全部")
        ttk.Combobox(bar, textvariable=self.var_lv, values=["全部", "info", "warn", "error"],
                     width=8, state="readonly").pack(side="left")
        ttk.Button(bar, text="刷新", command=self.refresh_console).pack(side="left", padx=6)
        ttk.Button(bar, text="清屏显示", command=lambda: self.txt_log.delete("1.0", "end")).pack(side="left")
        ttk.Button(bar, text="打开日志文件", command=lambda: self.open_path(E.LOG_PATH)).pack(side="left", padx=6)
        self.lbl_log = tk.Label(bar, text="", bg=p["panel"], fg=p["ink3"], font=(FONT, 8))
        self.lbl_log.pack(side="right")
        self.txt_log = tk.Text(wrap, relief="flat", bg=p["sunken"], fg=p["ink2"], font=(MONO, 9),
                               wrap="none", padx=8, pady=6)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.refresh_console()
        return wrap

    # ---------- 扩展面板通用外壳：技能 / 插件 / MCP ----------
    def visible(self, pid):
        """面板是否可见（未打开、或标签未选中 → False）。热路径刷新据此跳过无谓重排。"""
        f = self.frames.get(pid)
        if f is None:
            return False
        try:
            return bool(f.winfo_ismapped())
        except Exception:
            return False

    def on_tab_changed(self, event=None):
        """切回某个曾被跳过刷新的面板时补一次，保证「省刷新」不丢数据。"""
        try:
            pend = [pid for pid in list(self._stale) if self.visible(pid)]
        except Exception:
            return
        for pid in pend:
            self._stale.discard(pid)
            if pid == "runtime":
                self.refresh_runtime()
            elif pid == "changes":
                self.refresh_changes()
            elif pid == "console":
                self.refresh_console()
            elif pid == "skills":
                self.refresh_skills()
            elif pid == "plugins":
                self.refresh_plugins()
            elif pid == "mcp":
                self.refresh_mcp()

    def _ext_frame(self, parent, title, sub):
        """扩展面板统一外壳：标题 + 说明 + 右侧状态；返回 (wrap, bar, body, lbl_state)。"""
        p = self.palette()
        wrap = tk.Frame(parent, bg=p["bg"])
        head = tk.Frame(wrap, bg=p["panel"])
        head.pack(fill="x")
        tk.Label(head, text=title, bg=p["panel"], fg=p["ink"], font=(FONT, 10, "bold")).pack(
            side="left", padx=10, pady=(8, 2))
        tk.Label(head, text=sub, bg=p["panel"], fg=p["ink3"], font=(FONT, 8)).pack(side="left")
        lbl_state = tk.Label(head, text="", bg=p["panel"], fg=p["ink3"], font=(MONO, 8))
        lbl_state.pack(side="right", padx=10)
        bar = tk.Frame(wrap, bg=p["panel"])
        bar.pack(fill="x", padx=8, pady=5)
        body = tk.Frame(wrap, bg=p["bg"])
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        return wrap, bar, body, lbl_state

    def _ext_buttons(self, bar, items):
        """一行扁平按钮；items = [(文案, 回调), ...]。"""
        p = self.palette()
        for txt, fn in items:
            tk.Button(bar, text=txt, font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                      command=fn).pack(side="left", padx=3, pady=1)

    def _split(self, body):
        """左右分栏容器（左列表 / 右详情），用于三个扩展面板。"""
        p = self.palette()
        pan = ttk.Panedwindow(body, orient="horizontal")
        pan.pack(fill="both", expand=True)
        left = tk.Frame(pan, bg=p["bg"])
        right = tk.Frame(pan, bg=p["bg"])
        pan.add(left, weight=3)
        pan.add(right, weight=2)
        return left, right

    def _detail_text(self, parent, name):
        """只读等宽详情框（注册到 self.<name> 便于 alive() 判定存在性）。"""
        p = self.palette()
        w = tk.Text(parent, relief="flat", bg=p["sunken"], fg=p["ink"], font=(MONO, 9),
                    wrap="word", padx=8, pady=6)
        w.configure(state="disabled")
        setattr(self, name, w)
        return w

    def _set_detail(self, name, text):
        w = self.alive(name)
        if w is None:
            return
        try:
            w.configure(state="normal")
            w.delete("1.0", "end")
            w.insert("1.0", text)
            w.configure(state="disabled")
        except Exception:
            pass

    def _tree(self, parent, cols, name, height=12):
        """带滚动条与列定义的 Treeview（columns=(键, 表头, 宽), ...）。"""
        wrap = tk.Frame(parent, bg=self.palette()["bg"])
        tv = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings", height=height)
        for key, title, w in cols:
            tv.heading(key, text=title)
            tv.column(key, width=w, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        setattr(self, name, tv)
        return wrap

    def _restore_sel(self, tv, iid):
        """刷新后恢复选中项：否则「启用/停用、删除」这类连续操作每点一次都要重新点选。"""
        if not iid or tv is None:
            return
        try:
            if tv.exists(iid):
                tv.selection_set(iid)
                tv.focus(iid)
        except Exception:
            pass

    def _tree_sel(self, name):
        tv = self.alive(name)
        sel = tv.selection() if tv is not None else ()
        return sel[0] if sel else ""

    # ================= 技能库 =================
    def panel_skills(self, parent):
        """技能库面板：SKILL.md 技能包的列、读、建、删、打开目录（对应核心工具 skills）。"""
        p = self.palette()
        wrap, bar, body, lbl_state = self._ext_frame(
            parent, "技能库", "SKILL.md 技能包 · 可被「插件」按 {{占位}} 注入调用")
        left, right = self._split(body)
        self._tree(left, (("name", "技能", 170), ("desc", "说明", 320), ("size", "大小", 70),
                          ("files", "文件数", 60)), "tv_skills").pack(fill="both", expand=True)
        self._detail_text(right, "txt_skill").pack(fill="both", expand=True)
        self.tv_skills.bind("<<TreeviewSelect>>", lambda e: self.show_skill())
        self._ext_buttons(bar, (
            ("刷新", self.refresh_skills),
            ("查看 SKILL.md", self.show_skill),
            ("新建技能…", self.act_new_skill),
            ("删除所选…", self.act_del_skill),
            ("正文插入输入框", self.act_skill_to_input),
            ("打开技能目录", self.act_open_skill_dir),
        ))
        self.lbl_skills_state = lbl_state
        self._skill_rows = {}
        self.refresh_skills()
        return wrap

    def refresh_skills(self):
        tv = self.alive("tv_skills")
        if tv is None:
            return
        import picore as C
        try:
            r = C.skills_list()
        except Exception as e:
            self.toast("技能读取失败：%s: %s" % (type(e).__name__, e))
            return
        items = r.get("items") or []
        keep = self._tree_sel("tv_skills")
        self._skill_rows = {x["name"]: x for x in items}
        tv.delete(*tv.get_children())
        for x in items:
            tv.insert("", "end", iid=x["name"], values=(x["name"], (x.get("description") or "")[:90],
                                                        "%d B" % x.get("bytes", 0),
                                                        len(x.get("files") or [])))
        self._restore_sel(tv, keep)
        lbl = getattr(self, "lbl_skills_state", None)
        if lbl is not None:
            try:
                lbl.configure(text="%d 个技能 · %s" % (len(items), r.get("dir") or ""))
            except Exception:
                pass

    def _skill_sel(self):
        tv = self.alive("tv_skills")
        sel = tv.selection() if tv is not None else ()
        return sel[0] if sel else ""

    def show_skill(self):
        name = self._skill_sel()
        if not name:
            return
        import picore as C
        r = C.skill_read(name)
        self._set_detail("txt_skill", r.get("text") or ("读取失败：" + str(r.get("error"))))

    def act_new_skill(self):
        p = self.palette()
        win = tk.Toplevel(self.root)
        win.title(APP + " · 新建技能")
        win.geometry("760x560")
        win.configure(bg=p["bg"])
        win.transient(self.root)
        tk.Label(win, text="技能名（字母 / 数字 / - / _）", bg=p["bg"], fg=p["ink2"], font=(FONT, 9)).pack(
            anchor="w", padx=12, pady=(10, 0))
        e_name = tk.Entry(win, bg=p["sunken"], fg=p["ink"], relief="flat", insertbackground=p["ink"],
                          font=(MONO, 9))
        e_name.pack(fill="x", padx=12, ipady=3)
        tk.Label(win, text="一句话说明 description（写清用途，便于模型判断何时该用）", bg=p["bg"],
                 fg=p["ink2"], font=(FONT, 9)).pack(anchor="w", padx=12, pady=(10, 0))
        e_desc = tk.Entry(win, bg=p["sunken"], fg=p["ink"], relief="flat", insertbackground=p["ink"],
                          font=(MONO, 9))
        e_desc.pack(fill="x", padx=12, ipady=3)
        tk.Label(win, text="正文（Markdown；插件调用时会替换 {{参数}} 占位）", bg=p["bg"], fg=p["ink2"],
                 font=(FONT, 9)).pack(anchor="w", padx=12, pady=(10, 0))
        body = tk.Text(win, bg=p["sunken"], fg=p["ink"], relief="flat", insertbackground=p["ink"],
                       font=(MONO, 9), wrap="word")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        body.insert("1.0", "# 技能名\n\n## 用途\n\n## 步骤\n1. \n")

        def ok():
            import picore as C
            name = e_name.get().strip()
            if not name:
                messagebox.showinfo(APP, "先填技能名。", parent=win)
                return
            r = C.skill_create(name, e_desc.get().strip(), body.get("1.0", "end-1c"))
            if r.get("ok"):
                win.destroy()
                self.refresh_skills()
                self.toast("已创建技能：" + name)
            else:
                messagebox.showerror(APP, str(r.get("error") or "创建失败"), parent=win)
        row = tk.Frame(win, bg=p["bg"])
        row.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(row, text="创建", style="Acc.TButton", command=ok).pack(side="left")
        ttk.Button(row, text="取消", command=win.destroy).pack(side="left", padx=6)

    def act_del_skill(self):
        name = self._skill_sel()
        if not name:
            messagebox.showinfo(APP, "先选中一个技能。")
            return
        if not messagebox.askyesno(APP, "删除技能 %s ？（整个技能目录会移除）" % name):
            return
        import picore as C
        r = C.skill_delete(name)
        self.refresh_skills()
        self.toast(r.get("text") or r.get("error") or ("已删除 " + name))

    def act_skill_to_input(self):
        name = self._skill_sel()
        w = self.alive("txt_skill")
        if not name or w is None:
            return
        text = w.get("1.0", "end-1c")
        if not text.strip():
            return
        self.txt_input.delete("1.0", "end")
        self.txt_input.insert("1.0", text)
        self.toast("技能正文已插入输入框（可编辑后发送）")

    def act_open_skill_dir(self):
        import picore as C
        self.open_path(C.SKILL_DIR)

    # ================= 插件基座 =================
    def panel_plugins(self, parent):
        """插件基座面板：manifest 列表 / 启停 / 查看 / 删除 / 示例 / 调用插件工具。"""
        p = self.palette()
        wrap, bar, body, lbl_state = self._ext_frame(
            parent, "插件基座", "plugin.json 声明式清单 · 工具自动注册进模型工具表 · 根目录内边界读写")
        self.lbl_plugins_state = lbl_state
        left, right = self._split(body)
        self._tree(left, (("id", "插件 id", 150), ("name", "名称", 140), ("ver", "版本", 60),
                          ("st", "状态", 70), ("tools", "注册工具", 220)), "tv_plugins",
                   height=9).pack(fill="both", expand=True)
        self._detail_text(right, "txt_plugin").pack(fill="both", expand=True)
        self.tv_plugins.bind("<<TreeviewSelect>>", lambda e: self.show_plugin())
        # 调用区：选一个插件工具并传 JSON 参数，真实执行
        call = tk.Frame(wrap, bg=p["panel"])
        call.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(call, text="调用插件工具", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left")
        self.cmb_plugin_tool = ttk.Combobox(call, width=40, state="readonly")
        self.cmb_plugin_tool.pack(side="left", padx=6)
        tk.Label(call, text="参数 JSON", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left")
        self.ent_plugin_args = tk.Entry(call, bg=p["sunken"], fg=p["ink"], relief="flat",
                                        insertbackground=p["ink"], font=(MONO, 9), width=34)
        self.ent_plugin_args.insert(0, "{}")
        self.ent_plugin_args.pack(side="left", padx=6, ipady=3)
        self._ext_buttons(call, (("运行", self.act_plugin_call),))
        self.txt_plugin_out = tk.Text(wrap, height=7, relief="flat", bg=p["sunken"], fg=p["ink2"],
                                      font=(MONO, 9), wrap="word", padx=8, pady=4)
        self.txt_plugin_out.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.txt_plugin_out.insert("1.0", "尚未调用插件工具。")
        self._ext_buttons(bar, (
            ("刷新", self.refresh_plugins),
            ("查看清单", self.show_plugin),
            ("启用 / 停用", self.act_plugin_toggle),
            ("删除所选…", self.act_plugin_delete),
            ("新建示例插件", self.act_plugin_example),
            ("重载基座", lambda: self.reload_plugins_async()),
            ("打开插件根目录", self.act_open_plugin_dir),
        ))
        self._plugin_rows = {}
        self.refresh_plugins()
        return wrap

    def refresh_plugins(self):
        tv = self.alive("tv_plugins")
        if tv is None:
            return
        try:
            import piplugins
            st = piplugins.state()
        except Exception as e:
            self.toast("插件读取失败：%s: %s" % (type(e).__name__, e))
            return
        self._plugin_rows = {x["id"]: x for x in st.get("items") or []}
        keep = self._tree_sel("tv_plugins")
        tv.delete(*tv.get_children())
        for x in st.get("items") or []:
            state = "停用" if not x.get("enabled") else ("错误" if x.get("error") else "启用")
            tv.insert("", "end", iid=x["id"], values=(x["id"], x.get("name") or "", x.get("version") or "",
                                                      state, ", ".join(x.get("registered") or []) or "—"))
        self._restore_sel(tv, keep)
        lbl = getattr(self, "lbl_plugins_state", None)
        if lbl is not None:
            try:
                lbl.configure(text="%d 个插件 · %d 个注册工具 · %s"
                                   % (st.get("total", 0), st.get("tools", 0), st.get("dir") or ""))
            except Exception:
                pass
        self.refresh_plugin_tools()

    def refresh_plugin_tools(self):
        cmb = self.alive("cmb_plugin_tool")
        if cmb is None:
            return
        names = [n for n in E.TOOLS if n.startswith("plugin_")]
        try:
            cmb.configure(values=names)
            if names and not cmb.get():
                cmb.set(names[0])
        except Exception:
            pass

    def _plugin_sel(self):
        tv = self.alive("tv_plugins")
        sel = tv.selection() if tv is not None else ()
        return sel[0] if sel else ""

    def show_plugin(self):
        pid = self._plugin_sel()
        if not pid:
            return
        import piplugins
        r = piplugins.read_plugin(pid)
        self._set_detail("txt_plugin", r.get("text") or ("读取失败：" + str(r.get("error"))))

    def act_plugin_toggle(self):
        pid = self._plugin_sel()
        if not pid:
            messagebox.showinfo(APP, "先选中一个插件。")
            return
        cur = (self._plugin_rows.get(pid) or {}).get("enabled", True)
        import piplugins
        r = piplugins.set_enabled(pid, not cur)
        self.refresh_plugins()
        self.refresh_tools()
        self.toast(r.get("text") or r.get("error") or "")

    def act_plugin_delete(self):
        pid = self._plugin_sel()
        if not pid:
            messagebox.showinfo(APP, "先选中一个插件。")
            return
        if not messagebox.askyesno(APP, "删除插件 %s ？（插件目录会被移除，不可回滚）" % pid):
            return
        import piplugins
        r = piplugins.delete_plugin(pid)
        self.refresh_plugins()
        self.refresh_tools()
        self.toast(r.get("text") or r.get("error") or "")

    def act_plugin_example(self, pid="skill-caller"):
        import piplugins
        r = piplugins.create_example(pid, overwrite=True)
        self.refresh_plugins()
        self.refresh_tools()
        if r.get("ok"):
            self.show_panel("plugins")
            self.toast("已创建示例插件：" + str(r.get("id") or pid))
            self.append_system("已创建示例插件 %s（声明 skill + shell 两个工具，可直接在「插件」面板调用或让模型调用）"
                               % (r.get("id") or pid))
        else:
            messagebox.showerror(APP, str(r.get("error") or "创建失败"))

    def reload_plugins_async(self):
        def work():
            try:
                import piplugins
                st = piplugins.load_all()
                n = len([x for x in st.values() if x.get("enabled", True)])
                txt = "插件基座已重载：%d 个插件（%d 个启用）" % (len(st), n)
            except Exception as e:
                txt = "插件重载失败：%s: %s" % (type(e).__name__, e)
            self.q.put(("ext", {"panel": "plugins", "text": txt}))
        threading.Thread(target=work, daemon=True).start()

    def act_open_plugin_dir(self):
        import piplugins
        r = piplugins.open_root()
        self.toast(r.get("text") or r.get("error") or "")

    def act_plugin_call(self):
        cmb = self.alive("cmb_plugin_tool")
        out = self.alive("txt_plugin_out")
        if cmb is None:
            return
        name = (cmb.get() or "").strip()
        if not name or name not in E.TOOLS:
            messagebox.showinfo(APP, "先选择一个插件工具（插件需已启用并注册工具）。")
            return
        ent = self.alive("ent_plugin_args")
        raw = (ent.get() if ent is not None else "") or "{}"
        try:
            args = json.loads(raw) if raw.strip() else {}
        except Exception as e:
            messagebox.showerror(APP, "参数 JSON 解析失败：%s" % e)
            return
        t = E.TOOLS[name]
        if t.get("mutating") and not messagebox.askyesno(APP, "插件工具 %s 标记为「需审批」（可能改动本机）。执行？" % name):
            return
        if out is not None:
            out.delete("1.0", "end")
            out.insert("1.0", "运行中：%s %s\n" % (name, json.dumps(args, ensure_ascii=False)))

        def work():
            try:
                res = E.run_tool(name, args, {"workspace": self.cfg.get("workspace"),
                                              "sandbox": bool((self.cfg.get("prefs") or {}).get("sandbox", True))})
                text = ("✔ %sms\n" % res.get("ms", 0)) + str(res.get("text") or "")
                okv = res.get("ok")
            except Exception as e:
                text, okv = "%s: %s" % (type(e).__name__, e), False
            self.q.put(("pluginrun", {"tool": name, "ok": okv, "text": text}))
        threading.Thread(target=work, daemon=True).start()

    # ================= MCP 服务器 =================
    def panel_mcp(self, parent):
        """MCP 面板：服务器列表 / 状态 / 连接重载 / 测试 / 启停 / 删除 / 新增。"""
        p = self.palette()
        wrap, bar, body, lbl_state = self._ext_frame(
            parent, "MCP 服务器", "连接后在工具表生成 mcp_* 动态工具 · 支持 stdio 与 http 传输")
        self.lbl_mcp_state = lbl_state
        left, right = self._split(body)
        self._tree(left, (("id", "服务器", 140), ("tr", "传输", 70), ("st", "状态", 90),
                          ("tools", "工具数", 70), ("target", "命令 / URL", 300)), "tv_mcp",
                   height=9).pack(fill="both", expand=True)
        self._detail_text(right, "txt_mcp").pack(fill="both", expand=True)
        self.tv_mcp.bind("<<TreeviewSelect>>", lambda e: self.show_mcp())
        form = tk.Frame(wrap, bg=p["panel"])
        form.pack(fill="x", padx=8, pady=(0, 4))
        tk.Label(form, text="新增 / 更新服务器", bg=p["panel"], fg=p["ink"], font=(FONT, 9, "bold")).pack(side="left")
        tk.Label(form, text="id", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left", padx=(10, 2))
        self.ent_mcp_id = tk.Entry(form, width=14, bg=p["sunken"], fg=p["ink"], relief="flat",
                                   insertbackground=p["ink"], font=(MONO, 9))
        self.ent_mcp_id.pack(side="left", ipady=3)
        tk.Label(form, text="传输", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left", padx=(8, 2))
        self.cmb_mcp_tr = ttk.Combobox(form, values=("stdio", "http"), width=7, state="readonly")
        self.cmb_mcp_tr.set("stdio")
        self.cmb_mcp_tr.pack(side="left")
        tk.Label(form, text="command", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left", padx=(8, 2))
        self.ent_mcp_cmd = tk.Entry(form, bg=p["sunken"], fg=p["ink"], relief="flat",
                                    insertbackground=p["ink"], font=(MONO, 9), width=26)
        self.ent_mcp_cmd.pack(side="left", ipady=3)
        tk.Label(form, text="args / url", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left", padx=(8, 2))
        self.ent_mcp_url = tk.Entry(form, bg=p["sunken"], fg=p["ink"], relief="flat",
                                    insertbackground=p["ink"], font=(MONO, 9), width=26)
        self.ent_mcp_url.pack(side="left", ipady=3)
        self.var_mcp_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="启用", variable=self.var_mcp_on).pack(side="left", padx=6)
        self._ext_buttons(form, (("保存", self.act_mcp_save),))
        self.txt_mcp_out = tk.Text(wrap, height=6, relief="flat", bg=p["sunken"], fg=p["ink2"],
                                   font=(MONO, 9), wrap="word", padx=8, pady=4)
        self.txt_mcp_out.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        self.txt_mcp_out.insert("1.0", "尚未连接 MCP 服务器。")
        self._ext_buttons(bar, (
            ("刷新", self.refresh_mcp),
            ("查看详情", self.show_mcp),
            ("连接 / 重载全部", lambda: self.mcp_reload_async()),
            ("测试所选", self.act_mcp_test),
            ("删除所选…", self.act_mcp_delete),
            ("停用全部（释放进程）", self.act_mcp_stop_all),
            ("打开 mcp.json", self.act_open_mcp_cfg),
        ))
        self._mcp_rows = {}
        self.refresh_mcp()
        return wrap

    def refresh_mcp(self):
        tv = self.alive("tv_mcp")
        if tv is None:
            return
        import picore as C
        try:
            st = C.mcp_state()
        except Exception as e:
            self.toast("MCP 读取失败：%s: %s" % (type(e).__name__, e))
            return
        self._mcp_rows = {x["id"]: x for x in st.get("servers") or []}
        keep = self._tree_sel("tv_mcp")
        tv.delete(*tv.get_children())
        mark = {"running": "● 已连接", "stopped": "○ 未启动", "disabled": "– 已停用", "error": "✘ 错误"}
        for x in st.get("servers") or []:
            stt = mark.get(x.get("status"), x.get("status"))
            if x.get("error") and x.get("status") != "running":
                stt = "✘ 错误"                     # 启动失败后 status 会回落为 stopped，这里按错误显示
            tv.insert("", "end", iid=str(x.get("id")), values=(
                x.get("id"), x.get("transport"), stt,
                len(x.get("tools") or []), x.get("url") or x.get("command") or ""))
        self._restore_sel(tv, keep)
        lbl = getattr(self, "lbl_mcp_state", None)
        if lbl is not None:
            try:
                lbl.configure(text="%d 个服务器 · %d 个动态工具 · %s"
                                   % (len(st.get("servers") or []), st.get("live_tools", 0), st.get("path") or ""))
            except Exception:
                pass

    def _mcp_sel(self):
        tv = self.alive("tv_mcp")
        sel = tv.selection() if tv is not None else ()
        return sel[0] if sel else ""

    def show_mcp(self):
        sid = self._mcp_sel()
        if not sid:
            return
        import picore as C
        row = self._mcp_rows.get(sid) or {}
        spec = next((s for s in (C.mcp_load().get("servers") or []) if s.get("id") == sid), {})
        txt = json.dumps({"配置": spec, "运行状态": row}, ensure_ascii=False, indent=2)
        self._set_detail("txt_mcp", txt)

    def act_mcp_save(self):
        import picore as C
        sid = (self.ent_mcp_id.get() or "").strip()
        tr = self.cmb_mcp_tr.get() or "stdio"
        cmd = (self.ent_mcp_cmd.get() or "").strip()
        extra = (self.ent_mcp_url.get() or "").strip()
        spec = {"id": sid, "transport": tr, "enabled": bool(self.var_mcp_on.get())}
        if tr == "http":
            if not extra:
                messagebox.showinfo(APP, "http 传输需要填 url。")
                return
            spec["url"] = extra
        else:
            if not cmd:
                messagebox.showinfo(APP, "stdio 传输需要填 command（如 npx / python）。")
                return
            spec["command"] = cmd
            spec["args"] = [a for a in re.split(r"\s+", extra) if a]
        r = C.mcp_upsert(spec)
        self.refresh_mcp()
        self.toast("已保存 MCP 服务器：%s（点「连接 / 重载全部」生效）" % r.get("id"))
        self.mcp_reload_async()

    def act_mcp_test(self):
        sid = self._mcp_sel()
        if not sid:
            messagebox.showinfo(APP, "先选中一个服务器。")
            return
        import picore as C
        spec = next((s for s in (C.mcp_load().get("servers") or []) if s.get("id") == sid), None)
        if not spec:
            return
        out = self.alive("txt_mcp_out")
        if out is not None:
            out.delete("1.0", "end")
            out.insert("1.0", "正在临时启动 %s 并列出工具…\n" % sid)

        def work():
            r = C.mcp_test_spec(dict(spec))
            lines = ["%s %s" % ("✔ 连接成功" if r.get("ok") else "✘ 连接失败", r.get("error") or "")]
            for t in r.get("tools") or []:
                lines.append("  · %s  %s" % (t.get("name"), t.get("desc")))
            self.q.put(("mcpout", "\n".join(lines)))
        threading.Thread(target=work, daemon=True).start()

    def act_mcp_delete(self):
        sid = self._mcp_sel()
        if not sid:
            messagebox.showinfo(APP, "先选中一个服务器。")
            return
        if not messagebox.askyesno(APP, "从 mcp.json 删除服务器 %s ？" % sid):
            return
        import picore as C
        C.mcp_delete(sid)
        self.refresh_mcp()
        self.refresh_tools()
        self.toast("已删除 MCP 服务器：" + sid)

    def act_mcp_stop_all(self):
        import picore as C
        C.mcp_stop_all()
        self.refresh_mcp()
        self.toast("已停用全部 MCP 服务器（工具表已释放）")

    def act_open_mcp_cfg(self):
        import picore as C
        self.open_path(C.MCP_PATH)

    def mcp_reload_async(self):
        self.toast("正在重载 MCP 服务器（连接可能需要几秒）…")
        out = self.alive("txt_mcp_out")
        if out is not None:
            out.delete("1.0", "end")
            out.insert("1.0", "正在重载 MCP 服务器…\n")

        def work():
            import picore as C
            try:
                C.mcp_reload()
                st = C.mcp_state()
                lines = ["MCP 重载完成：%d 个服务器 · %d 个动态工具"
                         % (len(st.get("servers") or []), st.get("live_tools", 0))]
                for x in st.get("servers") or []:
                    bad = bool(x.get("error"))
                    lines.append("  %s %s（%s）%s" % (
                        "✘" if bad else ("●" if x.get("status") == "running" else "○"),
                        x.get("id"), "错误" if bad else x.get("status"),
                        ("  " + x["error"][:140]) if bad else ""))
                txt = "\n".join(lines)
            except Exception as e:
                txt = "MCP 重载失败：%s: %s" % (type(e).__name__, e)
            self.q.put(("mcpout", txt))
            self.q.put(("ext", {"panel": "mcp"}))
        threading.Thread(target=work, daemon=True).start()

    def panel_settings(self, parent):
        p = self.palette()
        wrap = tk.Frame(parent, bg=p["panel"])
        pr = self.cfg.get("prefs") or {}
        cols = tk.Frame(wrap, bg=p["panel"])
        cols.pack(fill="x", padx=14, pady=(10, 0))
        left = tk.Frame(cols, bg=p["panel"])
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(cols, bg=p["panel"])
        right.pack(side="left", fill="both", expand=True, padx=(24, 0))
        tk.Label(left, text="运行参数", bg=p["panel"], fg=p["ink"], font=(FONT, 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        tk.Label(right, text="行为与网络", bg=p["panel"], fg=p["ink"], font=(FONT, 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.var_temp = tk.DoubleVar(value=float(pr.get("temperature", 0.7)))
        self.var_steps = tk.IntVar(value=int(pr.get("max_steps", 40)))
        self.var_to = tk.IntVar(value=int(pr.get("timeout", 180)))
        self.var_shto = tk.IntVar(value=int(pr.get("shell_timeout", 60)))
        self.var_maxout = tk.IntVar(value=int(pr.get("max_output_tokens", 0)))
        self.var_retries = tk.IntVar(value=int(pr.get("retries", 2)))
        self.var_price_in = tk.DoubleVar(value=float(pr.get("price_in", 0) or 0))
        self.var_price_out = tk.DoubleVar(value=float(pr.get("price_out", 0) or 0))
        self.var_proxy = tk.StringVar(value=pr.get("proxy") or "")
        self.var_appr = tk.BooleanVar(value=bool(pr.get("approve_mutating", True)))
        self.var_tools = tk.BooleanVar(value=bool(pr.get("enable_tools", True)))
        self.var_sandbox = tk.BooleanVar(value=bool(pr.get("sandbox", True)))
        self.var_autocp = tk.BooleanVar(value=bool(pr.get("auto_compact", True)))
        self.var_autotitle = tk.BooleanVar(value=bool(pr.get("auto_title", True)))
        for i, (lab, var, tip) in enumerate([
                ("温度 temperature", self.var_temp, "0-2，越低越确定"),
                ("单轮最大步数", self.var_steps, "模型可连续调用工具的次数上限"),
                ("模型超时（秒）", self.var_to, "单次请求超时"),
                ("命令超时（秒）", self.var_shto, "shell / py_run 默认超时"),
                ("最大输出 tokens", self.var_maxout, "0 = 交给模型默认"),
                ("网络重试次数", self.var_retries, "429/5xx/连接失败时退避重试"),
                ("输入单价（元/千token）", self.var_price_in, "填 0 则不计费"),
                ("输出单价（元/千token）", self.var_price_out, "填 0 则不计费")], start=1):
            tk.Label(left, text=lab, bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).grid(
                row=i, column=0, sticky="w", pady=2)
            tk.Entry(left, textvariable=var, bg=p["sunken"], fg=p["ink"], relief="flat", width=16,
                     insertbackground=p["ink"], font=(MONO, 9)).grid(row=i, column=1, sticky="w", padx=8, ipady=3)
            tk.Label(left, text=tip, bg=p["panel"], fg=p["ink3"], font=(FONT, 8)).grid(row=i, column=2, sticky="w")
        tk.Label(right, text="HTTP/HTTPS 代理（留空跟随系统）", bg=p["panel"], fg=p["ink2"],
                 font=(FONT, 9)).grid(row=1, column=0, sticky="w", pady=2)
        tk.Entry(right, textvariable=self.var_proxy, bg=p["sunken"], fg=p["ink"], relief="flat", width=34,
                 insertbackground=p["ink"], font=(MONO, 9)).grid(row=1, column=1, sticky="w", padx=8, ipady=3)
        tk.Label(right, text="例如 http://127.0.0.1:7890", bg=p["panel"], fg=p["ink3"],
                 font=(FONT, 8)).grid(row=1, column=2, sticky="w")
        for i, (lab, var, tip) in enumerate([
                ("沙箱：文件与命令限制在工作区内", self.var_sandbox, "越界路径会被拒绝"),
                ("写入 / 执行类工具需要审批", self.var_appr, "弹窗可改参数"),
                ("允许模型调用工具", self.var_tools, "关闭则只纯文本对话"),
                ("上下文接近上限时自动压缩", self.var_autocp, "75% 阈值触发"),
                ("首轮结束后自动命名会话", self.var_autotitle, "调用一次极小请求")], start=2):
            ttk.Checkbutton(right, text=lab, variable=var, style="Card.TCheckbutton").grid(
                row=i, column=0, columnspan=3, sticky="w", pady=2)
            tk.Label(right, text=tip, bg=p["panel"], fg=p["ink3"], font=(FONT, 8)).grid(row=i, column=3, sticky="w")
        qa = tk.Frame(wrap, bg=p["panel"])
        qa.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(qa, text="问答（QA）：需求不明确时先问清", bg=p["panel"], fg=p["ink"],
                 font=(FONT, 10, "bold")).pack(anchor="w")
        rowq = tk.Frame(qa, bg=p["panel"])
        rowq.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(rowq, text="先问后做（模型先用 ask_user 出选项问你，再动手）", variable=self.var_qa,
                        command=self.toggle_qa, style="Card.TCheckbutton").pack(side="left")
        tk.Label(rowq, text="单轮问题上限（题）", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(side="left", padx=(16, 4))
        tk.Entry(rowq, textvariable=self.var_qamax, bg=p["sunken"], fg=p["ink"], relief="flat", width=4,
                 insertbackground=p["ink"], font=(MONO, 9)).pack(side="left", ipady=2)
        ttk.Button(rowq, text="试一次问答卡", command=self.qa_demo_card).pack(side="left", padx=10)
        ttk.Button(rowq, text="问答状态", command=lambda: self.slash("/qa")).pack(side="left")
        tk.Label(qa, text="开启后：有歧义的需求会先弹问答卡（单选 / 多选 / 可自定义），选完再执行；"
                          "Esc 跳过则模型按最合理默认继续并列出假设。上限 1-%d 题。" % E.QA_MAX_Q,
                 bg=p["panel"], fg=p["ink3"], font=(FONT, 8)).pack(anchor="w", pady=(2, 0))
        tk.Label(wrap, text="系统提示词", bg=p["panel"], fg=p["ink2"], font=(FONT, 9)).pack(
            anchor="w", padx=14, pady=(10, 2))
        self.txt_sys = tk.Text(wrap, height=9, relief="flat", bg=p["sunken"], fg=p["ink"],
                               insertbackground=p["ink"], font=(FONT, 9), wrap="word")
        self.txt_sys.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        self.txt_sys.insert("1.0", pr.get("system") or E.DEFAULT_SYSTEM)
        bar = tk.Frame(wrap, bg=p["panel"])
        bar.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(bar, text="保存设置", style="Acc.TButton", command=self.save_settings).pack(side="left")
        ttk.Button(bar, text="恢复默认（不影响模型与密钥）", command=self.reset_settings).pack(side="left", padx=6)
        ttk.Button(bar, text="打开数据目录", command=lambda: self.open_path(E.HOME)).pack(side="left")
        ttk.Button(bar, text="清除本机数据（会话+配置）", style="Danger.TButton",
                   command=self.wipe).pack(side="right")
        return wrap

    def panel_about(self, parent):
        p = self.palette()
        wrap = tk.Frame(parent, bg=p["panel"])
        txt = tk.Text(wrap, relief="flat", bg=p["panel"], fg=p["ink"], font=(FONT, 9), wrap="word",
                      padx=14, pady=10)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        info = [
            "%s %s · 原生桌面应用（Python %s + tkinter %s）" % (APP, VERSION, ".".join(map(str, sys.version_info[:3])),
                                                               tk.TkVersion),
            "非网页、无浏览器内核；全部能力在本机进程内执行。",
            "",
            "【这个应用真正做了什么】",
            "· 对话：直连你配置的 OpenAI 兼容端点，真实流式（SSE）返回，token 用量取自接口返回。",
            "· 工具：%d 个工具在本机真实执行 —— 命令行、读写文件、正则搜索、HTTP 抓取、跑 Python、热点抓取、系统信息、计算。"
            % len(E.TOOLS),
            "· 审批：写入与执行类工具调用前弹窗确认，可拒绝；密钥在工具输出中被自动隐去。",
            "· 持久化：会话、配置、布局分别落盘，重启即恢复。",
            "",
            "【诊断】",
            "查看子进程是否真的在跑：ls ~/.pistudio 下的 config.json / sessions.json / pistudio.log",
            "命令行自检：python pistudio.py --selftest",
            "命令行真实对话：python pistudio.py --chat",
            "单次提问：python pistudio.py --ask \"列出当前目录\"",
            "跑一条命令：python pistudio.py --run \"python --version\"",
            "",
            "【路径】",
            "数据目录：%s" % E.HOME,
            "配置：%s" % E.CONFIG_PATH,
            "会话：%s" % E.SESSIONS_PATH,
            "日志：%s" % E.LOG_PATH,
            "错误：%s" % E.ERROR_PATH,
            "工作区：%s" % (self.cfg.get("workspace") or ""),
            "",
            "【环境】",
            E.run_tool("sys_info", {}, {"workspace": self.cfg.get("workspace")})["text"],
            "",
            "【真实工具清单】",
        ]
        for t in E.TOOLS.values():
            info.append("· %-10s [%s] %s%s" % (t["name"], t["group"], t["desc"], "  ← 需审批" if t["mutating"] else ""))
        txt.insert("1.0", "\n".join(info))
        txt.configure(state="disabled")
        return wrap

    def on_tab_menu(self, event):
        nb = event.widget
        try:
            idx = int(nb.index("@%d,%d" % (event.x, event.y)))
        except Exception:
            return
        pid = None
        for p, f in self.frames.items():
            try:
                if int(nb.index(f)) == idx:
                    pid = p
                    break
            except Exception:
                continue
        if not pid:
            return
        self._menu_pid = pid
        self.tab_menu.tk_popup(event.x_root, event.y_root)

    def move_current(self, where):
        if getattr(self, "_menu_pid", None):
            self.move_panel(self._menu_pid, where)

    def close_current(self):
        if getattr(self, "_menu_pid", None):
            self.close_panel(self._menu_pid)

    def move_panel(self, pid, where):
        if self.dock_of.get(pid) == where:
            return
        f = self.frames.pop(pid, None)
        if f is None:
            return
        nb = self.nb_main if self.dock_of.get(pid) == "main" else self.nb_bottom
        try:
            nb.forget(f)
        except Exception:
            pass
        f.destroy()
        self.add_panel(pid, self.panel_titles[pid], where)

    def close_panel(self, pid):
        if pid == "chat":
            messagebox.showinfo(APP, "对话面板是核心面板，不能关闭。")
            return
        f = self.frames.pop(pid, None)
        if f is None:
            return
        nb = self.nb_main if self.dock_of.get(pid) == "main" else self.nb_bottom
        try:
            nb.forget(f)
        except Exception:
            pass
        f.destroy()

    def show_panel(self, pid):
        f = self.frames.get(pid)
        if f is None:
            self.add_panel(pid, self.panel_titles[pid], self.panel_dock.get(pid, "main"))
            f = self.frames.get(pid)
        if f is None:
            return
        nb = self.nb_main if self.dock_of.get(pid) == "main" else self.nb_bottom
        try:
            nb.select(f)          # 关键：新建标签后必须选中，否则「打开 X 面板」只是把它加到标签栏却不显示
        except Exception:
            pass

    def reset_layout(self):
        for pid in list(self.frames.keys()):
            if pid != "chat":
                self.close_panel(pid)
        for pid, _title, where, default in PANEL_DEFS:
            if pid != "chat" and default:
                self.add_panel(pid, self.panel_titles[pid], where)
        self.toast("布局已重置")

    def refresh_sessions(self):
        q = (self.var_search.get() or "").strip().lower()
        self.tree.delete(*self.tree.get_children())
        groups = {}
        for s in self.sessions:
            if q:
                blob = (s.get("title") or "") + " " + " ".join(str(m.get("content", "")) for m in s["messages"])
                if q not in blob.lower():
                    continue
            groups.setdefault(day_of(s.get("updated") or s.get("created") or time.time()), []).append(s)
        for g in list(groups.keys()):
            node = self.tree.insert("", "end", text="  " + g, open=True)
            for s in sorted(groups[g], key=lambda x: -(x.get("updated") or 0)):
                self.tree.insert(node, "end", iid=s["id"], text="    " + (s.get("title") or "会话"))
        if self.tree.exists(self.cur):
            self.tree.selection_set(self.cur)

    def on_pick_session(self, e=None):
        sel = self.tree.selection()
        if not sel or sel[0] not in [s["id"] for s in self.sessions]:
            return
        if sel[0] == self.cur:
            return                                   # 程序化重选同一会话（刷新列表触发）：不重载、不重绘
        self.cur = sel[0]
        self.stream_buf = ""
        self.render_chat()
        self.refresh_changes()
        self.refresh_ctx()

    def on_session_menu(self, e):
        row = self.tree.identify_row(e.y)
        if row and row in [s["id"] for s in self.sessions]:
            self.tree.selection_set(row)
            self.cur = row
            self.sess_menu.tk_popup(e.x_root, e.y_root)

    def act_new(self):
        s = E.new_session()
        self.sessions.insert(0, s)
        self.cur = s["id"]
        self.stream_buf = ""
        self.save_all()
        self.refresh_sessions()
        self.render_chat()

    def act_rename(self):
        s = self.session()
        name = simpledialog.askstring(APP, "会话名称", initialvalue=s.get("title") or "")
        if name:
            s["title"] = name
            s["updated"] = time.time()
            self.save_all()
            self.refresh_sessions()

    def act_dup(self):
        s = self.session()
        c = E.new_session((s.get("title") or "会话") + " 副本")
        c["messages"] = json.loads(json.dumps(s.get("messages") or []))
        self.sessions.insert(0, c)
        self.cur = c["id"]
        self.save_all()
        self.refresh_sessions()
        self.render_chat()

    def act_delete(self):
        if len(self.sessions) <= 1:
            messagebox.showinfo(APP, "至少保留一个会话。")
            return
        if not messagebox.askyesno(APP, "删除会话「%s」？" % (self.session().get("title") or "")):
            return
        self.sessions = [s for s in self.sessions if s["id"] != self.cur]
        self.cur = self.sessions[0]["id"]
        self.save_all()
        self.refresh_sessions()
        self.render_chat()

    def act_clear(self):
        if not messagebox.askyesno(APP, "清空当前会话的全部消息？"):
            return
        self.session()["messages"] = []
        self.stream_buf = ""
        self.save_all()
        self.render_chat()

    def act_export(self):
        s = self.session()
        path = filedialog.asksaveasfilename(defaultextension=".md",
                                            initialfile="%s.md" % (s.get("title") or "session").replace("/", "_"),
                                            filetypes=[("Markdown", "*.md"), ("全部", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(session_markdown(s))
            E.log("info", "export", path)
            self.toast("已导出 " + path)
        except Exception as e:
            messagebox.showerror(APP, str(e))

    def act_list_tools(self):
        lines = ["可用工具（%d）" % len(E.TOOLS)]
        for t in E.TOOLS.values():
            lines.append("· %-10s [%s] %s%s" % (t["name"], t["group"], t["desc"], "  ← 需审批" if t["mutating"] else ""))
        self.append_system("\n".join(lines))

    def pick_workspace(self):
        d = filedialog.askdirectory(initialdir=self.var_ws.get() or os.path.expanduser("~"))
        if d:
            self.var_ws.set(d)
            self.cfg["workspace"] = d
            E.save_config(self.cfg)
            self.toast("工作区：" + d)

    def toggle_key(self):
        self.ent_key.configure(show="" if self.ent_key.cget("show") else "•")

    def apply_model(self):
        prov = self.cfg.setdefault("provider", {})
        prov["base_url"] = self.var_base.get().strip()
        prov["api_key"] = self.var_key.get().strip()
        prov["model"] = (self.var_mdl.get() or self.var_model.get() or "").strip()
        prov.pop("_autodetected", None)
        self.cfg["workspace"] = self.var_ws.get().strip() or self.cfg.get("workspace")
        E.save_config(self.cfg)
        self.var_model.set(prov["model"])
        self.toast("已应用：%s · %s" % (E.norm_base(prov["base_url"]), prov["model"] or "未选模型"))
        self.do_probe()

    def on_pick_model(self, e=None):
        lst = self.alive("lst_models")
        if lst is None:
            return
        sel = lst.curselection()
        if not sel:
            return
        name = lst.get(sel[0])
        self.var_mdl.set(name)
        self.var_model.set(name)
        self.apply_model()

    def import_host(self):
        found = E.host_providers()
        if not found:
            messagebox.showinfo(APP, "未在 ~/.nmoiaigent/config.sqlite 中找到已配置的 Provider。")
            return
        hp = found[0]
        self.var_base.set(hp["base_url"])
        self.var_key.set(hp["api_key"])
        self.var_mdl.set(hp["model"])
        self.set_model_list(hp.get("models") or [])
        self.set_probe_text("已导入：%s · %s · 模型 %s · 密钥 %s" % (
            hp["name"], hp["base_url"], hp["model"], E.mask_key(hp["api_key"])), self.palette()["ok"])
        self.apply_model()

    def set_probe_text(self, text, color=None):
        lbl = self.alive("lbl_probe")
        if lbl is not None:
            try:
                lbl.configure(text=text, fg=color or self.palette()["ink3"])
            except Exception:
                pass

    def set_model_list(self, models):
        lst = self.alive("lst_models")
        if lst is None:
            return
        lst.delete(0, "end")
        for m in models:
            lst.insert("end", m)

    def detect_local(self):
        self.set_probe_text("正在扫描本机常用端口…", self.palette()["ink3"])

        def work():
            self.q.put(("local", E.detect_local_endpoints()))
        threading.Thread(target=work, daemon=True).start()

    def do_probe(self, manual=False):
        prov = {"base_url": self.var_base.get().strip(), "api_key": self.var_key.get().strip(),
                "model": self.var_mdl.get().strip()}
        self.lbl_conn.configure(text="● 探测中…", fg=self.palette()["warn"])
        self.set_probe_text("正在请求 %s/models …" % E.norm_base(prov["base_url"]))

        def work():
            self.q.put(("probe", E.probe(prov, timeout=10)))
        threading.Thread(target=work, daemon=True).start()

    def boot(self):
        E.log("info", "startup", "PI Studio %s 启动 · %s" % (VERSION, sys.executable))
        self.refresh_console()
        self.init_core()
        def work():
            imported = E.autoconfigure(self.cfg)
            prov = self.cfg.get("provider") or {}
            res = E.probe(prov, timeout=10)
            self.q.put(("boot", {"imported": imported, "probe": res, "provider": prov}))
        threading.Thread(target=work, daemon=True).start()

    def init_core(self):
        try:
            import picore as CORE

            def prov(marker, title):
                for s in self.sessions:
                    if s.get("cron_marker") == marker:
                        return s
                s = E.new_session(title)
                s["cron_marker"] = marker
                self.sessions.insert(0, s)
                return s

            CORE.init(self.cfg, enable_scheduler=True, session_provider=prov, session_saver=self.save_all)
            self.refresh_tools()
            E.log("info", "core", "PI 核心能力层已挂载（记忆/技能/MCP/调度/错误库/子代理/账本/插件基座）")
        except Exception as e:
            E.log("error", "core", "核心层挂载失败：%s: %s" % (type(e).__name__, e))

    def refresh_tools(self):
        """按「分组 + 关键词」过滤后填充工具列表；同时刷新插件 / MCP 面板的工具下拉。"""
        lst = self.alive("lst_tools")
        if lst is None:
            return
        grp = ""
        kw = ""
        try:
            grp = (self.var_toolgrp.get() or "全部")
        except Exception:
            grp = "全部"
        try:
            kw = (self.var_toolkw.get() or "").strip().lower()
        except Exception:
            kw = ""
        groups = sorted({t.get("group") or "其他" for t in E.TOOLS.values()})
        cmb = self.alive("cmb_toolgrp")
        if cmb is not None:
            try:
                cmb.configure(values=["全部"] + groups)
            except Exception:
                pass
        lst.delete(0, "end")
        self.tool_ids = []
        for n, t in E.TOOLS.items():
            if grp and grp != "全部" and (t.get("group") or "其他") != grp:
                continue
            if kw and kw not in ("%s %s %s" % (n, t.get("group", ""), t.get("desc", ""))).lower():
                continue
            self.tool_ids.append(n)
            lst.insert("end", "%-9s %s%s" % (n, t["group"], "  ⚠审批" if t["mutating"] else ""))
        lbl = self.alive("lbl_tools_n")
        if lbl is not None:
            try:
                lbl.configure(text="显示 %d / %d 个工具 · 分组 %s" % (len(self.tool_ids), len(E.TOOLS),
                                                                " · ".join(groups)))
            except Exception:
                pass
        self.refresh_plugin_tools()

    def on_pick_tool(self, e=None):
        sel = self.lst_tools.curselection()
        if not sel:
            return
        name = self.tool_ids[sel[0]]
        t = E.TOOLS[name]
        self.cur_tool = name
        self.lbl_tool.configure(text="%s  ·  %s" % (name, t["group"]))
        self.lbl_tool_desc.configure(text=t["desc"] + ("　（执行前需审批）" if t["mutating"] else ""))
        for w in self.form.winfo_children():
            w.destroy()
        self.tool_forms[name] = {}
        props = (t["parameters"] or {}).get("properties") or {}
        required = set((t["parameters"] or {}).get("required") or [])
        p = self.palette()
        if not props:
            tk.Label(self.form, text="该工具不需要参数。", bg=p["panel"], fg=p["ink3"],
                     font=(FONT, 9)).pack(anchor="w")
            return
        for key, spec in props.items():
            row = tk.Frame(self.form, bg=p["panel"])
            row.pack(fill="x", pady=2)
            star = " *" if key in required else ""
            tk.Label(row, text=key + star, bg=p["panel"], fg=p["ink2"], font=(MONO, 9), width=14,
                     anchor="w").pack(side="left")
            typ = spec.get("type", "string")
            if spec.get("enum"):
                var = tk.StringVar(value=spec["enum"][0])
                w = ttk.Combobox(row, textvariable=var, values=spec["enum"], width=20)
                w.pack(side="left")
                self.tool_forms[name][key] = ("enum", var)
            elif typ in ("string",) and (key in ("content", "code", "old", "new", "text") or sym_long(spec)):
                w = tk.Text(row, height=4, width=90, relief="flat", bg=p["sunken"], fg=p["ink"],
                            insertbackground=p["ink"], font=(MONO, 9), wrap="none")
                w.pack(side="left", fill="x", expand=True)
                self.tool_forms[name][key] = ("text", w)
            else:
                var = tk.StringVar()
                tk.Entry(row, textvariable=var, bg=p["sunken"], fg=p["ink"], relief="flat",
                         insertbackground=p["ink"], font=(MONO, 9), width=80).pack(side="left", fill="x", expand=True)
                self.tool_forms[name][key] = (typ, var)

    def collect_tool_args(self):
        name = getattr(self, "cur_tool", None)
        args = {}
        for key, (typ, w) in (self.tool_forms.get(name) or {}).items():
            if typ == "text":
                v = w.get("1.0", "end-1c")
            elif typ == "enum":
                v = w.get()
            else:
                v = w.get()
            if v == "" and typ != "boolean":
                continue
            if typ == "integer":
                try:
                    v = int(v)
                except Exception:
                    v = 0
            elif typ == "number":
                try:
                    v = float(v)
                except Exception:
                    v = 0.0
            elif typ == "boolean":
                v = str(v).strip().lower() in ("1", "true", "yes", "是")
            args[key] = v
        return args

    def run_selected_tool(self):
        name = getattr(self, "cur_tool", None)
        if not name:
            messagebox.showinfo(APP, "请先在左侧选择一个工具。")
            return
        args = self.collect_tool_args()
        ws = self.cfg.get("workspace")
        self.set_tool_out("运行中：%s %s" % (name, json.dumps(args, ensure_ascii=False)[:400]))

        def work():
            s = self.session()
            prefs = self.cfg.get("prefs") or {}

            def _cp(p, t, b, a, m):
                E.make_checkpoint(s, p, t, b, a, m)

            res = E.run_tool(name, args, {"workspace": ws, "shell_timeout": prefs.get("shell_timeout", 60),
                                          "sandbox": bool(prefs.get("sandbox", True)),
                                          "checkpoint": _cp})
            self.q.put(("toolrun", {"name": name, "args": args, "res": res}))
        threading.Thread(target=work, daemon=True).start()

    def set_tool_out(self, text):
        self.tool_out = text
        w = self.alive("txt_tool")
        if w is None:
            return
        w.delete("1.0", "end")
        w.insert("1.0", text)

    def save_settings(self):
        pr = self.cfg.setdefault("prefs", {})
        try:
            pr["temperature"] = float(self.var_temp.get())
            pr["max_steps"] = int(self.var_steps.get())
            pr["timeout"] = int(self.var_to.get())
            pr["shell_timeout"] = int(self.var_shto.get())
            pr["max_output_tokens"] = int(self.var_maxout.get())
            pr["retries"] = int(self.var_retries.get())
            pr["price_in"] = float(self.var_price_in.get())
            pr["price_out"] = float(self.var_price_out.get())
        except Exception as e:
            messagebox.showerror(APP, "参数不是有效数字：" + str(e))
            return
        pr["proxy"] = self.var_proxy.get().strip()
        pr["approve_mutating"] = bool(self.var_appr.get())
        pr["enable_tools"] = bool(self.var_tools.get())
        pr["sandbox"] = bool(self.var_sandbox.get())
        pr["auto_compact"] = bool(self.var_autocp.get())
        pr["auto_title"] = bool(self.var_autotitle.get())
        try:
            pr["qa_first"] = bool(self.var_qa.get())
            pr["qa_max"] = max(1, min(int(self.var_qamax.get() or 4), E.QA_MAX_Q))
        except Exception:
            pr["qa_max"] = 4
        pr["system"] = self.txt_sys.get("1.0", "end-1c")
        E.save_config(self.cfg)
        self.refresh_ctx()
        self.toast("设置已保存 · 沙箱 %s · 审批 %s · 重试 %d · 代理 %s" % (
            "开" if pr["sandbox"] else "关", "开" if pr["approve_mutating"] else "关", pr["retries"],
            pr["proxy"] or "跟随系统"))

    def reset_settings(self):
        keep = {k: (self.cfg.get("prefs") or {}).get(k) for k in ("price_in", "price_out", "proxy")}
        self.cfg["prefs"] = json.loads(json.dumps(E.DEFAULT_CONFIG["prefs"]))
        self.cfg["prefs"].update({k: v for k, v in keep.items() if v is not None})
        E.save_config(self.cfg)
        self.show_panel("settings")
        self.close_panel("settings")
        self.show_panel("settings")
        self.toast("已恢复默认设置")

    def wipe(self):
        if not messagebox.askyesno(APP, "将删除本机的会话与配置（%s），不可恢复。继续？" % E.HOME):
            return
        for f in (E.CONFIG_PATH, E.SESSIONS_PATH, E.UI_PATH):
            try:
                os.remove(f)
            except Exception:
                pass
        self.toast("已清除，重启应用后生效")

    def toggle_theme(self):
        self.ui["theme"] = "light" if self.ui.get("theme", "dark") == "dark" else "dark"
        E.save_ui(self.ui)
        self.frames = {}
        self.dock_of = {}
        for w in self.root.winfo_children():
            w.destroy()
        self.build_style()
        self.build_menu()
        self.build_topbar()
        self.build_status()
        self.build_root()
        self.build_sidebar()
        self.build_docks()
        self.refresh_sessions()
        self.render_chat()
        self.refresh_tools()
        self.refresh_runtime()
        self.refresh_changes()
        self.refresh_ctx()
        self.refresh_console()
        self.root.after(300, lambda: self.do_probe())

    def open_path(self, path):
        try:
            if os.path.isdir(path):
                os.startfile(path)
            elif os.path.isfile(path):
                os.startfile(path)
            else:
                os.makedirs(os.path.dirname(path) or path, exist_ok=True)
                os.startfile(os.path.dirname(path) or path)
        except Exception as e:
            messagebox.showerror(APP, str(e))

    def append_system(self, text):
        t = self.alive("chat_text")
        if t is None:
            return
        t.configure(state="normal")
        t.insert("end", "\n", "dim")
        t.insert("end", text + "\n", "dim")
        t.configure(state="disabled")
        self._see_end(t)

    def alive(self, name):
        w = getattr(self, name, None)
        try:
            if w is not None and w.winfo_exists():
                return w
        except Exception:
            pass
        return None

    def render_chat(self):
        t = self.alive("chat_text")
        if t is None:
            return
        p = self.palette()
        t.configure(state="normal")
        t.delete("1.0", "end")
        s = self.session()
        self._msg_marks = []
        if not s["messages"]:
            t.insert("end", "\n会话已就绪。\n\n", "ai_h")
            t.insert("end", "· 直接输入问题即可，模型会按需调用本机工具并把过程显示在这里。\n"
                            "· 想验证「不是用来看的」：切到「工具」面板随便跑一个命令，或输入 /run python --version。\n"
                            "· 写文件类工具在真正落盘前会留检查点，「变更」面板可一键回滚。\n"
                            "· 输入 /help 查看全部斜杠命令。\n", "dim")
        msgs0 = s["messages"]
        CAP = 300                                   # 性能模式：只渲染最近 300 条
        if len(msgs0) > CAP:
            t.insert("end", "\n（性能模式：已折叠较早的 %d 条消息；导出与模型上下文不受影响）\n"
                            % (len(msgs0) - CAP), "dim")
            self._msg_marks.append(t.index("end-1c"))
            msgs0 = msgs0[-CAP:]
        for m in msgs0:
            self._msg_marks.append(t.index("end-1c"))
            self.render_one(t, m)
        if self.stream_buf:
            t.insert("end", "\nPI · %s\n" % ts(time.time()), "ai_h")
            self.md_render(t, self.stream_buf)
        t.configure(state="disabled")
        self._see_end(t)

    # ---------- 渲染性能层（流式合帧 / 局部重绘 / 跟随滚动） ----------
    @staticmethod
    def _line_no(pos):
        try:
            return int(str(pos).split(".")[0])
        except Exception:
            return -1

    def _see_end(self, t, force=False):
        """只在用户位于底部（或强制）时自动滚动，避免与手动翻看“抢滚动”。"""
        try:
            if force or t.yview()[1] > 0.995:
                self._follow = True
                t.see("end")
            else:
                self._follow = False
        except Exception:
            pass

    def _schedule_flush(self):
        """把高频流式增量合并为 ~40ms 一帧，避免逐 token 触发 Tk 重排（卡顿主因）。"""
        if self._flush_job is None:
            try:
                self._flush_job = self.root.after(40, self._flush_stream)
            except Exception:
                self._flush_job = None

    def _flush_stream(self, final=False):
        self._flush_job = None
        t = self.alive("chat_text")
        if t is None:
            return
        try:
            # 1) 推理流：只追加新片段，永不重排整段
            if self._reason_shown < len(self._reason_buf):
                t.configure(state="normal")
                t.insert("end", self._reason_buf[self._reason_shown:], "dim")
                t.configure(state="disabled")
                self._reason_shown = len(self._reason_buf)
                self._see_end(t)
            # 2) 正文流
            if not self.stream_buf or not (self._s_dirty or final):
                return
            self._s_dirty = False
            buf = self.stream_buf
            t.configure(state="normal")
            if self._stream_mark is None:
                self._stream_mark = t.index("end-1c")
            if self._stream_mode == "md" and len(buf) > 12000:
                # 超长消息（如整段代码）：最后一帧 Markdown，之后仅追加，结束再整理
                self._stream_mode = "raw"
                t.delete(self._stream_mark, "end-1c")
                self.md_render(t, buf)
                self._raw_shown = len(buf)
            elif self._stream_mode == "md":
                t.delete(self._stream_mark, "end-1c")
                self.md_render(t, buf)
            else:
                if final and len(buf) <= 60000:
                    t.delete(self._stream_mark, "end-1c")
                    self.md_render(t, buf)
                    self._raw_shown = len(buf)
                elif self._raw_shown < len(buf):
                    t.insert("end", buf[self._raw_shown:], "ai")
                    self._raw_shown = len(buf)
            t.configure(state="disabled")
            self._see_end(t)
        except Exception as e:
            E.log("error", "flush", str(e))

    def append_user_msg(self, text):
        """发送时只追加这一条（不再全量重建），长会话发送零卡顿。"""
        t = self.alive("chat_text")
        if t is None:
            self.render_chat()
            return
        t.configure(state="normal")
        self._msg_marks.append(t.index("end-1c"))
        self.render_one(t, {"role": "user", "content": text, "ts": time.time()})
        t.configure(state="disabled")
        self._see_end(t, force=True)

    def finalize_turn(self):
        """本轮结束后只重绘「本轮区间」，历史消息不重排。"""
        t = self.alive("chat_text")
        s = self.session()
        if t is None or self._turn_mark is None:
            self._turn_mark = None
            self.render_chat()
            return
        try:
            t.configure(state="normal")
            t.delete(self._turn_mark, "end-1c")
            tl = self._line_no(self._turn_mark)
            self._msg_marks = [mp for mp in self._msg_marks if self._line_no(mp) < tl]
            for m in s["messages"][self._turn_msg_start:]:
                self._msg_marks.append(t.index("end-1c"))
                self.render_one(t, m)
            t.configure(state="disabled")
        except Exception as e:
            try:
                t.configure(state="disabled")
            except Exception:
                pass
            E.log("error", "finalize", str(e))
            self._turn_mark = None
            self.render_chat()
            return
        self._turn_mark = None
        self._see_end(t, force=bool(self._follow))

    def render_one(self, t, m):
        role = m.get("role")
        if role == "user":
            t.insert("end", "\n你 · %s\n" % ts(m.get("ts")), "user_h")
            t.insert("end", str(m.get("content", "")) + "\n", "user")
        elif role == "assistant":
            txt = str(m.get("content", "")).strip()
            if txt:
                t.insert("end", "\nPI · %s\n" % ts(m.get("ts")), "ai_h")
                self.md_render(t, txt)
            for c in (m.get("tool_calls") or []):
                t.insert("end", "  ↳ 调用 %s\n" % ((c.get("function") or {}).get("name") or "?"), "dim")
        elif role == "tool":
            ok = m.get("ok", True)
            t.insert("end", "\n⚙ %s  %s  %sms %s\n" % (m.get("name"), "✔" if ok else "✘",
                                                       m.get("ms", 0), ts(m.get("ts"))), "tool_h")
            body = str(m.get("content", ""))
            if body:
                t.insert("end", body[:3000] + ("\n…（省略 %d 字符，完整内容见「运行时」面板或日志）" % (len(body) - 3000) if len(body) > 3000 else "") + "\n", "tool")

    # ---------- Markdown 渲染：分片 → 批量插入 ----------
    @staticmethod
    def md_segments(text, base="ai"):
        """把 Markdown 文本切成 [片段, 标签, 片段, 标签, ...]，供 Text.insert(*segs) 一次性落盘。

        与逐次 insert 完全等价（含 (base, "icode") 这种组合标签），但把 Tcl 调用数从
        O(行数 + 行内片段) 降到 1 次/消息 —— 流式重绘与长消息回填的主要开销就在这。
        """
        segs = []

        def put(s, tag):
            if s:
                segs.append(s)
                segs.append(tag)

        fence = False
        for ln in str(text).split("\n"):
            s = ln.rstrip()
            if s.lstrip().startswith("```"):
                if fence:
                    fence = False
                else:
                    fence = True
                    lang = s.lstrip()[3:].strip()
                    put("  ▍%s\n" % (lang or "代码"), "codebar")
                continue
            if fence:
                put(ln + "\n", "code")
                continue
            if not s.strip():
                put("\n", base)
                continue
            m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", s)
            if m:
                put(m.group(2).strip() + "\n", "mdh")
                continue
            m = re.match(r"^(\s*)[-*·]\s+(.*)$", s)
            if m:
                put("  • ", "mdli")
                App.md_inline_segments(segs, m.group(2), "mdli")
                put("\n", "mdli")
                continue
            App.md_inline_segments(segs, s, base)
            put("\n", base)
        return segs

    @staticmethod
    def md_inline_segments(segs, s, base="ai"):
        """行内解析（`代码` / **加粗**），把片段追加进 segs（纯函数，可离线自检）。"""
        for part in re.split(r"(`[^`\n]+`)", s):
            if len(part) > 2 and part.startswith("`") and part.endswith("`"):
                segs.append(part[1:-1])
                segs.append((base, "icode"))
                continue
            for seg in re.split(r"(\*\*[^*\n]+\*\*)", part):
                if len(seg) > 4 and seg.startswith("**") and seg.endswith("**"):
                    segs.append(seg[2:-2])
                    segs.append((base, "bold"))
                elif seg:
                    segs.append(seg)
                    segs.append(base)

    def md_render(self, t, text, base="ai"):
        """把助手消息按轻量 Markdown 渲染（与网页版一致）：围栏代码块 / 标题 / 无序列表 / 行内代码 / 加粗。"""
        if "DSML" in str(text):
            text = E.strip_text_tool_markup(text)
        segs = App.md_segments(text, base)
        if segs:
            t.insert("end", *segs)          # 单次 Tcl 调用（分片渲染的关键优化）

    def refresh_runtime(self):
        r = self.rt
        tk_ = r.get("tokens") or {}
        p = self.palette()
        acc = self.ui.get("accent", "#5b8cff")
        # —— 运行时面板：字段栏位（状态 / 步数 / 用时 / 工具 / 各类 tokens）——
        # 该面板被关闭或未选中时直接跳过：这些字段是全局热路径（每步 / 每次工具都会来），
        # 旧实现会对已销毁的 Label 反复 configure 抛异常（被 except 吞掉，纯浪费）。
        if not self.visible("runtime"):
            self._stale.add("runtime")
        elif getattr(self, "_rtf", None):
            def put(k, v, fg=None):
                lv = self._rtf.get(k)
                if lv is not None:
                    try:
                        lv.configure(text=v)
                        if fg:
                            lv.configure(fg=fg)
                    except Exception:
                        pass
            stt = r.get("turn") or "空闲"
            put("turn", stt, acc if stt != "空闲" else p["ok"])
            put("step", "%s/%s" % (r.get("step", 0), r.get("max", 0)))
            put("elapsed", "%.1fs" % (r.get("elapsed") or 0))
            put("tools_n", str(len(r.get("tools") or [])))
            put("tin", fmt_n(tk_.get("prompt_tokens", 0)))
            put("tout", fmt_n(tk_.get("completion_tokens", 0)))
            put("treason", fmt_n(tk_.get("reasoning_tokens", 0)) if tk_.get("reasoning_tokens") else "—")
            put("ttotal", fmt_n(tk_.get("total_tokens", 0)))
            lw = self.alive("lbl_rt_tools")
            if lw is not None:
                lw.configure(text="模型 %s · 累计压缩 %d 次" % (
                    (self.cfg.get("provider") or {}).get("model") or "未选",
                    self.session().get("compactions", 0)))
        # —— 状态栏右侧：模型 · tokens（始终可见，必刷）——
        st = self.alive("lbl_usage")
        if st is not None:
            st.configure(text="%s · tokens %s" % ((self.cfg.get("provider") or {}).get("model") or "未选模型",
                                                  fmt_n(tk_.get("total_tokens", 0))))
        if self.busy:
            self._update_turn_chip()
        if not self.visible("runtime"):
            return
        tv = self.alive("tv_runs")
        if tv is not None:
            tv.delete(*tv.get_children())
            for i, x in enumerate(reversed(r.get("tools") or [])):
                tv.insert("", "end", iid=str(i), values=(x.get("name"), "✔" if x.get("ok") else "✘",
                                                         "%sms" % x.get("ms", 0),
                                                         (json.dumps(x.get("args") or {}, ensure_ascii=False)[:120])))
        self._stale.discard("runtime")

    def on_pick_run(self, e=None):
        tv = self.tv_runs
        sel = tv.selection()
        if not sel:
            return
        idx = len(self.rt.get("tools") or []) - 1 - int(sel[0])
        runs = self.rt.get("tools") or []
        if 0 <= idx < len(runs):
            r = runs[idx]
            self.set_tool_out("[%s]\n参数：%s\n\n%s" % (r.get("name"), json.dumps(r.get("args") or {},
                                                                               ensure_ascii=False),
                                                       r.get("text", "")))

    def refresh_console(self):
        w = self.alive("txt_log")
        if w is None:
            return
        if not self.visible("console"):        # 控制台常被压在底部标签后：不可见时省掉 600 行重排
            self._stale.add("console")
            return
        lv = getattr(self, "var_lv", None)
        want = lv.get() if lv is not None else "全部"
        w.delete("1.0", "end")
        recs = E.log_records()
        for r in recs[-600:]:
            if want != "全部" and r["level"] != want:
                continue
            w.insert("end", "%s %-5s %-12s %s\n" % (ts(r["ts"]), r["level"], r["source"], r["message"]))
        w.see("end")
        lbl = self.alive("lbl_log")
        if lbl is not None:
            lbl.configure(text="内存 %d 条 · 文件 %s" % (len(recs), E.LOG_PATH))

    def on_return(self, e):
        if e.state & 0x0001:
            return None
        self.on_send()
        return "break"

    def on_send(self):
        if self.busy:
            self.stop_turn()
            return
        text = self.txt_input.get("1.0", "end-1c").strip()
        att = self.drain_attachments()
        if not text and not att:
            return
        self.txt_input.delete("1.0", "end")
        if text.startswith("/") and not att:
            if self.slash(text):
                return
        if att:
            text = (text + "\n\n" + att).strip()
        s = self.session()
        s["messages"].append({"role": "user", "content": text, "ts": time.time()})
        if s.get("title") in ("新会话", "", None):
            s["title"] = text[:20]
        s["updated"] = time.time()
        self.stream_buf = ""
        self.append_user_msg(text)
        self.refresh_sessions()
        self.save_all()
        self.start_turn()

    def slash(self, text):
        parts = text[1:].split(None, 1)
        cmd = (parts[0] if parts else "").lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "help":
            self.append_system(
                "/help 帮助\n/clear 清空当前会话\n/new 新建会话\n/model <名称> 切换模型\n/models 探测端点并列出模型\n"
                "/workspace <路径> 设置工作区\n/run <命令> 直接在本机执行命令\n/selftest 运行真实自检\n"
                "/tools 列出工具\n/export 导出会话\n/stop 停止生成（并终止已启动的进程）\n/status 当前状态\n"
                "/ctx 上下文账本与模型窗口\n/compact 压缩较早历史为摘要\n/changes 列出文件变更检查点\n"
                "/rollback [id|all] 回滚文件变更\n/attach 附加文件\n/title <名称> 重命名会话\n/logs 最近的运行日志\n"
                "/skills [list|new|open] 技能库\n/plugins [list|reload|example] 插件基座\n/mcp [list|reload|test|off] MCP 服务器\n"
                "/qa [on|off|max N|demo] 先问后做（需求有歧义时先用选项问你）")
            return True
        if cmd == "qa":
            sub = arg.strip().lower()
            try:
                import picore as _C
            except Exception:
                _C = None
            if sub in ("on", "off"):
                self.var_qa.set(sub == "on")
                self.toggle_qa()
                return True
            if sub.startswith("max"):
                try:
                    n = int(sub.split()[1])
                except Exception:
                    self.append_system("用法：/qa max 3（1-%d）" % E.QA_MAX_Q)
                    return True
                self.var_qamax.set(n)
                st = _C.qa_set(self.cfg, max_q=n) if _C else {"max": n, "enabled": bool(self.var_qa.get())}
                self.append_system("单轮澄清问题上限：%s 题" % st.get("max"))
                return True
            if sub in ("demo", "test", "试"):
                self.qa_demo_card()
                return True
            st = _C.qa_state(self.cfg) if _C else {"enabled": bool(self.var_qa.get()),
                                                   "max": int(self.var_qamax.get() or 4),
                                                   "tool": "ask_user" in E.TOOLS}
            self.append_system(
                "先问后做：%s（qa_first=%s）\n单轮问题上限：%s 题（上限 %d）\n问答工具：%s（%s）\n"
                "用法：/qa on · /qa off · /qa max 3 · /qa demo\n"
                "打开后：模型需求有歧义时会先弹「问答卡」给你选项（单选/多选/自定义），选完再动手；"
                "Esc 跳过则模型按默认假设继续。" % (
                    "开" if st.get("enabled") else "关", bool(st.get("enabled")), st.get("max"), E.QA_MAX_Q,
                    st.get("tool"), "ask_user 已注册" if st.get("tool") else "未注册（请检查引擎）"))
            return True
        if cmd in ("skills", "skill"):
            self.show_panel("skills")
            sub = (arg or "list").lower()
            if sub == "new":
                self.act_new_skill()
                return True
            if sub == "open":
                self.act_open_skill_dir()
                return True
            if sub in ("reload", "list", ""):
                self.refresh_skills()
                import picore as _C
                items = list(getattr(self, "_skill_rows", {}).values())
                self.append_system("技能库（%d）：%s\n目录 %s · 面板已打开" % (
                    len(items), ", ".join(x["name"] for x in items) or "（空）", _C.SKILL_DIR))
            return True
        if cmd in ("plugins", "plugin"):
            self.show_panel("plugins")
            sub = (arg or "list").lower()
            if sub == "reload":
                self.reload_plugins_async()
                return True
            if sub == "example":
                self.act_plugin_example()
                return True
            if sub == "open":
                self.act_open_plugin_dir()
                return True
            self.refresh_plugins()
            rows = list(getattr(self, "_plugin_rows", {}).values())
            self.append_system("插件基座（%d）：\n%s" % (
                len(rows), "\n".join("· %s %s v%s %s 工具[%s] %s" % (
                    x["id"], x["name"], x.get("version"), "启用" if x.get("enabled") else "停用",
                    ", ".join(x.get("registered") or []) or "—", x.get("error") or "")
                    for x in rows) or "（空，可用 /plugins example 建一个示例）"))
            return True
        if cmd == "mcp":
            self.show_panel("mcp")
            sub = (arg or "list").lower()
            if sub == "reload":
                self.mcp_reload_async()
                return True
            if sub == "off":
                self.act_mcp_stop_all()
                return True
            if sub == "test":
                self.act_mcp_test()
                return True
            self.refresh_mcp()
            rows = list(getattr(self, "_mcp_rows", {}).values())
            self.append_system("MCP 服务器（%d）：\n%s" % (
                len(rows), "\n".join("· %s [%s] %s %s" % (
                    x.get("id"), x.get("transport"), x.get("status"), x.get("error") or "")
                    for x in rows) or "（空，可在「MCP」面板新增服务器）"))
            return True
        if cmd == "ctx":
            st = E.ctx_state(self.cfg, self.session())
            s = self.session()
            self.append_system("模型 %s\n窗口 %s tokens（%s）\n已用 %s（%.1f%%）\n消息 %d 条 · 已压缩 %d 次\n"
                               "推理模型 %s · 最大输出 %s" % (
                                   (self.cfg.get("provider") or {}).get("model"), st["limit"], st["source"],
                                   st["used"], st["ratio"] * 100, st["messages"],
                                   s.get("compactions", 0), st["reasoning"], st["maxOutput"]))
            return True
        if cmd == "compact":
            self.act_compact()
            return True
        if cmd == "changes":
            cps = E.checkpoints(self.session(), include_undone=True)
            if not cps:
                self.append_system("当前会话没有文件变更检查点。")
            else:
                self.append_system("检查点（%d）：\n%s" % (len(cps), "\n".join(
                    "%s  %-8s %-6s %s  %d→%d 字节" % (c["id"], c["tool"], "已回滚" if c.get("undone") else "生效中",
                                                    c["path"], c["bytes_before"], c["bytes_after"]) for c in cps)))
                self.show_panel("changes")
            return True
        if cmd == "rollback":
            target = arg or "all"
            cps = E.checkpoints(self.session())
            if not cps:
                self.append_system("没有可回滚的变更。")
                return True
            if target != "all":
                cps = [c for c in cps if c["id"] == target]
            if not messagebox.askyesno(APP, "回滚 %d 个变更？" % len(cps)):
                return True
            res = E.rollback(self.session(), None if target == "all" else target)
            self.save_all()
            self.refresh_changes()
            self.append_system("回滚结果：\n" + "\n".join(
                "%s %s — %s" % ("✔" if r["ok"] else "✘", r["path"], r["text"]) for r in res))
            return True
        if cmd == "attach":
            self.act_attach()
            return True
        if cmd == "title":
            if arg:
                self.session()["title"] = arg[:40]
                self.save_all()
                self.refresh_sessions()
                self.append_system("会话已重命名为：" + arg[:40])
            return True
        if cmd == "logs":
            recs = E.log_records()[-25:]
            self.append_system("最近日志：\n" + "\n".join(
                "%s %-5s %-12s %s" % (ts(r["ts"]), r["level"], r["source"], r["message"]) for r in recs))
            return True
        if cmd == "clear":
            self.session()["messages"] = []
            self.stream_buf = ""
            self.save_all()
            self.render_chat()
            return True
        if cmd == "new":
            self.act_new()
            return True
        if cmd == "model":
            if arg:
                self.var_mdl.set(arg)
                self.var_model.set(arg)
                self.apply_model()
            else:
                self.append_system("当前模型：" + (self.var_model.get() or "未选择"))
            return True
        if cmd == "models":
            self.do_probe(True)
            self.append_system("正在探测 %s …" % E.norm_base(self.var_base.get()))
            return True
        if cmd == "workspace":
            if arg and os.path.isdir(arg):
                self.var_ws.set(arg)
                self.cfg["workspace"] = arg
                E.save_config(self.cfg)
                self.append_system("工作区已设为 " + arg)
            else:
                self.append_system("用法：/workspace <存在的目录路径>")
            return True
        if cmd == "run":
            if not arg:
                self.append_system("用法：/run <命令>")
                return True
            res = E.run_tool("shell", {"command": arg}, {"workspace": self.cfg.get("workspace"),
                                                         "shell_timeout": (self.cfg.get("prefs") or {}).get("shell_timeout", 60)})
            s = self.session()
            s["messages"].append({"role": "user", "content": "/run " + arg, "ts": time.time()})
            s["messages"].append({"role": "tool", "name": "shell", "content": res["text"],
                                  "ok": res["ok"], "ms": res["ms"], "ts": time.time(),
                                  "tool_call_id": "slash", "args": {"command": arg}})
            self.save_all()
            self.render_chat()
            return True
        if cmd == "selftest":
            self.act_selftest()
            return True
        if cmd == "tools":
            self.act_list_tools()
            return True
        if cmd == "export":
            self.act_export()
            return True
        if cmd == "stop":
            self.stop_turn()
            return True
        if cmd == "status":
            pr = E.probe(self.cfg.get("provider") or {}, timeout=8)
            self.append_system("端点 %s\n模型 %s\n连接 %s\n工作区 %s\n工具 %d 个" % (
                E.norm_base((self.cfg.get("provider") or {}).get("base_url")),
                self.var_model.get() or "未选择",
                ("正常 · %dms · %d 个模型" % (pr["ms"], len(pr["models"]))) if pr["ok"] else ("异常 · " + pr["error"][:120]),
                self.cfg.get("workspace"), len(E.TOOLS)))
            return True
        self.append_system("未知命令 /%s，输入 /help 查看。" % cmd)
        return True

    def start_turn(self):
        self.busy = True
        self.cancel = threading.Event()
        self.stream_buf = ""
        self._s_dirty = False
        self._stream_mark = None
        self._stream_mode = "md"
        self._raw_shown = 0
        self._reason_buf = ""
        self._reason_shown = 0
        self.rt.update({"turn": "生成中", "step": 0, "max": (self.cfg.get("prefs") or {}).get("max_steps", 12),
                        "tokens": {}, "tools": [], "elapsed": 0})
        s = self.session()
        t = self.alive("chat_text")
        self._turn_mark = t.index("end-1c") if t is not None else None
        self._turn_msg_start = len(s["messages"])
        self._t0 = time.time()
        self.btn_send.configure(text="停止", style="Danger.TButton")
        try:
            self.pb_turn.configure(maximum=max(1, int(self.rt.get("max") or 1)), value=0)
        except Exception:
            pass
        self._turn_chips(True)
        self.lbl_hint.configure(text="生成中… Enter 停止 · Esc 停止 · Shift+Enter 换行")
        self._tick()
        self.refresh_runtime()
        self.refresh_ctx()
        self.lbl_status.configure(text="正在与模型通讯…")
        t0 = time.time()

        def work():
            try:
                try:
                    import piplan
                    res = piplan.run_turn_auto(self.cfg, s, lambda k, d: self.q.put((k, d)),
                                               self.cancel, self.approve_request, self.ask_request)
                except ImportError:
                    res = E.run_turn(self.cfg, s, lambda k, d: self.q.put((k, d)),
                                     self.cancel, self.approve_request, self.ask_request)
                res["elapsed_all"] = round(time.time() - t0, 3)
                self.q.put(("done", res))
            except Exception as e:
                self.q.put(("error", "%s: %s" % (type(e).__name__, e)))
                self.q.put(("done", {"ok": False, "error": str(e), "steps": 0, "usage": {}, "tools": []}))
        threading.Thread(target=work, daemon=True).start()

    def stop_turn(self):
        if not self.busy:
            return
        if self.cancel:
            self.cancel.set()
        if self.approve_ev:
            self.approve_result = False
            self.approve_ev.set()
        if self.qa_ev:                      # 问答卡也一起释放，否则工作线程会一直等
            self.qa_result = None
            self.qa_ev.set()
        self.lbl_status.configure(text="正在停止…")

    def approve_request(self, name, args, meta):
        if name in self.allow_session:
            return True
        try:                                   # 审批策略：规则命中 / 自动确认 → 不再弹窗
            import pipolicy
            d = pipolicy.decide(self.cfg, name, args, meta)
            if d is True:
                self.q.put(("notice", "自动确认（策略放行）：" + name))
                return True
            if d is False:
                self.q.put(("notice", "自动拒绝（策略禁止）：" + name))
                return False
        except Exception:
            pass
        ev = threading.Event()
        self.approve_ev = ev
        self.approve_result = False
        self.approved_args = None
        self.q.put(("approve", {"name": name, "args": args, "meta": meta}))
        ev.wait()
        self.approve_ev = None
        if self.approve_result and self.approved_args is not None:
            return {"allow": True, "args": self.approved_args}
        return self.approve_result

    def ask_approve(self, d):
        """审批卡片：风险分级色条 + 关键字段摘要 + 可编辑参数 + 快捷键（Enter 允许 / Esc 拒绝）。"""
        p = self.palette()
        name = d.get("name") or "?"
        args = d.get("args") or {}
        meta = d.get("meta") or {}
        if name in ("shell", "py_run"):
            level, lc = "高", p["err"]
        elif name.startswith(("fs_write", "fs_edit", "fs_delete", "fs_mkdir", "fs_rename", "fs_move", "fs_copy")):
            level, lc = "中", p["warn"]
        else:
            level, lc = "低", p["ok"]

        win = tk.Toplevel(self.root)
        win.title("需要批准 · %s · %s" % (name, APP))
        win.configure(bg=p["bg"])
        win.transient(self.root)
        try:
            self.root.update_idletasks()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            w, h = 760, 560
            win.geometry("%dx%d+%d+%d" % (w, h, rx + max(0, (rw - w) // 2), ry + max(0, (rh - h) // 3)))
        except Exception:
            win.geometry("760x560")
        win.grab_set()

        tk.Frame(win, bg=lc, height=4).pack(fill="x")               # 顶部风险色条
        head = tk.Frame(win, bg=p["bg"])
        head.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(head, text="代理请求在本机执行：%s" % name, bg=p["bg"], fg=p["ink"],
                 font=(FONT, 12, "bold")).pack(side="left")
        tk.Label(head, text=" 风险 %s " % level, bg=lc, fg="#ffffff",
                 font=(FONT, 9, "bold")).pack(side="left", padx=10)
        desc = str(meta.get("desc") or "")
        tk.Label(win, text=(desc or "该操作需要你确认后才会执行。"), bg=p["bg"], fg=p["ink2"],
                 font=(FONT, 9), wraplength=700, justify="left").pack(anchor="w", padx=16, pady=(2, 0))
        warn = self.approve_warning(d)
        if warn:
            tk.Label(win, text=warn, bg=p["bg"], fg=p["warn"], font=(FONT, 9, "bold"),
                     wraplength=700, justify="left").pack(anchor="w", padx=16, pady=(6, 0))

        # 摘要卡：按工具类型显示最关键的字段（命令 / 路径 / 内容量 …）
        rows = []
        if name == "shell":
            rows.append(("命令", str(args.get("command", ""))[:400]))
            if args.get("cwd"):
                rows.append(("工作目录", str(args.get("cwd"))))
            if args.get("timeout"):
                rows.append(("超时(秒)", str(args.get("timeout"))))
        elif name == "py_run":
            rows.append(("代码长度", "%d 字符" % len(str(args.get("code") or ""))))
        for key in ("path", "file", "target"):
            if args.get(key):
                rows.append(("目标路径", self.abs_path(args.get(key))))
                break
        if name in ("fs_write", "fs_append") and args.get("content") is not None:
            rows.append(("写入内容", "%d 字符" % len(str(args.get("content") or ""))))
        if rows:
            sumf = tk.Frame(win, bg=p["panel"])
            sumf.pack(fill="x", padx=16, pady=(8, 0))
            for k, v in rows:
                r = tk.Frame(sumf, bg=p["panel"])
                r.pack(fill="x", padx=10, pady=2)
                tk.Label(r, text=k, bg=p["panel"], fg=p["ink3"], font=(FONT, 9), width=10,
                         anchor="e").pack(side="left")
                tk.Label(r, text=v, bg=p["panel"], fg=p["ink"], font=(MONO, 9), anchor="w",
                         justify="left", wraplength=560).pack(side="left", padx=8)

        tk.Label(win, text="参数（可直接编辑；不改动即按原参数执行）", bg=p["bg"], fg=p["ink2"],
                 font=(FONT, 8)).pack(anchor="w", padx=16, pady=(8, 0))
        box = tk.Text(win, height=13, relief="flat", bg=p["sunken"], fg=p["ink"], font=(MONO, 9),
                      wrap="word", padx=10, pady=8, insertbackground=p["ink"])
        box.pack(fill="both", expand=True, padx=16, pady=(4, 6))
        pretty = json.dumps(args, ensure_ascii=False, indent=2)
        orig = pretty
        if len(pretty) > 4000:
            pretty = pretty[:4000] + "\n…（预览截断，未显示 %d 字符；直接「允许」按完整原参数执行）" % (len(pretty) - 4000)
        box.insert("1.0", pretty)

        err = tk.Label(win, text="", bg=p["bg"], fg=p["err"], font=(FONT, 8))
        err.pack(anchor="w", padx=16)
        self.approved_args = None

        def edited():
            raw = box.get("1.0", "end-1c")
            if raw.strip() == orig.strip():
                return "same"
            if "…（预览截断" in raw:
                return "truncated"
            if not raw.strip():
                return {}
            try:
                v = json.loads(raw)
                return v if isinstance(v, dict) else {"value": v}
            except Exception as e:
                err.configure(text="JSON 解析失败：" + str(e))
                return None

        def answer(v, always=False):
            if v:
                got = edited()
                if got == "truncated":
                    err.configure(text="参数过长导致预览截断：请先缩短参数再允许，或撤销改动后按原参数允许。")
                    return
                if got is None:
                    return
                self.approved_args = args if got == "same" else got
            if always:
                self.allow_session.add(name)
            self.approve_result = v
            if self.approve_ev:
                self.approve_ev.set()
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()

        bar = tk.Frame(win, bg=p["bg"])
        bar.pack(fill="x", padx=16, pady=(4, 14))
        ttk.Button(bar, text="允许执行", style="Acc.TButton", command=lambda: answer(True)).pack(side="left")
        ttk.Button(bar, text="拒绝", style="Danger.TButton", command=lambda: answer(False)).pack(side="left", padx=8)
        ttk.Button(bar, text="本会话始终允许：" + name, command=lambda: answer(True, True)).pack(side="left")
        tk.Label(bar, text="Enter 允许 · Esc 拒绝", bg=p["bg"], fg=p["ink3"],
                 font=(FONT, 8)).pack(side="right")
        if args.get("path"):
            ttk.Button(bar, text="打开所在目录", command=lambda: self.open_path(
                os.path.dirname(self.abs_path(args.get("path"))))).pack(side="right", padx=8)

        def on_ret(e=None):
            if isinstance(win.focus_get(), tk.Text):
                return None                                # 编辑框内 Enter = 换行
            answer(True)
            return "break"

        def on_esc(e=None):
            answer(False)
            return "break"

        win.bind("<Return>", on_ret)
        win.bind("<Control-Return>", lambda e: (answer(True, True), "break")[1])
        win.bind("<Escape>", on_esc)
        win.protocol("WM_DELETE_WINDOW", lambda: answer(False))
        win.focus_set()
        return win

    # ---------------------------------------------------------------- 问答（QA）
    def ask_request(self, payload, ctx=None):
        """引擎的问答通道（工作线程调用）：派发卡片 → 等主线程回填答案。

        返回 {"answers":{key:[label]}, "custom":{key:str}, "notes":{key:str}}；
        None = 用户跳过（提示模型按最合理默认继续）。
        """
        ev = threading.Event()
        self.qa_ev = ev
        self.qa_result = None
        self.q.put(("ask", payload))
        ev.wait()
        self.qa_ev = None
        return self.qa_result

    def ask_question(self, payload):
        """问答卡片：每题一组选项（单选 / 多选）+ 自定义输入；Enter 提交 · Esc 跳过。"""
        p = self.palette()
        acc = self.ui.get("accent") or "#5b8cff"
        qs = [dict(q) for q in ((payload or {}).get("questions") or []) if isinstance(q, dict)]
        if not qs:
            return None
        self._qa_payload = payload
        self._qa_state = []
        win = tk.Toplevel(self.root)
        win.title("需要你的选择 · 需求澄清 · %s" % APP)
        win.configure(bg=p["bg"])
        try:
            win.transient(self.root)
        except Exception:
            pass
        try:
            self.root.update_idletasks()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            w, h = 800, min(700, 260 + 150 * len(qs))
            win.geometry("%dx%d+%d+%d" % (w, h, rx + max(0, (rw - w) // 2), ry + max(0, (rh - h) // 4)))
        except Exception:
            win.geometry("800x600")
        try:
            win.grab_set()
        except Exception:
            pass
        tk.Frame(win, bg=acc, height=4).pack(fill="x")
        head = tk.Frame(win, bg=p["bg"])
        head.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(head, text="先确认需求，再动手", bg=p["bg"], fg=p["ink"],
                 font=(FONT, 12, "bold")).pack(side="left")
        tk.Label(head, text=" %d 个问题 " % len(qs), bg=acc, fg="#ffffff",
                 font=(FONT, 9, "bold")).pack(side="left", padx=10)
        tk.Label(head, text="ask_user", bg=p["bg"], fg=p["ink3"], font=(MONO, 8)).pack(side="right")
        tk.Label(win, text=str((payload or {}).get("intro") or "模型需要你在这几点上拍板，再继续执行："),
                 bg=p["bg"], fg=p["ink2"], font=(FONT, 9), wraplength=740,
                 justify="left").pack(anchor="w", padx=16, pady=(2, 0))
        note = str((payload or {}).get("note") or "").strip()
        if note:
            tk.Label(win, text=note, bg=p["bg"], fg=p["ink3"], font=(FONT, 8), wraplength=740,
                     justify="left").pack(anchor="w", padx=16, pady=(2, 0))
        body = tk.Frame(win, bg=p["bg"])
        body.pack(fill="both", expand=True, padx=16, pady=(8, 0))
        inner = body
        if len(qs) > 2:                                    # 问题多时给滚动区，卡片不被挤出屏幕
            cv = tk.Canvas(body, bg=p["bg"], highlightthickness=0)
            sb = ttk.Scrollbar(body, orient="vertical", command=cv.yview)
            cv.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            cv.pack(side="left", fill="both", expand=True)
            inner = tk.Frame(cv, bg=p["bg"])
            cv.create_window((4, 4), window=inner, anchor="nw", width=730)
            inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        for i, q in enumerate(qs, 1):
            card = tk.Frame(inner, bg=p["panel"])
            card.pack(fill="x", pady=(0, 8))
            top = tk.Frame(card, bg=p["panel"])
            top.pack(fill="x", padx=10, pady=(8, 2))
            tk.Label(top, text="%d." % i, bg=p["panel"], fg=acc,
                     font=(FONT, 10, "bold")).pack(side="left")
            tk.Label(top, text=str(q.get("q") or ""), bg=p["panel"], fg=p["ink"],
                     font=(FONT, 10, "bold"), wraplength=600, justify="left").pack(side="left", padx=6)
            if q.get("multi"):
                tk.Label(top, text="可多选", bg=p["panel"], fg=p["ink3"], font=(FONT, 8)).pack(side="left", padx=6)
            opts = [o for o in (q.get("options") or []) if isinstance(o, dict)]
            st = {"key": str(q.get("key") or ("q%d" % i)),
                  "single": tk.StringVar(value=""), "custom": tk.StringVar(value=""), "vars": {}, "opts": opts}
            if q.get("multi"):
                for j, o in enumerate(opts):
                    v = tk.BooleanVar(value=False)
                    st["vars"][str(o.get("label"))] = v
                    row = tk.Frame(card, bg=p["panel"])
                    row.pack(fill="x", padx=(28, 10), pady=1)
                    ttk.Checkbutton(row, text=str(o.get("label") or ""), variable=v,
                                    style="Card.TCheckbutton").pack(side="left")
                    if o.get("desc"):
                        tk.Label(row, text=str(o["desc"]), bg=p["panel"], fg=p["ink3"],
                                 font=(FONT, 8), wraplength=420, justify="left").pack(side="left", padx=8)
            else:
                st["single"].set(str((opts[0] or {}).get("label") or "") if opts else "")   # 默认选推荐项
                for o in opts:
                    row = tk.Frame(card, bg=p["panel"])
                    row.pack(fill="x", padx=(28, 10), pady=1)
                    ttk.Radiobutton(row, text=str(o.get("label") or ""), value=str(o.get("label") or ""),
                                    variable=st["single"], style="Card.TRadiobutton").pack(side="left")
                    if o.get("desc"):
                        tk.Label(row, text=str(o["desc"]), bg=p["panel"], fg=p["ink3"],
                                 font=(FONT, 8), wraplength=420, justify="left").pack(side="left", padx=8)
            if q.get("allow_custom", True):
                row = tk.Frame(card, bg=p["panel"])
                row.pack(fill="x", padx=(28, 10), pady=(2, 8))
                tk.Label(row, text="其他（自己填）", bg=p["panel"], fg=p["ink3"],
                         font=(FONT, 8)).pack(side="left")
                tk.Entry(row, textvariable=st["custom"], bg=p["sunken"], fg=p["ink"], relief="flat",
                         insertbackground=p["ink"], font=(MONO, 9)).pack(side="left", fill="x",
                                                                        expand=True, padx=8, ipady=2)
            else:
                tk.Label(card, text="", bg=p["panel"], height=1).pack()
            self._qa_state.append(st)
        err = tk.Label(win, text="", bg=p["bg"], fg=p["err"], font=(FONT, 8))
        err.pack(anchor="w", padx=16)

        def collect():
            answers, custom, notes = {}, {}, {}
            for st in self._qa_state:
                if st["vars"]:
                    picks = [lab for lab, v in st["vars"].items() if bool(v.get())]
                else:
                    one = st["single"].get().strip()
                    picks = [one] if one else []
                cu = st["custom"].get().strip()
                if picks:
                    answers[st["key"]] = picks
                if cu:
                    custom[st["key"]] = cu
            return {"answers": answers, "custom": custom, "notes": notes}

        def answer(skip=False, cancel_turn=False):
            self.qa_result = None if skip else collect()
            if cancel_turn:
                self.qa_result = None
                try:
                    if self.cancel:
                        self.cancel.set()
                except Exception:
                    pass
            if self.qa_ev:
                self.qa_ev.set()
            try:
                win.grab_release()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        bar = tk.Frame(win, bg=p["bg"])
        bar.pack(fill="x", padx=16, pady=(4, 14))
        ttk.Button(bar, text="提交答案", style="Acc.TButton",
                   command=lambda: answer(False)).pack(side="left")
        ttk.Button(bar, text="跳过，按你的判断做",
                   command=lambda: answer(True)).pack(side="left", padx=8)
        ttk.Button(bar, text="取消本轮", style="Danger.TButton",
                   command=lambda: answer(True, True)).pack(side="left")
        tk.Label(bar, text="Enter 提交 · Esc 跳过（模型会列出假设自己做）", bg=p["bg"], fg=p["ink3"],
                 font=(FONT, 8)).pack(side="right")
        self._qa_answer = answer                 # 自检可直接调用（等价于点按钮）
        win.bind("<Return>", lambda e: (answer(False), "break")[1])
        win.bind("<Escape>", lambda e: (answer(True), "break")[1])
        win.protocol("WM_DELETE_WINDOW", lambda: answer(True))
        try:
            win.focus_set()
        except Exception:
            pass
        return win

    def toggle_qa(self):
        """「先问后做」开关（设置面板与「扩展」菜单共用）。"""
        want = bool(self.var_qa.get())
        try:
            import picore as _C
            st = _C.qa_set(self.cfg, on=want, max_q=int(self.var_qamax.get() or 4))
        except Exception:
            pr = self.cfg.setdefault("prefs", {})
            pr["qa_first"] = want
            E.save_config(self.cfg)
            st = {"ok": True, "enabled": want, "max": int(self.var_qamax.get() or 4)}
        self.append_system("先问后做：%s（需求有歧义时模型会先用 ask_user 出选项问你，最多 %s 题）"
                           % ("开" if st.get("enabled") else "关", st.get("max")))
        self.toast("先问后做 %s" % ("已打开" if st.get("enabled") else "已关闭"))
        return st

    def qa_demo_card(self):
        """试一次问答卡（不经过模型，纯界面：验证选项/多选/自定义/跳过都能用）。"""
        try:
            import picore as _C
            payload = _C.qa_demo()
        except Exception:
            payload = {"intro": "演示问答卡", "questions": [
                {"key": "q1", "q": "选一个？", "options": [{"label": "A", "desc": "推荐"}, {"label": "B"}]}]}
        self.show_panel("chat")
        self.append_system("❓ 演示问答卡：选择后不会发给模型，只验证界面。")
        win = self.ask_question(payload)
        return win

    def abs_path(self, path):
        path = str(path or "")
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(self.cfg.get("workspace") or "", path))

    def approve_warning(self, d):
        name = d.get("name")
        args = d["args"] or {}
        lines = []
        if name == "shell":
            lines.append("⚠ 将在本机执行命令：" + str(args.get("command", ""))[:200])
        if name == "py_run":
            lines.append("⚠ 将用当前解释器执行代码（%d 字符）" % len(str(args.get("code") or "")))
        for key in ("path", "cwd"):
            if args.get(key):
                ap = self.abs_path(args[key])
                ws = self.cfg.get("workspace") or ""
                if ws and not E._inside(ap, ws):
                    lines.append("⚠ 目标在工作区之外：" + ap)
        if bool((self.cfg.get("prefs") or {}).get("sandbox", True)):
            lines.append("沙箱已开启：文件与命令路径被限制在工作区内。")
        return "\n".join(lines)

    def finish_turn(self, res):
        self.busy = False
        self._turn_chips(False)
        self.btn_send.configure(text="发送", style="Acc.TButton")
        self.lbl_hint.configure(text="Enter 发送 · Shift+Enter 换行 · /help 命令")
        self.stream_buf = ""
        self._stream_mark = None
        self._s_dirty = False
        self._raw_shown = 0
        self._stream_mode = "md"
        self._reason_buf = ""
        self._reason_shown = 0
        if self._flush_job is not None:
            try:
                self.root.after_cancel(self._flush_job)
            except Exception:
                pass
            self._flush_job = None
        self.rt["turn"] = "空闲"
        self.rt["elapsed"] = res.get("elapsed_all", res.get("elapsed", 0))
        if res.get("usage"):
            self.rt["tokens"] = res["usage"]
        if res.get("tools"):
            self.rt["tools"] = res["tools"]
        self.finalize_turn()                    # 性能：只重绘「本轮区间」，历史消息不重排
        self.refresh_runtime()
        self.refresh_console()
        self.refresh_changes()
        self.refresh_ctx()
        self.save_all()
        self.refresh_sessions()
        if res.get("cost") is not None:
            self.lbl_usage.configure(text="%s · tokens %s · 约 %s 元" % (
                (self.cfg.get("provider") or {}).get("model") or "未选模型",
                (res.get("usage") or {}).get("total_tokens", 0), res["cost"]))
        if res.get("error"):
            self.lbl_status.configure(text="出错：" + str(res["error"])[:160])
            E.log("error", "turn", str(res["error"]))
        elif res.get("stopped"):
            self.lbl_status.configure(text="已停止（进程树已终止）")
        elif res.get("limit"):
            self.lbl_status.configure(text="达到步数上限")
        else:
            self.lbl_status.configure(text="完成 · %s 步 · %.1fs · %d 次工具调用%s" % (
                res.get("steps", 0), res.get("elapsed_all", 0), len(res.get("tools") or []),
                (" · %d 个文件变更" % len(res.get("checkpoints") or [])) if res.get("checkpoints") else ""))

    def handle(self, kind, data):
        if kind == "assistant_begin":
            self._flush_stream()                  # 先把推理流未画出的尾巴补上
            self.stream_buf = ""
            self.stream_open = True
            self._stream_mark = None
            self._s_dirty = False
            self._raw_shown = 0
            self._stream_mode = "md"
            self._reason_buf = ""
            self._reason_shown = 0
            t = self.chat_text
            t.configure(state="normal")
            t.insert("end", "\nPI · %s\n" % ts(time.time()), "ai_h")
            t.configure(state="disabled")
            self._see_end(t)
        elif kind == "assistant_delta":
            self.stream_buf += data
            self._s_dirty = True
            self._schedule_flush()                # 合帧渲染：40ms 一帧，不再逐 token 重排
        elif kind == "assistant_message":
            self._flush_stream(final=True)        # 收尾一次（含 Markdown 整理）
        elif kind == "reasoning_delta":
            self._reason_buf += data
            self._schedule_flush()                # 推理流只追加，永不重排
        elif kind == "step":
            self.rt["step"] = data.get("i", 0)
            self.rt["max"] = data.get("max", 0)
            self.rt["turn"] = "生成中"
            try:
                self.pb_turn.configure(maximum=max(1, int(data.get("max") or 1)),
                                       value=int(data.get("i") or 0))
            except Exception:
                pass
            self.refresh_runtime()
            self._update_turn_chip()
            self.lbl_status.configure(text="第 %s/%s 步…" % (data.get("i"), data.get("max")))
        elif kind == "tool_start":
            self.rt["turn"] = "调用工具"
            self.refresh_runtime()
            t = self.chat_text
            t.configure(state="normal")
            if data.get("approved"):
                t.insert("end", "  ✔ 已批准（按当前参数执行）\n", "dim")
            else:
                t.insert("end", "\n⚙ %s  参数 %s\n" % (
                    data["name"], json.dumps(data.get("args") or {}, ensure_ascii=False)[:300]), "tool_h")
            t.configure(state="disabled")
            self._see_end(t)
            self.lbl_status.configure(text="执行 %s …" % data["name"])
            self._update_turn_chip()
        elif kind == "tool_end":
            t = self.chat_text
            t.configure(state="normal")
            t.insert("end", "  %s %sms\n" % ("✔" if data.get("ok") else "✘", data.get("ms", 0)), "tool_h")
            t.insert("end", (str(data.get("text", ""))[:6000]) + "\n", "tool")
            t.configure(state="disabled")
            self._see_end(t)
            self.rt["tools"].append(data)
            self.refresh_runtime()
            self._update_turn_chip()
            try:                                  # 观测落点：账本 + 工具失败自动入错误库（与网页后端同一入口）
                import picore as _C
                _C.observe_event("tool_end", data, self.cur)
            except Exception:
                pass
        elif kind == "usage":
            self.rt["tokens"] = data
            self.refresh_runtime()
        elif kind == "ctx":
            self.rt["ctx"] = data
            self.refresh_ctx()
        elif kind == "checkpoint":
            self.refresh_changes()
            self.append_system("已留检查点：%s（%d → %d 字节，可在「变更」面板回滚）" % (
                data.get("path"), data.get("bytes_before"), data.get("bytes_after")))
        elif kind == "title":
            self.refresh_sessions()
            self.toast("会话标题：" + str(data))
        elif kind == "compact_delta":
            self.lbl_status.configure(text="正在压缩上下文… %d 字" % len(data))
        elif kind == "compacted":
            self.refresh_ctx()
            self.refresh_sessions()
            self.save_all()
            if data.get("ok"):
                self.render_chat()
                self.append_system("✔ " + data.get("text", "已压缩"))
            else:
                self.append_system("✘ " + data.get("text", "压缩未执行"))
        elif kind == "notice":
            self.append_system(data)
        elif kind == "error":
            self.append_system("[错误] " + str(data))
            E.log("error", "engine", str(data))
        elif kind == "approve":
            self.append_system("⏳ 等待审批：%s（已在弹窗中等待决定：Enter 允许 / Esc 拒绝）" % (data.get("name") or "?"))
            self.ask_approve(data)
        elif kind == "ask":
            n = len(((data or {}).get("questions") or []))
            self.append_system("❓ 需求澄清：模型问了 %d 个问题（每题的选项已列在卡片里，Enter 提交 / Esc 跳过）" % n)
            self._update_turn_chip()
            self.lbl_status.configure(text="等待你的选择（%d 个问题）…" % n)
            self.ask_question(data)
        elif kind == "done":
            try:                                  # 观测落点：每轮 turn 写入账本（与网页后端同一入口）
                import picore as _C
                _C.observe_event("done", data, self.cur, title=(self.session() or {}).get("title"),
                                 model=(self.cfg.get("provider") or {}).get("model"))
            except Exception:
                pass
            self.finish_turn(data)
        elif kind == "probe":
            self.on_probe(data)
        elif kind == "boot":
            if data.get("imported"):
                hp = data["imported"]
                E.log("info", "boot", "自动导入 Provider %s (%s)" % (hp["name"], hp["base_url"]))
            prov = data.get("provider") or {}
            self.var_base.set(prov.get("base_url") or "")
            if (prov.get("api_key") or ""):
                self.var_key.set(prov["api_key"])
            if prov.get("model"):
                self.var_mdl.set(prov["model"])
                self.var_model.set(prov["model"])
            if prov.get("models"):
                self.cmb_model.configure(values=prov["models"])
                self.set_model_list(prov["models"])
            self.set_probe_text("启动自动配置：%s · %s · 密钥 %s" % (
                prov.get("name") or "?", E.norm_base(prov.get("base_url")), E.mask_key(prov.get("api_key"))))
            self.on_probe(data.get("probe") or {})
            self.refresh_console()
        elif kind == "web":
            self.append_system("网页版已启动：\n%s\n（浏览器会自动打开；把这个完整网址复制到任意浏览器也能用）" % data)
            self.toast("网页版已启动：" + data)
            try:
                webbrowser.open(data)
            except Exception as e:
                E.log("warn", "serve", str(e))
        elif kind == "web_err":
            self.append_system("[错误] 网页版启动失败：" + str(data))
            self.toast("网页版启动失败")
        elif kind == "toolrun":
            res = data["res"]
            self.set_tool_out("[%s] 参数：%s\n\n%s" % (data["name"], json.dumps(data["args"], ensure_ascii=False),
                                                      res.get("text", "")))
            self.rt["tools"].append({"name": data["name"], "ok": res.get("ok"), "ms": res.get("ms", 0),
                                     "args": data["args"], "text": res.get("text", "")})
            self.refresh_runtime()
            self.refresh_console()
            self.refresh_changes()
            self.lbl_status.configure(text="%s 执行完成 · %sms" % (data["name"], res.get("ms", 0)))
        elif kind == "pluginrun":
            w = self.alive("txt_plugin_out")
            if w is not None:
                w.delete("1.0", "end")
                w.insert("1.0", "[%s] %s\n%s" % (data.get("tool"), "成功" if data.get("ok") else "失败",
                                                 data.get("text") or ""))
            self.toast("插件工具 %s 执行完成" % data.get("tool"))
        elif kind == "mcpout":
            w = self.alive("txt_mcp_out")
            if w is not None:
                w.delete("1.0", "end")
                w.insert("1.0", str(data))
        elif kind == "ext":
            pid = (data or {}).get("panel")
            if pid == "mcp":
                self.refresh_mcp()
            elif pid == "plugins":
                self.refresh_plugins()
            elif pid == "skills":
                self.refresh_skills()
            try:
                self.refresh_tools()            # MCP / 插件重载后动态工具会变，工具面板同步
            except Exception:
                pass
            if (data or {}).get("text"):
                self.append_system(data["text"])
        elif kind == "local":
            hits = data or []
            allm = [m for h in hits for m in h["models"]]
            self.set_model_list(allm)
            if hits:
                self.set_probe_text("发现本机端点：%s" % " · ".join(
                    "%s(%s, %d 个模型)" % (h["name"], h["base_url"], len(h["models"])) for h in hits),
                    self.palette()["ok"])
                self.var_base.set(hits[0]["base_url"])
                self.var_key.set("")
                if hits[0]["models"]:
                    self.var_mdl.set(hits[0]["models"][0])
            else:
                self.set_probe_text("本机常用端口（11434/1234/8000/5000/8080）没有发现模型服务",
                                    self.palette()["warn"])

    def on_probe(self, res):
        self.probe_res = res
        p = self.palette()
        if res.get("ok"):
            self.lbl_conn.configure(text="● 已连接 %sms" % res["ms"], fg=p["ok"])
            self.set_model_list(res["models"])
            self.set_probe_text("连接正常 · %dms · 返回 %d 个模型" % (res["ms"], len(res["models"])), p["ok"])
            self.cmb_model.configure(values=res["models"])
            if res["models"] and not (self.var_model.get() or "").strip():
                self.var_model.set(res["models"][0])
                self.var_mdl.set(res["models"][0])
            self.lbl_status.configure(text="模型端点连接正常 · %d 个模型" % len(res["models"]))
        else:
            self.lbl_conn.configure(text="● 未连接", fg=p["err"])
            self.set_probe_text("连接失败：" + res.get("error", "")[:400], p["err"])
            self.lbl_status.configure(text="模型端点不可用 —— 打开「模型」面板配置")

    def act_start_web(self):
        if getattr(self, "web_url", None):
            try:
                webbrowser.open(self.web_url)
            except Exception:
                pass
            self.append_system("网页版已在运行：%s" % self.web_url)
            return
        self.append_system("正在启动网页版后端…")

        def work():
            try:
                import piserver
                piserver.HUB = piserver.Hub()
                port = piserver.free_port()
                url = "http://127.0.0.1:%d/?t=%s" % (port, piserver.HUB.token)
                httpd = piserver.ThreadingHTTPServer(("127.0.0.1", port), piserver.Handler)
                httpd.daemon_threads = True
                self.web_url = url
                self.q.put(("web", url))
                E.log("info", "serve", "GUI 内启动网页版：" + url)
                httpd.serve_forever()
            except Exception as e:
                self.q.put(("web_err", "%s: %s" % (type(e).__name__, e)))
        threading.Thread(target=work, daemon=True).start()

    def act_selftest(self):
        self.lbl_status.configure(text="正在运行自检…")
        self.append_system("正在运行真实自检（引擎 / 核心能力层 / 界面渲染）…")

        def work():
            r = E.selftest(self.cfg)
            try:                                # 与命令行 --selftest、后端 /api/selftest 同一口径
                import picore as _CORE
                _CORE.register_tools()
                c = _CORE.selftest()
                r["rows"] = r["rows"] + c["items"]
                r["passed"] += c["passed"]
                r["total"] += c["total"]
            except Exception as e:
                r["rows"].append({"name": "core 自检", "ok": False,
                                  "detail": "%s: %s" % (type(e).__name__, e)})
                r["total"] += 1
            try:
                u = ui_selftest(skip_tk=True)   # 本路径在后台线程：跳过需要真实 Tk 的两项
                r["rows"] = r["rows"] + u["rows"]
                r["passed"] += u["passed"]
                r["total"] += u["total"]
            except Exception as e:
                r["rows"].append({"name": "app 界面自检", "ok": False,
                                  "detail": "%s: %s" % (type(e).__name__, e)})
                r["total"] += 1
            self.q.put(("selftest", r))
        threading.Thread(target=work, daemon=True).start()

    def act_toggle_auto(self):
        try:
            import pipolicy
            on = not pipolicy.auto(self.cfg)
            if on and not messagebox.askyesno(APP, "开启后所有写操作（含命令执行）不再弹审批框，确定？"):
                return
            pipolicy.set_auto(on)
            self.cfg.setdefault("prefs", {})["policy_auto"] = on
            E.save_config(self.cfg)
            self.toast("自动确认已%s" % ("开启" if on else "关闭"))
        except Exception as e:
            messagebox.showerror(APP, str(e))

    # ---------- 治理中心（契约 / 计划 / 审批 / 接口） ----------
    def act_governance(self):
        p = self.palette()
        win = tk.Toplevel(self.root)
        win.title(APP + " · 治理中心")
        win.geometry("1000x660")
        win.configure(bg=p["bg"])
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        # ===== 契约 =====
        t1 = tk.Frame(nb, bg=p["bg"]); nb.add(t1, text=" 契约 ")
        top = tk.Frame(t1, bg=p["bg"]); top.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(top, text="目标：", bg=p["bg"], fg=p["ink2"], font=(FONT, 9)).pack(side="left")
        e_goal = tk.Entry(top, font=(FONT, 9)); e_goal.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Label(t1, text="验收项（每行一个）", bg=p["bg"], fg=p["ink2"], font=(FONT, 8)).pack(anchor="w", padx=8)
        e_evals = tk.Text(t1, height=3, font=(FONT, 9), bg=p["sunken"], fg=p["ink"],
                          insertbackground=p["ink"], relief="flat")
        e_evals.pack(fill="x", padx=8)
        bar1 = tk.Frame(t1, bg=p["bg"]); bar1.pack(fill="x", padx=8, pady=5)
        ct_tv = ttk.Treeview(t1, columns=("id", "state", "goal", "ev"), show="headings", height=9)
        for c, w, tt in (("id", 60, "id"), ("state", 88, "状态"), ("goal", 560, "目标"), ("ev", 90, "验收")):
            ct_tv.heading(c, text=tt); ct_tv.column(c, width=w, anchor="w")
        ct_tv.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        def ct_draw():
            try:
                import picontract
                ct_tv.delete(*ct_tv.get_children())
                for c in picontract.list_()["items"]:
                    ct_tv.insert("", "end", values=(c["id"], c["state"], c["goal"][:90],
                                                    "%d/%d" % (c["pass"], c["total"])))
            except Exception as e:
                self.toast("契约读取失败：" + str(e))

        def ct_do(act):
            try:
                import picontract
                if act == "create":
                    g = e_goal.get().strip()
                    if not g:
                        messagebox.showinfo(APP, "先填目标"); return
                    evs = [x.strip() for x in e_evals.get("1.0", "end").split("\n") if x.strip()]
                    r = picontract.create(g, evs)
                else:
                    sel = ct_tv.selection()
                    if not sel:
                        return
                    cid = ct_tv.item(sel[0], "values")[0]
                    if act == "sign": r = picontract.sign(cid)
                    elif act == "start": r = picontract.start(cid)
                    elif act == "check": r = picontract.check(cid, None)
                    elif act == "settle": r = picontract.settle(cid, ["原生结算"], ["用户确认"])
                    elif act == "report":
                        r = picontract.report(cid)
                        if r.get("ok"):
                            tw = tk.Toplevel(win); tw.title("契约报告 " + cid); tw.geometry("760x560")
                            tx = tk.Text(tw, wrap="word", font=(FONT, 9), bg=p["sunken"], fg=p["ink"])
                            tx.pack(fill="both", expand=True)
                            tx.insert("1.0", r.get("text") or ""); tx.configure(state="disabled")
                    else: r = {"ok": False, "text": "未知动作"}
                self.toast(str(r.get("text") or r.get("error") or act))
                ct_draw()
            except Exception as e:
                messagebox.showerror(APP, str(e))
        for txt, act in (("新建契约", "create"), ("签署", "sign"), ("开始", "start"),
                         ("检查(全部通过)", "check"), ("结算", "settle"), ("报告", "report"), ("刷新", "")):
            tk.Button(bar1, text=txt, font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                      command=(ct_draw if act == "" else (lambda a=act: ct_do(a)))).pack(side="left", padx=4)
        ct_draw()

        # ===== 计划 =====
        t2 = tk.Frame(nb, bg=p["bg"]); nb.add(t2, text=" 计划 ")
        bar2 = tk.Frame(t2, bg=p["bg"]); bar2.pack(fill="x", padx=8, pady=(8, 4))
        pl_tv = ttk.Treeview(t2, columns=("id", "st", "t"), show="headings", height=12)
        for c, w, tt in (("id", 70, "id"), ("st", 90, "状态"), ("t", 700, "步骤")):
            pl_tv.heading(c, text=tt); pl_tv.column(c, width=w, anchor="w")
        pl_tv.pack(fill="both", expand=True, padx=8)
        bar2b = tk.Frame(t2, bg=p["bg"]); bar2b.pack(fill="x", padx=8, pady=6)
        v_auto = tk.BooleanVar(value=bool((self.cfg.get("prefs") or {}).get("auto_continue")))
        e_add = tk.Entry(bar2b, font=(FONT, 9)); e_add.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def pl_ctx():
            return {"session": self.session()}

        def pl_draw():
            try:
                import piplan
                pl_tv.delete(*pl_tv.get_children())
                st = piplan.state(pl_ctx())
                pl_tv.insert("", "end", values=("", "目标", st.get("goal") or "—"))
                for x in st["items"]:
                    pl_tv.insert("", "end", values=(x["id"], x["status"], x["t"]))
            except Exception as e:
                self.toast("计划读取失败：" + str(e))

        def pl_mark(status):
            sel = pl_tv.selection()
            if not sel:
                return
            sid = pl_tv.item(sel[0], "values")[0]
            if not sid:
                return
            import piplan
            piplan.mark(pl_ctx(), sid, status)
            self.save_all(); pl_draw()
        tk.Button(bar2b, text="添加", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=lambda: (__import__("piplan").add(pl_ctx(), e_add.get().strip()), e_add.delete(0, "end"), pl_draw())
                  if e_add.get().strip() else None).pack(side="left", padx=4)
        tk.Button(bar2b, text="完成", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=lambda: pl_mark("done")).pack(side="left", padx=4)
        tk.Button(bar2b, text="未完成", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=lambda: pl_mark("pending")).pack(side="left", padx=4)
        tk.Button(bar2b, text="清空", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=lambda: (__import__("piplan").clear(pl_ctx()), self.save_all(), pl_draw())).pack(side="left", padx=4)

        def pl_orch():
            self.toast("编排中（子代理逐条执行，可能较久）…")
            def w2():
                import piplan
                r = piplan.orchestrate(pl_ctx(), None, readonly=True, limit=3)
                self.q.put(("notice", r.get("text") or r.get("error") or "编排结束"))
                self.save_all()
            threading.Thread(target=w2, daemon=True).start()
        tk.Button(bar2b, text="编排未完成步骤", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=pl_orch).pack(side="left", padx=4)

        def pl_auto():
            on = bool(v_auto.get())
            self.cfg.setdefault("prefs", {})["auto_continue"] = on
            E.save_config(self.cfg)
            self.toast("强制一次性完成（自动续跑）已%s" % ("开启" if on else "关闭"))
        tk.Checkbutton(bar2b, text="强制一次性完成（自动续跑）", variable=v_auto, command=pl_auto,
                       bg=p["bg"], fg=p["ink2"], selectcolor=p["panel"], activebackground=p["bg"],
                       font=(FONT, 9)).pack(side="right")
        pl_draw()

        # ===== 审批 =====
        t3 = tk.Frame(nb, bg=p["bg"]); nb.add(t3, text=" 审批 ")
        bar3 = tk.Frame(t3, bg=p["bg"]); bar3.pack(fill="x", padx=8, pady=(8, 4))
        v_apauto = tk.BooleanVar()
        try:
            import pipolicy as _pl
            v_apauto.set(_pl.auto(self.cfg))
        except Exception:
            pass

        def ap_auto():
            import pipolicy
            on = bool(v_apauto.get())
            if on and not messagebox.askyesno(APP, "开启后所有写操作不再弹审批框，确定？"):
                v_apauto.set(False); return
            pipolicy.set_auto(on)
            self.cfg.setdefault("prefs", {})["policy_auto"] = on
            E.save_config(self.cfg)
            self.toast("自动确认已%s" % ("开启" if on else "关闭"))
        tk.Checkbutton(bar3, text="自动确认（写操作免审批）", variable=v_apauto, command=ap_auto,
                       bg=p["bg"], fg=p["ink2"], selectcolor=p["panel"], activebackground=p["bg"],
                       font=(FONT, 9)).pack(side="left")
        e_tool = tk.Entry(bar3, width=16, font=(FONT, 9)); e_tool.insert(0, "*"); e_tool.pack(side="left", padx=6)
        e_pat = tk.Entry(bar3, width=30, font=(FONT, 9)); e_pat.pack(side="left", padx=4)
        ap_act = ttk.Combobox(bar3, values=("allow", "deny", "ask"), width=7, state="readonly"); ap_act.set("allow")
        ap_act.pack(side="left", padx=4)
        ap_tv = ttk.Treeview(t3, columns=("id", "tool", "pat", "act"), show="headings", height=8)
        for c, w, tt in (("id", 90, "id"), ("tool", 140, "工具"), ("pat", 470, "正则"), ("act", 90, "动作")):
            ap_tv.heading(c, text=tt); ap_tv.column(c, width=w, anchor="w")
        ap_tv.pack(fill="both", expand=True, padx=8)
        ap_hist = tk.Text(t3, height=8, font=(FONT, 8), bg=p["sunken"], fg=p["ink2"], relief="flat")
        ap_hist.pack(fill="both", expand=False, padx=8, pady=6)

        def ap_draw():
            try:
                import pipolicy as pl
                ap_tv.delete(*ap_tv.get_children())
                st = pl.state(self.cfg)
                for r in st["rules"]:
                    ap_tv.insert("", "end", values=(r.get("id"), r.get("tool"), r.get("pattern"), r.get("action")))
                ap_hist.configure(state="normal"); ap_hist.delete("1.0", "end")
                for h in st["history"][:25]:
                    ap_hist.insert("end", "%s  %-5s  %s · %s\n" % (
                        ts(h.get("ts")), h.get("decision"), h.get("tool"), str(h.get("subject"))[:60]))
                ap_hist.configure(state="disabled")
            except Exception as e:
                self.toast("审批读取失败：" + str(e))

        def ap_add():
            import pipolicy
            r = pipolicy.add_rule(e_tool.get().strip() or "*", e_pat.get().strip(), ap_act.get())
            self.toast(r.get("text") or r.get("error") or "")
            ap_draw()
        tk.Button(bar3, text="添加规则", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=ap_add).pack(side="left", padx=4)
        tk.Button(bar3, text="删除所选", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=lambda: (__import__("pipolicy").del_rule(ap_tv.item(ap_tv.selection()[0], "values")[0]),
                                   ap_draw()) if ap_tv.selection() else None).pack(side="left", padx=4)
        tk.Button(bar3, text="刷新", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=ap_draw).pack(side="left", padx=4)
        ap_draw()

        # ===== 接口 =====
        t4 = tk.Frame(nb, bg=p["bg"]); nb.add(t4, text=" 接口 ")
        bar4 = tk.Frame(t4, bg=p["bg"]); bar4.pack(fill="x", padx=8, pady=(8, 4))
        e_id = tk.Entry(bar4, width=16, font=(FONT, 9)); e_id.pack(side="left", padx=(0, 4))
        e_url = tk.Entry(bar4, font=(FONT, 9)); e_url.pack(side="left", fill="x", expand=True, padx=4)
        m_sel = ttk.Combobox(bar4, values=("GET", "POST", "PUT", "DELETE"), width=7, state="readonly"); m_sel.set("GET")
        m_sel.pack(side="left", padx=4)
        api_tv = ttk.Treeview(t4, columns=("id", "m", "url"), show="headings", height=12)
        for c, w, tt in (("id", 140, "id（工具 api_<id>）"), ("m", 70, "方法"), ("url", 660, "URL")):
            api_tv.heading(c, text=tt); api_tv.column(c, width=w, anchor="w")
        api_tv.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        def api_draw():
            try:
                import piapi
                api_tv.delete(*api_tv.get_children())
                for it in piapi.list_apis()["items"]:
                    api_tv.insert("", "end", values=(it["id"], it["method"], it["url"]))
            except Exception as e:
                self.toast("接口读取失败：" + str(e))

        def api_add():
            import piapi
            r = piapi.upsert({"id": e_id.get().strip(), "url": e_url.get().strip(), "method": m_sel.get()})
            self.toast(r.get("text") or r.get("error") or "")
            api_draw()
        tk.Button(bar4, text="保存", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=api_add).pack(side="left", padx=4)

        def api_test():
            if not api_tv.selection():
                return
            import piapi
            r = piapi.call_api(api_tv.item(api_tv.selection()[0], "values")[0], {})
            self.q.put(("notice", "接口测试 %s · HTTP %s · %s" % ("成功" if r.get("ok") else "失败",
                                                                  r.get("code"), str(r.get("text"))[:200])))
        tk.Button(bar4, text="测试调用", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=api_test).pack(side="left", padx=4)
        tk.Button(bar4, text="删除所选", font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                  command=lambda: (__import__("piapi").delete_api(api_tv.item(api_tv.selection()[0], "values")[0]),
                                   api_draw()) if api_tv.selection() else None).pack(side="left", padx=4)
        api_draw()

    # ---------- 文件管理 ----------
    def act_files(self):
        p = self.palette()
        win = tk.Toplevel(self.root)
        win.title(APP + " · 文件管理")
        win.geometry("1040x660")
        win.configure(bg=p["bg"])
        top = tk.Frame(win, bg=p["bg"]); top.pack(fill="x", padx=8, pady=6)
        e_path = tk.Entry(top, font=(FONT, 9)); e_path.pack(side="left", fill="x", expand=True, padx=(0, 6))
        fs_tv = ttk.Treeview(win, columns=("n", "k", "s"), show="headings", height=10)
        for c, w, tt in (("n", 460, "名称"), ("k", 90, "类型"), ("s", 160, "大小 / 时间")):
            fs_tv.heading(c, text=tt); fs_tv.column(c, width=w, anchor="w")
        fs_tv.pack(fill="both", expand=False, padx=8)
        body = tk.Text(win, height=12, font=(FONT, 9), bg=p["sunken"], fg=p["ink"],
                       insertbackground=p["ink"], relief="flat", wrap="none")
        body.pack(fill="both", expand=True, padx=8, pady=6)
        meta = tk.Label(win, text="", bg=p["bg"], fg=p["ink3"], font=(FONT, 8), anchor="w")
        meta.pack(fill="x", padx=8)
        self._fs_cur = {"dir": "", "file": ""}

        def guard(p0, must=True):
            sandbox = bool((self.cfg.get("prefs") or {}).get("sandbox", True))
            ap = os.path.abspath(os.path.expanduser(str(p0 or "")))
            ws = os.path.abspath(self.cfg.get("workspace") or E.HOME)
            home = os.path.abspath(E.HOME)
            if sandbox and not (E._inside(ap, ws) or ap == ws or E._inside(ap, home) or ap == home):
                raise PermissionError("沙箱限制：%s 不在工作区/数据目录内" % ap)
            if must and not os.path.exists(ap):
                raise FileNotFoundError(ap)
            return ap

        def fs_draw(path=None):
            try:
                d = guard(path or self._fs_cur["dir"] or (self.cfg.get("workspace") or E.HOME))
                if os.path.isfile(d):
                    d = os.path.dirname(d)
                self._fs_cur["dir"] = d
                e_path.delete(0, "end"); e_path.insert(0, d)
                fs_tv.delete(*fs_tv.get_children())
                for n in sorted(os.listdir(d), key=lambda x: (not os.path.isdir(os.path.join(d, x)), x.lower())):
                    fp = os.path.join(d, n)
                    try:
                        st = os.stat(fp)
                        fs_tv.insert("", "end", values=(n, "dir" if os.path.isdir(fp) else "file",
                                                        "%d B · %s" % (st.st_size, ts(st.st_mtime))))
                    except Exception:
                        fs_tv.insert("", "end", values=(n, "?", ""))
            except Exception as e:
                messagebox.showerror(APP, str(e))

        def fs_open(_ev=None):
            sel = fs_tv.selection()
            if not sel:
                return
            n = fs_tv.item(sel[0], "values")[0]
            fp = os.path.join(self._fs_cur["dir"], str(n))
            if os.path.isdir(fp):
                fs_draw(fp); return
            try:
                self._fs_cur["file"] = fp
                with open(fp, "rb") as f:
                    raw = f.read(1500000)
                body.configure(state="normal"); body.delete("1.0", "end")
                body.insert("1.0", E._decode(raw) if b"\x00" not in raw[:4000] else "（二进制文件，不显示内容）")
                meta.configure(text="%s · %d B" % (fp, len(raw)))
            except Exception as e:
                messagebox.showerror(APP, str(e))

        def fs_save():
            fp = self._fs_cur.get("file")
            if not fp:
                messagebox.showinfo(APP, "先打开一个文件"); return
            ctx = {"workspace": self.cfg.get("workspace"), "sandbox": bool((self.cfg.get("prefs") or {}).get("sandbox", True))}
            s = self.session()
            ctx["checkpoint"] = lambda p_, t_, b_, a_, m_: E.make_checkpoint(s, p_, t_, b_, a_, m_)
            r = E.run_tool("fs_write", {"path": fp, "content": body.get("1.0", "end-1c")}, ctx)
            self.save_all()
            try:
                self.refresh_changes()
            except Exception:
                pass
            self.toast("已保存（留检查点）：" + fp if r.get("ok") else "保存失败：" + str(r.get("text")))

        def fs_mkdir():
            name = simpledialog.askstring(APP, "新文件夹名：", parent=win)
            if not name:
                return
            fp = guard(os.path.join(self._fs_cur["dir"], name), must=False)
            os.makedirs(fp, exist_ok=True)
            fs_draw()

        def fs_newfile():
            name = simpledialog.askstring(APP, "新文件名：", parent=win)
            if not name:
                return
            fp = guard(os.path.join(self._fs_cur["dir"], name), must=False)
            with open(fp, "w", encoding="utf-8") as f:
                f.write("")
            fs_draw()

        def fs_del():
            if not self._fs_cur.get("file"):
                messagebox.showinfo(APP, "先打开一个文件"); return
            fp = self._fs_cur["file"]
            if not messagebox.askyesno(APP, "删除 %s ？（文本文件可回滚）" % fp):
                return
            try:
                before = None
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        before = f.read()
                except Exception:
                    pass
                if before is not None:
                    E.make_checkpoint(self.session(), fp, "fs_delete", before, None, "delete")
                os.remove(fp)
                self.save_all()
                self.toast("已删除：" + fp)
                body.delete("1.0", "end"); self._fs_cur["file"] = ""
                fs_draw()
            except Exception as e:
                messagebox.showerror(APP, str(e))

        for txt, fn in (("打开", fs_draw), ("上级", lambda: fs_draw(os.path.dirname(self._fs_cur["dir"]))),
                        ("工作区", lambda: fs_draw(self.cfg.get("workspace"))),
                        ("＋文件", fs_newfile), ("＋文件夹", fs_mkdir), ("保存(留检查点)", fs_save),
                        ("删除", fs_del), ("打开所在目录", lambda: self.open_path(self._fs_cur["dir"])),
                        ("刷新", lambda: fs_draw())):
            tk.Button(top, text=txt, font=(FONT, 9), bg=p["panel2"], fg=p["ink"], relief="flat",
                      command=fn).pack(side="left", padx=3)
        e_path.bind("<Return>", lambda _e: fs_draw(e_path.get().strip()))
        fs_tv.bind("<Double-1>", fs_open)
        fs_draw(self.cfg.get("workspace"))

    def toast(self, msg):
        self.lbl_status.configure(text=msg)
        E.log("info", "ui", msg)

    def save_all(self):
        try:
            E.save_sessions(self.sessions)
            E.save_config(self.cfg)
            E.save_ui(self.ui)
        except Exception as e:
            E.log("error", "save", str(e))

    def pump(self):
        # 关键稳定性保证：任何单条事件处理异常都不得中断泵循环。
        # （旧实现异常穿透 try/queue.Empty 后，after 不再续约 → 界面看起来“卡死”）
        n = 0
        try:
            while n < 800:                      # 限量排空，避免一次处理巨量积压造成假死
                try:
                    kind, data = self.q.get_nowait()
                except queue.Empty:
                    break
                n += 1
                try:
                    if kind == "selftest":
                        self.append_system(E.format_selftest(data))
                        self.lbl_status.configure(text="自检完成：%d/%d 通过" % (data["passed"], data["total"]))
                    else:
                        self.handle(kind, data)
                except Exception as e:
                    E.log("error", "pump", "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
                    try:
                        self.lbl_status.configure(text="界面处理出现异常（已写入日志）：%s" % e)
                    except Exception:
                        pass
        except Exception as e:
            E.log("error", "pump", "pump loop: %s" % e)
        finally:
            try:
                self._pump_job = self.root.after(60, self.pump)
            except Exception:
                pass

    def cancel_jobs(self):
        """取消所有待执行的 after 定时器：避免退出时控制台打印 Tcl 'invalid command name …' 噪音。"""
        for attr in ("_pump_job", "_boot_job", "_tick_job", "_flush_job"):
            job = getattr(self, attr, None)
            setattr(self, attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass

    def on_close(self):
        if self.busy and not messagebox.askyesno(APP, "正在生成中，确定退出？"):
            return
        if self.cancel:
            self.cancel.set()
        self.cancel_jobs()
        self.save_all()
        E.log("info", "shutdown", "退出")
        self.root.destroy()


def ui_selftest(skip_tk=False):
    """原生 app 的界面结构 / 渲染回归自检（不依赖网络）。

    skip_tk=True 供「界面内 F5 自检」使用：那条路径跑在后台线程里，Tk 只能在主线程构造，
    因此两个需要真实 Text/标签的项改为标注跳过（命令行 --selftest 与 /api/selftest 会真跑）。
    与核心自检（core·…）互补：这里只验证「原生 app 自己」的东西 —— 面板注册表、
    扩展面板方法、Markdown 分片器保真、批量渲染与旧实现等价、渲染性能预算、打开面板即切标签。
    """
    rows = []

    def add(name, ok, detail):
        rows.append({"name": "app·" + name, "ok": bool(ok), "detail": str(detail)[:200]})

    # 1) 面板注册表：唯一 + 每个面板都有 panel_<pid>() 实现 + chat 是常驻面板
    try:
        pids = [p for p, _t, _w, _d in PANEL_DEFS]
        dup = len(pids) - len(set(pids))
        missing = [p for p in pids if not callable(getattr(App, "panel_" + p, None))]
        bad_dock = [p for p, _t, w, _d in PANEL_DEFS if w not in ("main", "bottom")]
        ok = (dup == 0 and not missing and not bad_dock and "chat" in pids
              and PANEL_DEFAULT.get("chat") and pids[0] == "chat")
        add("界面 面板注册表", ok, "面板 %d 个（默认打开 %d）· 缺失实现 %s · 重复 %d · 非法停靠 %s"
            % (len(pids), sum(1 for _p, _t, _w, d in PANEL_DEFS if d), missing or "无", dup, bad_dock or "无"))
    except Exception as e:
        add("界面 面板注册表", False, "%s: %s" % (type(e).__name__, e))

    # 2) 扩展面板（技能 / 插件 / MCP）与斜杠命令齐备
    try:
        need_panels = ("skills", "plugins", "mcp")
        miss = [p for p in need_panels if not callable(getattr(App, "panel_" + p, None))]
        need_act = ("refresh_skills", "act_new_skill", "act_del_skill", "refresh_plugins",
                    "act_plugin_toggle", "act_plugin_delete", "act_plugin_example", "act_plugin_call",
                    "refresh_mcp", "act_mcp_save", "act_mcp_test", "act_mcp_delete", "mcp_reload_async")
        miss_act = [m for m in need_act if not callable(getattr(App, m, None))]
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pistudio.py"),
                   "r", encoding="utf-8", errors="replace").read()
        cmds = [c for c in ('cmd in ("skills", "skill")', 'cmd in ("plugins", "plugin")', 'cmd == "mcp"')
                if c in src]
        add("界面 扩展面板·技能/插件/MCP", (not miss and not miss_act and len(cmds) == 3),
            "面板实现缺失 %s · 方法缺失 %s · 斜杠命令 %d/3" % (miss or "无", miss_act or "无", len(cmds)))
    except Exception as e:
        add("界面 扩展面板·技能/插件/MCP", False, "%s: %s" % (type(e).__name__, e))

    # 3) Markdown 分片器：标记剥离后的纯文本保真
    try:
        sample = ("# 标题\n- 列表 `code` **粗** 项\n\n```py\nprint(1)\n```\n普通段落 **加粗** 与 `行内`\n")
        segs = App.md_segments(sample, "ai")
        flat = "".join(segs[0::2])
        tags = {s if isinstance(s, str) else "|".join(s) for s in segs[1::2]}
        ok = ("**" not in flat and "`" not in flat and "#" not in flat
              and "标题" in flat and "print(1)" in flat and "普通段落 加粗 与 行内" in flat
              and {"mdh", "mdli", "code", "codebar", "ai|bold", "ai|icode"} <= tags)
        add("渲染 Markdown 分片器", ok, "片段 %d · 标签 %d 种 · 代码块/标题/列表/行内/加粗全覆盖"
            % (len(segs) // 2, len(tags)))
    except Exception as e:
        add("渲染 Markdown 分片器", False, "%s: %s" % (type(e).__name__, e))

    # 4) 批量渲染与旧「逐次 insert」等价（真实 Tk Text：正文与标签区间逐一对齐）
    def _md_render_legacy(t, text, base="ai"):
        """重构前的实现（逐次 insert），作为等价性参照。"""
        fence = False
        for ln in str(text).split("\n"):
            s = ln.rstrip()
            if s.lstrip().startswith("```"):
                if fence:
                    fence = False
                else:
                    fence = True
                    t.insert("end", "  ▍%s\n" % (s.lstrip()[3:].strip() or "代码"), "codebar")
                continue
            if fence:
                t.insert("end", ln + "\n", "code")
                continue
            if not s.strip():
                t.insert("end", "\n", base)
                continue
            m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", s)
            if m:
                t.insert("end", m.group(2).strip() + "\n", "mdh")
                continue
            m = re.match(r"^(\s*)[-*·]\s+(.*)$", s)
            if m:
                t.insert("end", "  • ", "mdli")
                for part in re.split(r"(`[^`\n]+`)", m.group(2)):
                    if len(part) > 2 and part.startswith("`") and part.endswith("`"):
                        t.insert("end", part[1:-1], ("mdli", "icode"))
                        continue
                    for sg in re.split(r"(\*\*[^*\n]+\*\*)", part):
                        if len(sg) > 4 and sg.startswith("**") and sg.endswith("**"):
                            t.insert("end", sg[2:-2], ("mdli", "bold"))
                        elif sg:
                            t.insert("end", sg, "mdli")
                t.insert("end", "\n", "mdli")
                continue
            for part in re.split(r"(`[^`\n]+`)", s):
                if len(part) > 2 and part.startswith("`") and part.endswith("`"):
                    t.insert("end", part[1:-1], (base, "icode"))
                    continue
                for sg in re.split(r"(\*\*[^*\n]+\*\*)", part):
                    if len(sg) > 4 and sg.startswith("**") and sg.endswith("**"):
                        t.insert("end", sg[2:-2], (base, "bold"))
                    elif sg:
                        t.insert("end", sg, base)
            t.insert("end", "\n", base)

    # Tk 相关项共用一个解释器（先后创建/销毁多个 Tk 会在 stderr 打出 ttk ThemeChanged 噪音）
    tkmod = None
    tkroot = None
    app = None
    if not skip_tk:
        try:
            import tkinter as _tk
            tkmod = _tk
            tkroot = _tk.Tk()
            tkroot.geometry("1000x700")
            tkroot.withdraw()
        except Exception:
            tkmod = None
            tkroot = None
    corpus = ("# 一级标题\n## 二级 **粗**\n- 项目 `a` 与 **b**\n- 纯文本\n\n"
              "普通段落带 `行内代码` 与 **加粗**，以及结尾。\n"
              "```python\nimport os\nprint('hi')\n```\n尾行\n")
    if skip_tk:
        add("渲染 批量插入 ≡ 旧逐次插入", True, "界面内自检：跳过（Tk 只能在主线程构造，由 `--selftest` 覆盖）")
    else:
        if tkroot is None:
            add("渲染 批量插入 ≡ 旧逐次插入", True, "本环境无可用 Tk，已跳过")
        else:
            try:
                t1 = tkmod.Text(tkroot)
                t2 = tkmod.Text(tkroot)
                for w in (t1, t2):
                    for tag in ("ai", "codebar", "code", "mdh", "mdli", "bold", "icode"):
                        w.tag_configure(tag)
                segs = App.md_segments(corpus, "ai")
                t1.insert("end", *segs)
                _md_render_legacy(t2, corpus)
                same_text = t1.get("1.0", "end-1c") == t2.get("1.0", "end-1c")
                same_tags = all(t1.tag_ranges(x) == t2.tag_ranges(x)
                                for x in ("ai", "codebar", "code", "mdh", "mdli", "bold", "icode"))
                add("渲染 批量插入 ≡ 旧逐次插入", same_text and same_tags,
                    "正文一致=%s · 7 类标签区间一致=%s · 单次 Tcl 调用替代 %d 次"
                    % (same_text, same_tags, len(segs) // 2))
            except Exception as e:
                add("渲染 批量插入 ≡ 旧逐次插入", False, "%s: %s" % (type(e).__name__, e))

    # 5) 渲染性能预算：2000 行级 Markdown 的分片耗时（每次流式重绘都会走这条路）
    try:
        blk = []
        for i in range(250):
            blk.append("## 小节 %d" % i)
            blk.append("- 列表 `c%d` **粗** 文本 %d" % (i, i))
            blk.append("")
            blk.append("段落文本，含 `inline` 与 **bold** 混排。" * 3)
            blk.append("")
            blk.append("```python\ndef f_%d(x):\n    return x * %d\n```" % (i, i))
        text = "\n".join(blk)
        t0 = time.perf_counter()
        segs = App.md_segments(text, "ai")
        dt = (time.perf_counter() - t0) * 1000.0
        add("渲染 性能预算（分片 < 200ms）", dt < 200 and len(segs) > 1000,
            "%d 行 / %d 字符 → 分片 %.1f ms · %d 片段" % (text.count("\n") + 1, len(text), dt, len(segs) // 2))
    except Exception as e:
        add("渲染 性能预算（分片 < 200ms）", False, "%s: %s" % (type(e).__name__, e))
    # 6) 打开面板必须真的切到那个标签（含新建的扩展面板），否则「打开 X 面板」等于没反应
    if skip_tk:
        add("界面 打开面板即切换标签", True, "界面内自检：跳过（Tk 只能在主线程构造，由 `--selftest` 覆盖）")
    elif tkroot is None:
        add("界面 打开面板即切换标签", True, "本环境无可用 Tk，已跳过")
    else:
        try:
            app = App(tkroot)
            tkroot.update()
            detail = []
            ok = True
            for pid in ("skills", "plugins", "mcp", "console"):
                app.show_panel(pid)
                tkroot.update()
                nb = app.nb_main if app.dock_of.get(pid) == "main" else app.nb_bottom
                hit = nb.select()
                same = (str(hit) == str(app.frames.get(pid)))
                ok = ok and same
                detail.append("%s:%s" % (pid, "已切换" if same else "未切换"))
            add("界面 打开面板即切换标签", ok, " · ".join(detail))
        except Exception as e:
            add("界面 打开面板即切换标签", False, "%s: %s" % (type(e).__name__, e))
    # 7) 问答（QA）卡片：真实点选 → 答案 JSON 往返、跳过语义、控件齐备
    if skip_tk:
        add("问答 卡片真实往返（选项/多选/自定义/跳过）", True,
            "界面内自检：跳过（Tk 只能在主线程构造，由 `--selftest` 覆盖）")
    elif tkroot is None:
        add("问答 卡片真实往返（选项/多选/自定义/跳过）", True, "本环境无可用 Tk，已跳过")
    else:
        try:
            if app is None:
                app = App(tkroot)
            tkroot.update()
            payload = {"intro": "自检：需求澄清", "questions": [
                {"key": "scope", "q": "改动范围？",
                 "options": [{"label": "只改 app", "desc": "推荐"}, {"label": "全仓库"}]},
                {"key": "extra", "q": "还要一起做哪些？",
                 "options": [{"label": "补自检"}, {"label": "更新文档"}], "multi": True}]}

            def walk(w):
                out = []
                for c in w.winfo_children():
                    out.append(c)
                    out.extend(walk(c))
                return out

            win = app.ask_question(payload)
            tkroot.update()
            kids = walk(win)
            n_radio = len([c for c in kids if isinstance(c, ttk.Radiobutton)])
            n_check = len([c for c in kids if isinstance(c, ttk.Checkbutton)])
            n_entry = len([c for c in kids if isinstance(c, tkmod.Entry)])
            app._qa_state[1]["vars"]["更新文档"].set(True)      # 多选：勾第二项
            app._qa_state[0]["custom"].set("只动 app/pistudio.py")
            app._qa_answer(False)                              # 等价于点「提交答案」
            tkroot.update()
            r1 = app.qa_result or {}
            ok1 = (n_radio == 2 and n_check == 2 and n_entry == 2
                   and r1.get("answers", {}).get("scope") == ["只改 app"]      # 单选默认选推荐项
                   and r1.get("answers", {}).get("extra") == ["更新文档"]
                   and r1.get("custom", {}).get("scope") == "只动 app/pistudio.py")
            win2 = app.ask_question(payload)
            tkroot.update()
            app._qa_answer(True)                               # 等价于点「跳过，按你的判断做」
            tkroot.update()
            ok2 = app.qa_result is None
            add("问答 卡片真实往返（选项/多选/自定义/跳过）", ok1 and ok2,
                "单选控件 %d · 多选控件 %d · 自定义框 %d · 答案 scope=%s extra=%s · 跳过→None=%s"
                % (n_radio, n_check, n_entry, r1.get("answers", {}).get("scope"),
                   r1.get("answers", {}).get("extra"), ok2))
        except Exception as e:
            add("问答 卡片真实往返（选项/多选/自定义/跳过）", False, "%s: %s" % (type(e).__name__, e))
    # 8) 问答通道接线：三端（原生/网页/终端）+ /qa 命令 + API（不依赖 Tk）
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        src_app = open(os.path.join(here, "pistudio.py"), "r", encoding="utf-8", errors="replace").read()
        src_srv = open(os.path.join(here, "piserver.py"), "r", encoding="utf-8", errors="replace").read()
        src_wb = ""
        for cand in (os.path.join(os.path.dirname(here), "workbench.html"),
                     os.path.join(here, "web", "index.html")):
            try:
                src_wb += open(cand, "r", encoding="utf-8", errors="replace").read()
            except Exception:
                pass
        miss = [m for m in ("ask_request", "ask_question", "toggle_qa", "qa_demo_card") if not callable(getattr(App, m, None))]
        need_app = ['cmd == "qa"', 'elif kind == "ask"', "self.ask_request", "self.var_qa"]
        need_srv = ['"/api/answer"', "def ask(payload", "resolve_question", "self.questions"]
        need_web = ["showAsk", "/api/answer", "'ask'"]
        ok = (not miss
              and all(x in src_app for x in need_app)
              and all(x in src_srv for x in need_srv)
              and all(x in src_wb for x in need_web))
        add("问答 三端接线（原生卡/网页卡/终端 + /api/answer）", ok,
            "原生方法缺失 %s · 原生接线 %d/%d · 后端 %d/%d · 网页 %d/%d"
            % (miss or "无", sum(x in src_app for x in need_app), len(need_app),
               sum(x in src_srv for x in need_srv), len(need_srv),
               sum(x in src_wb for x in need_web), len(need_web)))
    except Exception as e:
        add("问答 三端接线（原生卡/网页卡/终端 + /api/answer）", False, "%s: %s" % (type(e).__name__, e))

    # 9) 网页工作台（workbench.html）：多对话面板 / 重绘不丢内容 / 面板尺寸稳定 / 各面板接线
    try:
        here2 = os.path.dirname(os.path.abspath(__file__))
        wb_path = os.path.join(os.path.dirname(here2), "workbench.html")
        wb = open(wb_path, "r", encoding="utf-8", errors="replace").read() if os.path.isfile(wb_path) else ""
        g_multi = ["const CHAT_PREFIX='chat:'", "function isChatId(", "function chatId(", "function panelDef(",
                   "function V(sid)", "function openChatPanel(", "function loadSession(", "const LOCKED=new Set();"]
        g_keep = ["function liveItemHTML(", "function appendLiveTool(", "function updateLiveTool(", "function refreshPanel(",
                  "function fillContent(", "function activateTab(node,i,wrap)", "function renderMsgs(sid)",
                  "function flushStream(sid)", "function handleEvent(e,sid)", "async function send(sid)",
                  "async function webSelfTest(", "function applyDeepLink("]
        g_size = ["node.sizes=w.map(x=>x/tot*100)", "sp.sizes=first?[30,70]:[70,30]", "双击均分"]
        g_panels = ['id="wbSessSort"', 'id="wbSessCloseAll"', "sesscard", 'data-pref="temperature"',
                    "fldbad", "const dirtyKeys=", "function fsLang(", "function bindFiles(root)", 'id="wbFsCrumb"', 'class="fsrow',
                    "function applyDeepLink("]

        def _miss(xs):
            return [x for x in xs if x not in wb]

        m1, m2, m3, m4 = _miss(g_multi), _miss(g_keep), _miss(g_size), _miss(g_panels)
        add("网页 多对话面板（sid 视图 + 动态面板 + 可停靠）", bool(wb) and not m1, "缺失 %s" % (m1 or "无"))
        add("网页 重绘不丢内容（live 缓冲 + 局部刷新）", bool(wb) and not m2, "缺失 %s" % (m2 or "无"))
        add("网页 面板尺寸稳定（增删面板不重置为等分）", bool(wb) and not m3, "缺失 %s" % (m3 or "无"))
        add("网页 会话/设置/文件面板接线", bool(wb) and not m4, "缺失 %s" % (m4 or "无"))
        try:                                   # JS 语法（有 node 才跑）
            import re as _re, tempfile, subprocess as _sp, shutil as _sh
            blocks = _re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", wb)
            node = _sh.which("node")
            if not node or not blocks:
                add("网页 JS 语法（node --check）", True,
                    "跳过：%s" % ("未找到 node" if not node else "没有内联脚本"))
            else:
                fd, tmp = tempfile.mkstemp(suffix=".js")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("\n;\n".join(blocks))
                try:
                    p = _sp.run([node, "--check", tmp], capture_output=True, text=True)
                finally:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                add("网页 JS 语法（node --check）", p.returncode == 0,
                    "%d 个内联块 · exit %d %s" % (len(blocks), p.returncode, (p.stderr or "").strip()[:140]))
        except Exception as e:
            add("网页 JS 语法（node --check）", False, "%s: %s" % (type(e).__name__, e))
    except Exception as e:
        add("网页 工作台结构", False, "%s: %s" % (type(e).__name__, e))

    if tkroot is not None:                     # 单次销毁共享解释器
        try:
            try:
                app.cancel_jobs()              # 先停掉队列泵/启动探测的 after 任务
            except Exception:
                pass
            tkroot.destroy()
        except Exception:
            pass

    passed = sum(1 for r in rows if r["ok"])
    return {"rows": rows, "passed": passed, "total": len(rows)}


def sym_long(spec):
    return "description" in spec and any(k in (spec.get("description") or "") for k in ("内容", "代码", "多行", "正文"))


def session_markdown(s):
    out = ["# %s" % (s.get("title") or "会话"), "",
           "> 导出时间：%s ｜ 消息 %d 条" % (time.strftime("%Y-%m-%d %H:%M:%S"), len(s.get("messages") or [])), ""]
    for m in s.get("messages") or []:
        role = m.get("role")
        if role == "user":
            out += ["## 你 · %s" % ts(m.get("ts")), "", str(m.get("content", "")), ""]
        elif role == "assistant":
            txt = str(m.get("content", "")).strip()
            if txt:
                out += ["## PI · %s" % ts(m.get("ts")), "", txt, ""]
            for c in (m.get("tool_calls") or []):
                out.append("`调用 %s`" % ((c.get("function") or {}).get("name") or "?"))
            if m.get("tool_calls"):
                out.append("")
        elif role == "tool":
            out += ["### ⚙ %s  %s  %sms" % (m.get("name"), "成功" if m.get("ok", True) else "失败", m.get("ms", 0)),
                    "", "```text", str(m.get("content", ""))[:20000], "```", ""]
    return "\n".join(out)


def fatal(exc_text):
    try:
        E.ensure_home()
        with open(E.ERROR_PATH, "a", encoding="utf-8") as f:
            f.write("\n===== %s =====\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), exc_text))
    except Exception:
        pass
    try:
        ctypes.windll.user32.MessageBoxW(None, "%s 启动失败：\n\n%s\n\n完整信息见 %s" % (
            APP, exc_text[-1200:], E.ERROR_PATH), APP + " · 启动错误", 0x10)
    except Exception:
        sys.stderr.write(exc_text)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("--serve", "serve", "--web"):
        import piserver
        return piserver.main(argv[1:])
    rc = E.cli()
    if rc is not None:
        return rc
    try:
        root = tk.Tk()
        app = App(root)
        root.mainloop()
        return 0
    except Exception:
        fatal(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
