import json
import mimetypes
import os
import queue
import random
import shutil
import socket
import string
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import piengine as E
import picore as CORE

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)
WORKBENCH = os.path.join(ROOT_DIR, "workbench.html")   # 工作台（最终形态）在项目根目录
DEFAULT_PORT = 8795
APPROVAL_TIMEOUT = 600        # 秒：审批无人应答时自动拒绝，避免这次 run 永久挂起
QUESTION_TIMEOUT = 900        # 秒：问答无人应答时按「跳过」处理（模型改用默认假设继续）


def free_port(start=DEFAULT_PORT, tries=40):
    for i in range(tries):
        p = start + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def new_token(n=24):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


class Hub:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = E.load_config()
        self.sessions = E.load_sessions() or [E.new_session()]
        self.runs = {}
        self.approvals = {}
        self.questions = {}
        self.token = new_token()

    def session(self, sid):
        for s in self.sessions:
            if s["id"] == sid:
                return s
        return None

    def save(self):
        with self.lock:
            E.save_sessions(self.sessions)

    def start_run(self, sid, text):
        s = self.session(sid)
        if s is None:
            raise E.EngineError("会话不存在")
        s["messages"].append({"role": "user", "content": text, "ts": time.time()})
        if s.get("title") in (None, "", "新会话"):
            s["title"] = text[:20]
        s["updated"] = time.time()
        q = queue.Queue()
        cancel = threading.Event()
        res = {}
        rid = E.uuid.uuid4().hex[:8]

        def on_event(kind, data):
            if kind == "tool_end":
                CORE.observe_event("tool_end", data, sid)
            q.put((kind, data))

        def approve(name, args, meta):
            try:                                  # 审批策略：规则/自动确认（命中则不出弹窗）
                import pipolicy
                d = pipolicy.decide(self.cfg, name, args, meta)
                if d is True:
                    q.put(("notice", "自动确认（策略放行）：" + name))
                    return True
                if d is False:
                    q.put(("notice", "自动拒绝（策略禁止）：" + name))
                    E.log("warn", "policy", "策略拒绝 %s" % name)
                    return False
            except Exception:
                pass
            aid = E.uuid.uuid4().hex[:8]
            ev = threading.Event()
            with self.lock:
                self.approvals[aid] = {"event": ev, "result": False, "args": None, "name": name, "run": rid}
            q.put(("approve", {"id": aid, "name": name, "args": args,
                               "meta": {"desc": meta.get("desc"), "group": meta.get("group"),
                                        "mutating": meta.get("mutating")}}))
            if not ev.wait(timeout=APPROVAL_TIMEOUT):
                with self.lock:
                    self.approvals.pop(aid, None)
                E.log("warn", "serve", "审批超时 %ds（无人应答），已自动拒绝：%s" % (APPROVAL_TIMEOUT, name))
                return False
            with self.lock:
                rec = self.approvals.pop(aid, None)
            if not rec:
                return False
            if rec["result"] and rec["args"] is not None:
                return {"allow": True, "args": rec["args"]}
            return bool(rec["result"])

        def ask(payload, ctx=None):
            """问答（QA）通道：推一张带选项的问题卡给网页，等用户点选/跳过。"""
            qid = E.uuid.uuid4().hex[:8]
            ev = threading.Event()
            with self.lock:
                self.questions[qid] = {"event": ev, "result": None, "run": rid, "payload": payload}
            q.put(("ask", {"id": qid, "payload": payload}))
            if not ev.wait(timeout=QUESTION_TIMEOUT):
                with self.lock:
                    self.questions.pop(qid, None)
                q.put(("notice", "问答超时 %ds（无人应答），已改为按默认假设继续。" % QUESTION_TIMEOUT))
                return None
            with self.lock:
                rec = self.questions.pop(qid, None)
            return (rec or {}).get("result")

        def work():
            try:
                try:
                    import piplan
                    r = piplan.run_turn_auto(self.cfg, s, on_event, cancel, approve, ask)
                except ImportError:
                    r = E.run_turn(self.cfg, s, on_event, cancel, approve, ask)
            except Exception as e:
                r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "text": "",
                     "steps": 0, "usage": {}, "tools": [], "checkpoints": []}
                E.log("error", "serve", traceback.format_exc())
            res.update(r)
            CORE.observe_event("done", r, sid, title=s.get("title"),
                               model=(HUB.cfg.get("provider") or {}).get("model"))
            self.save()
            q.put(("__end__", r))

        with self.lock:
            self.runs[rid] = {"cancel": cancel, "q": q, "res": res, "sid": sid, "ts": time.time()}
        threading.Thread(target=work, daemon=True).start()
        return rid, q, cancel

    def resolve_approval(self, aid, allow, args=None):
        with self.lock:
            rec = self.approvals.get(aid)
            if not rec:
                return False
            rec["result"] = bool(allow)
            rec["args"] = args if isinstance(args, dict) else None
            rec["event"].set()
            return True

    def resolve_question(self, qid, result, skip=False):
        """回填一张问答卡的答案（skip=True 表示「跳过，按你的判断做」）。"""
        with self.lock:
            rec = self.questions.get(qid)
            if not rec:
                return False
            rec["result"] = None if skip else (result if isinstance(result, dict) else {"answers": {}})
            rec["event"].set()
            return True

    def cancel_run(self, rid):
        """停止一次运行：置取消位，并把这次运行等待中的审批一并拒绝（否则工作线程会卡在审批等待上）。"""
        run = self.runs.get(rid)
        if run:
            run["cancel"].set()
        with self.lock:
            for aid, rec in list(self.approvals.items()):
                if rec.get("run") == rid:
                    rec["result"] = False
                    rec["args"] = None
                    rec["event"].set()
            for qid, rec in list(self.questions.items()):     # 等待中的问答卡也一起放行（按跳过）
                if rec.get("run") == rid:
                    rec["result"] = None
                    rec["event"].set()
        return bool(run)


HUB = None


class Handler(BaseHTTPRequestHandler):
    server_version = "PIStudio/%s" % E.VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("PI_SERVE_VERBOSE"):
            E.log("info", "http", fmt % args)

    def _token_ok(self, query):
        tok = (query.get("t") or [""])[0] or self.headers.get("X-PI-Token", "")
        return tok and tok == HUB.token

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text, ctype="text/plain; charset=utf-8", code=200):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path
        try:
            if path in ("/", "/workbench", "/workbench.html"):
                if not self._token_ok(q):
                    return self._text("PI Studio 后端已启动，但缺少访问令牌。\n请使用启动器输出的完整网址打开。", code=403)
                return self._serve_workbench()
            # 旧版网页界面仍然保留，方便对照
            if path in ("/classic", "/classic.html", "/index.html"):
                if not self._token_ok(q):
                    return self._text("PI Studio 后端已启动，但缺少访问令牌。\n请使用启动器输出的完整网址打开。", code=403)
                return self._serve_file("index.html")
            if path.startswith("/static/"):
                return self._serve_file(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._text("", "image/x-icon", 204)
            if not path.startswith("/api/"):
                return self._json({"ok": False, "error": "not found"}, 404)
            if not self._token_ok(q):
                return self._json({"ok": False, "error": "缺少或错误的令牌（请用启动器打印的完整网址）"}, 403)
            return self._api_get(path, q)
        except Exception as e:
            E.log("error", "http", traceback.format_exc())
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if not self._token_ok(q):
                return self._json({"ok": False, "error": "缺少或错误的令牌"}, 403)
            body = self._body()
            return self._api_post(u.path, body)
        except Exception as e:
            E.log("error", "http", traceback.format_exc())
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    def _serve_workbench(self):
        """工作台（最终形态）：项目根目录的 workbench.html。缺失时回落到经典界面。"""
        if os.path.isfile(WORKBENCH):
            with open(WORKBENCH, "rb") as f:
                return self._text(f.read(), "text/html; charset=utf-8")
        return self._serve_file("index.html")

    def _serve_file(self, rel):
        rel = rel.lstrip("/").replace("\\", "/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.normpath(WEB_DIR)) or not os.path.isfile(full):
            return self._json({"ok": False, "error": "not found"}, 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            return self._text(f.read(), ctype)

    def _fsctx(self, session=None):
        prefs = HUB.cfg.get("prefs") or {}
        return {"workspace": HUB.cfg.get("workspace") or E.HOME,
                "shell_timeout": prefs.get("shell_timeout", 60),
                "sandbox": bool(prefs.get("sandbox", True)),
                "proxy": prefs.get("proxy") or "", "cfg": HUB.cfg, "session": session}

    def _abspath(self, p0):
        """相对路径按工作区解析（面板/脚本都可能传相对路径）。"""
        p0 = str(p0 or "").replace("/", os.sep)
        if not p0:
            return HUB.cfg.get("workspace") or E.HOME
        if not os.path.isabs(p0):
            p0 = os.path.join(HUB.cfg.get("workspace") or E.HOME, p0)
        return os.path.abspath(os.path.expanduser(p0))

    def _guard(self, p0, must_exist=True):
        """路径守卫：工作区或数据目录内（或关沙箱）；越界抛错。"""
        p = self._abspath(p0)
        sandbox = bool((HUB.cfg.get("prefs") or {}).get("sandbox", True))
        ws = os.path.abspath(HUB.cfg.get("workspace") or E.HOME)
        home = os.path.abspath(E.HOME)
        if sandbox and not (E._inside(p, ws) or p == ws or E._inside(p, home) or p == home):
            raise PermissionError("沙箱限制：%s 不在工作区/数据目录内（设置 → 关闭沙箱可解除）" % p)
        if must_exist and not os.path.exists(p):
            raise FileNotFoundError(p)
        return p

    def _fs_list(self, p0):
        try:
            p = self._guard(p0 or (HUB.cfg.get("workspace") or E.HOME), True)
            if os.path.isfile(p):
                st = os.stat(p)
                return self._json({"ok": True, "path": p, "file": True, "size": st.st_size,
                                   "mtime": st.st_mtime, "entries": [], "parent": os.path.dirname(p)})
            ents = []
            with os.scandir(p) as it:
                for de in it:
                    try:
                        st = de.stat()
                        ents.append({"name": de.name, "dir": de.is_dir(), "size": st.st_size,
                                     "mtime": st.st_mtime})
                    except Exception:
                        ents.append({"name": de.name, "dir": de.is_dir(), "size": 0, "mtime": 0})
            ents.sort(key=lambda x: (not x["dir"], x["name"].lower()))
            return self._json({"ok": True, "path": p, "file": False, "parent": os.path.dirname(p),
                               "entries": ents[:1000], "dirs": len([e for e in ents if e["dir"]]),
                               "files": len([e for e in ents if not e["dir"]]),
                               "workspace": HUB.cfg.get("workspace")})
        except Exception as e:
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def _fs_read(self, p0):
        try:
            p = self._guard(p0, True)
            if os.path.isdir(p):
                return self._fs_list(p)
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                raw = f.read(1500000)
            return self._json({"ok": True, "path": p, "text": E._decode(raw), "size": size,
                               "truncated": size > 1500000, "mtime": os.path.getmtime(p),
                               "binary": b"\x00" in raw[:4000]})
        except Exception as e:
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def _fs_raw(self, p0):
        try:
            p = self._guard(p0, True)
            if not os.path.isfile(p):
                return self._json({"ok": False, "error": "不是文件"}, 404)
            if os.path.getsize(p) > 30 * 1024 * 1024:
                return self._json({"ok": False, "error": "文件过大（>30MB）"})
            ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
            with open(p, "rb") as f:
                return self._text(f.read(), ctype)
        except Exception as e:
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 404)

    def _fs_search(self, qq, p0):
        try:
            ctx = self._fsctx()
            r = E.run_tool("fs_search", {"query": qq, "path": p0 or "."}, ctx)
            return self._json({"ok": bool(r.get("ok")), "text": r.get("text", "")})
        except Exception as e:
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def artifacts_payload(self, sid):
        s = HUB.session(sid) or (HUB.sessions[0] if HUB.sessions else None)
        files = []
        if s is not None:
            for cp in E.checkpoints(s, include_undone=True)[:200]:
                files.append({"id": cp.get("id"), "path": cp.get("path"), "tool": cp.get("tool"),
                              "ts": cp.get("ts"), "undone": bool(cp.get("undone")),
                              "bytes": cp.get("bytes_after", 0), "mode": cp.get("mode"),
                              "diff": (cp.get("diff") or "")[:2000]})
        def ls(d, kind, limit=15):
            out = []
            try:
                for n in sorted(os.listdir(d), reverse=True)[:limit]:
                    p = os.path.join(d, n)
                    if os.path.isfile(p):
                        out.append({"kind": kind, "name": n, "path": p,
                                    "ts": os.path.getmtime(p), "size": os.path.getsize(p)})
            except Exception:
                pass
            return out
        reports = (ls(os.path.join(E.HOME, "core", "subagents"), "subagent")
                   + ls(os.path.join(E.HOME, "core", "contracts", "reports"), "contract")
                   + ls(os.path.join(E.HOME, "core", "orchestrated"), "orchestrated")
                   + ls(os.path.join(E.HOME, "core", "cron-logs"), "cron"))
        reports.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        return {"ok": True, "session": (s or {}).get("id"), "files": files,
                "reports": reports[:40], "dir": (os.path.join(E.HOME, "core"))}

    def _api_get(self, path, q):
        if path == "/api/health":
            prov = HUB.cfg.get("provider") or {}
            return self._json({"ok": True, "version": E.VERSION, "python": sys.version.split()[0],
                               "home": E.HOME, "workspace": HUB.cfg.get("workspace"),
                               "provider": {"name": prov.get("name"), "base_url": E.norm_base(prov.get("base_url")),
                                            "model": prov.get("model"), "key": E.mask_key(prov.get("api_key")),
                                            "models": prov.get("models") or []},
                               "meta": E.model_meta(HUB.cfg), "tools": len(E.TOOLS)})
        if path == "/api/state":
            return self._json(self.state_payload())
        if path == "/api/session":
            sid = (q.get("id") or [""])[0]
            s = HUB.session(sid)
            if not s:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            return self._json({"ok": True, "session": s,
                               "ctx": E.ctx_state(HUB.cfg, s),
                               "checkpoints": E.checkpoints(s, include_undone=True)})
        if path == "/api/tools":
            return self._json({"ok": True, "tools": [
                {"name": t["name"], "group": t["group"], "desc": t["desc"],
                 "mutating": t["mutating"], "parameters": t["parameters"]} for t in E.TOOLS.values()]})
        if path == "/api/console":
            n = int((q.get("n") or ["250"])[0])
            ring = E.log_records()[-n:]
            logs = (E.log_file_records(n - len(ring)) + ring)[-n:] if len(ring) < n else ring
            return self._json({"ok": True, "logs": logs, "file": E.LOG_PATH})
        if path == "/api/changes":
            sid = (q.get("session") or [""])[0]
            s = HUB.session(sid)
            return self._json({"ok": True, "checkpoints": E.checkpoints(s, include_undone=True) if s else []})
        if path == "/api/selftest":
            r = E.selftest(HUB.cfg, do_network=(q.get("net") or ["1"])[0] != "0")
            c = CORE.selftest()
            r["rows"] = r["rows"] + c["items"]
            r["passed"] += c["passed"]
            r["total"] += c["total"]
            try:                                # 原生 app 的界面结构 / 渲染项（app· 前缀），与 CLI 自检口径一致
                import pistudio as _APPUI
                u = _APPUI.ui_selftest()
                r["rows"] = r["rows"] + u["rows"]
                r["passed"] += u["passed"]
                r["total"] += u["total"]
            except Exception as e:
                r["rows"].append({"name": "app 界面自检", "ok": False,
                                  "detail": "%s: %s" % (type(e).__name__, e)})
                r["total"] += 1
            return self._json({"ok": True, "result": r, "text": E.format_selftest(r)})
        if path == "/api/sessions":
            return self._json({"ok": True, "sessions": [
                {"id": s["id"], "title": s.get("title"), "updated": s.get("updated"),
                 "messages": len(s.get("messages") or []),
                 "checkpoints": len(E.checkpoints(s))} for s in HUB.sessions]})
        if path == "/api/memory":
            qq = (q.get("q") or [""])[0]
            limit = int((q.get("limit") or ["50"])[0])
            if qq:
                r = CORE.mem_search(qq, min(limit, 30))
            else:
                r = CORE.mem_list(limit)
            r["stats"] = CORE.mem_stats()
            return self._json(r)
        if path == "/api/memory/read":
            return self._json(CORE.mem_read((q.get("slug") or [""])[0]))
        if path == "/api/skills":
            return self._json(CORE.skills_list())
        if path == "/api/skills/read":
            return self._json(CORE.skill_read((q.get("name") or [""])[0]))
        if path == "/api/mcp":
            return self._json(CORE.mcp_state())
        if path == "/api/cron":
            return self._json(CORE.cron_list())
        if path == "/api/cron/logs":
            return self._json(CORE.cron_logs((q.get("id") or [None])[0], int((q.get("limit") or ["100"])[0])))
        if path == "/api/errors":
            qq = (q.get("q") or [""])[0]
            rs = (q.get("resolved") or [""])[0]
            if qq or rs:
                resolved = None if rs == "" else (rs == "1")
                return self._json(CORE.err_search(qq, resolved=resolved, limit=int((q.get("limit") or ["20"])[0])))
            return self._json(CORE.err_stats())
        if path == "/api/errors/get":
            return self._json(CORE.err_get((q.get("id") or [""])[0]))
        if path == "/api/ledger":
            return self._json(CORE.ledger_recent(int((q.get("n") or ["100"])[0])))
        if path == "/api/ledger/stats":
            return self._json(CORE.ledger_stats(int((q.get("days") or ["14"])[0])))
        if path == "/api/subagents":
            return self._json(CORE.subagent_list(int((q.get("limit") or ["50"])[0])))
        if path == "/api/subagents/get":
            return self._json(CORE.subagent_get((q.get("id") or [""])[0]))
        if path == "/api/plugins":
            import piplugins
            return self._json(piplugins.state())
        if path == "/api/plugins/read":
            import piplugins
            return self._json(piplugins.read_plugin((q.get("id") or [""])[0]))
        if path == "/api/contracts":
            import picontract
            return self._json(picontract.list_())
        if path == "/api/contracts/get":
            import picontract
            c = picontract.get((q.get("id") or [""])[0])
            return self._json({"ok": bool(c), "contract": c} if c else {"ok": False, "error": "契约不存在"})
        if path == "/api/plan":
            import piplan
            s = HUB.session((q.get("session") or [""])[0])
            return self._json(piplan.state({"session": s} if s else None))
        if path == "/api/approvals":
            import pipolicy
            return self._json(pipolicy.state(HUB.cfg))
        if path == "/api/apis":
            import piapi
            return self._json(piapi.list_apis())
        if path == "/api/artifacts":
            return self._json(self.artifacts_payload((q.get("session") or [""])[0]))
        if path == "/api/fs":
            return self._fs_list((q.get("path") or [""])[0])
        if path == "/api/fs/read":
            return self._fs_read((q.get("path") or [""])[0])
        if path == "/api/fs/raw":
            return self._fs_raw((q.get("path") or [""])[0])
        if path == "/api/fs/search":
            return self._fs_search((q.get("q") or [""])[0], (q.get("path") or [""])[0])
        return self._json({"ok": False, "error": "unknown GET " + path}, 404)

    def _api_post(self, path, body):
        if path == "/api/chat":
            sid = body.get("session_id") or ""
            text = (body.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "空消息"}, 400)
            if HUB.session(sid) is None:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            rid, q, cancel = HUB.start_run(sid, text)
            return self._stream(rid, q, cancel)
        if path == "/api/approve":
            ok = HUB.resolve_approval(body.get("id"), body.get("allow"), body.get("args"))
            return self._json({"ok": ok})
        if path == "/api/answer":
            ok = HUB.resolve_question(body.get("id"), {"answers": body.get("answers") or {},
                                                       "custom": body.get("custom") or {},
                                                       "notes": body.get("notes") or {}},
                                       skip=bool(body.get("skip")))
            return self._json({"ok": ok})
        if path == "/api/qa":
            st = CORE.qa_state(HUB.cfg)
            if "qa_first" in body:
                st = CORE.qa_set(HUB.cfg, on=bool(body.get("qa_first")), max_q=body.get("qa_max"))
            return self._json(st)
        if path == "/api/stop":
            rid = body.get("run_id") or ""
            if rid not in HUB.runs:
                with HUB.lock:
                    for k, r in HUB.runs.items():
                        if r["sid"] == body.get("session_id"):
                            rid = k
                            break
            if rid in HUB.runs:
                HUB.cancel_run(rid)
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "没有正在进行的运行"})
        if path == "/api/tool":
            name = body.get("name")
            args = body.get("args") or {}
            prefs = HUB.cfg.get("prefs") or {}
            s = HUB.session(body.get("session_id") or "") or (HUB.sessions[0] if HUB.sessions else None)
            ctx = {"workspace": HUB.cfg.get("workspace"),
                   "shell_timeout": prefs.get("shell_timeout", 60),
                   "sandbox": bool(prefs.get("sandbox", True)),
                   "proxy": prefs.get("proxy") or "",
                   "session": s, "cfg": HUB.cfg}
            if s is not None:
                def _cp(path_, tool_, before_, after_, mode_):
                    return E.make_checkpoint(s, path_, tool_, before_, after_, mode_)
                ctx["checkpoint"] = _cp
            r = E.run_tool(name, args, ctx)
            n_cp = len(E.checkpoints(s)) if s is not None else 0
            if s is not None:
                HUB.save()
            return self._json({"ok": True, "result": r, "session": s.get("id") if s else None,
                               "checkpoints": n_cp})
        if path == "/api/session/new":
            s = E.new_session(body.get("title") or "新会话")
            with HUB.lock:
                HUB.sessions.insert(0, s)
            HUB.save()
            return self._json({"ok": True, "session": s})
        if path == "/api/session/rename":
            s = HUB.session(body.get("id"))
            if not s:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            s["title"] = (body.get("title") or "").strip()[:60] or s.get("title")
            s["updated"] = time.time()
            HUB.save()
            return self._json({"ok": True})
        if path == "/api/session/delete":
            with HUB.lock:
                HUB.sessions = [x for x in HUB.sessions if x["id"] != body.get("id")]
                if not HUB.sessions:
                    HUB.sessions = [E.new_session()]
            HUB.save()
            return self._json({"ok": True, "sessions": [s["id"] for s in HUB.sessions]})
        if path == "/api/session/clear":
            s = HUB.session(body.get("id"))
            if not s:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            s["messages"] = []
            s["checkpoints"] = []
            s["ctx_used"] = 0
            HUB.save()
            return self._json({"ok": True})
        if path == "/api/session/truncate":
            s = HUB.session(body.get("id"))
            if not s:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            idx = int(body.get("index") or 0)
            dropped = len(s.get("messages") or []) - idx
            s["messages"] = (s.get("messages") or [])[:idx]
            s["ctx_used"] = 0
            s["updated"] = time.time()
            HUB.save()
            return self._json({"ok": True, "dropped": dropped, "session": s,
                               "ctx": E.ctx_state(HUB.cfg, s)})
        if path == "/api/rollback":
            s = HUB.session(body.get("session"))
            if not s:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            res = E.rollback(s, body.get("id"))
            HUB.save()
            return self._json({"ok": True, "results": res,
                               "checkpoints": E.checkpoints(s, include_undone=True)})
        if path == "/api/probe":
            prov = dict(HUB.cfg.get("provider") or {})
            body_prov = body.get("provider") or {}
            for k in ("base_url", "api_key", "model"):
                if body_prov.get(k):
                    prov[k] = body_prov[k]
            pr = E.probe(prov, proxy=(HUB.cfg.get("prefs") or {}).get("proxy") or "",
                         retries=int((HUB.cfg.get("prefs") or {}).get("retries") or 2))
            return self._json({"ok": True, "probe": pr})
        if path == "/api/settings":
            with HUB.lock:
                if isinstance(body.get("prefs"), dict):
                    HUB.cfg.setdefault("prefs", {}).update(body["prefs"])
                if isinstance(body.get("provider"), dict):
                    p = HUB.cfg.setdefault("provider", {})
                    for k, v in body["provider"].items():
                        if k == "api_key" and not v:
                            continue
                        p[k] = v
                if body.get("workspace"):
                    HUB.cfg["workspace"] = body["workspace"]
                E.save_config(HUB.cfg)
            return self._json({"ok": True, "state": self.state_payload()})
        if path == "/api/compact":
            s = HUB.session(body.get("session"))
            if not s:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            r = E.compact_session(HUB.cfg, s, None)
            HUB.save()
            return self._json({"ok": True, "result": r, "session": s,
                               "ctx": E.ctx_state(HUB.cfg, s)})
        if path == "/api/import_host":
            found = E.host_providers()
            if not found:
                return self._json({"ok": False, "error": "未找到宿主配置"})
            hp = dict(found[0])
            hp.pop("_autodetected", None)
            with HUB.lock:
                HUB.cfg["provider"] = hp
                E.save_config(HUB.cfg)
            return self._json({"ok": True, "provider": hp, "state": self.state_payload()})
        if path == "/api/detect_local":
            return self._json({"ok": True, "endpoints": E.detect_local_endpoints()})
        if path == "/api/session/duplicate":
            s = HUB.session(body.get("id"))
            if not s:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            c = E.new_session(body.get("title") or ((s.get("title") or "会话") + " 副本"))
            c["messages"] = json.loads(json.dumps(s.get("messages") or []))
            with HUB.lock:
                HUB.sessions.insert(0, c)
            HUB.save()
            return self._json({"ok": True, "session": c,
                               "sessions": [x["id"] for x in HUB.sessions]})
        if path == "/api/session/clear_cp":
            s = HUB.session(body.get("id"))
            if not s:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            s["checkpoints"] = []
            HUB.save()
            return self._json({"ok": True})
        if path == "/api/session/append":
            s = HUB.session(body.get("id"))
            if not s:
                return self._json({"ok": False, "error": "会话不存在"}, 404)
            entries = body.get("entries")
            if not isinstance(entries, list):
                entries = []
            n = 0
            for it in entries[:20]:
                if not isinstance(it, dict) or not it.get("role"):
                    continue
                m = dict(it)
                m.setdefault("ts", time.time())
                s["messages"].append(m)
                n += 1
            s["updated"] = time.time()
            HUB.save()
            return self._json({"ok": True, "appended": n, "session": s,
                               "ctx": E.ctx_state(HUB.cfg, s)})
        if path == "/api/profiles/save":
            name = (body.get("name") or "").strip()[:60]
            if not name:
                return self._json({"ok": False, "error": "档案名不能为空"})
            item = {"name": name, "kind": "openai",
                    "base_url": (body.get("base_url") or "").strip(),
                    "api_key": (body.get("api_key") or "").strip(),
                    "model": (body.get("model") or "").strip()}
            with HUB.lock:
                profs = HUB.cfg.setdefault("profiles", [])
                for i, x in enumerate(profs):
                    if (x.get("name") or "") == name:
                        if not item["api_key"]:
                            item["api_key"] = x.get("api_key") or ""
                        profs[i] = item
                        break
                else:
                    profs.append(item)
                E.save_config(HUB.cfg)
            return self._json({"ok": True, "profiles": [{"name": p.get("name"),
                                                         "base_url": p.get("base_url"),
                                                         "model": p.get("model"),
                                                         "has_key": bool((p.get("api_key") or "").strip())}
                                                        for p in (HUB.cfg.get("profiles") or [])]})
        if path == "/api/profiles/apply":
            name = (body.get("name") or "").strip()
            prof = next((x for x in (HUB.cfg.get("profiles") or []) if (x.get("name") or "") == name), None)
            if not prof:
                return self._json({"ok": False, "error": "档案不存在：" + name}, 404)
            with HUB.lock:
                prov = HUB.cfg.setdefault("provider", {})
                for k in ("base_url", "api_key", "model", "kind"):
                    if prof.get(k):
                        prov[k] = prof[k]
                prov.pop("_autodetected", None)
                E.save_config(HUB.cfg)
            return self._json({"ok": True, "state": self.state_payload()})
        if path == "/api/settings/reset":
            with HUB.lock:
                keep = {k: (HUB.cfg.get("prefs") or {}).get(k)
                        for k in ("price_in", "price_out", "proxy")}
                HUB.cfg["prefs"] = json.loads(json.dumps(E.DEFAULT_CONFIG["prefs"]))
                HUB.cfg["prefs"].update({k: v for k, v in keep.items() if v is not None})
                E.save_config(HUB.cfg)
            return self._json({"ok": True, "state": self.state_payload()})
        if path == "/api/wipe":
            if not body.get("confirm"):
                return self._json({"ok": False, "error": "危险操作：需要 confirm=true"})
            for f in (E.CONFIG_PATH, E.SESSIONS_PATH, E.UI_PATH):
                try:
                    os.remove(f)
                except Exception:
                    pass
            with HUB.lock:
                HUB.cfg = E.load_config()
                HUB.sessions = E.load_sessions() or [E.new_session()]
            CORE.init(HUB.cfg, enable_scheduler=False)
            E.log("warn", "serve", "用户经接口清除了本机配置与会话（wipe）")
            return self._json({"ok": True, "state": self.state_payload()})
        if path == "/api/open_path":
            aliases = {"home": E.HOME, "log": E.LOG_PATH, "config": E.CONFIG_PATH,
                       "sessions": E.SESSIONS_PATH, "ui": E.UI_PATH,
                       "workspace": HUB.cfg.get("workspace") or ""}
            p = (body.get("path") or "").strip()
            p = aliases.get(p.lower(), p)
            if not p:
                return self._json({"ok": False, "error": "缺少 path"})
            ap = os.path.abspath(p)
            home = os.path.abspath(E.HOME)
            allowed = ap == home or ap.startswith(home + os.sep)
            for cand in (E.LOG_PATH, E.CONFIG_PATH, E.SESSIONS_PATH, E.UI_PATH):
                if os.path.abspath(cand) == ap:
                    allowed = True
            ws = HUB.cfg.get("workspace")
            if ws and os.path.abspath(ws) == ap:
                allowed = True
            if not allowed:
                return self._json({"ok": False, "error": "该路径不在允许打开的范围内（数据目录/日志/配置/工作区）"})
            try:
                if os.path.isdir(ap):
                    os.startfile(ap)
                elif os.path.isfile(ap):
                    os.startfile(ap)
                else:
                    d = os.path.dirname(ap) or ap
                    os.makedirs(d, exist_ok=True)
                    os.startfile(d)
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
            return self._json({"ok": True, "opened": ap})
        if path == "/api/memory/write":
            return self._json(CORE.mem_write(body.get("type") or "project", body.get("slug"),
                                             body.get("description"), body.get("body"),
                                             tags=body.get("tags"), confidence=body.get("confidence") or "medium",
                                             source_quote=body.get("source_quote") or ""))
        if path == "/api/memory/update":
            if body.get("mode") == "append" and body.get("body") is not None:
                return self._json(CORE.mem_update(body.get("slug"), append=body.get("body")))
            return self._json(CORE.mem_update(body.get("slug"), body=body.get("body"),
                                              description=body.get("description")))
        if path == "/api/memory/delete":
            return self._json(CORE.mem_delete(body.get("slug")))
        if path == "/api/skills/create":
            return self._json(CORE.skill_create(body.get("name"), body.get("description"), body.get("body"),
                                                body.get("files")))
        if path == "/api/skills/delete":
            return self._json(CORE.skill_delete(body.get("name")))
        if path == "/api/mcp/upsert":
            spec = body.get("spec") or {}
            if not isinstance(spec, dict) or not (spec.get("command") or spec.get("url")):
                return self._json({"ok": False, "error": "spec 需要 command（stdio）或 url（http）"})
            return self._json(CORE.mcp_upsert(spec))
        if path == "/api/mcp/delete":
            return self._json(CORE.mcp_delete(body.get("id")))
        if path == "/api/mcp/reload":
            return self._json(CORE.mcp_reload())
        if path == "/api/mcp/test":
            spec = body.get("spec")
            if isinstance(spec, dict) and (spec.get("command") or spec.get("url")):
                return self._json(CORE.mcp_test_spec(spec))
            sid = body.get("id")
            spec2 = next((x for x in (CORE.mcp_load().get("servers") or []) if x.get("id") == sid), None)
            if not spec2:
                return self._json({"ok": False, "error": "服务器不存在：" + str(sid)})
            return self._json(CORE.mcp_test_spec(spec2))
        if path == "/api/cron":
            act = body.get("action") or "list"
            if act == "create":
                return self._json(CORE.cron_create(body.get("name"), body.get("cron"), body.get("type") or "bash",
                                                   body.get("script") or "", requests=body.get("requests") or [],
                                                   prompt=body.get("prompt") or "",
                                                   timeout_seconds=body.get("timeout_seconds") or 300,
                                                   remaining=body.get("remaining"),
                                                   enabled=bool(body.get("enabled", True)),
                                                   workdir=body.get("workdir") or "",
                                                   allow_mutating=bool(body.get("allow_mutating"))))
            if act == "update":
                return self._json(CORE.cron_update(body.get("id"), body))
            if act == "delete":
                return self._json(CORE.cron_delete(body.get("id")))
            if act == "enable":
                return self._json(CORE.cron_update(body.get("id"), {"enabled": True}))
            if act == "disable":
                return self._json(CORE.cron_update(body.get("id"), {"enabled": False}))
            if act == "run_now":
                return self._json(CORE.cron_run_now(body.get("id")))
            return self._json(CORE.cron_list())
        if path == "/api/errors":
            act = body.get("action") or "record"
            if act == "record":
                return self._json(CORE.err_record(body.get("message") or "", tool=body.get("tool") or ""))
            if act == "solve":
                return self._json(CORE.err_solve(body.get("id"), body.get("title") or "",
                                                 steps=body.get("steps") or [], code_snippet=body.get("code") or ""))
            if act == "feedback":
                return self._json(CORE.err_feedback(body.get("id"), body.get("solution_id"),
                                                    success=bool(body.get("success", True))))
            if act == "delete":
                return self._json(CORE.err_delete(body.get("id")))
            return self._json(CORE.err_stats())
        if path == "/api/subagents/run":
            task = (body.get("task") or "").strip()
            if not task:
                return self._json({"ok": False, "error": "缺少 task"})
            rec = CORE.subagent_start(task, name=body.get("name") or "",
                                      readonly=bool(body.get("readonly", True)),
                                      max_steps=int(body.get("max_steps") or 24),
                                      timeout=int(body.get("timeout") or 600))
            return self._json({"ok": True, "sub": rec})
        if path == "/api/contracts":
            import picontract
            act = str(body.get("action") or "create").lower()
            if act == "create":
                return self._json(picontract.create(body.get("goal"), body.get("evals"),
                                                    body.get("allow"), body.get("deny"), body.get("cost")))
            if act == "sign":
                return self._json(picontract.sign(body.get("id")))
            if act == "start":
                return self._json(picontract.start(body.get("id")))
            if act == "guard":
                return self._json(picontract.guard(body.get("id"), body.get("targets")))
            if act == "amend":
                return self._json(picontract.amend(body.get("id"), body.get("delta") or "",
                                                   body.get("goal"), body.get("allow"), body.get("deny")))
            if act == "check":
                results = body.get("results")
                if not results and body.get("all_pass"):
                    c = picontract.get(body.get("id")) or {}
                    results = [{"k": e.get("k"), "status": "pass"} for e in (c.get("evals") or [])]
                return self._json(picontract.check(body.get("id"), results))
            if act == "settle":
                return self._json(picontract.settle(body.get("id"), body.get("ai"), body.get("user"),
                                                    body.get("notes") or body.get("note") or ""))
            if act == "breach":
                return self._json(picontract.breach(body.get("id"), body.get("reason") or ""))
            if act == "report":
                return self._json(picontract.report(body.get("id")))
            if act == "score":
                return self._json(picontract.score(body.get("id"), body.get("clarity"), body.get("accuracy")))
            if act == "terminate":
                return self._json(picontract.terminate(body.get("id")))
            if act == "delete":
                db = picontract._load()
                db["items"] = [c for c in db["items"] if c.get("id") != body.get("id")]
                picontract._save(db)
                return self._json({"ok": True, "text": "已删除契约 " + str(body.get("id"))})
            return self._json({"ok": False, "error": "未知契约动作：" + act})
        if path == "/api/plan":
            import piplan
            s = HUB.session(body.get("session") or "") or (HUB.sessions[0] if HUB.sessions else None)
            ctx = {"session": s} if s is not None else None
            act = str(body.get("action") or "get").lower()
            if act == "set":
                return self._json(piplan.set_plan(ctx, body.get("goal"), body.get("items") or []))
            if act == "add":
                return self._json(piplan.add(ctx, body.get("t") or body.get("text") or ""))
            if act in ("done", "doing", "pending"):
                return self._json(piplan.mark(ctx, body.get("id"), act))
            if act == "clear":
                return self._json(piplan.clear(ctx))
            if act == "orchestrate":
                r = piplan.orchestrate(ctx, body.get("tasks"),
                                       readonly=bool(body.get("readonly", True)),
                                       limit=int(body.get("limit") or 3))
                if s is not None:
                    HUB.save()
                return self._json(r)
            return self._json(piplan.state(ctx) if s is not None else piplan.state())
        if path == "/api/approvals":
            import pipolicy
            act = str(body.get("action") or "list").lower()
            if act == "add":
                return self._json(pipolicy.add_rule(body.get("tool"), body.get("pattern"),
                                                    body.get("rule_action") or body.get("mode") or "allow",
                                                    body.get("note")))
            if act == "delete":
                return self._json(pipolicy.del_rule(body.get("id")))
            if act == "auto":
                on = bool(body.get("on"))
                pipolicy.set_auto(on)
                HUB.cfg.setdefault("prefs", {})["policy_auto"] = on
                E.save_config(HUB.cfg)
                return self._json({"ok": True, "auto": on,
                                   "text": "自动确认已%s" % ("开启（写操作免审批）" if on else "关闭")})
            if act == "history":
                return self._json(pipolicy.history(body.get("limit") or 80))
            if act == "clear":
                return self._json(pipolicy.clear_history())
            return self._json(pipolicy.state(HUB.cfg))
        if path == "/api/apis":
            import piapi
            act = str(body.get("action") or "list").lower()
            if act in ("add", "update", "upsert"):
                return self._json(piapi.upsert(body.get("spec") or body))
            if act == "delete":
                return self._json(piapi.delete_api(body.get("id")))
            if act == "test":
                return self._json(piapi.test_api(body.get("spec") or body.get("id"), body.get("args")))
            if act == "call":
                return self._json(piapi.call_api(body.get("id") or body.get("tool"), body.get("args")))
            return self._json(piapi.list_apis())
        if path == "/api/fs/write":
            s = HUB.session(body.get("session_id") or "") or (HUB.sessions[0] if HUB.sessions else None)
            ctx = self._fsctx(s)
            if s is not None:
                ctx["checkpoint"] = lambda p_, t_, b_, a_, m_: E.make_checkpoint(s, p_, t_, b_, a_, m_)
            r = E.run_tool("fs_write", {"path": self._abspath(body.get("path")), "content": body.get("text") or ""}, ctx)
            if s is not None:
                HUB.save()
            return self._json({"ok": bool(r.get("ok")), "result": r,
                               "checkpoints": len(E.checkpoints(s)) if s is not None else 0})
        if path == "/api/fs/mkdir":
            try:
                p = E.resolve_path(self._fsctx(), self._abspath(body.get("path")))
                os.makedirs(p, exist_ok=True)
                return self._json({"ok": True, "path": p})
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        if path == "/api/fs/rename":
            try:
                ctx = self._fsctx()
                src = E.resolve_path(ctx, self._abspath(body.get("path")))
                dst = E.resolve_path(ctx, self._abspath(body.get("to")))
                if os.path.exists(dst):
                    return self._json({"ok": False, "error": "目标已存在：" + dst})
                shutil.move(src, dst)
                return self._json({"ok": True, "path": dst})
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        if path == "/api/fs/delete":
            try:
                ctx = self._fsctx()
                s = HUB.session(body.get("session_id") or "")
                p = E.resolve_path(ctx, self._abspath(body.get("path")))
                if os.path.isdir(p):
                    if not body.get("confirm"):
                        return self._json({"ok": False, "error": "删除目录需要 confirm=true"})
                    shutil.rmtree(p)
                    return self._json({"ok": True, "deleted": p, "dir": True, "rollbackable": False})
                before = None
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        before = f.read()
                except Exception:
                    before = None
                rollbackable = bool(s is not None and before is not None)
                if rollbackable:
                    E.make_checkpoint(s, p, "fs_delete", before, None, "delete")
                    HUB.save()
                os.remove(p)
                return self._json({"ok": True, "deleted": p, "dir": False, "rollbackable": rollbackable})
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        if path == "/api/shutdown":
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._json({"ok": True})
        if path.startswith("/api/plugins/"):
            import piplugins
            act = path.rsplit("/", 1)[-1]
            if act == "reload":
                piplugins.load_all()
                st = piplugins.state()
                st["text"] = "插件已重载：%d 个插件 / %d 个工具" % (st["total"], st["tools"])
                return self._json(st)
            if act == "enable":
                return self._json(piplugins.set_enabled(body.get("id"), True))
            if act == "disable":
                return self._json(piplugins.set_enabled(body.get("id"), False))
            if act == "delete":
                return self._json(piplugins.delete_plugin(body.get("id")))
            if act == "create_example":
                return self._json(piplugins.create_example(body.get("id") or "skill-caller",
                                                           body.get("skill") or "",
                                                           overwrite=bool(body.get("overwrite"))))
            if act == "open_root":
                return self._json(piplugins.open_root())
            if act == "call":
                return self._json(piplugins.call_tool(body.get("tool"), body.get("args_json")))
            return self._json({"ok": False, "error": "未知插件动作：" + act}, 404)
        return self._json({"ok": False, "error": "unknown POST " + path}, 404)

    def state_payload(self):
        prov = dict(HUB.cfg.get("provider") or {})
        prov_safe = {k: v for k, v in prov.items() if k != "api_key"}
        prov_safe["api_key_set"] = bool((prov.get("api_key") or "").strip())
        prov_safe["api_key_masked"] = E.mask_key(prov.get("api_key"))
        prov_safe["base_url"] = E.norm_base(prov.get("base_url"))
        core = CORE.state()
        try:
            import picontract, pipolicy, piapi, piplan
            core["contracts"] = picontract.state()
            core["approvals"] = {"auto": pipolicy.auto(HUB.cfg),
                                 "rules": len(pipolicy.state().get("rules") or [])}
            core["apis"] = {"total": len(piapi.list_apis().get("items") or [])}
            s0 = HUB.sessions[0] if HUB.sessions else None
            core["plan"] = piplan.state({"session": s0} if s0 is not None else None)
        except Exception:
            pass
        return {
            "ok": True,
            "version": E.VERSION,
            "workspace": HUB.cfg.get("workspace"),
            "provider": prov_safe,
            "prefs": {k: v for k, v in (HUB.cfg.get("prefs") or {}).items() if k != "system"},
            "system_prompt": (HUB.cfg.get("prefs") or {}).get("system") or E.DEFAULT_SYSTEM,
            "profiles": [{"name": p.get("name"), "base_url": p.get("base_url"), "model": p.get("model"),
                          "has_key": bool((p.get("api_key") or "").strip())} for p in (HUB.cfg.get("profiles") or [])],
            "model_meta": prov.get("model_meta") or {},
            "ctx": E.ctx_state(HUB.cfg, HUB.sessions[0] if HUB.sessions else E.new_session()),
            "tools": [{"name": t["name"], "group": t["group"], "desc": t["desc"], "mutating": t["mutating"],
                       "parameters": t["parameters"]} for t in E.TOOLS.values()],
            "sessions": [{"id": s["id"], "title": s.get("title"), "updated": s.get("updated"),
                          "messages": len(s.get("messages") or []),
                          "checkpoints": len(E.checkpoints(s))} for s in HUB.sessions],
            "paths": {"home": E.HOME, "config": E.CONFIG_PATH, "sessions": E.SESSIONS_PATH,
                      "log": E.LOG_PATH, "events": E.EVENTS_PATH},
            "core": core,
            "qa": CORE.qa_state(HUB.cfg),
            "qa_demo": CORE.qa_demo(),
        }

    def _stream(self, rid, q, cancel):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(obj):
            try:
                self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                return True
            except Exception:
                return False
        if not send({"kind": "run", "run_id": rid}):
            cancel.set()
            return
        last = time.time()
        while True:
            try:
                kind, data = q.get(timeout=0.5)
            except queue.Empty:
                if not send({"kind": "ping", "ts": time.time()}):
                    cancel.set()          # 客户端（浏览器）已断开：终止运行，避免它卡在审批等待上
                    break
                if time.time() - last > 900:
                    cancel.set()
                    break
                continue
            last = time.time()
            if kind == "__end__":
                send({"kind": "done", "result": {
                    k: v for k, v in data.items() if k != "schema"}})
                break
            try:
                ok = send({"kind": kind, "data": data})
            except TypeError:
                ok = send({"kind": kind, "data": str(data)})
            if not ok:
                cancel.set()
                break
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass
        with HUB.lock:
            HUB.runs.pop(rid, None)


def serve(port=None, open_browser=True, token=None, host="127.0.0.1"):
    global HUB
    E.ensure_home()
    HUB = Hub()
    if token:
        HUB.token = token

    def cron_session(marker, title):
        with HUB.lock:
            for s in HUB.sessions:
                if s.get("cron_marker") == marker:
                    return s
            s = E.new_session(title)
            s["cron_marker"] = marker
            HUB.sessions.insert(0, s)
        HUB.save()
        return s

    CORE.init(HUB.cfg, enable_scheduler=True, session_provider=cron_session, session_saver=HUB.save)
    port = port or free_port()
    url = "http://%s:%d/?t=%s" % (host, port, HUB.token)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    E.log("info", "serve", "PI Studio %s 后端已启动 %s" % (E.VERSION, url))
    print("=" * 78)
    print(" PI Studio %s · 后端已启动" % E.VERSION)
    print(" 浏览器打开： %s" % url)
    print(" 模型： %s  端点： %s" % ((HUB.cfg.get("provider") or {}).get("model") or "未选择",
                                     E.norm_base((HUB.cfg.get("provider") or {}).get("base_url"))))
    print(" 工作区： %s" % HUB.cfg.get("workspace"))
    print(" 数据目录： %s" % E.HOME)
    print(" 停止：Ctrl+C   （或用接口 POST /api/shutdown）")
    print("=" * 78)
    print("（令牌已包含在上面的网址里；直接把这个网址贴到浏览器即可，不要只打开 localhost:%d）" % port)
    sys.stdout.flush()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as e:
            E.log("warn", "serve", "无法自动打开浏览器：%s" % e)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
        try:
            CORE.shutdown()
        except Exception:
            pass
        E.log("info", "serve", "后端已停止")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port = None
    token = None
    no_browser = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
            continue
        if a == "--token" and i + 1 < len(argv):
            token = argv[i + 1]
            i += 2
            continue
        if a == "--no-browser":
            no_browser = True
        i += 1
    return serve(port, not no_browser, token)


if __name__ == "__main__":
    sys.exit(main() or 0)
