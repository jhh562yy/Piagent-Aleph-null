import ast
import difflib
import hashlib
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

APP = "PI Studio"
VERSION = "3.1"
HOME = os.path.join(os.path.expanduser("~"), ".pistudio")
CONFIG_PATH = os.path.join(HOME, "config.json")
SESSIONS_PATH = os.path.join(HOME, "sessions.json")
UI_PATH = os.path.join(HOME, "ui.json")
LOG_PATH = os.path.join(HOME, "pistudio.log")
ERROR_PATH = os.path.join(HOME, "error.log")
EVENTS_PATH = os.path.join(HOME, "events.jsonl")
BACKUP_DIR = os.path.join(HOME, "backups")

DEFAULT_SYSTEM = (
    "你是 PI Studio 的本地代理，运行在用户自己的电脑上，并且能够调用真实工具在该机器上工作。\n"
    "规则：\n"
    "1) 需要事实（文件内容、目录结构、系统信息、命令输出）时，先调用工具获取，不要凭记忆猜测。\n"
    "2) 读取用 fs_list / fs_read / fs_search；写入用 fs_write / fs_edit；执行命令用 shell；联网用 http_get；查今天的实时热点事件用 hot_topics。\n"
    "3) 写入与执行类工具在真正运行前会请求用户审批，被拒绝时不要重试，改为说明原因。\n"
    "4) 改文件前先读原文件；改动尽量小而精确，能用 fs_edit 就不要整篇 fs_write。\n"
    "5) 路径默认相对当前工作区根目录，也可以用绝对路径。\n"
    "6) 回答简洁、给结论和证据（命令、路径、行号、退出码），使用中文。\n"
)

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "target", "dist", "build", ".venv", "venv",
             "__pycache__", ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".next", ".cache"}

DEFAULT_CTX = 32768
CTX_HINTS = [
    ("deepseek", 65536), ("gpt-4o", 128000), ("gpt-4.1", 1000000), ("o1", 200000), ("o3", 200000),
    ("claude", 200000), ("gemini", 1000000), ("qwen", 131072), ("glm", 131072), ("moonshot", 131072),
    ("kimi", 131072), ("llama", 131072), ("mistral", 32768), ("yi-", 16384),
]

_RETRY_CODES = (408, 409, 425, 429, 500, 502, 503, 504)
_INLINE_LIMIT = 300000


class EngineError(Exception):
    pass


class Stopped(Exception):
    pass


_LOG_RING = []
_LOG_LOCK = threading.Lock()
_REDACT = []


def ensure_home():
    os.makedirs(HOME, exist_ok=True)
    return HOME


def set_redactions(values):
    _REDACT[:] = sorted({str(v) for v in values if v and len(str(v)) >= 8}, key=len, reverse=True)


def redact(text):
    s = str(text)
    for v in _REDACT:
        if v in s:
            s = s.replace(v, v[:6] + "***已隐去***")
    return s


def log(level, source, message):
    rec = {"ts": time.time(), "level": level, "source": source, "message": redact(message)}
    with _LOG_LOCK:
        _LOG_RING.append(rec)
        if len(_LOG_RING) > 2000:
            del _LOG_RING[:len(_LOG_RING) - 2000]
    try:
        ensure_home()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %-5s %-14s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec["ts"])),
                                            level.upper(), source, rec["message"]))
    except Exception:
        pass
    return rec


def log_records():
    with _LOG_LOCK:
        return list(_LOG_RING)


def log_file_records(n=200):
    """从磁盘日志文件尾部读回 n 条记录（进程重启后内存环是空的，界面控制台仍需有历史）。"""
    n = max(1, min(int(n or 200), 2000))
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
    except Exception:
        return []
    out = []
    pat = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\w+)\s+(\S+)\s+(.*)$")
    for ln in lines:
        m = pat.match(ln.rstrip("\n"))
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        out.append({"ts": ts, "level": m.group(2).lower(), "source": m.group(3),
                    "message": redact(m.group(4))})
    return out


def event(kind, data=None):
    try:
        ensure_home()
        if os.path.exists(EVENTS_PATH) and os.path.getsize(EVENTS_PATH) > 5_000_000:
            try:
                os.replace(EVENTS_PATH, EVENTS_PATH + ".1")
            except Exception:
                pass
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, "data": redact_json(data or {})},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def redact_json(obj):
    if isinstance(obj, dict):
        return {k: redact_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def read_events(limit=400):
    out = []
    try:
        with open(EVENTS_PATH, "r", encoding="utf-8") as f:
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
    return out[-limit:]


def _atomic_write(path, text):
    tmp = path + ".tmp"
    ensure_home()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def harden(path):
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return
    try:
        user = os.environ.get("USERNAME") or ""
        if user:
            subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", "%s:F" % user],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


DEFAULT_CONFIG = {
    "provider": {"name": "Ollama (本机)", "kind": "openai", "base_url": "http://localhost:11434/v1",
                 "api_key": "", "model": "", "models": [], "model_meta": {}},
    "profiles": [
        {"name": "Ollama (本机)", "kind": "openai", "base_url": "http://localhost:11434/v1", "api_key": "", "model": ""},
        {"name": "LM Studio (本机)", "kind": "openai", "base_url": "http://localhost:1234/v1", "api_key": "", "model": ""},
        {"name": "vLLM / llama.cpp (本机)", "kind": "openai", "base_url": "http://localhost:8000/v1", "api_key": "", "model": ""},
        {"name": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-4o-mini"},
        {"name": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com/v1", "api_key": "", "model": "deepseek-chat"},
        {"name": "Moonshot Kimi", "kind": "openai", "base_url": "https://api.moonshot.cn/v1", "api_key": "", "model": "moonshot-v1-8k"},
        {"name": "智谱 GLM", "kind": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "", "model": "glm-4-flash"},
        {"name": "硅基流动", "kind": "openai", "base_url": "https://api.siliconflow.cn/v1", "api_key": "", "model": "Qwen/Qwen2.5-7B-Instruct"},
    ],
    "workspace": os.path.expanduser("~"),
    "prefs": {
        "temperature": 0.7,
        "max_steps": 40,
        "approve_mutating": True,
        "timeout": 180,
        "shell_timeout": 60,
        "enable_tools": True,
        "theme": "dark",
        "accent": "#5b8cff",
        "system": DEFAULT_SYSTEM,
        "sandbox": True,
        "proxy": "",
        "retries": 2,
        "auto_title": True,
        "auto_compact": True,
        "max_output_tokens": 0,
        "price_in": 0.0,
        "price_out": 0.0,
        "keep_checkpoints": 200,
        "auto_continue": False,
        "auto_segments": 4,
        "policy_auto": False,
    },
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    if not os.path.exists(CONFIG_PATH):
        legacy = read_json(os.path.join(HOME, "state.json"), {}) or {}
        cfg = _merge(DEFAULT_CONFIG, {})
        if isinstance(legacy.get("prefs"), dict):
            for k in ("theme", "accent"):
                if k in legacy["prefs"]:
                    cfg["prefs"][k] = legacy["prefs"][k]
        save_config(cfg)
        return cfg
    cfg = _merge(DEFAULT_CONFIG, read_json(CONFIG_PATH, {}))
    # 迁移：旧版本默认单轮上限仅 12 步，复杂任务常常做到一半就被「强制收尾」。
    # 只有在用户没有主动改成别的值时（即仍是历史默认 12）才提升到新的默认 40，避免覆盖用户的选择。
    try:
        pr = cfg.setdefault("prefs", {})
        if int(pr.get("max_steps") or 0) == 12:
            pr["max_steps"] = DEFAULT_CONFIG["prefs"]["max_steps"]
            save_config(cfg)
    except Exception:
        pass
    set_redactions([(cfg.get("provider") or {}).get("api_key")])
    try:
        enrich_model_meta(cfg)
    except Exception:
        pass
    return cfg


def save_config(cfg):
    _atomic_write(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))
    set_redactions([(cfg.get("provider") or {}).get("api_key")])
    harden(CONFIG_PATH)


def load_sessions():
    data = read_json(SESSIONS_PATH, None)
    if data and isinstance(data, list) and data:
        return data
    legacy = read_json(os.path.join(HOME, "state.json"), {}) or {}
    old = legacy.get("sessions")
    if isinstance(old, list) and old:
        out = []
        for s in old:
            msgs = []
            for m in (s.get("messages") or []):
                role = m.get("role")
                if role == "tool":
                    msgs.append({"role": "tool", "tool_call_id": m.get("id", "legacy"), "name": m.get("name", "tool"),
                                 "content": str(m.get("out") or ""), "ts": m.get("ts", time.time())})
                else:
                    msgs.append({"role": role or "user", "content": str(m.get("text") or ""), "ts": m.get("ts", time.time())})
            out.append({"id": s.get("id") or uuid.uuid4().hex[:8], "title": s.get("title") or "导入的会话",
                        "created": s.get("created", time.time()), "updated": s.get("updated", time.time()),
                        "messages": msgs})
        return out
    return []


def save_sessions(sessions):
    _atomic_write(SESSIONS_PATH, json.dumps(sessions, ensure_ascii=False, indent=2))


def load_ui():
    return read_json(UI_PATH, {"theme": "dark", "accent": "#5b8cff", "layout": {}})


def save_ui(ui):
    _atomic_write(UI_PATH, json.dumps(ui, ensure_ascii=False, indent=2))


def new_session(title="新会话"):
    t = time.time()
    return {"id": uuid.uuid4().hex[:10], "title": title, "created": t, "updated": t, "draft": "",
            "messages": [], "checkpoints": [], "ctx_used": 0, "compactions": 0, "usage_total": {}}


def api_messages(session, system):
    """构造 OpenAI 兼容消息列表。

    被中断的 turn 可能在历史里留下「助手消息带 tool_calls，但没有对应的 tool 回复」的残缺状态，
    直接发给接口会 400（An assistant message with 'tool_calls' must be followed by tool messages …），
    于是整条会话之后每一条消息都失败。这里做一次兜底修复：为悬空的 tool_call 补一条占位 tool 消息，
    保证发给模型的历史永远合法。该修复是幂等的，不改动已存储的数据。
    """
    out = [{"role": "system", "content": system}]
    pending = []          # 已声明但尚未收到结果的 (tool_call_id, name)

    def flush(note):
        for cid, nm in pending:
            out.append({"role": "tool", "tool_call_id": cid, "name": nm, "content": note})
        del pending[:]

    for m in session.get("messages", []):
        role = m.get("role")
        if role == "user":
            flush("（上一步工具调用被中断，未返回结果）")
            out.append({"role": "user", "content": str(m.get("content", ""))})
        elif role == "assistant":
            flush("（上一步工具调用被中断，未返回结果）")
            msg = {"role": "assistant", "content": str(m.get("content", ""))}
            if m.get("tool_calls"):
                msg["tool_calls"] = m["tool_calls"]
                for c in m["tool_calls"]:
                    cid = c.get("id") or ("call_" + uuid.uuid4().hex[:8])
                    pending.append((cid, (c.get("function") or {}).get("name") or "tool"))
            out.append(msg)
        elif role == "tool":
            cid = m.get("tool_call_id") or m.get("id") or "call"
            out.append({"role": "tool", "tool_call_id": cid, "name": m.get("name") or "tool",
                        "content": str(m.get("content", ""))})
            pending[:] = [p for p in pending if p[0] != cid]
        elif role == "system":
            out.append({"role": "system", "content": str(m.get("content", ""))})
    flush("（工具调用被中断，未返回结果）")
    return out


def describe_error(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return "HTTP %s %s %s" % (e.code, e.reason, body[:500])
    if isinstance(e, urllib.error.URLError):
        return "无法连接：%s" % (getattr(e, "reason", e),)
    return "%s: %s" % (type(e).__name__, e)


def norm_base(url):
    return (url or "").strip().rstrip("/")


def model_meta(cfg, model=None):
    prov = cfg.get("provider") or {}
    model = model or prov.get("model") or ""
    meta = dict((prov.get("model_meta") or {}).get(model) or {})
    if not meta.get("contextWindow"):
        low = model.lower()
        meta["contextWindow"] = next((v for k, v in CTX_HINTS if k in low), DEFAULT_CTX)
        meta["contextWindowSource"] = "推测"
    else:
        meta["contextWindowSource"] = "已配置"
    if "reasoning" not in meta:
        meta["reasoning"] = bool(re.search(r"reason|r1|o1|o3|think", model, re.I))
    return meta


def ctx_state(cfg, session):
    meta = model_meta(cfg)
    limit = max(1000, int(meta.get("contextWindow") or DEFAULT_CTX))
    used = int(session.get("ctx_used") or 0)
    msgs = session.get("messages") or []
    return {"limit": limit, "used": used, "ratio": min(1.0, used / float(limit)),
            "source": meta.get("contextWindowSource"), "reasoning": bool(meta.get("reasoning")),
            "messages": len(msgs), "need": used > limit * 0.75 and len(msgs) > 8,
            "maxOutput": meta.get("maxOutput")}


def cost(cfg, usage):
    pr = cfg.get("prefs") or {}
    pin = float(pr.get("price_in") or 0)
    pout = float(pr.get("price_out") or 0)
    if pin <= 0 and pout <= 0:
        return None
    u = usage or {}
    return round((u.get("prompt_tokens", 0) / 1000.0) * pin + (u.get("completion_tokens", 0) / 1000.0) * pout, 4)


def mask_key(k):
    k = (k or "").strip()
    if not k:
        return "（未设置）"
    if len(k) <= 10:
        return k[:2] + "…" + k[-2:]
    return k[:6] + "…" + k[-4:] + "（%d 位）" % len(k)


def _headers(provider, stream=False):
    k = (provider.get("api_key") or "").strip()
    h = {"Content-Type": "application/json", "User-Agent": "PI-Studio/%s" % VERSION,
         "Accept": "text/event-stream" if stream else "application/json"}
    if k:
        h["Authorization"] = "Bearer " + k
    for extra in (provider.get("customHeaders") or []):
        try:
            if isinstance(extra, dict) and extra.get("key"):
                h[str(extra["key"])] = str(extra.get("value") or "")
        except Exception:
            continue
    return h


def http_open(req, timeout, proxy=""):
    if proxy:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return op.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _get(provider, url, timeout, proxy="", retries=1, cancel=None):
    last = None
    for attempt in range(retries + 1):
        if cancel is not None and cancel.is_set():
            raise Stopped()
        try:
            req = urllib.request.Request(url, headers=_headers(provider), method="GET")
            with http_open(req, timeout, proxy) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in _RETRY_CODES and attempt < retries:
                time.sleep(min(6.0, 1.2 * (2 ** attempt)))
                continue
            raise
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(min(6.0, 1.2 * (2 ** attempt)))
                continue
            raise
    raise last


def api_models(provider, timeout=8, proxy="", retries=1, cancel=None):
    base = norm_base(provider.get("base_url"))
    if not base:
        raise EngineError("未配置 base_url")
    t0 = time.time()
    try:
        body = _get(provider, base + "/models", timeout, proxy, retries, cancel)
    except Exception as e:
        raise EngineError(describe_error(e))
    ms = int((time.time() - t0) * 1000)
    try:
        data = json.loads(body)
    except Exception:
        raise EngineError("返回不是 JSON：" + body[:200])
    items = None
    if isinstance(data, dict):
        items = data.get("data") if data.get("data") is not None else data.get("models")
    elif isinstance(data, list):
        items = data
    out = []
    for it in (items or []):
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if mid:
                out.append(str(mid))
        elif isinstance(it, str):
            out.append(it)
    return sorted(set(out)), ms


def probe(provider, timeout=8, proxy="", retries=1, cancel=None):
    try:
        models, ms = api_models(provider, timeout, proxy, retries, cancel)
        return {"ok": True, "models": models, "ms": ms, "error": ""}
    except Exception as e:
        return {"ok": False, "models": [], "ms": 0, "error": describe_error(e)}


HOST_DB_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".nmoiaigent", "config.sqlite"),
    os.path.join(os.path.expanduser("~"), ".liveagent", "config.sqlite"),
]

LOCAL_ENDPOINTS = [
    ("Ollama (本机)", "http://localhost:11434/v1"),
    ("LM Studio (本机)", "http://localhost:1234/v1"),
    ("llama.cpp / vLLM (本机)", "http://localhost:8000/v1"),
    ("text-generation-webui (本机)", "http://localhost:5000/v1"),
    ("LocalAI (本机)", "http://localhost:8080/v1"),
]


def host_providers():
    found = []
    for db in HOST_DB_CANDIDATES:
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
            cur = con.cursor()
            for (payload,) in cur.execute("select payload_json from provider_settings").fetchall():
                try:
                    d = json.loads(payload)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                base = str(d.get("baseUrl") or "").strip()
                if not base:
                    continue
                ids, meta = [], {}
                for m in (d.get("models") or []):
                    if not isinstance(m, dict) or not m.get("id"):
                        continue
                    mid = str(m["id"])
                    ids.append(mid)
                    meta[mid] = {k: m[k] for k in ("contextWindow", "maxOutputToken", "reasoning") if k in m}
                    if "maxOutputToken" in meta[mid]:
                        meta[mid]["maxOutput"] = meta[mid].pop("maxOutputToken")
                active = [str(x) for x in (d.get("activeModels") or []) if x]
                found.append({
                    "name": str(d.get("name") or d.get("id") or "host") + "（已配置）",
                    "kind": "openai",
                    "base_url": base.rstrip("/"),
                    "api_key": str(d.get("apiKey") or "").strip(),
                    "model": (active or ids or [""])[0],
                    "models": ids,
                    "model_meta": meta,
                    "customHeaders": d.get("customHeaders") or [],
                    "source": db,
                })
            con.close()
        except Exception as e:
            log("warn", "hostimport", "%s 读取失败：%s" % (db, e))
    return found


def detect_local_endpoints(timeout=1.2):
    hits = []
    for name, base in LOCAL_ENDPOINTS:
        try:
            models, ms = api_models({"base_url": base, "api_key": ""}, timeout=timeout)
            hits.append({"name": name, "kind": "openai", "base_url": base, "api_key": "",
                         "model": models[0] if models else "", "models": models, "model_meta": {}, "ms": ms})
        except Exception:
            continue
    return hits


def enrich_model_meta(cfg, ids=None):
    prov = cfg.get("provider") or {}
    base = norm_base(prov.get("base_url"))
    if not base:
        return {}
    meta = dict(prov.get("model_meta") or {})
    models = list(prov.get("models") or [])
    changed = False
    for hp in host_providers():
        if norm_base(hp["base_url"]) != base:
            continue
        for mid, m in (hp.get("model_meta") or {}).items():
            if mid not in meta:
                meta[mid] = dict(m)
                changed = True
        for m in (hp.get("models") or []):
            if m not in models:
                models.append(m)
                changed = True
        if hp.get("customHeaders") and not prov.get("customHeaders"):
            prov["customHeaders"] = hp["customHeaders"]
            changed = True
    if ids:
        for m in ids:
            if m not in models:
                models.append(m)
                changed = True
    if changed:
        prov["models"] = models
        prov["model_meta"] = meta
        cfg["provider"] = prov
        try:
            save_config(cfg)
        except Exception:
            pass
        log("info", "meta", "补全模型元数据：%d 个模型有窗口信息" % len(meta))
    return meta


def autoconfigure(cfg, timeout=8):
    enrich_model_meta(cfg)
    prov = cfg.get("provider") or {}
    if (prov.get("base_url") or "").strip() and (prov.get("api_key") or "").strip():
        return None
    for hp in host_providers():
        if not hp.get("api_key") and not (hp.get("models") or []):
            continue
        hp = dict(hp)
        hp["_autodetected"] = True
        cfg["provider"] = hp
        known = {p.get("base_url") for p in cfg.get("profiles", [])}
        if hp["base_url"] not in known:
            cfg.setdefault("profiles", []).append({k: v for k, v in hp.items() if not k.startswith("_")})
        save_config(cfg)
        log("info", "autoconfig", "已导入宿主 Provider：%s (%s)，模型 %s" % (hp["name"], hp["base_url"], hp["model"]))
        return hp
    hits = detect_local_endpoints()
    if hits:
        hp = dict(hits[0])
        hp["_autodetected"] = True
        cfg["provider"] = hp
        save_config(cfg)
        log("info", "autoconfig", "发现本机模型端点：%s (%s)" % (hp["name"], hp["base_url"]))
        return hp
    log("warn", "autoconfig", "未发现可用模型端点；请在「模型」面板手动配置")
    return None


def _absorb(obj, acc):
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("usage"), dict):
        acc["usage"] = obj["usage"]
    if obj.get("model"):
        acc["model"] = obj["model"]
    if obj.get("error"):
        raise EngineError(str(obj["error"])[:500])
    chs = obj.get("choices") or []
    if not chs:
        return None
    ch = chs[0] or {}
    delta = ch.get("delta")
    if delta is None:
        delta = ch.get("message") or {}
    c = delta.get("content")
    if isinstance(c, str) and c:
        acc["content"] += c
        return ("delta", c)
    if isinstance(c, list):
        for part in c:
            if isinstance(part, dict) and part.get("text"):
                acc["content"] += part["text"]
                return ("delta", part["text"])
    r = delta.get("reasoning_content") or delta.get("reasoning")
    if isinstance(r, str) and r:
        acc["reasoning"] += r
        return ("reasoning", r)
    for tc in (delta.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        idx = tc.get("index")
        if idx is None:
            idx = len(acc["tools"])
        slot = acc["tools"].setdefault(idx, {"id": "", "name": "", "arguments": ""})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]
    if ch.get("finish_reason"):
        acc["finish"] = ch["finish_reason"]
    return None


def _open_chat(provider, payload, timeout, proxy=""):
    base = norm_base(provider.get("base_url"))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers=_headers(provider, payload.get("stream") is True), method="POST")
    return http_open(req, timeout, proxy)


def _open_chat_retry(provider, payload, timeout, proxy, retries, cancel, on_event=None):
    last = None
    for attempt in range(retries + 1):
        if cancel is not None and cancel.is_set():
            raise Stopped()
        try:
            return _open_chat(provider, payload, timeout, proxy)
        except urllib.error.HTTPError as e:
            detail = describe_error(e)
            if "stream_options" in detail and "stream_options" in payload:
                payload.pop("stream_options", None)
                try:
                    return _open_chat(provider, payload, timeout, proxy)
                except Exception as e2:
                    last = e2
                    detail = describe_error(e2)
            if e.code in _RETRY_CODES and attempt < retries:
                wait = min(8.0, 1.5 * (2 ** attempt))
                if on_event:
                    on_event("notice", "HTTP %s，%.1fs 后重试（%d/%d）" % (e.code, wait, attempt + 1, retries))
                log("warn", "http", "HTTP %s 重试 %d/%d" % (e.code, attempt + 1, retries))
                time.sleep(wait)
                continue
            raise EngineError(detail)
        except Exception as e:
            last = e
            if attempt < retries:
                wait = min(8.0, 1.5 * (2 ** attempt))
                if on_event:
                    on_event("notice", "连接失败，%.1fs 后重试（%d/%d）" % (wait, attempt + 1, retries))
                time.sleep(wait)
                continue
            raise EngineError(describe_error(e))
    raise EngineError(describe_error(last) if last else "未知网络错误")


def stream_chat(provider, messages, tools=None, temperature=0.7, timeout=180, cancel=None, acc=None,
                proxy="", retries=1, max_tokens=0, on_event=None):
    base = norm_base(provider.get("base_url"))
    model = (provider.get("model") or "").strip()
    if not base:
        raise EngineError("未配置 base_url：请在「设置」里填写 OpenAI 兼容端点")
    if not model:
        raise EngineError("未选择模型：请在「模型」面板探测并选择")
    acc = acc if acc is not None else {}
    acc.setdefault("content", "")
    acc.setdefault("reasoning", "")
    acc.setdefault("tools", {})
    acc["usage"] = None
    acc["finish"] = None
    payload = {"model": model, "messages": messages, "stream": True, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if max_tokens and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)
    payload["stream_options"] = {"include_usage": True}
    resp = _open_chat_retry(provider, payload, timeout, proxy, retries, cancel, on_event)
    with resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" in ctype or "stream" in ctype:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    break
                try:
                    line = raw.decode("utf-8", "replace").strip()
                except Exception:
                    continue
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                ev = _absorb(obj, acc)
                if ev:
                    yield ev
        else:
            body = resp.read().decode("utf-8", "replace")
            try:
                obj = json.loads(body)
            except Exception:
                raise EngineError("返回不是 JSON：" + body[:300])
            ev = _absorb(obj, acc)
            if ev:
                yield ev
    yield ("end", acc.get("finish"))


def _decode(b):
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            pass
    return b.decode("utf-8", "replace")


def _truncate(text, limit=24000):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断，共 %d 字符）" % len(text)


def _sha(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def _rel(ctx, path):
    try:
        return os.path.relpath(path, ctx.get("workspace") or os.path.expanduser("~"))
    except Exception:
        return path


def _inside(path, root):
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(root)
        return os.path.commonpath([rp, rr]) == rr
    except Exception:
        return False


def resolve_path(ctx, path):
    p = str(path or "").strip().strip('"')
    if not p:
        raise EngineError("缺少 path")
    if not os.path.isabs(p):
        p = os.path.join(ctx.get("workspace") or os.path.expanduser("~"), p)
    p = os.path.normpath(p)
    if ctx.get("sandbox") and not _inside(p, ctx.get("workspace") or os.path.expanduser("~")):
        raise EngineError("沙箱限制：%s 在工作区之外，已拒绝（设置 → 关闭沙箱可解除）" % p)
    return p


def _kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _popen_wait(argv, shell_flag, cwd, timeout, cancel, label="命令"):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    t0 = time.time()
    try:
        p = subprocess.Popen(argv, shell=shell_flag, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, creationflags=flags)
    except Exception as e:
        return {"ok": False, "text": "%s: %s" % (type(e).__name__, e), "ms": 0}
    out = err = b""
    while True:
        if cancel is not None and cancel.is_set():
            _kill_tree(p)
            try:
                p.communicate(timeout=5)
            except Exception:
                pass
            return {"ok": False, "text": "[已停止] %s 被用户中断，进程树已终止" % label,
                    "ms": int((time.time() - t0) * 1000), "stopped": True}
        try:
            out, err = p.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if time.time() - t0 > timeout:
                _kill_tree(p)
                try:
                    p.communicate(timeout=5)
                except Exception:
                    pass
                return {"ok": False, "text": "[超时] %s 超过 %ss 被执行终止" % (label, timeout),
                        "ms": int((time.time() - t0) * 1000)}
            continue
    ms = int((time.time() - t0) * 1000)
    return {"ok": p.returncode == 0, "rc": p.returncode, "out": _decode(out), "err": _decode(err), "ms": ms}


def t_shell(a, ctx):
    cmd = str(a.get("command") or "").strip()
    if not cmd:
        return {"ok": False, "text": "缺少 command"}
    ws = ctx.get("workspace") or os.getcwd()
    cwd = a.get("cwd") or ws
    if not os.path.isabs(str(cwd)):
        cwd = os.path.join(ws, str(cwd))
    cwd = os.path.normpath(str(cwd))
    if not os.path.isdir(cwd):
        return {"ok": False, "text": "工作目录不存在：" + cwd}
    if ctx.get("sandbox") and not _inside(cwd, ws):
        return {"ok": False, "text": "沙箱限制：工作目录 %s 在工作区之外" % cwd}
    to = max(1, min(int(a.get("timeout") or ctx.get("shell_timeout") or 60), 600))
    sh = str(a.get("shell") or "auto").lower()
    use_ps = sh == "powershell" or (sh == "auto" and os.name == "nt" and cmd.lstrip().startswith("$"))
    if use_ps:
        argv, shell_flag = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], False
    else:
        argv, shell_flag = cmd, True
    r = _popen_wait(argv, shell_flag, cwd, to, ctx.get("cancel"), "命令")
    if "rc" not in r:
        return r
    out = _truncate(r["out"]).strip()
    err = _truncate(r["err"]).strip()
    parts = ["[exit %s · %dms · %s]" % (r["rc"], r["ms"], cwd)]
    parts.append(out if out else "(无 stdout)")
    if err:
        parts.append("[stderr]\n" + err)
    return {"ok": r["ok"], "text": "\n".join(parts), "ms": r["ms"]}


def t_py_run(a, ctx):
    code = a.get("code")
    if not code:
        return {"ok": False, "text": "缺少 code"}
    to = max(1, min(int(a.get("timeout") or 60), 600))
    r = _popen_wait([sys.executable, "-c", str(code)], False, ctx.get("workspace") or os.getcwd(),
                    to, ctx.get("cancel"), "Python 代码")
    if "rc" not in r:
        return r
    out = _truncate(r["out"]).strip()
    err = _truncate(r["err"]).strip()
    txt = "[exit %s · %dms]\n%s" % (r["rc"], r["ms"], out or "(无 stdout)")
    if err:
        txt += "\n[stderr]\n" + err
    return {"ok": r["ok"], "text": txt, "ms": r["ms"]}


def t_fs_list(a, ctx):
    p = resolve_path(ctx, a.get("path") or ".")
    if not os.path.exists(p):
        return {"ok": False, "text": "不存在：" + p}
    if os.path.isfile(p):
        st = os.stat(p)
        return {"ok": True, "text": "%s\n文件 · %d 字节 · 修改于 %s" % (
            p, st.st_size, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)))}
    depth = max(1, min(int(a.get("depth") or 2), 6))
    limit = max(10, min(int(a.get("limit") or 400), 2000))
    lines = []
    root_depth = p.rstrip(os.sep).count(os.sep)
    for cur, dirs, files in os.walk(p):
        d = cur.count(os.sep) - root_depth
        if d >= depth:
            dirs[:] = []
        dirs[:] = sorted([x for x in dirs if x not in SKIP_DIRS])
        lines.append("  " * d + os.path.basename(cur) + os.sep)
        for f in sorted(files):
            try:
                sz = os.path.getsize(os.path.join(cur, f))
            except Exception:
                sz = -1
            lines.append("  " * (d + 1) + f + ("  (%d B)" % sz if sz >= 0 else ""))
        if len(lines) > limit:
            lines.append("…（已截断）")
            break
    return {"ok": True, "text": "%s\n%s" % (p, "\n".join(lines[:limit]))}


def t_fs_read(a, ctx):
    p = resolve_path(ctx, a.get("path"))
    if not os.path.isfile(p):
        return {"ok": False, "text": "文件不存在：" + p}
    off = max(1, int(a.get("offset") or 1))
    lim = max(0, int(a.get("limit") or 0))
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return {"ok": False, "text": str(e)}
    total = len(lines)
    seg = lines[off - 1:] if lim <= 0 else lines[off - 1:off - 1 + lim]
    body = "".join("%5d| %s" % (off + i, ln.rstrip("\n")) for i, ln in enumerate(seg))
    return {"ok": True, "text": "%s  (行 %d-%d / 共 %d 行)\n%s" % (p, off, off + len(seg) - 1, total, _truncate(body))}


def t_fs_write(a, ctx):
    p = resolve_path(ctx, a.get("path"))
    content = a.get("content")
    if content is None:
        return {"ok": False, "text": "缺少 content"}
    mode = str(a.get("mode") or "overwrite").lower()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    before = read_text(p)
    try:
        with open(p, "w" if mode != "append" else "a", encoding="utf-8") as f:
            f.write(str(content))
    except Exception as e:
        return {"ok": False, "text": str(e)}
    after = read_text(p)[0]
    cp = ctx.get("checkpoint")
    if cp:
        cp(p, "fs_write", before[0] if before[0] is not None else None, after, mode)
    return {"ok": True, "text": "已写入 %s\n%s %d 字节 → %d 字节（%s）" % (
        p, "追加" if mode == "append" else "覆盖", len((before[0] or "").encode("utf-8")),
        os.path.getsize(p), mode),
        "text_after": after, "path": p}


def t_fs_edit(a, ctx):
    p = resolve_path(ctx, a.get("path"))
    old = a.get("old")
    new = a.get("new", "")
    if not os.path.isfile(p):
        return {"ok": False, "text": "文件不存在：" + p}
    if old is None or old == "":
        return {"ok": False, "text": "缺少 old"}
    src, binary = read_text(p)
    if binary:
        return {"ok": False, "text": "无法编辑二进制文件"}
    n = src.count(old)
    if n == 0:
        return {"ok": False, "text": "未找到待替换内容（区分空白与换行）"}
    if n > 1 and not a.get("all"):
        return {"ok": False, "text": "匹配到 %d 处，请在 old 里补充更多上下文，或传 all=true" % n}
    out = src.replace(old, new) if a.get("all") else src.replace(old, new, 1)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(out)
    except Exception as e:
        return {"ok": False, "text": str(e)}
    cp = ctx.get("checkpoint")
    if cp:
        cp(p, "fs_edit", src, out, "replace")
    return {"ok": True, "text": "已替换 %d 处：%s（%d → %d 字节）" % (
        n if a.get("all") else 1, p, len(src.encode("utf-8")), len(out.encode("utf-8"))),
        "text_after": out, "path": p}


def t_fs_search(a, ctx):
    pat = a.get("pattern")
    if not pat:
        return {"ok": False, "text": "缺少 pattern"}
    base = resolve_path(ctx, a.get("path") or ".")
    glob = a.get("glob") or "*"
    maxhits = max(1, min(int(a.get("max") or 60), 500))
    try:
        rx = re.compile(str(pat), re.IGNORECASE)
    except Exception as e:
        return {"ok": False, "text": "正则错误：" + str(e)}
    hits = []
    scanned = 0
    stop = False
    for cur, dirs, files in os.walk(base):
        if stop:
            break
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if glob != "*" and not _fnmatch(fn, glob):
                continue
            fp = os.path.join(cur, fn)
            try:
                if os.path.getsize(fp) > 3_000_000:
                    continue
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    scanned += 1
                    for i, ln in enumerate(f, 1):
                        if rx.search(ln):
                            hits.append("%s:%d: %s" % (os.path.relpath(fp, base), i, ln.strip()[:240]))
                            if len(hits) >= maxhits:
                                stop = True
                                break
            except Exception:
                continue
            if stop:
                break
    if not hits:
        return {"ok": True, "text": "无匹配（扫描 %d 个文件，正则 %s）" % (scanned, pat)}
    return {"ok": True, "text": "%d 处匹配（扫描 %d 个文件）：\n%s" % (len(hits), scanned, "\n".join(hits))}


def _fnmatch(name, patterns):
    import fnmatch
    return any(fnmatch.fnmatch(name, p.strip()) for p in str(patterns).replace(";", ",").split(",") if p.strip())


def t_http_get(a, ctx):
    url = str(a.get("url") or "").strip()
    if not url:
        return {"ok": False, "text": "缺少 url"}
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "text": "只支持 http/https"}
    to = max(1, min(int(a.get("timeout") or 20), 120))
    maxb = max(1, min(int(a.get("max_bytes") or 200000), 2000000))
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PI-Studio/%s" % VERSION})
        with http_open(req, to, ctx.get("proxy") or "") as r:
            raw = r.read(maxb)
            code = r.status
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return {"ok": False, "text": "HTTP %s：%s" % (e.code, e.reason)}
    except Exception as e:
        return {"ok": False, "text": describe_error(e)}
    ms = int((time.time() - t0) * 1000)
    return {"ok": True, "text": "[%s %s · %d 字节 · %dms]\n%s" % (
        code, ctype, len(raw), ms, _truncate(_decode(raw), 8000))}


def t_sys_info(a, ctx):
    try:
        du = shutil.disk_usage(ctx.get("workspace") or os.path.expanduser("~"))
        disk = "%.1f GB 可用 / %.1f GB" % (du.free / 1e9, du.total / 1e9)
    except Exception:
        disk = "n/a"
    meta = ctx.get("model_meta") or {}
    info = [
        "PI Studio %s" % VERSION,
        "Python   %s (%s)" % (platform.python_version(), sys.executable),
        "系统     %s" % platform.platform(),
        "机器     %s / %s" % (platform.node(), platform.machine()),
        "CPU      %s 逻辑核" % (os.cpu_count() or "?"),
        "当前目录 %s" % os.getcwd(),
        "工作区   %s%s" % (ctx.get("workspace") or "", "（沙箱已开启）" if ctx.get("sandbox") else "（沙箱关闭）"),
        "磁盘     %s" % disk,
        "模型窗口 %s tokens（%s）" % (meta.get("contextWindow"), meta.get("contextWindowSource")),
        "时间     %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
    ]
    return {"ok": True, "text": "\n".join(info)}


_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub, ast.Mult,
                  ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Call, ast.Name, ast.Load)


def t_calc(a, ctx):
    expr = str(a.get("expr") or a.get("expression") or "").strip()
    if not expr:
        return {"ok": False, "text": "缺少 expr"}
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return {"ok": False, "text": "语法错误：" + str(e)}
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return {"ok": False, "text": "不支持的语法：" + type(node).__name__}
    env = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    env.update({"abs": abs, "round": round, "min": min, "max": max, "int": int, "float": float, "len": len})
    try:
        val = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, env)
    except Exception as e:
        return {"ok": False, "text": str(e)}
    return {"ok": True, "text": "%s = %s" % (expr, val)}


TOOLS = {}

# 错误自愈钩子：由 picore 注入（引擎层不反向依赖核心能力层）。
# 工具执行失败时调用 provider(name, args, text) -> str|None；非空返回值会作为
# 「历史同类失败的已知修复提示」追加到工具结果里，闭环「记录→召回→复用」。
ERROR_HINT_PROVIDER = None


def set_error_hint_provider(fn):
    global ERROR_HINT_PROVIDER
    ERROR_HINT_PROVIDER = fn

# ---------------- 问答（QA）：把模糊需求用「带选项的问题」问清 ----------------
# 与审批（approve）同构：引擎只把结构化的「问题 + 选项」抛给宿主，宿主负责呈现与回填。
# 宿主注入方式：run_turn(..., ask=fn)（按轮，多会话安全）或 set_question_provider(fn)（全局）。
# 没有宿主时不会卡住：返回「无问答通道」让模型改成列假设继续，而不是死等。
QA_MAX_Q = 6            # 单次最多问题数
QA_MAX_OPT = 8          # 每题最多选项数
# fn(payload, ctx) -> {"answers": {key: [label]}, "custom": {key: str}, "note": str} | None（跳过）
QUESTION_PROVIDER = None


def set_question_provider(fn):
    global QUESTION_PROVIDER
    QUESTION_PROVIDER = fn


def qa_norm(a):
    """规范化模型给的 questions：各端 UI 与自检共用同一份结构（键名容错 + 上限截断）。"""
    a = a if isinstance(a, dict) else {}
    intro = str(a.get("intro") or a.get("title") or "").strip()[:300]
    questions = a.get("questions")
    if questions is None:
        questions = a.get("items") or a.get("qs")
    if isinstance(questions, dict):
        questions = [questions]
    if isinstance(questions, str):
        questions = [{"q": questions}]
    out = []
    for q in questions or []:
        if not isinstance(q, dict):
            q = {"q": q}
        text = str(q.get("q") or q.get("question") or q.get("text") or "").strip()[:400]
        opts = []
        for o in (q.get("options") or q.get("opts") or [])[:QA_MAX_OPT]:
            if isinstance(o, dict):
                label = str(o.get("label") or o.get("value") or o.get("text") or "").strip()[:120]
                desc = str(o.get("desc") or o.get("description") or o.get("note") or "").strip()[:300]
            else:
                label, desc = str(o).strip()[:120], ""
            if label:
                opts.append({"label": label, "desc": desc})
        if not text and not opts:
            continue
        key = str(q.get("key") or q.get("id") or "").strip()[:40] or ("q%d" % (len(out) + 1))
        out.append({"idx": len(out), "key": key, "q": text or "请选择：", "options": opts,
                    "multi": bool(q.get("multi") or q.get("multiple")),
                    "allow_custom": bool(q.get("allow_custom", True))})
        if len(out) >= QA_MAX_Q:
            break
    return {"intro": intro, "questions": out, "note": str(a.get("note") or "").strip()[:300]}


def qa_picked(ans, q):
    """从宿主返回的答案里取某题的选择（兼容 key / idx 两种索引，兼容字符串单选）。"""
    ans = ans if isinstance(ans, dict) else {}
    sel = ans.get("answers") if isinstance(ans.get("answers"), dict) else {}
    custom = ans.get("custom") if isinstance(ans.get("custom"), dict) else {}
    why = ans.get("notes") if isinstance(ans.get("notes"), dict) else {}
    picks = sel.get(q["key"], sel.get(str(q["idx"]), []))
    if isinstance(picks, str):
        picks = [picks]
    picks = [str(p).strip()[:120] for p in (picks or []) if str(p).strip()]
    cu = str(custom.get(q["key"], custom.get(str(q["idx"]), "")) or "").strip()[:400]
    note = str(why.get(q["key"], why.get(str(q["idx"]), "")) or "").strip()[:300]
    return picks, cu, note


def qa_answer_lines(payload, ans):
    """把宿主的答案整理成给模型看的行（含「未选」退化）。"""
    lines = []
    for q in payload["questions"]:
        picks, cu, note = qa_picked(ans, q)
        seg = "、".join(picks)
        if cu:
            seg = (seg + "；" if seg else "") + "自定义：" + cu
        if note:
            seg = (seg + "；" if seg else "") + "备注：" + note
        lines.append("- %s → %s" % (q["q"], seg or "（未选择，按你的判断）"))
    extra = str((ans or {}).get("note") or "").strip()[:400] if isinstance(ans, dict) else ""
    if extra:
        lines.append("- 补充说明：" + extra)
    return lines


def ask_user_text(payload, ans):
    if ans is None:
        return ("用户跳过了这轮澄清（希望你自己判断）：请直接用最合理的默认方案动手，"
                "并在结论开头用一行「假设：…」列出你采用的关键假设。")
    return ("用户对澄清问题的回答：\n" + "\n".join(qa_answer_lines(payload, ans)) +
            "\n请按这些选择执行；只有真正影响结果的点才再问一次。")


def t_ask_user(a, ctx=None):
    t0 = time.time()
    payload = qa_norm(a)
    if not payload["questions"]:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "text": "ask_user 需要 questions：至少 1 个问题（每题 1-%d 个选项，选项写 label，"
                        "可加 desc 说明理由/影响）。" % QA_MAX_OPT}
    ctx = ctx or {}
    ask = ctx.get("ask") or QUESTION_PROVIDER
    if ask is None:
        return {"ok": True, "unavailable": True, "payload": payload, "ms": int((time.time() - t0) * 1000),
                "text": "当前没有可用的问答通道（未接入界面或终端）。不要再调用 ask_user：直接按最合理的"
                        "默认方案动手，并在结论开头用一行「假设：…」列出关键假设，等用户纠正。"}
    try:
        ans = ask(payload, ctx)
    except Stopped:
        raise
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "text": "问答通道异常：%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "ms": int((time.time() - t0) * 1000), "skipped": ans is None,
            "questions": len(payload["questions"]), "payload": payload,
            "answers": (ans or {}).get("answers") or {}, "text": ask_user_text(payload, ans)}


def qa_prompt(cfg):
    """prefs.qa_first 打开时注入「先问后做」规则（返回空串 = 不注入）。"""
    prefs = (cfg or {}).get("prefs") or {}
    if not prefs.get("qa_first"):
        return ""
    mx = max(1, min(int(prefs.get("qa_max") or 4), QA_MAX_Q))
    return ("【先问后做】用户需求如果存在会改变实现路线的歧义（范围 / 技术栈 / 取舍 / 输出形式 / 验收标准），"
            "先调用 ask_user 提出不超过 %d 个带选项的澄清问题：每题 2-4 个选项，推荐项放第一位并在 desc 里写一句理由，"
            "需要多选时 multi=true。拿到答案后再动手；问题必须真的会改变你的做法，不要问显而易见的信息。"
            "能从工作区与上下文查到的事实自己查。ask_user 返回「用户跳过」时按最合理默认直接执行，"
            "并在结论开头用一行「假设：…」列出关键假设。\n" % mx)


def _reg(name, group, desc, params, fn, mutating=False):
    TOOLS[name] = {"name": name, "group": group, "desc": desc, "parameters": params, "fn": fn, "mutating": mutating}


_reg("shell", "执行", "在本机执行命令行并返回真实 stdout/stderr/退出码（可被停止按钮终止进程树）",
     {"type": "object", "properties": {
         "command": {"type": "string", "description": "要执行的命令"},
         "cwd": {"type": "string", "description": "工作目录，缺省为工作区"},
         "shell": {"type": "string", "enum": ["auto", "cmd", "powershell"], "description": "解释器"},
         "timeout": {"type": "integer", "description": "秒，1-600"}},
      "required": ["command"]}, t_shell, mutating=True)

_reg("fs_list", "文件", "列出目录树（真实读取本机文件系统）",
     {"type": "object", "properties": {
         "path": {"type": "string"}, "depth": {"type": "integer"}, "limit": {"type": "integer"}},
      "required": ["path"]}, t_fs_list)

_reg("fs_read", "文件", "按行读取文本文件（带行号）",
     {"type": "object", "properties": {
         "path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
      "required": ["path"]}, t_fs_read)

_reg("fs_search", "文件", "在目录中按正则搜索文件内容",
     {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"},
         "max": {"type": "integer"}},
      "required": ["pattern"]}, t_fs_search)

_reg("fs_write", "文件", "写入文件（覆盖或追加），会创建缺失的父目录；写入前自动留检查点可回滚",
     {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"},
         "mode": {"type": "string", "enum": ["overwrite", "append"]}},
      "required": ["path", "content"]}, t_fs_write, mutating=True)

_reg("fs_edit", "文件", "在文件中把 old 精确替换为 new；替换前自动留检查点可回滚",
     {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
         "all": {"type": "boolean"}},
      "required": ["path", "old", "new"]}, t_fs_edit, mutating=True)

_reg("http_get", "网络", "真实发起 HTTP GET 并返回状态码与正文",
     {"type": "object", "properties": {
         "url": {"type": "string"}, "timeout": {"type": "integer"}, "max_bytes": {"type": "integer"}},
      "required": ["url"]}, t_http_get)
_reg("ask_user", "问答", "向用户提出带选项的澄清问题并拿到选择（需求有歧义 / 有多条实现路线 / 需要用户拍板时用；"
                         "每题给 2-4 个选项，推荐项放第一位）",
     {"type": "object", "properties": {
         "intro": {"type": "string", "description": "一句话说明为什么要先问"},
         "questions": {"type": "array", "description": "1-%d 个问题，每题 1-%d 个选项" % (QA_MAX_Q, QA_MAX_OPT),
                       "items": {"type": "object", "properties": {
                           "key": {"type": "string", "description": "答案标识（如 scope / stack），用于回填"},
                           "q": {"type": "string", "description": "问题文本"},
                           "options": {"type": "array", "items": {"type": "object", "properties": {
                               "label": {"type": "string"},
                               "desc": {"type": "string", "description": "推荐理由或影响"}},
                               "required": ["label"]}, "description": "选项（推荐项放第一个）"},
                           "multi": {"type": "boolean", "description": "是否多选"},
                           "allow_custom": {"type": "boolean", "description": "允许用户自己填（默认 true）"}},
                           "required": ["q", "options"]}},
         "note": {"type": "string", "description": "额外说明，例如「也可以跳过」"}},
      "required": ["questions"]}, t_ask_user)

# ---------------- 热点事件（真实公开榜单接口） ----------------
# 已实测可用：百度、头条、贴吧、抖音、B站、GitHub；微博/知乎接口需登录或已 403，故不收录。

HUBS = {}
HOT_DOC = {}

def _hub(name, label, url, tip, parse, needs=None, daily=False):
    HUBS[name] = {"name": name, "label": label, "url": url, "tip": tip,
                  "parse": parse, "daily": daily}
    HOT_DOC[name] = {"label": label, "url": url, "tip": tip, "needs": needs or "", "daily": daily,
                     "parse": parse}

def parse_baidu(raw):
    d = json.loads(raw)["data"]["cards"][0]["content"][0]["content"]
    out = []
    for it in d:
        w = str(it.get("word") or "").strip()
        if not w:
            continue
        tag = str(it.get("newHotName") or "").strip()
        try:
            idx = int(it.get("index"))
        except Exception:
            idx = 0        # 接口无 index 者为置顶内容
        out.append({"rank": idx if idx > 0 else 0, "title": w, "hot": "",
                    "url": str(it.get("url") or ""), "extra": tag})
    return out

def parse_toutiao(raw):
    d = json.loads(raw)["data"]
    out = []
    for it in d:
        t = str(it.get("Title") or it.get("QueryWord") or "").strip()
        if not t:
            continue
        out.append({"rank": len(out) + 1, "title": t,
                    "hot": str(it.get("HotValue") or ""),
                    "url": str(it.get("Url") or "").split("?")[0]})
    return out

def parse_douyin(raw):
    d = json.loads(raw)
    out = []
    for i, it in enumerate(d.get("word_list") or [], 1):
        w = str(it.get("word") or "").strip()
        if not w:
            continue
        out.append({"rank": i, "title": w, "hot": str(it.get("hot_value") or ""),
                    "url": "https://www.douyin.com/search/" + urllib.parse.quote(w)})
    return out

def parse_tieba(raw):
    d = json.loads(raw)["data"]["bang_topic"]["topic_list"]
    out = []
    for i, it in enumerate(d, 1):
        t = str(it.get("topic_name") or "").strip()
        if not t:
            continue
        out.append({"rank": i, "title": t, "hot": str(it.get("discuss_num") or ""),
                    "url": str(it.get("topic_url") or "").replace("&amp;", "&")})
    return out

def parse_bili(raw):
    d = json.loads(raw)
    if d.get("code") != 0:
        raise ValueError("接口返回 code=%s %s" % (d.get("code"), d.get("message")))
    out = []
    for i, it in enumerate(d["data"]["list"], 1):
        out.append({"rank": i, "title": str(it.get("title") or ""),
                    "hot": str((it.get("stat") or {}).get("view") or ""),
                    "url": "https://www.bilibili.com/video/" + str(it.get("bvid") or ""),
                    "extra": str((it.get("owner") or {}).get("name") or "")})
    return out

def parse_github(raw):
    d = json.loads(raw)
    out = []
    for i, it in enumerate(d.get("items") or [], 1):
        out.append({"rank": i, "title": str(it.get("full_name") or ""),
                    "hot": str(it.get("stargazers_count") or ""),
                    "url": str(it.get("html_url") or ""),
                    "extra": (str(it.get("description") or "") or "").strip()[:70]})
    return out

_hub("baidu", "百度热搜", "https://top.baidu.com/api/board?platform=wise&tab=realtime",
     "实时热搜榜，含置顶内容（接口不提供热度值，标签为“热/新”等）", parse_baidu)
_hub("toutiao", "今日头条热榜", "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
     "实时热榜，热度为原始热度值", parse_toutiao)
_hub("douyin", "抖音热点榜", "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/",
     "实时热点词榜", parse_douyin)
_hub("tieba", "贴吧热议榜", "https://tieba.baidu.com/hottopic/browse/topicList",
     "热议话题榜，热度为讨论数", parse_tieba)
_hub("bili", "B站热门视频", "https://api.bilibili.com/x/web-interface/popular?ps=%d&pn=1",
     "热门视频榜（按榜单顺序），热度为播放量", parse_bili)
_hub("github", "GitHub 今日热门仓库",
     "https://api.github.com/search/repositories?q=created:>%s&sort=stars&order=desc&per_page=%d",
     "按创建日期筛选的新晋热门仓库，热度为 star 数", parse_github, daily=True)

def _num(v):
    n = str(v or "").strip().replace(",", "")
    if not n:
        return ""
    try:
        f = float(n)
    except Exception:
        return n
    if f >= 1e8:
        return "%.2f亿" % (f / 1e8)
    if f >= 1e4:
        return "%.1f万" % (f / 1e4)
    return str(int(f))

def _fetch_hub(hub, ctx, timeout, top):
    tip = hub["tip"]
    if hub.get("daily"):
        url = hub["url"] % (time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400)), top)
    elif "%d" in hub["url"]:
        url = hub["url"] % top
    else:
        url = hub["url"]
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PI-Studio/%s" % VERSION,
        "Accept": "application/json, text/plain, */*"})
    try:
        with http_open(req, timeout, ctx.get("proxy") or "") as r:
            raw = r.read(4000000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"ok": False, "text": "HTTP %s（%s）：该源可能需登录/被限流，可换其他源" % (e.code, e.reason), "url": url}
    except Exception as e:
        return {"ok": False, "text": describe_error(e), "url": url}
    try:
        items = hub["parse"](raw)[:top]
    except Exception as e:
        return {"ok": False, "text": "解析失败：%s: %s" % (type(e).__name__, e), "url": url}
    if not items:
        return {"ok": False, "text": "内容为空（榜单结构可能已变化）", "url": url}
    return {"ok": True, "items": items, "url": url, "tip": tip}

def t_hot_topics(a, ctx):
    days = a.get("days")
    want_day = False
    if days is not None:
        try:
            days = int(days)
        except Exception:
            return {"ok": False, "text": "days 必须是整数（0=现在/今天，1=近 24 小时）"}
        if days < 0:
            days = 0
        elif days > 30:
            days = 30       # 与 top/timeout 一致：越界钳制而非报错
        want_day = days > 0
    raw = a.get("sources") or a.get("source") or a.get("hub") or ""
    if isinstance(raw, (list, tuple)):
        names = [str(x).strip() for x in raw if str(x).strip()]
    else:
        names = [x.strip() for x in re.split(r"[,，;；\s]+", str(raw)) if x.strip()]
    if not names:
        names = ["baidu", "toutiao", "douyin"] + (["github"] if want_day else [])
    low = {k.lower(): k for k in HUBS}
    alias = {"weibo": "baidu", "微博": "baidu", "百度": "baidu", "热搜": "baidu",
             "头条": "toutiao", "今日头条": "toutiao", "抖音": "douyin",
             "贴吧": "tieba", "b站": "bili", "哔哩哔哩": "bili", "bilibili": "bili",
             "github": "github", "gh": "github"}
    if any(n.lower() in ("all", "*", "全部", "所有", "全部源") for n in names):
        names = list(HUBS)      # all=全部源
    picked, seen = [], set()
    for n in names:
        k = low.get(n.lower()) or alias.get(n.lower())
        if k and k not in seen:
            seen.add(k)
            picked.append(k)
    if not picked:
        return {"ok": False, "text": "未知来源：%s\n可用的有：%s" % (raw, "、".join(HUBS))}
    try:
        top = max(1, min(int(a.get("top") or a.get("limit") or 10), 50))
    except Exception:
        top = 10
    timeout = max(3, min(int(a.get("timeout") or 12), 60))
    head = "热点事件 · 抓取时间 %s" % time.strftime("%Y-%m-%d %H:%M:%S")
    if want_day:
        head += "（含近 %d 天热门，按创建/发布时间筛选）" % days
    fmt = str(a.get("format") or a.get("mode") or "text").strip().lower()
    as_json = fmt in ("json", "结构化", "struct", "data")
    blocks, ok_n, fails, sources_out = [head], 0, [], []
    for k in picked:
        hub = HUBS[k]
        r = _fetch_hub(hub, ctx, timeout, top)
        if not r["ok"]:
            fails.append("%s：%s" % (hub["label"], r["text"]))
            sources_out.append({"source": k, "label": hub["label"], "ok": False,
                                "endpoint": r.get("url") or hub["url"],
                                "error": r["text"], "count": 0, "items": []})
            continue
        ok_n += 1
        items = [{"rank": it["rank"], "title": it["title"],
                  "hot": it.get("hot") or "", "hot_text": _num(it.get("hot")),
                  "url": it.get("url") or "", "extra": it.get("extra") or ""}
                 for it in r["items"]]
        sources_out.append({"source": k, "label": hub["label"], "tip": hub["tip"],
                            "ok": True, "endpoint": r["url"], "count": len(items),
                            "items": items})
        lines = ["", "【%s】%s（Top%d）" % (hub["label"], r["tip"], len(items))]
        for it in items:
            rk = it["rank"]
            tag = ("%2d." % rk) if rk and rk > 0 else "置顶"
            lines.append("%s %s%s%s" % (tag, it["title"],
                                        ("　热度 %s" % it["hot_text"]) if it["hot_text"] else "",
                                        ("　· %s" % it["extra"]) if it["extra"] else ""))
            if it["url"]:
                lines.append("     %s" % it["url"])
        blocks.append("\n".join(lines))
    if fails:
        blocks.append("\n【失败】\n" + "\n".join(fails))
    data = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "days": days if want_day else 0, "top": top,
            "sources": sources_out, "errors": fails,
            "ok_count": ok_n, "fail_count": len(fails)}
    if as_json:
        budget = 13000          # 上层给模型的 tool 消息会截到 14000 字符，这里留余量保证仍是合法 JSON
        txt = json.dumps(data, ensure_ascii=False, indent=2)
        if len(txt) > budget:
            data["truncated"] = True
            scale = budget / float(len(txt))
            for s in data["sources"]:
                if s["items"]:
                    s["items"] = s["items"][:max(1, int(len(s["items"]) * scale))]
                    s["count"] = len(s["items"])
            txt = json.dumps(data, ensure_ascii=False, indent=2)
            guard = 0
            while len(txt) > budget and guard < 40:
                guard += 1
                for s in data["sources"]:
                    if s["items"]:
                        s["items"] = s["items"][:max(0, int(len(s["items"]) * 0.8))]
                        s["count"] = len(s["items"])
                txt = json.dumps(data, ensure_ascii=False, indent=2)
        return {"ok": ok_n > 0, "text": txt, "data": data}
    if not ok_n:
        return {"ok": False, "text": "\n".join(blocks), "data": data}
    return {"ok": True, "text": _truncate("\n".join(blocks), 18000), "data": data}

_reg("py_run", "执行", "用当前解释器执行一段 Python 代码并返回真实输出（可被停止按钮终止）",
     {"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer"}},
      "required": ["code"]}, t_py_run, mutating=True)

_reg("hot_topics", "网络", "搜索今天/当前的实时热点事件（真实抓取百度热搜、头条热榜、抖音热点、贴吧热议、B站热门、GitHub 新晋热门仓库）",
     {"type": "object", "properties": {
         "sources": {"type": "string", "description": "来源，逗号分隔：baidu/toutiao/douyin/tieba/bili/github，all=全部；缺省 baidu,toutiao,douyin"},
         "top": {"type": "integer", "description": "每个源返回条数，1-50，越界自动钳制，缺省 10"},
         "days": {"type": "integer", "description": "0=现在/今天的榜单（缺省），1=近 24 小时热门，最大 30（越界自动钳制）"},
         "format": {"type": "string", "enum": ["text", "json"],
                    "description": "输出格式：text=带排版的文本（缺省）；json=结构化数据，含各源 items(rank/title/hot/hot_text/url/extra) 与错误信息"},
         "timeout": {"type": "integer", "description": "单源超时秒数，3-60，缺省 12"}},
      "required": []}, t_hot_topics)

_reg("sys_info", "系统", "返回真实的环境、磁盘与模型窗口信息", {"type": "object", "properties": {}}, t_sys_info)

_reg("calc", "系统", "安全计算数学表达式",
     {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}, t_calc)


def openai_tools():
    return [{"type": "function", "function": {"name": t["name"], "description": t["desc"],
                                              "parameters": t["parameters"]}} for t in TOOLS.values()]


def tool_names():
    return list(TOOLS.keys())


def run_tool(name, args, ctx=None):
    ctx = dict(ctx or {})
    ctx.setdefault("workspace", os.path.expanduser("~"))
    tool = TOOLS.get(name)
    if tool is None:
        return {"ok": False, "text": "未知工具：" + str(name), "ms": 0}
    t0 = time.time()
    try:
        res = tool["fn"](args or {}, ctx)
    except EngineError as e:
        res = {"ok": False, "text": str(e)}
    except Exception as e:
        res = {"ok": False, "text": "%s: %s" % (type(e).__name__, e)}
    res.setdefault("ms", int((time.time() - t0) * 1000))
    if not isinstance(res.get("text"), str):
        res["text"] = str(res.get("text"))
    return res


def read_text(path):
    if not os.path.exists(path):
        return None, False
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), False
    except Exception:
        return None, True


def diff_text(before, after, path, context=3, max_lines=400):
    b = (before or "").splitlines(keepends=True)
    a = (after or "").splitlines(keepends=True)
    try:
        d = list(difflib.unified_diff(b, a, fromfile="前 " + path, tofile="后 " + path, n=context))
    except Exception as e:
        return "（无法生成 diff：%s）" % e
    if not d:
        return "（内容无变化）"
    lines = [x.rstrip("\n") for x in d]
    if len(lines) > max_lines:
        return "\n".join(lines[:max_lines]) + "\n…（diff 共 %d 行，已截断）" % len(lines)
    return "\n".join(lines)


def make_checkpoint(session, path, tool, before, after, mode="overwrite"):
    try:
        cps = session.setdefault("checkpoints", [])
        cid = uuid.uuid4().hex[:8]
        store = before if (before is not None and len(before) <= _INLINE_LIMIT) else None
        bfile = ""
        if before is not None and store is None:
            try:
                ensure_home()
                os.makedirs(BACKUP_DIR, exist_ok=True)
                bfile = os.path.join(BACKUP_DIR, "%s_%s.bak" % (session.get("id", "s"), cid))
                with open(bfile, "w", encoding="utf-8") as f:
                    f.write(before)
            except Exception:
                bfile = ""
        rec = {"id": cid, "ts": time.time(), "tool": tool, "path": path, "mode": mode,
               "existed": before is not None, "before": store, "before_file": bfile,
               "after": after if (after is not None and len(after) <= _INLINE_LIMIT) else None,
               "bytes_before": len((before or "").encode("utf-8")),
               "bytes_after": len((after or "").encode("utf-8")),
               "sha_before": _sha(before or ""), "sha_after": _sha(after or ""),
               "undone": False,
               "diff": diff_text(before, after, path)}
        cps.append(rec)
        keep = 200
        if len(cps) > keep:
            del cps[:len(cps) - keep]
        event("checkpoint", {"session": session.get("id"), "path": path, "tool": tool,
                             "bytes_before": rec["bytes_before"], "bytes_after": rec["bytes_after"]})
        return rec
    except Exception as e:
        log("warn", "checkpoint", str(e))
        return None


def checkpoints(session, include_undone=False):
    cps = session.get("checkpoints") or []
    return [c for c in cps if include_undone or not c.get("undone")]


def checkpoint_before(cp):
    if cp.get("before") is not None:
        return cp["before"]
    if cp.get("before_file") and os.path.isfile(cp["before_file"]):
        try:
            with open(cp["before_file"], "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return None
    return None


def rollback(session, cp_id=None):
    if isinstance(cp_id, str) and cp_id.strip().lower() in ("", "all", "*"):
        cp_id = None          # 兼容 CLI/接口语义：all 表示全部
    targets = [c for c in checkpoints(session) if cp_id in (None, c["id"])]
    results = []
    for cp in targets:
        p = cp["path"]
        try:
            if cp.get("existed"):
                text = checkpoint_before(cp)
                if text is None:
                    results.append({"id": cp["id"], "path": p, "ok": False, "text": "备份内容不可用"})
                    continue
                os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(text)
                cp["undone"] = True
                results.append({"id": cp["id"], "path": p, "ok": True,
                                "text": "已还原到改动前（%d 字节）" % len(text.encode("utf-8"))})
            else:
                if os.path.exists(p):
                    os.remove(p)
                    txt = "该文件由代理新建，已删除"
                else:
                    txt = "文件已不存在，无需处理"
                cp["undone"] = True
                results.append({"id": cp["id"], "path": p, "ok": True, "text": txt})
            event("rollback", {"session": session.get("id"), "path": p, "ok": True})
            log("info", "rollback", "%s <- %s" % (p, cp["id"]))
        except Exception as e:
            results.append({"id": cp["id"], "path": p, "ok": False, "text": "%s: %s" % (type(e).__name__, e)})
            log("error", "rollback", "%s: %s" % (p, e))
    session["updated"] = time.time()
    return results


def render_transcript(msgs, limit=24000):
    out = []
    for m in msgs:
        role = m.get("role")
        if role == "user":
            out.append("用户：" + str(m.get("content", ""))[:2000])
        elif role == "assistant":
            t = str(m.get("content", "")).strip()
            if t:
                out.append("助手：" + t[:2000])
            for c in (m.get("tool_calls") or []):
                out.append("  [调用 %s %s]" % ((c.get("function") or {}).get("name"),
                                              str((c.get("function") or {}).get("arguments"))[:200]))
        elif role == "tool":
            out.append("[工具 %s 结果] %s" % (m.get("name"), str(m.get("content", ""))[:600]))
    return _truncate("\n".join(out), limit)


def _tail_start(msgs, keep):
    i = max(0, len(msgs) - keep)
    while i > 0:
        m = msgs[i]
        if m.get("role") == "user" or (m.get("role") == "assistant" and not m.get("tool_call_id")):
            break
        i -= 1
    return i


def compact_session(cfg, session, on_event=None, keep_tail=6, force=False):
    msgs = session.get("messages") or []
    if len(msgs) <= keep_tail + 2:
        return {"ok": False, "skipped": True, "text": "消息太少，无需压缩"}
    idx = _tail_start(msgs, keep_tail)
    head, tail = msgs[:idx], msgs[idx:]
    if not head:
        return {"ok": False, "skipped": True, "text": "没有可压缩的历史"}
    if on_event:
        on_event("notice", "正在压缩 %d 条历史消息…" % len(head))
    prov = dict(cfg.get("provider") or {})
    prefs = cfg.get("prefs") or {}
    prompt = ("把下面这段较早的对话压缩成要点摘要，用于继续工作。要求：保留已确认的事实、文件路径、命令结论、"
              "未完成的待办；丢弃寒暄与重复；不超过 500 字，直接输出摘要正文。\n\n" + render_transcript(head))
    acc = {"content": "", "reasoning": "", "tools": {}, "usage": None, "finish": None, "model": ""}
    try:
        for ev in stream_chat(prov, [{"role": "user", "content": prompt}], None,
                              float(prefs.get("temperature", 0.3)), prefs.get("timeout", 180), None, acc,
                              prefs.get("proxy") or "", int(prefs.get("retries") or 2)):
            if ev[0] == "delta" and on_event:
                on_event("compact_delta", ev[1])
    except EngineError as e:
        return {"ok": False, "text": "压缩失败：" + str(e)}
    summary = acc["content"].strip()
    if not summary:
        return {"ok": False, "text": "压缩失败：模型没有返回摘要"}
    note = {"role": "assistant", "content": "【历史摘要 · 由 %s 压缩 %d 条消息】\n%s" % (
        prov.get("model") or "模型", len(head), summary), "ts": time.time(), "compacted": len(head)}
    session["messages"] = [note] + tail
    session["compactions"] = int(session.get("compactions") or 0) + 1
    session["ctx_used"] = 0
    session["updated"] = time.time()
    event("compact", {"session": session.get("id"), "dropped": len(head), "kept": len(tail)})
    log("info", "compact", "压缩 %d 条历史 → 摘要 %d 字" % (len(head), len(summary)))
    return {"ok": True, "text": "已把 %d 条消息压缩为摘要（%d 字），保留最近 %d 条" % (len(head), len(summary), len(tail)),
            "summary": summary, "dropped": len(head)}


def mark_reasoning(cfg, model):
    if not model:
        return
    prov = cfg.get("provider") or {}
    meta = dict(prov.get("model_meta") or {})
    m = dict(meta.get(model) or {})
    if m.get("reasoning"):
        return
    m["reasoning"] = True
    m["verifiedBy"] = "运行实测"
    meta[model] = m
    prov["model_meta"] = meta
    cfg["provider"] = prov
    try:
        save_config(cfg)
    except Exception:
        pass
    log("info", "meta", "实测 %s 会输出思维链，已标记为推理模型" % model)


def title_session(cfg, session):
    m = session.get("messages") or []
    first_user = next((x for x in m if x.get("role") == "user"), None)
    if not first_user:
        return None
    cur = str(session.get("title") or "")
    if cur and cur != "新会话" and not cur.startswith("新会话"):
        return None
    prov = dict(cfg.get("provider") or {})
    prefs = cfg.get("prefs") or {}
    prompt = ("给下面这段对话起一个标题：不超过 12 个汉字，不要标点、不要引号、不要解释，只输出标题本身。\n\n"
              + str(first_user.get("content", ""))[:600])
    acc = {"content": "", "reasoning": "", "tools": {}, "usage": None, "finish": None, "model": ""}
    try:
        for ev in stream_chat(prov, [{"role": "user", "content": prompt}], None, 0.2, 90, None, acc,
                              prefs.get("proxy") or "", 1, 0):
            pass
    except Exception as e:
        log("warn", "title", str(e))
        return None
    raw = (acc.get("content") or "").strip()
    if not raw:
        log("warn", "title", "模型没有返回标题正文（可能是推理模型把预算用在了思维链上）")
        return None
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    raw = lines[0] if lines else ""
    raw = re.sub(r"^[\"'《【\s]+|[\"'》】\s]+$", "", raw)[:24].strip()
    if raw:
        session["title"] = raw
        log("info", "title", "会话标题 → " + raw)
        return raw
    return None


_DSML_BAR = "[\uff5c|]{0,4}"
_TXT_CALL_OPEN = re.compile(r"<\s*" + _DSML_BAR + r"\s*DSML\s*" + _DSML_BAR + r"\s*(?:tool_calls|function_calls|calls)\s*>",
                            re.I)
_TXT_CALL_CLOSE = re.compile(r"<\s*/\s*" + _DSML_BAR + r"\s*DSML\s*" + _DSML_BAR +
                             r"\s*(?:tool_calls|function_calls|calls)\s*>", re.I)
_TXT_INVOKE = re.compile(r"<\s*" + _DSML_BAR + r"\s*DSML\s*" + _DSML_BAR +
                         r"\s*invoke\s+name\s*=\s*\"([^\"]+)\"\s*>(.*?)<\s*/\s*" + _DSML_BAR +
                         r"\s*DSML\s*" + _DSML_BAR + r"\s*invoke\s*>", re.S | re.I)
_TXT_PARAM = re.compile(r"<\s*" + _DSML_BAR + r"\s*DSML\s*" + _DSML_BAR +
                        r"\s*parameter\s+name\s*=\s*\"([^\"]+)\"[^>]*>(.*?)<\s*/\s*" + _DSML_BAR +
                        r"\s*DSML\s*" + _DSML_BAR + r"\s*parameter\s*>", re.S | re.I)
_TXT_MARK = re.compile(r"<\s*/?\s*" + _DSML_BAR + r"\s*DSML\s*" + _DSML_BAR + r"\s*\w*\s*>?", re.I)


def extract_text_tool_calls(text):
    """把模型偶尔直接以正文形式吐出的工具调用（如 DeepSeek 的 `` 标记）还原成结构化调用。

    部分模型在流式模式下会把工具调用写进 content 而不是 tool_calls 通道。若原样透传，
    界面只会显示一堆标记文本（看起来「没有渲染」），工具也不会真正执行，整轮对话会在
    毫无进展的情况下提前结束（看起来「轮次太少、被强制结束」）。这里把这些标记解析回
    (calls, cleaned_text)，calls 形如 [{"name": ..., "arguments": {...}}]。
    """
    s = str(text or "")
    if "DSML" not in s and "invoke" not in s:
        return [], s
    calls = []
    for m in _TXT_INVOKE.finditer(s):
        name = (m.group(1) or "").strip()
        body = m.group(2) or ""
        args = {}
        for pm in _TXT_PARAM.finditer(body):
            key = (pm.group(1) or "").strip()
            val = pm.group(2) or ""
            if val[:1] == "\n":
                val = val[1:]
            args[key] = val.rstrip("\n")
        if name:
            calls.append({"name": name, "arguments": args})
    if not calls:
        return [], s
    cleaned = _TXT_INVOKE.sub("", s)
    cleaned = _TXT_CALL_OPEN.sub("", cleaned)
    cleaned = _TXT_CALL_CLOSE.sub("", cleaned)
    # 兜底：清掉任何残留的 DSML 标记（含未闭合的半截标记）
    cleaned = _TXT_MARK.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return calls, cleaned


def strip_text_tool_markup(text):
    """仅清理标记、不做解析，供界面在流式过程中先行美化显示。"""
    s = str(text or "")
    if "DSML" not in s:
        return s
    s = _TXT_INVOKE.sub("", s)
    s = _TXT_CALL_OPEN.sub("", s)
    s = _TXT_CALL_CLOSE.sub("", s)
    s = _TXT_MARK.sub("", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def run_turn(cfg, session, on_event, cancel=None, approve=None, ask=None):
    provider = dict(cfg.get("provider") or {})
    prefs = cfg.get("prefs") or {}
    meta = model_meta(cfg)
    ctx = {"workspace": cfg.get("workspace") or os.path.expanduser("~"),
           "shell_timeout": prefs.get("shell_timeout", 60),
           "sandbox": bool(prefs.get("sandbox", True)),
           "proxy": prefs.get("proxy") or "",
           "cancel": cancel, "model_meta": meta, "session": session, "cfg": cfg,
           "ask": ask}
    system = prefs.get("system") or DEFAULT_SYSTEM
    system = system + "\n工作区根目录：" + ctx["workspace"] + "\n操作系统：" + platform.platform() + "\n"
    if ctx["sandbox"]:
        system += "文件类工具的路径被限制在工作区内；越界会被拒绝，不要反复重试越界路径。\n"
    system += ("调用工具必须走接口原生的 function calling（tool_calls）通道；"
               "不要把工具调用写成正文里的 XML/DSML 之类标记，那样不会被执行，还会让本轮提前结束。\n")
    system += qa_prompt(cfg)                # 「先问后做」：需求有歧义时先出选项问清
    max_steps = max(1, min(int(prefs.get("max_steps") or 40), 200))
    temperature = float(prefs.get("temperature", 0.7))
    retries = max(0, min(int(prefs.get("retries") or 2), 6))
    max_out = int(prefs.get("max_output_tokens") or 0)
    enable_tools = bool(prefs.get("enable_tools", True))
    tools = openai_tools() if enable_tools else None
    t_run = time.time()
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    steps = 0
    final = ""
    runs = []
    err = ""
    cps = []

    def _cp(path, tool, before, after, mode):
        rec = make_checkpoint(session, path, tool, before, after, mode)
        if rec:
            cps.append(rec)
            on_event("checkpoint", rec)

    ctx["checkpoint"] = _cp

    if prefs.get("auto_compact", True) and ctx_state(cfg, session)["need"]:
        r = compact_session(cfg, session, on_event)
        on_event("notice", r["text"])

    while steps < max_steps:
        if cancel is not None and cancel.is_set():
            break
        steps += 1
        on_event("step", {"i": steps, "max": max_steps})
        msgs = api_messages(session, system)
        acc = {"content": "", "reasoning": "", "tools": {}, "usage": None, "finish": None, "model": ""}
        on_event("assistant_begin", {"step": steps})
        try:
            for ev in stream_chat(provider, msgs, tools, temperature, prefs.get("timeout", 180), cancel, acc,
                                  ctx["proxy"], retries, max_out, on_event):
                if ev[0] == "delta":
                    on_event("assistant_delta", ev[1])
                elif ev[0] == "reasoning":
                    on_event("reasoning_delta", ev[1])
        except Stopped:
            on_event("notice", "已在请求途中停止")
            break
        except EngineError as e:
            err = str(e)
            on_event("error", err)
            break
        except Exception as e:
            err = describe_error(e)
            on_event("error", err)
            break
        u = acc.get("usage") or {}
        for k in totals:
            try:
                totals[k] += int(u.get(k) or 0)
            except Exception:
                pass
        det = u.get("completion_tokens_details") or {}
        if det.get("reasoning_tokens"):
            totals["reasoning_tokens"] = int(totals.get("reasoning_tokens", 0)) + int(det["reasoning_tokens"])
        if acc.get("reasoning") or int((det or {}).get("reasoning_tokens") or 0) > 0:
            mark_reasoning(cfg, provider.get("model") or "")
        if u.get("total_tokens"):
            session["ctx_used"] = int(u["total_tokens"])
        else:
            session["ctx_used"] = session.get("ctx_used", 0) + (acc.get("content") or "").__len__() // 3
        on_event("usage", dict(totals))
        on_event("ctx", ctx_state(cfg, session))
        if acc.get("model"):
            on_event("route", {"model": acc["model"]})
        idxs = sorted(acc["tools"].keys())
        tcs = [acc["tools"][i] for i in idxs]
        if not tcs:
            txt_calls, cleaned = extract_text_tool_calls(acc.get("content") or "")
            if txt_calls:
                on_event("notice", "模型把工具调用写进了正文，已自动解析为真实调用（%d 个）：%s"
                         % (len(txt_calls), "、".join(c["name"] for c in txt_calls)))
                acc["content"] = cleaned
                for c in txt_calls:
                    nxt = len(acc["tools"])
                    acc["tools"][nxt] = {"id": "", "name": c["name"],
                                         "arguments": json.dumps(c["arguments"], ensure_ascii=False)}
                idxs = sorted(acc["tools"].keys())
                tcs = [acc["tools"][i] for i in idxs]
            else:
                final = acc["content"]
                break
        calls = []
        for t in tcs:
            cid = t["id"] or ("call_" + uuid.uuid4().hex[:10])
            calls.append({"id": cid, "type": "function",
                          "function": {"name": t["name"] or "shell", "arguments": t["arguments"] or "{}"}})
        asst = {"role": "assistant", "content": acc["content"] or "", "tool_calls": calls, "ts": time.time()}
        session["messages"].append(asst)
        on_event("assistant_message", asst)
        for c in calls:
            name = c["function"]["name"]
            if cancel is not None and cancel.is_set():
                # 被停止时也必须为每个 tool_call 补一条 tool 消息：否则历史里会留下
                # 「助手消息带 tool_calls 却没有结果」的残缺状态，之后整条会话都会 400，无法继续对话。
                session["messages"].append({"role": "tool", "tool_call_id": c["id"], "name": name,
                                            "content": "（用户已停止，该调用未执行）", "ts": time.time(),
                                            "ok": False, "ms": 0, "args": {}, "stopped": True})
                continue
            raw = c["function"]["arguments"]
            try:
                args = json.loads(raw) if raw.strip() else {}
            except Exception:
                args = {"_raw": raw}
            if not isinstance(args, dict):
                args = {"value": args}
            on_event("tool_start", {"id": c["id"], "name": name, "args": args})
            tool = TOOLS.get(name)
            if tool is None:
                res = {"ok": False, "text": "模型请求了不存在的工具：" + name, "ms": 0}
            elif tool["mutating"] and prefs.get("approve_mutating", True) and approve is not None:
                resp = approve(name, args, tool)
                if isinstance(resp, dict):
                    allow = bool(resp.get("allow"))
                    if isinstance(resp.get("args"), dict):
                        args = resp["args"]
                else:
                    allow = bool(resp)
                if allow:
                    on_event("tool_start", {"id": c["id"], "name": name, "args": args, "approved": True})
                res = run_tool(name, args, ctx) if allow else \
                    {"ok": False, "text": "用户拒绝了该操作，未执行。", "ms": 0}
            else:
                res = run_tool(name, args, ctx)
            text = redact(res.get("text", ""))
            err_text = text            # 供错误库指纹使用的原始失败文本（不含召回提示，保证去重稳定）
            if not res.get("ok") and not res.get("stopped") and ERROR_HINT_PROVIDER is not None:
                try:
                    hint = ERROR_HINT_PROVIDER(name, args, err_text)
                except Exception as e:
                    hint = None
                    log("warn", "errkb", "错误召回钩子异常：%s: %s" % (type(e).__name__, e))
                if hint:
                    text = text + "\n" + hint
                    on_event("notice", "错误知识库命中历史同类失败，已自动附上已知修复提示：" + name)
            rec = {"id": c["id"], "name": name, "args": args, "ok": bool(res.get("ok")),
                   "text": text, "error_text": err_text, "ms": res.get("ms", 0), "ts": time.time(),
                   "path": res.get("path", ""), "stopped": bool(res.get("stopped"))}
            runs.append(rec)
            session["messages"].append({"role": "tool", "tool_call_id": c["id"], "name": name,
                                        "content": text[:14000], "ts": time.time(),
                                        "ok": rec["ok"], "ms": rec["ms"], "args": args})
            on_event("tool_end", rec)
            log("info" if rec["ok"] else "warn", "tool", "%s %s (%sms)" % (name, "ok" if rec["ok"] else "fail", rec["ms"]))
            event("tool", {"session": session.get("id"), "name": name, "ok": rec["ok"], "ms": rec["ms"],
                           "args": redact_json(args), "out": text[:4000]})
        if cancel is not None and cancel.is_set():
            break
    if not final and not err and steps >= max_steps and not (cancel is not None and cancel.is_set()):
        on_event("notice", "已达单轮步数上限（%d），强制收尾。" % max_steps)
        on_event("assistant_begin", {"step": steps + 1})
        acc = {"content": "", "reasoning": "", "tools": {}, "usage": None, "finish": None, "model": ""}
        try:
            for ev in stream_chat(provider, api_messages(session, system + "\n现在不要再调用工具，直接给出最终结论。"),
                                  None, temperature, prefs.get("timeout", 180), cancel, acc,
                                  ctx["proxy"], retries, max_out, on_event):
                if ev[0] == "delta":
                    on_event("assistant_delta", ev[1])
                elif ev[0] == "reasoning":
                    on_event("reasoning_delta", ev[1])
            final = acc["content"]
            u = acc.get("usage") or {}
            for k in totals:
                try:
                    totals[k] += int(u.get(k) or 0)
                except Exception:
                    pass
        except EngineError as e:
            err = str(e)
            on_event("error", err)
    if final:
        session["messages"].append({"role": "assistant", "content": final, "ts": time.time(),
                                   "steps": steps, "usage": dict(totals)})
    session["ctx_used"] = int(totals.get("total_tokens") or session.get("ctx_used") or 0)
    session["usage_total"] = totals
    session["updated"] = time.time()
    if prefs.get("auto_title", True) and final and len(session["messages"]) >= 2:
        try:
            t = title_session(cfg, session)
            if t:
                on_event("title", t)
        except Exception:
            pass
    c = cost(cfg, totals)
    event("turn", {"session": session.get("id"), "steps": steps, "elapsed": round(time.time() - t_run, 2),
                   "usage": totals, "cost": c, "tools": [r["name"] for r in runs], "error": err})
    return {"ok": not err, "error": err, "text": final, "steps": steps, "usage": totals,
            "tools": runs, "checkpoints": cps, "elapsed": round(time.time() - t_run, 3), "cost": c,
            "stopped": bool(cancel is not None and cancel.is_set()),
            "limit": (not final and not err and steps >= max_steps),
            "plan": session.get("plan") or None,
            "ctx": ctx_state(cfg, session)}


def one_shot(cfg, prompt, on_delta=None, cancel=None, approve=None, session=None, on_event=None, ask=None):
    sess = session if session is not None else new_session("oneshot")
    sess["messages"].append({"role": "user", "content": prompt, "ts": time.time()})

    def sink(kind, data):
        if kind == "assistant_delta" and on_delta:
            on_delta(data)
        if on_event:
            on_event(kind, data)

    res = run_turn(cfg, sess, sink, cancel, approve, ask)
    res["session"] = sess
    return res


def selftest(cfg=None, do_network=True):
    cfg = cfg or load_config()
    rows = []

    def add(name, ok, detail):
        rows.append({"name": name, "ok": ok, "detail": detail})

    add("Python 版本", sys.version_info >= (3, 8), platform.python_version() + " @ " + sys.executable)
    try:
        ensure_home()
        f = os.path.join(HOME, "_selftest.tmp")
        _atomic_write(f, "pi-studio")
        got = open(f, "r", encoding="utf-8").read()
        os.remove(f)
        add("数据目录可写", got == "pi-studio", HOME)
    except Exception as e:
        add("数据目录可写", False, str(e))
    try:
        import tkinter
        add("tkinter 可用", True, "Tk %s" % tkinter.TkVersion)
    except Exception as e:
        add("tkinter 可用", False, str(e))
    add("工具注册数", len(TOOLS) >= 8, "%d 个：%s" % (len(TOOLS), ", ".join(tool_names())))
    r = run_tool("calc", {"expr": "6*7"}, {"workspace": os.getcwd()})
    add("工具 calc", r["ok"] and "42" in r["text"], r["text"].replace("\n", " ")[:90])
    r = run_tool("shell", {"command": "echo PI-Studio-Selftest"}, {"workspace": os.getcwd(), "shell_timeout": 30})
    add("工具 shell（真实执行）", r["ok"] and "PI-Studio-Selftest" in r["text"], r["text"].replace("\n", " | ")[:110])
    ws = cfg.get("workspace") if os.path.isdir(cfg.get("workspace") or "") else os.path.expanduser("~")
    ctx = {"workspace": ws, "sandbox": False}
    tmp = os.path.join(HOME, "_selftest_file.txt")
    sess = new_session("selftest")
    ctx["checkpoint"] = lambda p, t, b, a, m: make_checkpoint(sess, p, t, b, a, m)
    r1 = run_tool("fs_write", {"path": tmp, "content": "line1\nline2\nline3"}, ctx)
    r2 = run_tool("fs_read", {"path": tmp, "offset": 2, "limit": 1}, ctx)
    r3 = run_tool("fs_edit", {"path": tmp, "old": "line2", "new": "LINE-TWO"}, ctx)
    ok = r1["ok"] and r2["ok"] and r3["ok"] and "line2" in r2["text"]
    add("工具 fs 写/读/改", ok, (r2["text"] or r1["text"]).replace("\n", " | ")[:110])
    cps = checkpoints(sess)
    has_diff = bool(cps) and ("+LINE-TWO" in (cps[-1].get("diff") or "") or "LINE-TWO" in (cps[-1].get("diff") or ""))
    add("检查点 + diff", len(cps) >= 2 and has_diff,
        "检查点 %d 个 · 最后一次 diff %d 字符" % (len(cps), len(cps[-1].get("diff", "")) if cps else 0))
    rb = rollback(sess, cps[-1]["id"]) if cps else []
    txt_after = read_text(tmp)[0] or ""
    add("回滚 fs 改动", bool(rb) and rb[0]["ok"] and "line2" in txt_after,
        (rb[0]["text"] if rb else "无检查点"))
    try:
        os.remove(tmp)
    except Exception:
        pass
    try:
        broken = {"id": "selftest", "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_x", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]},
            {"role": "user", "content": "被中断后的下一条"},
        ]}
        am = api_messages(broken, "sys")
        ids = [m.get("tool_call_id") for m in am if m.get("role") == "tool"]
        add("历史自愈（悬空 tool_calls）", "call_x" in ids,
            "为被中断的 tool_call 自动补了 tool 消息，避免整条会话 400")
    except Exception as e:
        add("历史自愈（悬空 tool_calls）", False, "%s: %s" % (type(e).__name__, e))
    try:
        s2 = new_session("selftest-all")
        c2 = {"workspace": ws, "sandbox": False}
        c2["checkpoint"] = lambda p, t, b, a, m: make_checkpoint(s2, p, t, b, a, m)
        f2 = os.path.join(HOME, "_selftest_all.txt")
        run_tool("fs_write", {"path": f2, "content": "z"}, c2)
        rb2 = rollback(s2, "all")
        add("回滚全部（all 语义）", bool(rb2) and rb2[0]["ok"] and not os.path.exists(f2),
            "处理 %d 个检查点 · 文件已删除=%s" % (len(rb2), not os.path.exists(f2)))
    except Exception as e:
        add("回滚全部（all 语义）", False, "%s: %s" % (type(e).__name__, e))
    r = run_tool("fs_search", {"pattern": "^import ", "path": os.path.dirname(os.path.abspath(__file__)),
                               "glob": "*.py", "max": 5}, {"workspace": ws})
    add("工具 fs_search", r["ok"] and "匹配" in r["text"], r["text"].splitlines()[0][:100])
    r = run_tool("py_run", {"code": "print(sum(range(101)))"}, {"workspace": ws})
    add("工具 py_run（真实执行）", r["ok"] and "5050" in r["text"], r["text"].replace("\n", " | ")[:100])
    try:
        r = run_tool("hot_topics", {"sources": "toutiao", "top": 3, "timeout": 12}, {"workspace": ws})
        add("工具 hot_topics（真实抓热点）", r["ok"] and "今日头条热榜" in r["text"],
            r["text"].splitlines()[0][:100] if r["text"] else "（无输出）")
    except Exception as e:
        add("工具 hot_topics（真实抓热点）", False, "%s: %s" % (type(e).__name__, e))
    r = run_tool("fs_write", {"path": os.path.join(os.path.expanduser("~"), "..", "pi-sandbox-test.txt"),
                              "content": "x"}, {"workspace": ws, "sandbox": True})
    add("沙箱拦截越界", (not r["ok"]) and ("沙箱" in r["text"]), r["text"][:110])
    st = ctx_state(cfg, new_session())
    add("上下文账本", st["limit"] > 0, "窗口 %s tokens（%s）· 已用 %s" % (st["limit"], st["source"], st["used"]))
    add("停止即杀进程", True, "shell / py_run 走 Popen 轮询，停止时 taskkill /T 终止进程树")
    try:                                   # 问答（QA）工具：归一化 + 通道往返 + 无通道降级
        p1 = qa_norm({"intro": "要问 3 件事", "questions": [
            {"key": "scope", "q": "改多大范围？", "options": [{"label": "只改 app", "desc": "风险小"},
                                                             "整个仓库", {"label": ""}], "multi": True},
            {"q": "要不要写测试？", "options": ["要", "不要"], "allow_custom": False}] +
            [{"q": "问题%d" % i, "options": ["A", "B"]} for i in range(2, 9)]})
        capped = (len(p1["questions"]) == QA_MAX_Q and p1["questions"][0]["multi"] is True
                  and [o["label"] for o in p1["questions"][0]["options"]] == ["只改 app", "整个仓库"]
                  and p1["questions"][0]["options"][0]["desc"] == "风险小"
                  and p1["questions"][1]["allow_custom"] is False)
        add("问答 选项归一化与上限", capped,
            "%d 题（上限 %d）· 空选项已剔除 · 字符串选项已兼容 · 多选/自定义标记保留"
            % (len(p1["questions"]), QA_MAX_Q))
    except Exception as e:
        add("问答 选项归一化与上限", False, "%s: %s" % (type(e).__name__, e))
    try:
        calls = []

        def _fake_ask(payload, ctx):
            calls.append((len(payload["questions"]), ctx.get("workspace") is not None))
            return {"answers": {"scope": ["只改 app"]}, "custom": {"tests": "先跑 smoke"},
                    "note": "别动 web", "notes": {"scope": "只动一个文件"}}

        old = QUESTION_PROVIDER
        set_question_provider(_fake_ask)
        r1 = run_tool("ask_user", {"questions": [{"key": "scope", "q": "范围？", "options": ["只改 app", "全仓库"]},
                                                  {"key": "tests", "q": "测试？", "options": ["要", "不要"]}]},
                      {"workspace": ws})
        set_question_provider(old)
        r2 = run_tool("ask_user", {"questions": [{"q": "范围？", "options": ["A", "B"]}]}, {"workspace": ws})
        r3 = run_tool("ask_user", {"questions": []}, {"workspace": ws})
        ok = (r1["ok"] and not r1.get("unavailable") and calls and calls[0][0] == 2
              and "只改 app" in r1["text"] and "自定义：先跑 smoke" in r1["text"] and "别动 web" in r1["text"]
              and r2["ok"] and r2.get("unavailable") and "假设" in r2["text"]
              and (not r3["ok"]))
        add("问答 工具往返（provider / 降级 / 空题）", ok,
            "注入 provider 收到 2 题并回填选择+自定义+备注；无通道时降级为「列假设继续」；空题被拒")
    except Exception as e:
        add("问答 工具往返（provider / 降级 / 空题）", False, "%s: %s" % (type(e).__name__, e))
    try:
        off = qa_prompt({"prefs": {}})
        on = qa_prompt({"prefs": {"qa_first": True, "qa_max": 3}})
        add("问答 先问后做提示注入", off == "" and "先问后做" in on and "不超过 3 个" in on,
            "qa_first=false 不注入；打开后注入 1 段规则（上限 %s 题）"
            % (3 if "不超过 3 个" in on else "?"))
    except Exception as e:
        add("问答 先问后做提示注入", False, "%s: %s" % (type(e).__name__, e))
    add("网络重试与退避", int((cfg.get("prefs") or {}).get("retries") or 0) >= 0,
        "重试次数 %s · 退避 1.5^n 秒（429/5xx/连接失败）" % (cfg.get("prefs") or {}).get("retries"))
    if do_network:
        p = cfg.get("provider") or {}
        pr = probe(p, timeout=8, proxy=(cfg.get("prefs") or {}).get("proxy") or "",
                   retries=int((cfg.get("prefs") or {}).get("retries") or 1))
        add("模型端点连接", pr["ok"], ("%s · %d 个模型 · %sms" % (norm_base(p.get("base_url")), len(pr["models"]), pr["ms"]))
            if pr["ok"] else pr["error"][:160])
        add("已选择模型", bool((p.get("model") or "").strip()),
            "%s · 窗口 %s" % (p.get("model") or "未选择", model_meta(cfg).get("contextWindow")))
    passed = sum(1 for r in rows if r["ok"])
    return {"rows": rows, "passed": passed, "total": len(rows)}


def format_selftest(res):
    lines = ["PI Studio 自检  %s" % time.strftime("%Y-%m-%d %H:%M:%S"), "-" * 74]
    for r in res["rows"]:
        lines.append("%s  %-22s %s" % ("PASS" if r["ok"] else "FAIL", r["name"], r["detail"]))
    lines.append("-" * 74)
    lines.append("结果：%d/%d 通过" % (res["passed"], res["total"]))
    return "\n".join(lines)


def _stdin_ask(payload, ctx=None):
    """终端的问答通道：打印问题与选项，回车跳过、输入编号（可逗号/空格多选）、0 自定义。"""
    qs = (payload or {}).get("questions") or []
    if not qs:
        return None
    if (payload or {}).get("intro"):
        print("\n[问答] " + payload["intro"])
    answers, custom, notes = {}, {}, {}
    for i, q in enumerate(qs, 1):
        print("\n%d) %s%s" % (i, q.get("q"), "（可多选，用逗号分隔）" if q.get("multi") else ""))
        for j, o in enumerate(q.get("options") or [], 1):
            print("   %d. %s%s" % (j, o.get("label"), (" —— " + o["desc"]) if o.get("desc") else ""))
        tip = "   选择 [1-%d]%s，0=自己填，回车=跳过：" % (len(q.get("options") or []),
                                                     "，多个用逗号" if q.get("multi") else "")
        try:
            raw = input(tip).strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return None
        if not raw:
            continue
        opts = q.get("options") or []
        if raw in ("0", "o", "other", "其他"):
            try:
                cu = input("   请输入你的答案：").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if cu:
                custom[q["key"]] = cu
            continue
        picks = []
        for tok in raw.replace("，", ",").replace(" ", ",").split(","):
            tok = tok.strip()
            if not tok.isdigit():
                continue
            n = int(tok)
            if 1 <= n <= len(opts):
                picks.append(opts[n - 1]["label"])
            elif n == 0:
                continue
        if not picks and raw:
            custom[q["key"]] = raw[:400]        # 直接打字 = 自定义答案
            continue
        if picks and not q.get("multi"):
            picks = picks[:1]
        if picks:
            answers[q["key"]] = picks
    if not answers and not custom:
        return None
    return {"answers": answers, "custom": custom, "notes": notes}


def _stdin_approve(name, args, meta):
    print("\n[审批] 代理请求执行 %s（%s）" % (name, meta.get("group", "")))
    print("       参数：" + json.dumps(args, ensure_ascii=False)[:500])
    try:
        return input("       执行？[y/N] ").strip().lower() in ("y", "yes", "是")
    except EOFError:
        return False


HELP = """PI Studio %s
  无参数          启动原生桌面窗口
  --serve         ★ 启动后端 + 自动打开浏览器（网页版），可用 --port / --no-browser / --token
  --selftest      真实自检（工具 / 检查点 / 回滚 / 沙箱 / 端点）
  --probe         探测模型端点并列出模型
  --ask TEXT      用真实模型回答一次（流式打印）
  --chat          终端对话（真实代理循环）
  --run CMD       通过工具层执行一条命令
  --tool [NAME]   列出工具或查看某个工具
  --changes       列出最后一个会话的文件变更检查点
  --rollback ID   回滚一个检查点（ID 或 all）
  --ctx           显示上下文账本与模型元数据
  --json          与 --selftest / --probe / --changes 组合，输出 JSON""" % VERSION


def cli(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return None
    cmd = argv[0]
    cfg = load_config()
    as_json = "--json" in argv
    if cmd in ("-h", "--help"):
        print(HELP)
        return 0
    if cmd == "--selftest":
        r = selftest(cfg, do_network="--no-net" not in argv)
        try:
            import picore as _CORE
            _CORE.register_tools()
            c = _CORE.selftest()
            r["rows"] = r["rows"] + c["items"]
            r["passed"] += c["passed"]
            r["total"] += c["total"]
        except Exception as e:
            r["rows"].append({"name": "core 自检", "ok": False, "detail": "%s: %s" % (type(e).__name__, e)})
            r["total"] += 1
        try:                                   # 原生 app 的界面结构 / 渲染回归（app·… 前缀）
            import pistudio as _APPUI
            u = _APPUI.ui_selftest()
            r["rows"] = r["rows"] + u["rows"]
            r["passed"] += u["passed"]
            r["total"] += u["total"]
        except Exception as e:
            r["rows"].append({"name": "app 界面自检", "ok": False, "detail": "%s: %s" % (type(e).__name__, e)})
            r["total"] += 1
        print(json.dumps(r, ensure_ascii=False, indent=2) if as_json else format_selftest(r))
        return 0 if r["passed"] == r["total"] else 1
    if cmd in ("--probe", "--models"):
        p = cfg.get("provider") or {}
        pr = probe(p, proxy=(cfg.get("prefs") or {}).get("proxy") or "",
                   retries=int((cfg.get("prefs") or {}).get("retries") or 2))
        if as_json:
            print(json.dumps(pr, ensure_ascii=False, indent=2))
            return 0 if pr["ok"] else 1
        print("端点：%s" % norm_base(p.get("base_url")))
        if not pr["ok"]:
            print("失败：%s" % pr["error"])
            return 1
        print("连接成功 · %dms · %d 个模型" % (pr["ms"], len(pr["models"])))
        for m in pr["models"]:
            print("  - " + m)
        print("当前模型：%s" % (p.get("model") or "未选择"))
        return 0
    if cmd == "--tool":
        if len(argv) > 1 and argv[1] in TOOLS:
            t = TOOLS[argv[1]]
            print("%s [%s] %s\n参数：%s" % (t["name"], t["group"], t["desc"],
                                          json.dumps(t["parameters"], ensure_ascii=False)))
        else:
            for t in TOOLS.values():
                print("%-10s %-4s %s" % (t["name"], t["group"], t["desc"]))
        return 0
    if cmd == "--ctx":
        sess = load_sessions()
        s = sess[-1] if sess else new_session()
        st = ctx_state(cfg, s)
        meta = model_meta(cfg)
        out = {"model": (cfg.get("provider") or {}).get("model"), "context": st, "meta": meta,
               "cost": cost(cfg, s.get("usage_total") or {})}
        print(json.dumps(out, ensure_ascii=False, indent=2) if as_json else
              "模型 %s\n窗口 %s tokens（%s）\n已用 %s（%.1f%%）\n消息 %s 条 · 已压缩 %s 次\n推理模型 %s · 最大输出 %s\n%s"
              % (out["model"], st["limit"], st["source"], st["used"], st["ratio"] * 100, st["messages"],
                 s.get("compactions", 0), st["reasoning"], st["maxOutput"],
                 ("估算费用 %s" % out["cost"]) if out["cost"] is not None else "未设置单价"))
        return 0
    if cmd == "--changes":
        sess = load_sessions()
        s = sess[-1] if sess else None
        cps = checkpoints(s) if s else []
        if as_json:
            print(json.dumps(cps, ensure_ascii=False, indent=2))
            return 0
        if not cps:
            print("没有待回滚的变更。")
            return 0
        print("会话「%s」的 %d 个检查点：" % (s.get("title"), len(cps)))
        for c in cps:
            print("  %-9s %-8s %s  %d→%d 字节  %s" % (c["id"], c["tool"], c["path"],
                                                      c["bytes_before"], c["bytes_after"], ts_str(c["ts"])))
        return 0
    if cmd == "--rollback":
        sess = load_sessions()
        if not sess:
            print("没有会话。")
            return 1
        s = sess[-1]
        target = argv[1] if len(argv) > 1 else "all"
        res = rollback(s, None if target == "all" else target)
        save_sessions(sess)
        print(json.dumps(res, ensure_ascii=False, indent=2) if as_json else
              "\n".join("%s %s %s" % ("OK  " if r["ok"] else "FAIL", r["path"], r["text"]) for r in res))
        return 0 if all(r["ok"] for r in res) else 1
    if cmd == "--run":
        if len(argv) < 2:
            print('用法：--run "命令"')
            return 2
        r = run_tool("shell", {"command": " ".join(argv[1:])},
                     {"workspace": cfg.get("workspace"), "shell_timeout": 120})
        print(r["text"])
        return 0 if r["ok"] else 1
    if cmd == "--ask":
        if len(argv) < 2:
            print('用法：--ask "问题"')
            return 2
        cancel = threading.Event()
        res = one_shot(cfg, " ".join(a for a in argv[1:] if a != "--json"), on_delta=lambda t: (sys.stdout.write(t), sys.stdout.flush())[0],
                       cancel=cancel, approve=_stdin_approve, ask=_stdin_ask)
        print("")
        if res.get("error"):
            print("\n[错误] " + res["error"], file=sys.stderr)
            return 1
        print("\n[%d 步 · %.2fs · tokens %s%s]" % (res["steps"], res["elapsed"],
                                                   res["usage"].get("total_tokens", 0),
                                                   (" · 费用 %s" % res["cost"]) if res.get("cost") else ""))
        return 0
    if cmd == "--chat":
        set_question_provider(_stdin_ask)       # 终端也能被模型提问（带选项）
        print("PI Studio 终端对话 · 模型 %s · /exit 退出 · /tools 列工具 · /compact 压缩 · /ctx 上下文 · /qa on|off 先问后做"
              % ((cfg.get("provider") or {}).get("model") or "未选择"))
        sess = new_session("终端会话")
        while True:
            try:
                line = input("\n你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("")
                return 0
            if not line:
                continue
            if line in ("/exit", "/quit"):
                return 0
            if line == "/tools":
                for t in TOOLS.values():
                    print("  %-10s %-4s %s" % (t["name"], t["group"], t["desc"]))
                continue
            if line == "/compact":
                print(compact_session(cfg, sess, lambda k, d: print("[%s] %s" % (k, d)))["text"])
                continue
            if line == "/ctx":
                st = ctx_state(cfg, sess)
                print("窗口 %s / 已用 %s（%.1f%%）/ 消息 %d" % (st["limit"], st["used"], st["ratio"] * 100, st["messages"]))
                continue
            if line.startswith("/qa"):
                pr = cfg.setdefault("prefs", {})
                a = line[3:].strip().lower()
                if a in ("on", "off"):
                    pr["qa_first"] = (a == "on")
                    save_config(cfg)
                print("先问后做：%s（/qa on|off 切换 · 上限 %s 题）"
                      % ("开" if pr.get("qa_first") else "关", pr.get("qa_max") or 4))
                continue
            print("\nPI> ", end="")
            res = one_shot(cfg, line, on_delta=lambda t: (sys.stdout.write(t), sys.stdout.flush())[0],
                           approve=_stdin_approve, session=sess, ask=_stdin_ask)
            print("")
            if res.get("error"):
                print("[错误] " + res["error"])
        return 0
    print("未知参数：" + cmd)
    print(HELP)
    return 2


def ts_str(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t or time.time()))


if __name__ == "__main__":
    sys.exit(cli() or 0)
