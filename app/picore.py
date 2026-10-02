# -*- coding: utf-8 -*-
"""PI Core：核心能力层（记忆 / 技能 / MCP / 调度 / 错误知识库 / 账本 / 子代理）。
对应核心 PI 基座清单 D/F 组（14 记忆基座、13 Skills、15 MCP、16 调度、25 错误库、12 多代理、19 可观测性）。
本模块把能力注册进 piengine 的真实工具表，并提供 REST 层数据与自检。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import piengine as E

CORE_DIR = os.path.join(E.HOME, "core")
MEM_DIR = os.path.join(CORE_DIR, "memory")
SKILL_DIR = os.path.join(CORE_DIR, "skills")
SUB_DIR = os.path.join(CORE_DIR, "subagents")
MCP_PATH = os.path.join(CORE_DIR, "mcp.json")
CRON_PATH = os.path.join(CORE_DIR, "cron.json")
CRON_LOG = os.path.join(CORE_DIR, "cron-journal.jsonl")
ERR_PATH = os.path.join(CORE_DIR, "errors.json")
LEDGER_PATH = os.path.join(CORE_DIR, "ledger.jsonl")

# 自检用最小 MCP stdio 服务器（不依赖任何第三方包）：验证 initialize / tools/list / tools/call 往返。
MCP_SELFTEST_SRC = '''# -*- coding: utf-8 -*-
"""由 PI core 自检临时写出：最小 MCP stdio 服务器。"""
import json, sys

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\\n")
    sys.stdout.flush()

TOOLS = [{"name": "echo", "description": "回显 text",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
         {"name": "sum", "description": "两数相加",
          "inputSchema": {"type": "object", "properties": {"a": {"type": "number"},
                                                           "b": {"type": "number"}}}}]

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05",
              "capabilities": {"tools": {}}, "serverInfo": {"name": "pi-selftest", "version": "1.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        a = params.get("arguments") or {}
        if params.get("name") == "sum":
            text = "SUM:%s" % (float(a.get("a", 0)) + float(a.get("b", 0)))
        else:
            text = "ECHO:" + str(a.get("text", ""))
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "未知方法"}})
'''

LOCK = threading.RLock()
_CFG = {}
_SESSION_PROVIDER = None
_SESSION_SAVER = None
_SCHED_STOP = threading.Event()
_SCHED_STARTED = False
_MCP = {}
_MCP_LOCK = threading.RLock()
_SUBS = {}


def _dirs():
    for d in (CORE_DIR, MEM_DIR, SKILL_DIR, SUB_DIR):
        os.makedirs(d, exist_ok=True)


def _now():
    return time.time()


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    _dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _append_jsonl(path, obj, keep=600):
    _dirs()
    with LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        try:
            if os.path.getsize(path) > 512 * 1024:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-keep:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(lines)
        except Exception:
            pass


def _read_jsonl(path, n=200):
    if not os.path.isfile(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out[-n:]


# ============================================================ 记忆基座

def _mem_index():
    return _read_json(os.path.join(MEM_DIR, "index.json"), {"items": []})


def _mem_save_index(idx):
    _write_json(os.path.join(MEM_DIR, "index.json"), idx)


def _slug_ok(s):
    s = str(s or "").strip()
    s = re.sub(r"[\s/\\:*?\"<>|]+", "-", s)
    s = s.strip(".-")
    return s[:60]


def mem_write(mtype, slug, description, body, tags=None, confidence="medium", source_quote=""):
    _dirs()
    slug = _slug_ok(slug)
    if not slug:
        return {"ok": False, "error": "slug 不能为空"}
    mtype = mtype if mtype in ("user", "feedback", "project", "reference", "daily") else "project"
    path = os.path.join(MEM_DIR, slug + ".md")
    with LOCK:
        idx = _mem_index()
        item = next((x for x in idx["items"] if x.get("slug") == slug), None)
        if item is None:
            item = {"slug": slug, "type": mtype, "ts": _now(), "created": _now(),
                    "description": "", "tags": [], "confidence": "", "source_quote": ""}
            idx["items"].append(item)
        item.update({"type": mtype, "description": str(description or "")[:120], "updated": _now(),
                     "tags": list(tags or item.get("tags") or [])[:8], "confidence": confidence or "medium",
                     "source_quote": str(source_quote or "")[:80]})
        with open(path, "w", encoding="utf-8") as f:
            f.write("# %s\n\n%s\n" % (item["description"] or slug, str(body or "")))
        _mem_save_index(idx)
    E.log("info", "memory", "记忆写入 %s（%s）" % (slug, mtype))
    return {"ok": True, "slug": slug, "path": path}


def mem_read(slug):
    slug = _slug_ok(slug)
    path = os.path.join(MEM_DIR, slug + ".md")
    if not os.path.isfile(path):
        return {"ok": False, "error": "记忆不存在：" + slug}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return {"ok": True, "slug": slug, "text": f.read()}


def mem_update(slug, body=None, description=None, append=None):
    slug = _slug_ok(slug)
    r = mem_read(slug)
    if not r.get("ok"):
        return r
    text = r["text"]
    if append:
        text = text.rstrip() + "\n\n" + str(append)
    if body is not None:
        head = text.split("\n", 2)
        text = (head[0] + "\n\n" + str(body)) if head and head[0].startswith("# ") else str(body)
    with LOCK:
        with open(os.path.join(MEM_DIR, slug + ".md"), "w", encoding="utf-8") as f:
            f.write(text)
        idx = _mem_index()
        item = next((x for x in idx["items"] if x.get("slug") == slug), None)
        if item is not None:
            item["updated"] = _now()
            if description:
                item["description"] = str(description)[:120]
            _mem_save_index(idx)
    return {"ok": True, "slug": slug}


def mem_delete(slug):
    slug = _slug_ok(slug)
    with LOCK:
        idx = _mem_index()
        n = len(idx["items"])
        idx["items"] = [x for x in idx["items"] if x.get("slug") != slug]
        if len(idx["items"]) == n:
            return {"ok": False, "error": "记忆不存在：" + slug}
        _mem_save_index(idx)
        try:
            os.remove(os.path.join(MEM_DIR, slug + ".md"))
        except Exception:
            pass
    E.log("info", "memory", "记忆删除 %s" % slug)
    return {"ok": True}


def mem_list(limit=50):
    idx = _mem_index()
    items = sorted(idx["items"], key=lambda x: x.get("updated") or x.get("ts") or 0, reverse=True)
    return {"ok": True, "items": items[:limit], "total": len(idx["items"])}


def mem_search(q, limit=8):
    q = str(q or "").strip().lower()
    idx = _mem_index()
    scored = []
    for it in idx["items"]:
        score = 0
        slug = (it.get("slug") or "").lower()
        desc = (it.get("description") or "").lower()
        if q and q in slug:
            score += 5
        if q and q in desc:
            score += 3
        body = ""
        try:
            with open(os.path.join(MEM_DIR, it["slug"] + ".md"), "r", encoding="utf-8", errors="replace") as f:
                body = f.read().lower()
        except Exception:
            pass
        if q:
            c = body.count(q)
            if c:
                score += min(4, c)
            if score == 0 and any(w and w in (slug + desc + body) for w in q.split()):
                score = 1
        else:
            score = 1
        if score > 0:
            scored.append((score, it, body))
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, it, body in scored[:limit]:
        pos = body.find(q) if q else 0
        frag = body[max(0, pos - 60):pos + 140].replace("\n", " ") if pos >= 0 else body[:160].replace("\n", " ")
        out.append({"slug": it.get("slug"), "type": it.get("type"), "description": it.get("description"),
                    "updated": it.get("updated") or it.get("ts"), "score": score, "fragment": frag.strip()})
    return {"ok": True, "items": out, "query": q}


def mem_stats():
    idx = _mem_index()
    by = {}
    for it in idx["items"]:
        by[it.get("type") or "?"] = by.get(it.get("type") or "?", 0) + 1
    return {"ok": True, "total": len(idx["items"]), "by_type": by,
            "dir": MEM_DIR, "latest": sorted(idx["items"], key=lambda x: x.get("updated") or x.get("ts") or 0,
                                             reverse=True)[:5]}


# ============================================================ 技能基座

def _skill_meta(name):
    path = os.path.join(SKILL_DIR, name, "SKILL.md")
    if not os.path.isfile(path):
        return None
    desc = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        m = re.search(r"^description:\s*(.+)$", text, re.M)
        if m:
            desc = m.group(1).strip()
        else:
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("---") and not line.startswith("name:"):
                    desc = line[:100]
                    break
    except Exception:
        pass
    return {"name": name, "description": desc, "path": path,
            "bytes": os.path.getsize(path), "files": sorted(os.listdir(os.path.join(SKILL_DIR, name)))[:20]}


def skills_list():
    _dirs()
    out = []
    for name in sorted(os.listdir(SKILL_DIR)):
        d = os.path.join(SKILL_DIR, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "SKILL.md")):
            m = _skill_meta(name)
            if m:
                out.append(m)
    return {"ok": True, "items": out, "dir": SKILL_DIR}


def skill_read(name):
    name = _slug_ok(name)
    path = os.path.join(SKILL_DIR, name, "SKILL.md")
    if not os.path.isfile(path):
        return {"ok": False, "error": "技能不存在：" + name}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return {"ok": True, "name": name, "text": f.read()}


def skill_create(name, description, body, files=None):
    name = _slug_ok(name)
    if not name:
        return {"ok": False, "error": "技能名不能为空"}
    d = os.path.join(SKILL_DIR, name)
    os.makedirs(d, exist_ok=True)
    text = "---\nname: %s\ndescription: %s\n---\n\n%s\n" % (name, str(description or "")[:150], str(body or ""))
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(text)
    for rel, content in (files or {}).items() if isinstance(files, dict) else []:
        rel = str(rel).replace("\\", "/").lstrip("/")
        if ".." in rel:
            continue
        fp = os.path.join(d, rel)
        os.makedirs(os.path.dirname(fp) or d, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(str(content))
    E.log("info", "skills", "技能创建 %s" % name)
    return {"ok": True, "name": name, "path": os.path.join(d, "SKILL.md")}


def skill_delete(name):
    name = _slug_ok(name)
    d = os.path.join(SKILL_DIR, name)
    if not os.path.isdir(d):
        return {"ok": False, "error": "技能不存在：" + name}
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    E.log("info", "skills", "技能删除 %s" % name)
    return {"ok": True}


# ============================================================ MCP 基座

def mcp_load():
    cfg = _read_json(MCP_PATH, None)
    if cfg is None:
        cfg = {"servers": []}
        _write_json(MCP_PATH, cfg)
    if isinstance(cfg, dict) and "servers" not in cfg:
        cfg = {"servers": []}
    return cfg


def mcp_save(cfg):
    _write_json(MCP_PATH, cfg)


class McpServer(object):
    def __init__(self, spec):
        self.spec = spec
        self.id = spec.get("id") or ""
        self.proc = None
        self.tools = []
        self.status = "stopped"
        self.error = ""
        self._lock = threading.RLock()
        self._idn = 0
        self._wait = {}
        self._reader = None
        self._session_id = None

    # ---------- stdio ----------
    def _reader_loop(self):
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                mid = msg.get("id")
                if mid is not None and mid in self._wait:
                    self._wait[mid].append(msg)
        except Exception:
            pass

    def _stdio_request(self, method, params, timeout=30):
        with self._lock:
            self._idn += 1
            mid = self._idn
            self._wait[mid] = []
            payload = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
                                 ensure_ascii=False) + "\n"
            self.proc.stdin.write(payload.encode("utf-8"))
            self.proc.stdin.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self._wait.get(mid):
                    msg = self._wait.pop(mid)[0]
                    if "error" in msg:
                        raise RuntimeError("MCP 错误：" + json.dumps(msg["error"], ensure_ascii=False)[:300])
                    return msg.get("result")
                if self.proc.poll() is not None:
                    raise RuntimeError("MCP 进程已退出（code %s）" % self.proc.returncode)
                time.sleep(0.03)
            raise RuntimeError("MCP 请求超时：%s" % method)

    def _stdio_notify(self, method, params=None):
        try:
            text = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}, ensure_ascii=False) + "\n"
            self.proc.stdin.write(text.encode("utf-8"))
            self.proc.stdin.flush()
        except Exception:
            pass

    # ---------- http ----------
    def _http_request(self, method, params, timeout=30):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                          ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        headers.update(self.spec.get("headers") or {})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(self.spec.get("url"), data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            ctype = r.headers.get("Content-Type") or ""
            raw = r.read().decode("utf-8", "replace")
        if "text/event-stream" in ctype:
            for chunk in raw.split("\n\n"):
                for line in chunk.splitlines():
                    if line.startswith("data: "):
                        try:
                            msg = json.loads(line[6:])
                        except Exception:
                            continue
                        if "error" in msg:
                            raise RuntimeError("MCP 错误：" + json.dumps(msg["error"], ensure_ascii=False)[:300])
                        if "result" in msg:
                            return msg["result"]
            raise RuntimeError("MCP SSE 响应中没有 result")
        msg = json.loads(raw)
        if isinstance(msg, dict) and "error" in msg:
            raise RuntimeError("MCP 错误：" + json.dumps(msg["error"], ensure_ascii=False)[:300])
        return msg.get("result") if isinstance(msg, dict) else msg

    # ---------- 对外 ----------
    def start(self, timeout=30):
        with self._lock:
            if self.status == "running":
                return True
            try:
                if self.spec.get("transport") == "http":
                    self._http_request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                      "clientInfo": {"name": "pi-studio", "version": E.VERSION}}, timeout)
                else:
                    cmd = [self.spec.get("command") or ""] + list(self.spec.get("args") or [])
                    env = dict(os.environ)
                    env.update(self.spec.get("env") or {})
                    cwd = self.spec.get("cwd") or _CFG.get("workspace") or os.path.expanduser("~")
                    self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.DEVNULL, env=env, cwd=cwd)
                    threading.Thread(target=self._reader_loop, daemon=True).start()
                    self._stdio_request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                       "clientInfo": {"name": "pi-studio", "version": E.VERSION}}, timeout)
                    self._stdio_notify("notifications/initialized")
                self.status = "running"
                self.error = ""
                self.refresh_tools(timeout)
                E.log("info", "mcp", "MCP 服务器已连接：%s（%d 个工具）" % (self.id, len(self.tools)))
                return True
            except Exception as e:
                self.status = "error"
                self.error = "%s: %s" % (type(e).__name__, e)
                E.log("warn", "mcp", "MCP 启动失败 %s：%s" % (self.id, self.error))
                self.stop()
                return False

    def refresh_tools(self, timeout=30):
        if self.status != "running":
            return []
        if self.spec.get("transport") == "http":
            res = self._http_request("tools/list", {}, timeout)
        else:
            res = self._stdio_request("tools/list", {}, timeout)
        self.tools = (res or {}).get("tools") or []
        return self.tools

    def call(self, tool, args, timeout=120):
        if self.status != "running":
            if not self.start():
                raise RuntimeError("MCP 服务器不可用：" + (self.error or self.id))
        if self.spec.get("transport") == "http":
            res = self._http_request("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        else:
            res = self._stdio_request("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        parts = []
        for c in ((res or {}).get("content") or []):
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text") or ""))
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        text = "\n".join(parts) if parts else json.dumps(res, ensure_ascii=False)[:2000]
        return {"ok": not (res or {}).get("isError"), "text": text}

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        self.proc = None
        self.status = "stopped"


def _safe_id(s):
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s or ""))[:40]


def _mcp_remove_tools(server_id):
    for name in [n for n in E.TOOLS.keys() if n.startswith("mcp_") and E.TOOLS[n].get("server") == server_id]:
        E.TOOLS.pop(name, None)


def _mcp_caller(server_id, tool_name):
    def fn(a, ctx):
        try:
            r = _MCP[server_id].call(tool_name, a or {}, timeout=180)
            return r
        except Exception as e:
            return {"ok": False, "text": "%s: %s" % (type(e).__name__, e)}
    return fn


def mcp_reload():
    servers = mcp_load().get("servers") or []
    with _MCP_LOCK:
        for sid in list(_MCP.keys()):
            _MCP[sid].stop()
            _mcp_remove_tools(sid)
            _MCP.pop(sid, None)
        out = []
        for spec in servers:
            if not spec.get("id"):
                continue
            if not spec.get("enabled", True):
                out.append({"id": spec["id"], "status": "disabled", "tools": 0})
                continue
            s = McpServer(spec)
            _MCP[spec["id"]] = s
            ok = s.start()
            if ok:
                for t in s.tools:
                    name = "mcp_%s_%s" % (_safe_id(spec["id"]), _safe_id(t.get("name")))
                    ann = t.get("annotations") or {}
                    E.TOOLS[name] = {"name": name, "group": "MCP",
                                     "desc": "[%s] %s" % (spec["id"], str(t.get("description") or t.get("name"))[:160]),
                                     "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                                     "fn": _mcp_caller(spec["id"], t.get("name")),
                                     "mutating": not bool(ann.get("readOnlyHint")),
                                     "server": spec["id"]}
            out.append({"id": spec["id"], "status": s.status, "error": s.error, "tools": len(s.tools)})
        E.log("info", "mcp", "MCP 重载完成：%d 个服务器" % len(out))
        return {"ok": True, "servers": out}


def mcp_state():
    servers = mcp_load().get("servers") or []
    out = []
    for spec in servers:
        sid = spec.get("id")
        live = _MCP.get(sid)
        out.append({"id": sid, "enabled": spec.get("enabled", True),
                    "transport": spec.get("transport") or "stdio",
                    "command": spec.get("command"), "url": spec.get("url"),
                    "status": (live.status if live else ("disabled" if not spec.get("enabled", True) else "stopped")),
                    "error": (live.error if live else ""),
                    "tools": ([{"name": t.get("name"), "desc": str(t.get("description") or "")[:120]}
                               for t in (live.tools if live else [])])})
    return {"ok": True, "servers": out, "path": MCP_PATH,
            "live_tools": len([n for n in E.TOOLS if n.startswith("mcp_")])}


def mcp_upsert(spec):
    with LOCK:
        cfg = mcp_load()
        servers = cfg.get("servers") or []
        sid = spec.get("id") or ("srv-" + uuid.uuid4().hex[:6])
        spec["id"] = sid
        for i, x in enumerate(servers):
            if x.get("id") == sid:
                servers[i] = spec
                break
        else:
            servers.append(spec)
        cfg["servers"] = servers
        mcp_save(cfg)
    return {"ok": True, "id": sid}


def mcp_delete(sid):
    with LOCK:
        cfg = mcp_load()
        cfg["servers"] = [x for x in (cfg.get("servers") or []) if x.get("id") != sid]
        mcp_save(cfg)
    with _MCP_LOCK:
        if sid in _MCP:
            _MCP[sid].stop()
            _mcp_remove_tools(sid)
            _MCP.pop(sid, None)
    return {"ok": True}


def mcp_stop_all():
    """停用全部：既要杀掉子进程，也要把对应的 mcp_* 工具从工具表摘掉。

    否则「停用」只是假停用 —— 工具仍在表里，模型一调用 McpServer.call 又会把服务器拉起来。
    """
    with _MCP_LOCK:
        for sid, s in list(_MCP.items()):
            s.stop()
            _mcp_remove_tools(sid)
    return {"ok": True, "stopped": len(_MCP),
            "text": "已停用 %d 个 MCP 服务器并释放其动态工具" % len(_MCP)}


def mcp_test_spec(spec):
    s = McpServer(spec or {})
    ok = s.start()
    tools = [{"name": t.get("name"), "desc": str(t.get("description") or "")[:120]} for t in s.tools]
    err = s.error
    s.stop()
    return {"ok": ok, "error": err, "tools": tools}


# ============================================================ 调度（Cron）

def cron_load():
    cfg = _read_json(CRON_PATH, None)
    if cfg is None:
        cfg = {"tasks": []}
        _write_json(CRON_PATH, cfg)
    return cfg


def cron_save(cfg):
    _write_json(CRON_PATH, cfg)


def _field_match(field, value, lo, hi):
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            try:
                step = max(1, int(s))
            except Exception:
                step = 1
        if part in ("*", ""):
            if (value - lo) % step == 0:
                return True
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                a, b = int(a), int(b)
            except Exception:
                continue
            if a <= value <= b and (value - a) % step == 0:
                return True
            continue
        try:
            if value == int(part):
                return True
        except Exception:
            continue
    return False


def cron_match(expr, tm=None):
    tm = tm or time.localtime()
    parts = str(expr or "").split()
    if len(parts) != 6:
        return False
    return (_field_match(parts[0], tm.tm_sec, 0, 59) and _field_match(parts[1], tm.tm_min, 0, 59)
            and _field_match(parts[2], tm.tm_hour, 0, 23) and _field_match(parts[3], tm.tm_mday, 1, 31)
            and _field_match(parts[4], tm.tm_mon, 1, 12) and _field_match(parts[5], tm.tm_wday, 0, 6))


def cron_create(name, cron, ttype="bash", script="", requests=None, prompt="", timeout_seconds=300,
                remaining=None, enabled=True, workdir="", allow_mutating=False):
    parts = str(cron or "").split()
    if len(parts) != 6:
        return {"ok": False, "error": "cron 需要 6 段：秒 分 时 日 月 周（如 0 0 9 * * *）"}
    task = {"id": uuid.uuid4().hex[:8], "name": str(name or "任务")[:60], "cron": cron,
            "type": ttype if ttype in ("bash", "http", "prompt") else "bash",
            "script": script or "", "requests": requests or [], "prompt": prompt or "",
            "timeout_seconds": int(timeout_seconds or 300), "remaining": remaining,
            "enabled": bool(enabled), "workdir": workdir or "", "allow_mutating": bool(allow_mutating),
            "created": _now(), "last_run": None, "last_result": "", "last_slot": ""}
    with LOCK:
        cfg = cron_load()
        cfg["tasks"].append(task)
        cron_save(cfg)
    E.log("info", "cron", "任务创建 %s（%s）" % (task["name"], cron))
    return {"ok": True, "task": task}


def cron_update(tid, patch):
    with LOCK:
        cfg = cron_load()
        for t in cfg["tasks"]:
            if t["id"] == tid:
                for k in ("name", "cron", "type", "script", "requests", "prompt", "timeout_seconds",
                          "remaining", "enabled", "workdir", "allow_mutating"):
                    if k in (patch or {}):
                        t[k] = patch[k]
                cron_save(cfg)
                return {"ok": True, "task": t}
    return {"ok": False, "error": "任务不存在：" + str(tid)}


def cron_delete(tid):
    with LOCK:
        cfg = cron_load()
        n = len(cfg["tasks"])
        cfg["tasks"] = [t for t in cfg["tasks"] if t["id"] != tid]
        if len(cfg["tasks"]) == n:
            return {"ok": False, "error": "任务不存在：" + str(tid)}
        cron_save(cfg)
    return {"ok": True}


def cron_list():
    cfg = cron_load()
    return {"ok": True, "tasks": cfg["tasks"], "path": CRON_PATH}


def cron_logs(tid=None, limit=100):
    rows = _read_jsonl(CRON_LOG, 400)
    if tid:
        rows = [r for r in rows if r.get("id") == tid]
    return {"ok": True, "logs": rows[-limit:]}


def _cron_exec_bash(task):
    script = str(task.get("script") or "")
    cwd = task.get("workdir") or _CFG.get("workspace") or os.path.expanduser("~")
    p = subprocess.run(script, shell=True, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=int(task.get("timeout_seconds") or 300))
    out = p.stdout or ""
    if p.stderr:
        out += "\n[stderr]\n" + p.stderr
    return p.returncode == 0, out.strip()[:4000]


def _cron_exec_http(task):
    lines = []
    ok = True
    for rq in (task.get("requests") or [])[:10]:
        try:
            data = None
            if rq.get("body") is not None:
                data = json.dumps(rq["body"]).encode("utf-8") if not isinstance(rq["body"], str) else rq["body"].encode("utf-8")
            req = urllib.request.Request(rq.get("url"), data=data,
                                         headers=rq.get("headers") or {}, method=rq.get("method") or "POST")
            with urllib.request.urlopen(req, timeout=min(60, int(task.get("timeout_seconds") or 300))) as r:
                body = r.read(2000).decode("utf-8", "replace")
                lines.append("%s %s → %s %s" % (rq.get("method") or "POST", rq.get("url"), r.status, body[:200]))
        except Exception as e:
            ok = False
            lines.append("%s → %s: %s" % (rq.get("url"), type(e).__name__, e))
    return ok, "\n".join(lines)


def _cron_exec_prompt(task):
    if _SESSION_PROVIDER is None:
        return False, "无会话提供器（未初始化），跳过 prompt 任务"
    s = _SESSION_PROVIDER("cron:" + task["id"], "⏱ " + (task.get("name") or "定时任务"))
    s.setdefault("messages", []).append({"role": "user", "content": str(task.get("prompt") or ""), "ts": _now()})
    allow = bool(task.get("allow_mutating"))
    approve = (lambda name, args, meta: True) if allow else (lambda name, args, meta: False)
    r = E.run_turn(dict(_CFG), s, lambda k, d: None, None, approve)
    if _SESSION_SAVER is not None:
        try:
            _SESSION_SAVER()
        except Exception:
            pass
    text = (r.get("text") or r.get("error") or "")[:2000]
    return bool(r.get("ok")), text


def cron_run_now(tid):
    t = next((x for x in cron_load()["tasks"] if x["id"] == tid), None)
    if not t:
        return {"ok": False, "error": "任务不存在：" + str(tid)}
    threading.Thread(target=_cron_fire, args=(t, "manual"), daemon=True).start()
    return {"ok": True, "started": tid}


def _cron_fire(task, trigger):
    t0 = time.time()
    try:
        if task.get("type") == "http":
            ok, text = _cron_exec_http(task)
        elif task.get("type") == "prompt":
            ok, text = _cron_exec_prompt(task)
        else:
            ok, text = _cron_exec_bash(task)
    except Exception as e:
        ok, text = False, "%s: %s" % (type(e).__name__, e)
        E.log("error", "cron", "任务 %s 异常：%s" % (task.get("name"), traceback.format_exc()))
    rec = {"ts": _now(), "id": task.get("id"), "name": task.get("name"), "type": task.get("type"),
           "trigger": trigger, "ok": ok, "text": text[:3000], "ms": int((time.time() - t0) * 1000)}
    _append_jsonl(CRON_LOG, rec)
    with LOCK:
        cfg = cron_load()
        for t in cfg["tasks"]:
            if t["id"] == task.get("id"):
                t["last_run"] = rec["ts"]
                t["last_result"] = ("✔ " if ok else "✘ ") + text[:400]
                if isinstance(t.get("remaining"), int) and trigger == "schedule":
                    t["remaining"] = max(0, t["remaining"] - 1)
                    if t["remaining"] == 0:
                        t["enabled"] = False
                t["last_slot"] = task.get("last_slot") or t.get("last_slot") or ""
                break
        cron_save(cfg)
    E.log("info", "cron", "任务 %s %s（%sms）" % (task.get("name"), "完成" if ok else "失败", rec["ms"]))


_RUNNING = set()


def _scheduler_loop():
    while not _SCHED_STOP.is_set():
        try:
            tm = time.localtime()
            slot = time.strftime("%Y%m%d%H%M%S", tm)
            if _CFG.get("core_cron_enabled", True):
                cfg = cron_load()
                for task in cfg.get("tasks") or []:
                    if not task.get("enabled"):
                        continue
                    if isinstance(task.get("remaining"), int) and task["remaining"] <= 0:
                        continue
                    if not cron_match(task.get("cron"), tm):
                        continue
                    if task.get("last_slot") == slot:
                        continue
                    if task["id"] in _RUNNING:
                        continue
                    task["last_slot"] = slot
                    with LOCK:
                        c2 = cron_load()
                        for t in c2["tasks"]:
                            if t["id"] == task["id"]:
                                t["last_slot"] = slot
                                if isinstance(t.get("remaining"), int):
                                    pass
                                break
                        cron_save(c2)
                    _RUNNING.add(task["id"])
                    threading.Thread(target=_cron_fire_guarded, args=(task, slot), daemon=True).start()
        except Exception as e:
            E.log("error", "cron", "调度循环：%s" % e)
        time.sleep(1.0)


def _cron_fire_guarded(task, slot):
    try:
        _cron_fire(task, "schedule")
    finally:
        _RUNNING.discard(task["id"])


def start_scheduler():
    global _SCHED_STARTED
    if _SCHED_STARTED:
        return
    _SCHED_STARTED = True
    _SCHED_STOP.clear()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    E.log("info", "cron", "调度器已启动（1 秒节拍）")


def stop_scheduler():
    _SCHED_STOP.set()


# ============================================================ 错误知识库

def _err_fp(message):
    m = str(message or "").lower()
    m = re.sub(r"\d+", "#", m)
    m = re.sub(r"\s+", " ", m).strip()[:200]
    return hashlib.sha1(m.encode("utf-8")).hexdigest()[:12]


def err_load():
    return _read_json(ERR_PATH, {"errors": []})


def err_save(db):
    _write_json(ERR_PATH, db)


def err_record(message, tool="", context=None, error_type="tool_error"):
    fp = _err_fp(message)
    with LOCK:
        db = err_load()
        item = next((x for x in db["errors"] if x.get("fp") == fp), None)
        if item is None:
            item = {"id": uuid.uuid4().hex[:8], "fp": fp, "message": str(message)[:1500],
                    "tool": tool or "", "error_type": error_type, "count": 0,
                    "first_ts": _now(), "last_ts": _now(), "resolved": False, "solutions": []}
            db["errors"].append(item)
        item["count"] += 1
        item["last_ts"] = _now()
        if context:
            item.setdefault("contexts", [])
            item["contexts"] = (item["contexts"] + [context])[-5:]
        err_save(db)
    return {"ok": True, "id": item["id"], "count": item["count"]}


def err_search(query="", resolved=None, limit=8):
    db = err_load()
    q = str(query or "").lower()
    out = []
    for x in db["errors"]:
        if resolved is not None and bool(x.get("resolved")) != resolved:
            continue
        hay = (x.get("message") or "") + " " + (x.get("tool") or "") + " " + " ".join(
            s.get("title") or "" for s in (x.get("solutions") or []))
        if q and q not in hay.lower():
            continue
        out.append({"id": x["id"], "message": (x.get("message") or "")[:300], "tool": x.get("tool"),
                    "count": x.get("count"), "resolved": x.get("resolved"),
                    "last_ts": x.get("last_ts"),
                    "solutions": [{"id": s.get("id"), "title": s.get("title"),
                                   "ok": s.get("success_count", 0), "fail": s.get("fail_count", 0),
                                   "steps": list(s.get("steps") or []),
                                   "code_snippet": s.get("code_snippet") or "",
                                   "source": s.get("source") or ""}
                                  for s in (x.get("solutions") or [])]})
    out.sort(key=lambda x: -(x.get("last_ts") or 0))
    return {"ok": True, "items": out[:limit], "total": len(db["errors"])}


def err_hint(tool="", message="", limit=2):
    """工具失败自动召回：先按指纹精确命中，再回退到同工具最近的含方案记录。
    返回可直接拼进工具结果的「已知修复」提示文本；无命中返回 None。"""
    try:
        db = err_load()
    except Exception:
        return None
    errs = db.get("errors") or []
    fp = _err_fp(message)
    item = next((x for x in errs if x.get("fp") == fp and x.get("solutions")), None)
    if item is None and tool:
        cand = [x for x in errs if x.get("tool") == tool and x.get("solutions")]
        cand.sort(key=lambda x: -(x.get("last_ts") or 0))
        item = cand[0] if cand else None
    if item is None:
        return None
    sols = sorted(item.get("solutions") or [],
                  key=lambda s: -((s.get("success_count") or 0) - (s.get("fail_count") or 0)))
    lines = []
    for s in sols[:max(1, int(limit or 1))]:
        lines.append("  · 方案《%s》（成功 %d / 失败 %d，来源 %s）"
                     % (s.get("title"), s.get("success_count", 0), s.get("fail_count", 0),
                        s.get("source") or "-"))
        for st in (s.get("steps") or [])[:6]:
            lines.append("      - " + str(st))
        if s.get("code_snippet"):
            lines.append("      代码：" + str(s.get("code_snippet"))[:400].replace("\n", "\n      "))
    if not lines:
        return None
    return ("[错误知识库 · 自动召回] 同类失败曾在 #%s 出现过 %d 次，历史已验证的修复如下：\n%s\n"
            "（若下面的修复有效，请调用 error_kb feedback 记一次成功以强化排序）"
            % (item.get("id"), item.get("count", 0), "\n".join(lines)))


def err_solve(eid, title, steps=None, code_snippet="", source="ai_generated", mark_resolved=True):
    with LOCK:
        db = err_load()
        item = next((x for x in db["errors"] if x["id"] == eid), None)
        if item is None:
            return {"ok": False, "error": "错误不存在：" + str(eid)}
        sol = {"id": uuid.uuid4().hex[:8], "title": str(title)[:200], "steps": list(steps or [])[:20],
               "code_snippet": str(code_snippet or "")[:4000], "source": source, "ts": _now(),
               "success_count": 0, "fail_count": 0}
        item.setdefault("solutions", []).append(sol)
        if mark_resolved:
            item["resolved"] = True
        err_save(db)
    E.log("info", "errkb", "错误 %s 已记录解决方案：%s" % (eid, title))
    return {"ok": True, "solution": sol}


def err_feedback(eid, solution_id=None, success=True):
    with LOCK:
        db = err_load()
        item = next((x for x in db["errors"] if x["id"] == eid), None)
        if item is None:
            return {"ok": False, "error": "错误不存在：" + str(eid)}
        if not item.get("solutions"):
            return {"ok": False, "error": "该错误还没有解决方案"}
        sol = None
        if solution_id:
            sol = next((s for s in item["solutions"] if s["id"] == solution_id), None)
        else:
            sol = item["solutions"][-1]
        if sol is None:
            return {"ok": False, "error": "解决方案不存在"}
        if success:
            sol["success_count"] = sol.get("success_count", 0) + 1
            item["resolved"] = True
        else:
            sol["fail_count"] = sol.get("fail_count", 0) + 1
            item["resolved"] = False
        err_save(db)
    return {"ok": True}


def err_stats():
    db = err_load()
    total = len(db["errors"])
    unresolved = len([x for x in db["errors"] if not x.get("resolved")])
    with_sol = len([x for x in db["errors"] if x.get("solutions")])
    return {"ok": True, "total": total, "unresolved": unresolved, "with_solution": with_sol,
            "path": ERR_PATH, "recent": sorted(db["errors"], key=lambda x: -(x.get("last_ts") or 0))[:8]}


def err_get(eid):
    db = err_load()
    item = next((x for x in db["errors"] if x["id"] == eid), None)
    return {"ok": bool(item), "error": item}


def err_delete(eid):
    with LOCK:
        db = err_load()
        n = len(db["errors"])
        db["errors"] = [x for x in db["errors"] if x["id"] != eid]
        if len(db["errors"]) == n:
            return {"ok": False, "error": "错误不存在：" + str(eid)}
        err_save(db)
    return {"ok": True}


def record_tool_failure(tool_name, text, args=None):
    try:
        text = str(text or "")
        if not text or "未知工具" in text:
            return {"ok": False, "skipped": True}
        return err_record(text, tool=tool_name, context={"args": args} if args else None)
    except Exception:
        return {"ok": False, "skipped": True}


# ============================================================ 问答（QA）：先问后做
# 开关与上限落在 cfg.prefs 里（qa_first / qa_max），界面、网页、终端三端共用同一个状态源，
# 系统提示注入由引擎 piengine.qa_prompt(cfg) 完成（这里只负责状态读写与演示题）。
QA_DEMO = {"intro": "这是一张演示问答卡：以后模型需求不清时，会先这样问你。",
           "questions": [
               {"key": "scope", "q": "改动范围？",
                "options": [{"label": "只改 app 目录", "desc": "推荐：影响面最小，随时可回滚"},
                            {"label": "连 web 前端一起改", "desc": "网页版同步生效，改动更大"}],
                "multi": False},
               {"key": "extra", "q": "还需要哪些一起做？（可多选）",
                "options": [{"label": "补自检项"}, {"label": "更新 README"}, {"label": "加截图"}],
                "multi": True}],
           "note": "演示卡不会真的发给模型，只验证你的界面能正常问答。"}


def qa_state(cfg=None):
    prefs = ((cfg or E.load_config()).get("prefs") or {})
    return {"ok": True, "enabled": bool(prefs.get("qa_first")), "max": max(1, min(int(prefs.get("qa_max") or 4), E.QA_MAX_Q)),
            "prompt": E.qa_prompt(cfg or {}), "tool": "ask_user" in E.TOOLS}


def qa_set(cfg, on=None, max_q=None, save=True):
    """打开/关闭「先问后做」或调整单轮问题上限（三端共用）。返回最新状态。"""
    if not isinstance(cfg, dict):
        cfg = E.load_config()
    prefs = cfg.setdefault("prefs", {})
    if on is not None:
        prefs["qa_first"] = bool(on)
    if max_q is not None:
        prefs["qa_max"] = max(1, min(int(max_q), E.QA_MAX_Q))
    if save:
        E.save_config(cfg)
    return qa_state(cfg)


def qa_demo():
    return json.loads(json.dumps(QA_DEMO))


# ============================================================ 账本（可观测性）

def ledger_append(entry):
    row = dict(entry or {})
    row.setdefault("ts", _now())
    _append_jsonl(LEDGER_PATH, row)


def ledger_recent(n=100):
    return {"ok": True, "rows": _read_jsonl(LEDGER_PATH, n)[::-1]}


def ledger_stats(days=14):
    rows = _read_jsonl(LEDGER_PATH, 5000)
    turns = [r for r in rows if r.get("kind") == "turn"]
    tools = [r for r in rows if r.get("kind") == "tool"]
    total_tokens = sum(int((r.get("usage") or {}).get("total_tokens") or 0) for r in turns)
    errs = len([r for r in turns if r.get("error")])
    by_day = {}
    tool_count = {}
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(r.get("ts") or 0))
        d = by_day.setdefault(day, {"turns": 0, "tools": 0, "tokens": 0})
        if r.get("kind") == "turn":
            d["turns"] += 1
            d["tokens"] += int((r.get("usage") or {}).get("total_tokens") or 0)
        if r.get("kind") == "tool":
            d["tools"] += 1
            tool_count[r.get("name") or "?"] = tool_count.get(r.get("name") or "?", 0) + 1
    top = sorted(tool_count.items(), key=lambda x: -x[1])[:10]
    return {"ok": True, "turns": len(turns), "tool_runs": len(tools), "tokens": total_tokens,
            "errors": errs, "by_day": by_day, "top_tools": top,
            "last_turn": turns[-1] if turns else None,
            "path": LEDGER_PATH}


def observe_event(kind, data, sid="", title="", model=""):
    """统一观测落点：网页后端与原生 app 共用同一入口。
    保证两条运行路径行为一致——每次工具调用写入账本、每次 turn 写入账本、
    工具失败自动进错误知识库（含 error_text 指纹，避免被召回提示污染去重）。"""
    try:
        d = data or {}
        if kind == "tool_end":
            ok = bool(d.get("ok", True))
            ledger_append({"kind": "tool", "sid": sid, "name": d.get("name"), "ok": ok, "ms": d.get("ms")})
            if not ok and not d.get("stopped"):
                record_tool_failure(d.get("name"), d.get("error_text") or d.get("text"), d.get("args"))
            return True
        if kind == "done":
            r = d.get("result") if isinstance(d.get("result"), dict) else d
            ledger_append({"kind": "turn", "sid": sid, "title": title,
                           "ok": bool(r.get("ok")), "error": r.get("error") or "",
                           "stopped": bool(r.get("stopped")), "steps": r.get("steps") or 0,
                           "usage": r.get("usage") or {}, "tools": len(r.get("tools") or []),
                           "elapsed": r.get("elapsed") or 0, "model": model})
            return True
    except Exception as e:
        E.log("warn", "ledger", "观测落点失败 %s：%s: %s" % (kind, type(e).__name__, e))
    return False


# ============================================================ 子代理

def _sub_save(rec):
    _dirs()
    _write_json(os.path.join(SUB_DIR, rec["id"] + ".json"), rec)


def subagent_run(task, name="", readonly=True, max_steps=24, timeout=600, cfg=None):
    sid = uuid.uuid4().hex[:8]
    rec = {"id": sid, "name": str(name or task)[:60], "task": str(task), "readonly": bool(readonly),
           "status": "running", "ts": _now(), "text": "", "steps": 0, "tools": [], "ms": 0, "error": ""}
    _SUBS[sid] = rec
    _sub_save(rec)
    cfg2 = json.loads(json.dumps(cfg or _CFG))
    cfg2.setdefault("prefs", {})["max_steps"] = max(1, min(int(max_steps or 24), 200))
    cfg2["prefs"]["approve_mutating"] = True
    session = E.new_session("子代理 · " + rec["name"])
    session["messages"].append({"role": "user", "content": "任务：%s" % task, "ts": _now()})
    cancel = threading.Event()

    def approve(aname, aargs, meta):
        return (not readonly)

    box = {}

    def work():
        try:
            box["r"] = E.run_turn(cfg2, session, lambda k, d: None, cancel, approve)
        except Exception as e:
            box["r"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "text": "", "steps": 0, "tools": []}
    th = threading.Thread(target=work, daemon=True)
    t0 = _now()
    th.start()
    th.join(timeout=max(30, int(timeout or 600)))
    if th.is_alive():
        cancel.set()
        th.join(20)
    r = box.get("r") or {"ok": False, "error": "超时未返回", "text": "", "steps": 0, "tools": []}
    rec.update({"status": "done", "ok": bool(r.get("ok")), "text": (r.get("text") or "")[:20000],
                "steps": r.get("steps") or 0,
                "tools": [{"name": t.get("name"), "ok": t.get("ok")} for t in (r.get("tools") or [])][:40],
                "error": r.get("error") or "", "ms": int((_now() - t0) * 1000)})
    transcript = []
    for m in session.get("messages") or []:
        if m.get("role") == "user":
            transcript.append({"role": "user", "content": str(m.get("content"))[:4000]})
        elif m.get("role") == "assistant" and m.get("content"):
            transcript.append({"role": "assistant", "content": str(m.get("content"))[:4000]})
        elif m.get("role") == "tool":
            transcript.append({"role": "tool", "name": m.get("name"), "ok": m.get("ok", True),
                               "content": str(m.get("content"))[:1500]})
    rec["transcript"] = transcript[-60:]
    _sub_save(rec)
    E.log("info", "subagent", "子代理 %s 完成：%s（%d 步，%sms）" % (sid, rec["name"], rec["steps"], rec["ms"]))
    return rec


def subagent_start(task, name="", readonly=True, max_steps=24, timeout=600):
    sid = "t" + uuid.uuid4().hex[:7]
    rec = {"id": sid, "name": str(name or task)[:60], "task": str(task), "readonly": bool(readonly),
           "status": "running", "ts": _now(), "text": "", "steps": 0, "tools": [], "ms": 0, "error": ""}
    _SUBS[sid] = rec
    _sub_save(rec)

    def work():
        r = subagent_run(task, name=name, readonly=readonly, max_steps=max_steps, timeout=timeout)
        rec.update({k: r.get(k) for k in ("status", "ok", "text", "steps", "tools", "error", "ms", "transcript")})
        _sub_save(rec)
    threading.Thread(target=work, daemon=True).start()
    return rec


def subagent_get(sid):
    if sid in _SUBS:
        return {"ok": True, "sub": _SUBS[sid]}
    p = os.path.join(SUB_DIR, str(sid) + ".json")
    d = _read_json(p, None)
    return {"ok": bool(d), "sub": d}


def subagent_list(limit=50):
    _dirs()
    rows = []
    for fn in os.listdir(SUB_DIR):
        if fn.endswith(".json"):
            d = _read_json(os.path.join(SUB_DIR, fn), None)
            if isinstance(d, dict):
                rows.append({k: d.get(k) for k in ("id", "name", "task", "status", "ok", "steps", "ms", "ts", "readonly")})
    rows.sort(key=lambda x: -(x.get("ts") or 0))
    return {"ok": True, "items": rows[:limit]}


# ============================================================ 引擎工具注册

def _tool(fn):
    def wrap(a, ctx):
        try:
            r = fn(a or {}, ctx or {})
            if isinstance(r, dict):
                r.setdefault("ok", True)
                if "text" not in r:
                    r["text"] = json.dumps({k: v for k, v in r.items() if k != "text"}, ensure_ascii=False)[:4000]
                return r
            return {"ok": True, "text": json.dumps(r, ensure_ascii=False)[:4000]}
        except Exception as e:
            return {"ok": False, "text": "%s: %s" % (type(e).__name__, e)}
    return wrap


def _fn_memory(a, ctx):
    act = a.get("action") or "search"
    if act == "write":
        return mem_write(a.get("type") or "project", a.get("slug"), a.get("description"),
                         a.get("body"), tags=a.get("tags"), confidence=a.get("confidence") or "medium",
                         source_quote=a.get("source_quote") or "")
    if act == "read":
        return mem_read(a.get("slug"))
    if act == "update":
        return mem_update(a.get("slug"), body=a.get("body"), description=a.get("description"),
                          append=a.get("append") or (a.get("body") if a.get("mode") == "append" else None))
    if act == "delete":
        return mem_delete(a.get("slug"))
    if act == "list":
        return mem_list(int(a.get("limit") or 50))
    if act == "stats":
        return mem_stats()
    return mem_search(a.get("q") or a.get("query") or "", int(a.get("limit") or 8))


def _fn_skills(a, ctx):
    act = a.get("action") or "list"
    if act == "read":
        return skill_read(a.get("name"))
    if act == "create":
        return skill_create(a.get("name"), a.get("description"), a.get("body"), a.get("files"))
    if act == "delete":
        return skill_delete(a.get("name"))
    return skills_list()


def _fn_mcp(a, ctx):
    act = a.get("action") or "list"
    if act == "reload":
        return mcp_reload()
    if act == "call":
        srv = a.get("server")
        if srv in _MCP:
            try:
                args = a.get("args_json")
                args = json.loads(args) if isinstance(args, str) and args.strip() else (args or {})
            except Exception:
                args = {}
            r = _MCP[srv].call(a.get("tool"), args, timeout=180)
            return r
        return {"ok": False, "error": "服务器未连接：" + str(srv)}
    if act == "test":
        sid = a.get("server")
        spec = next((x for x in (mcp_load().get("servers") or []) if x.get("id") == sid), None)
        if not spec:
            return {"ok": False, "error": "服务器不存在：" + str(sid)}
        s = McpServer(spec)
        ok = s.start()
        tools = [t.get("name") for t in s.tools]
        s.stop()
        return {"ok": ok, "error": s.error, "tools": tools}
    return mcp_state()


def _fn_cron(a, ctx):
    act = a.get("action") or "list"
    if act == "create":
        reqs = a.get("requests_json")
        if isinstance(reqs, str) and reqs.strip():
            try:
                reqs = json.loads(reqs)
            except Exception:
                reqs = []
        return cron_create(a.get("name"), a.get("cron"), a.get("type") or "bash", a.get("script") or "",
                           requests=reqs or [], prompt=a.get("prompt") or "",
                           timeout_seconds=a.get("timeout_seconds") or 300, remaining=a.get("remaining"),
                           enabled=a.get("enabled", True), workdir=a.get("workdir") or "",
                           allow_mutating=bool(a.get("allow_mutating")))
    if act == "update":
        return cron_update(a.get("id"), a)
    if act == "delete":
        return cron_delete(a.get("id"))
    if act == "run_now":
        return cron_run_now(a.get("id"))
    if act == "logs":
        return cron_logs(a.get("id"))
    return cron_list()


def _fn_error_kb(a, ctx):
    act = a.get("action") or "search"
    if act == "record":
        return err_record(a.get("message") or "", tool=a.get("tool") or "")
    if act == "solve":
        steps = a.get("steps_json")
        if isinstance(steps, str) and steps.strip():
            try:
                steps = json.loads(steps)
            except Exception:
                steps = [steps]
        return err_solve(a.get("id") or a.get("error_id"), a.get("title") or "", steps=steps or [],
                         code_snippet=a.get("code") or "")
    if act == "feedback":
        return err_feedback(a.get("id") or a.get("error_id"), a.get("solution_id"), success=bool(a.get("success", True)))
    if act == "stats":
        return err_stats()
    if act == "get":
        return err_get(a.get("id") or a.get("error_id"))
    if act == "list_unresolved":
        return err_search("", resolved=False, limit=int(a.get("limit") or 10))
    return err_search(a.get("query") or a.get("q") or "", limit=int(a.get("limit") or 8))


def _fn_spawn(a, ctx):
    task = a.get("task") or a.get("prompt") or ""
    if not task:
        return {"ok": False, "error": "缺少 task"}
    rec = subagent_run(task, name=a.get("name") or "", readonly=bool(a.get("readonly", True)),
                       max_steps=int(a.get("max_steps") or 24), timeout=int(a.get("timeout") or 600),
                       cfg=_CFG)
    text = "子代理 %s（%s）\n状态：%s ｜ %d 步 ｜ %dms\n\n报告：\n%s" % (
        rec["name"], "只读" if rec["readonly"] else "可写", "完成" if rec.get("ok") else ("失败：" + rec.get("error", "")),
        rec["steps"], rec["ms"], rec.get("text") or "（无输出）")
    return {"ok": rec.get("ok", False), "text": text[:8000], "sub_id": rec["id"]}


def register_tools():
    E.TOOLS["memory"] = {"name": "memory", "group": "核心", "desc": "跨会话记忆：list/read/search/write/update/delete/stats",
                         "parameters": {"type": "object", "properties": {
                             "action": {"type": "string", "enum": ["list", "read", "search", "write", "update", "delete", "stats"]},
                             "slug": {"type": "string"}, "q": {"type": "string"},
                             "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]},
                             "description": {"type": "string"}, "body": {"type": "string"},
                             "mode": {"type": "string", "enum": ["replace", "append"]},
                             "limit": {"type": "integer"}}, "required": ["action"]},
                         "fn": _tool(_fn_memory), "mutating": False}
    E.TOOLS["skills"] = {"name": "skills", "group": "核心", "desc": "技能库：list/read/create/delete（SKILL.md 技能包）",
                         "parameters": {"type": "object", "properties": {
                             "action": {"type": "string", "enum": ["list", "read", "create", "delete"]},
                             "name": {"type": "string"}, "description": {"type": "string"},
                             "body": {"type": "string"}}, "required": ["action"]},
                         "fn": _tool(_fn_skills), "mutating": False}
    E.TOOLS["mcp"] = {"name": "mcp", "group": "核心", "desc": "MCP 服务器：list/test/call/reload（连接后在工具表生成 mcp_* 动态工具）",
                      "parameters": {"type": "object", "properties": {
                          "action": {"type": "string", "enum": ["list", "test", "call", "reload"]},
                          "server": {"type": "string"}, "tool": {"type": "string"},
                          "args_json": {"type": "string"}}, "required": ["action"]},
                      "fn": _tool(_fn_mcp), "mutating": False}
    E.TOOLS["cron"] = {"name": "cron", "group": "核心",
                       "desc": "定时任务：list/create/update/delete/run_now/logs（bash/http/prompt 三类，6 段 cron）",
                       "parameters": {"type": "object", "properties": {
                           "action": {"type": "string", "enum": ["list", "create", "update", "delete", "run_now", "logs"]},
                           "id": {"type": "string"}, "name": {"type": "string"}, "cron": {"type": "string"},
                           "type": {"type": "string", "enum": ["bash", "http", "prompt"]},
                           "script": {"type": "string"}, "requests_json": {"type": "string"},
                           "prompt": {"type": "string"}, "timeout_seconds": {"type": "integer"},
                           "remaining": {"type": "integer"}, "enabled": {"type": "boolean"},
                           "workdir": {"type": "string"}, "allow_mutating": {"type": "boolean"}},
                           "required": ["action"]},
                       "fn": _tool(_fn_cron), "mutating": True}
    E.TOOLS["error_kb"] = {"name": "error_kb", "group": "核心",
                           "desc": "错误知识库：search/record/solve/feedback/stats/get/list_unresolved",
                           "parameters": {"type": "object", "properties": {
                               "action": {"type": "string", "enum": ["search", "record", "solve", "feedback", "stats", "get", "list_unresolved"]},
                               "query": {"type": "string"}, "message": {"type": "string"}, "tool": {"type": "string"},
                               "id": {"type": "string"}, "title": {"type": "string"}, "steps_json": {"type": "string"},
                               "code": {"type": "string"}, "solution_id": {"type": "string"},
                               "success": {"type": "boolean"}, "limit": {"type": "integer"}},
                               "required": ["action"]},
                           "fn": _tool(_fn_error_kb), "mutating": False}
    E.TOOLS["spawn_agent"] = {"name": "spawn_agent", "group": "核心",
                              "desc": "派生子代理独立执行研究/改查任务并回报（readonly 默认只读；可写模式会实际修改文件）",
                              "parameters": {"type": "object", "properties": {
                                  "task": {"type": "string"}, "name": {"type": "string"},
                                  "readonly": {"type": "boolean"}, "max_steps": {"type": "integer"},
                                  "timeout": {"type": "integer"}}, "required": ["task"]},
                              "fn": _tool(_fn_spawn), "mutating": True}
    # 错误自愈回路：工具失败时引擎回调本函数，自动召回历史已验证的修复提示
    try:
        E.set_error_hint_provider(lambda tool, args, text: err_hint(tool, text))
    except Exception as e:
        E.log("warn", "errkb", "错误召回钩子注册失败：%s: %s" % (type(e).__name__, e))


# ============================================================ 状态 / 初始化 / 自检

def state():
    try:
        mem = mem_stats()
    except Exception:
        mem = {"total": 0}
    try:
        sk = skills_list()
    except Exception:
        sk = {"items": []}
    try:
        cr = cron_list()
    except Exception:
        cr = {"tasks": []}
    try:
        er = err_stats()
    except Exception:
        er = {"total": 0, "unresolved": 0}
    try:
        st = ledger_stats()
    except Exception:
        st = {"turns": 0}
    try:
        import piplugins
        pl = piplugins.state()
    except Exception:
        pl = {"total": 0, "tools": 0, "dir": ""}
    return {"ok": True,
            "memory": {"total": mem.get("total", 0), "dir": MEM_DIR},
            "skills": {"total": len(sk.get("items") or []), "dir": SKILL_DIR},
            "plugins": {"total": pl.get("total", 0), "tools": pl.get("tools", 0), "dir": pl.get("dir", "")},
            "mcp": {"servers": len((mcp_load().get("servers") or [])),
                    "tools": len([n for n in E.TOOLS if n.startswith("mcp_")]), "path": MCP_PATH},
            "cron": {"tasks": len(cr.get("tasks") or []), "path": CRON_PATH,
                     "enabled": len([t for t in (cr.get("tasks") or []) if t.get("enabled")])},
            "errors": {"total": er.get("total", 0), "unresolved": er.get("unresolved", 0)},
            "ledger": {"turns": st.get("turns", 0), "tool_runs": st.get("tool_runs", 0),
                       "tokens": st.get("tokens", 0)}}


def init(cfg, enable_scheduler=True, session_provider=None, session_saver=None):
    global _CFG, _SESSION_PROVIDER, _SESSION_SAVER
    _CFG = cfg if isinstance(cfg, dict) else {}
    if session_provider is not None:
        _SESSION_PROVIDER = session_provider
    if session_saver is not None:
        _SESSION_SAVER = session_saver
    _dirs()
    E.ensure_home()
    register_tools()
    try:
        import piplugins
        piplugins.register()
    except Exception as e:
        E.log("warn", "plugins", "本地插件基座加载失败：%s: %s" % (type(e).__name__, e))
    try:
        import picontract
        picontract.register()
        import piplan
        piplan.register()
        import pipolicy
        pipolicy.register()
        import piapi
        piapi.register()
    except Exception as e:
        E.log("warn", "core", "治理套件加载失败：%s: %s" % (type(e).__name__, e))
    if enable_scheduler:
        start_scheduler()
    threading.Thread(target=lambda: mcp_reload(), daemon=True).start()
    E.log("info", "core", "PI 核心能力层已加载：记忆/技能/MCP/调度/错误库/账本/子代理（工具 %d 个）" % len(E.TOOLS))
    return state()


def shutdown():
    try:
        stop_scheduler()
    except Exception:
        pass
    mcp_stop_all()


def selftest():
    items = []
    try:                              # 自检自足：直接调用 selftest() 也要保证核心工具与插件基座都在表里
        register_tools()
    except Exception:
        pass
    try:                              # 保证插件基座已注册（CLI 路径只调 register_tools()，不经过 init）
        import piplugins
        if "plugins" not in E.TOOLS:
            piplugins.register()
    except Exception:
        pass
    try:                              # 治理套件（契约/计划/审批/接口）同样保证注册
        for mod, name in (("picontract", "contract"), ("piplan", "plan"),
                          ("pipolicy", "approvals"), ("piapi", "apis")):
            m = __import__(mod)
            if name not in E.TOOLS:
                m.register()
    except Exception:
        pass

    def chk(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        items.append({"name": "core·" + name, "ok": bool(ok), "detail": str(detail)[:200]})

    def t_mem():
        r = mem_write("project", "_selftest-mem", "自检记忆", "PI core selftest body")
        r2 = mem_read("_selftest-mem")
        r3 = mem_search("selftest body")
        mem_delete("_selftest-mem")
        return (r.get("ok") and r2.get("ok") and any(x.get("slug") == "_selftest-mem" for x in r3.get("items") or [])), \
            "写入/读取/搜索/删除"
    chk("记忆 写入·读取·搜索·删除", t_mem)

    def t_skill():
        r = skill_create("_selftest-skill", "自检技能", "第二步：read 回来")
        r2 = skill_read("_selftest-skill")
        skill_delete("_selftest-skill")
        return (r.get("ok") and r2.get("ok") and "_selftest-skill" in (r2.get("text") or "")), "创建/读取/删除"
    chk("技能 创建·读取·删除", t_skill)

    def t_cron():
        import datetime
        tm = time.localtime()
        a = cron_match("*/1 * * * * *", tm)
        b = cron_match("%d %d %d %d %d %d" % (tm.tm_sec, tm.tm_min, tm.tm_hour, tm.tm_mday, tm.tm_mon, tm.tm_wday), tm)
        c = cron_match("0 0 0 1 1 *", time.localtime(time.mktime((2024, 1, 1, 0, 0, 0, 0, 0, -1))))
        return (a and b and c), "通配/*步进/精确字段"
    chk("调度 cron 匹配引擎", t_cron)

    def t_err():
        m = "selftest error message 42"
        r1 = err_record(m, tool="selftest")
        r2 = err_record(m, tool="selftest")
        s = err_search("selftest error")
        db = err_load()
        db["errors"] = [x for x in db["errors"] if x.get("id") != r1.get("id")]
        err_save(db)
        return (r1.get("ok") and r2.get("id") == r1.get("id") and r2.get("count") >= 2 and s.get("items")), "指纹去重/搜索"
    chk("错误库 记录·指纹去重·搜索", t_err)

    def t_hint():
        m = "selftest hint error %d" % int(time.time() * 1000)
        r = err_record(m, tool="selftest-hint")
        err_solve(r["id"], "把 X 改成 Y", steps=["先做 A", "再做 B"], source="selftest")
        h = err_hint("selftest-hint", m)                 # 1) 指纹命中自动召回
        wired = E.ERROR_HINT_PROVIDER is not None        # 2) 引擎钩子已接入
        m2 = "selftest observe error %d" % int(time.time() * 1000)
        observe_event("tool_end", {"ok": False, "name": "selftest-hint", "error_text": m2, "ms": 1}, "selftest")
        found = any(m2[:60] in (x.get("message") or "") for x in err_search(m2, limit=5).get("items") or [])
        db = err_load()                                  # 3) 统一观测入口把失败写进错误库；清理
        keep = [x for x in db["errors"] if x.get("id") != r["id"] and x.get("fp") != _err_fp(m2)]
        db["errors"] = keep
        err_save(db)
        return bool(h and "把 X 改成 Y" in h and "先做 A" in h and wired and found), "记录→方案→自动召回（含钩子/观测入口）"
    chk("错误库 自动召回", t_hint)

    def t_ledger():
        before = ledger_stats().get("turns", 0)
        ledger_append({"kind": "turn", "selftest": True, "steps": 0, "usage": {"total_tokens": 1}})
        after = ledger_stats().get("turns", 0)
        return after >= before + 1, "写入/统计"
    chk("账本 写入·统计", t_ledger)

    def t_mcp():
        st = mcp_state()
        return st.get("ok"), "%d 个服务器配置" % len(st.get("servers") or [])
    chk("MCP 配置装载", t_mcp)

    def t_mcp_live():
        """真实 stdio 往返：临时起一个最小 MCP 服务器，验证 initialize→tools/list→tools/call→停用释放。"""
        path = os.path.join(CORE_DIR, "_selftest_mcp_server.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(MCP_SELFTEST_SRC)
        s = None
        try:
            s = McpServer({"id": "selftest-live", "transport": "stdio", "enabled": True,
                           "command": sys.executable, "args": [path]})
            ok = s.start(timeout=20)
            names = [t.get("name") for t in s.tools]
            r = s.call("sum", {"a": 40, "b": 2}, timeout=20) if ok else {"ok": False, "text": s.error}
            s.stop()
            good = ok and names == ["echo", "sum"] and "SUM:42" in (r.get("text") or "")
            return good, "工具 %s · 调用返回 %s" % (names, (r.get("text") or s.error or "").strip()[:50])
        finally:
            try:
                if s:
                    s.stop()
                os.remove(path)
            except Exception:
                pass
    chk("MCP 传输 连接·列工具·真实调用", t_mcp_live)

    def t_mcp_release():
        """停用必须真的释放：进程停掉 + mcp_* 工具从工具表摘除（否则调用又会把服务器拉起来）。"""
        path = os.path.join(CORE_DIR, "_selftest_mcp_release.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(MCP_SELFTEST_SRC)
        try:
            s = McpServer({"id": "selftest-rel", "transport": "stdio", "enabled": True,
                           "command": sys.executable, "args": [path]})
            s.start(timeout=20)
            with _MCP_LOCK:
                _MCP["selftest-rel"] = s
            for t in (s.tools or []):
                nm = "mcp_selftest_rel_%s" % _safe_id(t.get("name"))
                E.TOOLS[nm] = {"name": nm, "group": "MCP", "desc": "", "parameters": {},
                               "fn": _mcp_caller("selftest-rel", t.get("name")), "mutating": False,
                               "server": "selftest-rel"}
            before = len([n for n in E.TOOLS if n.startswith("mcp_")])
            mcp_stop_all()
            after = len([n for n in E.TOOLS if n.startswith("mcp_")])
            with _MCP_LOCK:
                _MCP.pop("selftest-rel", None)
            return (before >= 2 and after == 0), "停用前 %d 个动态工具 → 停用后 %d 个，进程已终止" % (before, after)
        finally:
            try:
                with _MCP_LOCK:
                    if "selftest-rel" in _MCP:
                        _MCP["selftest-rel"].stop()
                        _MCP.pop("selftest-rel", None)
                os.remove(path)
            except Exception:
                pass
    chk("MCP 停用 释放动态工具", t_mcp_release)

    def t_tools():
        names = [n for n in ("memory", "skills", "plugins", "mcp", "cron", "error_kb", "spawn_agent")
                 if n in E.TOOLS]
        return len(names) == 7, "引擎工具表：%s" % ",".join(names)
    chk("核心工具已注册", t_tools)

    def t_plugins():
        import piplugins
        if "plugins" not in E.TOOLS:
            piplugins.register()
        r = piplugins.selftest()
        return r["passed"] == r["total"], "插件基座 %d/%d" % (r["passed"], r["total"])
    chk("插件基座 创建·注册·调用技能·跑 shell", t_plugins)

    def t_qa():
        fake = {"prefs": {}}
        s0 = qa_state(fake)
        s1 = qa_set(fake, on=True, max_q=3, save=False)
        s2 = qa_set(fake, on=False, save=False)
        s3 = qa_set(fake, on=True, max_q=99, save=False)          # 越界自动钳制到 QA_MAX_Q
        demo = qa_demo()
        demo["questions"][0]["q"] = "被改坏了"
        ok = (s0["enabled"] is False and s1["enabled"] and s1["max"] == 3 and "先问后做" in s1["prompt"]
              and s2["enabled"] is False and s2["prompt"] == "" and s3["max"] == E.QA_MAX_Q
              and demo["questions"][0]["q"] != QA_DEMO["questions"][0]["q"]      # 演示题是副本，改不坏源
              and s1["tool"])
        return ok, "开关 on/off · 上限 3（越界钳制到 %d）· 提示注入 %d 字 · ask_user 已注册" % (
            E.QA_MAX_Q, len(s1["prompt"]))
    chk("问答 开关·上限·演示题", t_qa)

    def _mod_check(mod, regname, label):
        def t():
            m = __import__(mod)
            if regname not in E.TOOLS:
                m.register()
            r = m.selftest()
            return r["passed"] == r["total"], "%s %d/%d" % (label, r["passed"], r["total"])
        return t
    chk("契约系统 全周期状态机", _mod_check("picontract", "contract", "契约"))
    chk("计划与编排 步骤·续跑·编排", _mod_check("piplan", "plan", "计划"))
    chk("审批策略 规则·自动确认", _mod_check("pipolicy", "approvals", "审批"))
    chk("自定义接口 注册·调用", _mod_check("piapi", "apis", "接口"))

    passed = len([x for x in items if x["ok"]])
    return {"passed": passed, "total": len(items), "items": items}


def format_selftest(r):
    lines = ["PI 核心能力自检：%d/%d 通过" % (r["passed"], r["total"]), ""]
    for it in r["items"]:
        lines.append("%s %-30s %s" % ("✔" if it["ok"] else "✘", it["name"], it["detail"]))
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--selftest" in sys.argv:
        E.ensure_home()
        register_tools()
        try:
            import piplugins
            piplugins.register()
        except Exception:
            pass
        r = selftest()
        print(format_selftest(r))
        sys.exit(0 if r["passed"] == r["total"] else 1)
    print("PI Core 模块。用 --selftest 自检。")
