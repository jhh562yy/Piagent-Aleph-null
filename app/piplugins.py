# -*- coding: utf-8 -*-
"""PI 本地插件基座（核心清单 26）：目录枚举 / manifest 解析 / 边界读写 / 工具注册 / 技能调用。

插件根：~/.pistudio/core/plugins/<id>/
  plugin.json  —— 声明式清单：id / name / version / description / enabled / skills[] / tools[]
  index.js     —— 可选前端 UI 扩展点（本基座不执行 JS，仅登记展示，安全边界见 docs/plugin-security-design.md）

工具类型（tools[] 每项的 type）：
  skill —— 调用技能库中的技能：载入 SKILL.md 并把调用参数注入 {{key}} 占位（本基座默认类型）
  shell —— 执行本地命令（mutating=True，走引擎审批门）

边界：文本 256KB 上限、扩展名白名单、插件根目录内的越界写一律拒绝。
接入：由 picore.init() 在 register_tools() 之后调用 register()。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

import piengine as E

PLUGIN_DIR = os.path.join(E.HOME, "core", "plugins")
MAX_TEXT = 256 * 1024
PREFIX = "plugin_"
ALLOWED_EXT = {".json", ".js", ".mjs", ".cjs", ".md", ".txt", ".py", ".css", ".html", ".csv"}
SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")

LOCK = threading.RLock()
_LOADED = {}


# ------------------------------------------------------------ 基础设施
def _core():
    import picore
    return picore


def _ensure_dir():
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    return PLUGIN_DIR


def _safe_id(name):
    name = str(name or "").strip().strip("/\\")
    if name and SAFE.match(name) and ".." not in name:
        return name
    return ""


def _path(pid, *parts):
    """插件根目录内的边界路径；越界抛错。"""
    root = os.path.abspath(os.path.join(PLUGIN_DIR, pid))
    p = os.path.abspath(os.path.join(root, *parts))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("越界路径，已拒绝：" + p)
    return p


def _wrap(fn):
    def w(a, ctx):
        try:
            r = fn(a or {}, ctx or {})
            if isinstance(r, dict):
                r.setdefault("ok", True)
                if "text" not in r:
                    r["text"] = json.dumps({k: v for k, v in r.items() if k != "text"},
                                           ensure_ascii=False)[:4000]
                return r
            return {"ok": True, "text": json.dumps(r, ensure_ascii=False)[:4000]}
        except Exception as e:
            return {"ok": False, "text": "%s: %s" % (type(e).__name__, e)}
    return w


# ------------------------------------------------------------ manifest
def read_manifest(pid):
    pid = _safe_id(pid)
    if not pid:
        return None
    p = _path(pid, "plugin.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            man = json.load(f)
    except Exception as e:
        man = {"id": pid, "_error": "%s: %s" % (type(e).__name__, e)}
    if not isinstance(man, dict):
        man = {"id": pid, "_error": "plugin.json 必须是 JSON 对象"}
    man.setdefault("id", pid)
    man.setdefault("name", pid)
    man.setdefault("version", "0.0.0")
    man.setdefault("description", "")
    man.setdefault("enabled", True)
    man.setdefault("skills", [])
    man.setdefault("tools", [])
    return man


def _write_manifest(pid, man):
    clean = {k: v for k, v in man.items() if not k.startswith("_")}
    with open(_path(pid, "plugin.json"), "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------ 技能调用（核心）
def invoke_skill(skill, args):
    """调用技能库中的技能：载入 SKILL.md 并按 args 注入 {{key}} 占位。"""
    skill = str(skill or "").strip()
    if not skill:
        return {"ok": False, "error": "未指定 skill"}
    r = _core().skill_read(skill)
    if not r.get("ok"):
        return {"ok": False, "error": "技能调用失败：" + str(r.get("error") or skill), "skill": skill}
    text = r.get("text") or ""
    rendered = text
    used = []
    for k, v in (args or {}).items():
        if k in ("action", "tool", "args_json", "id", "timeout", "cwd"):
            continue
        ph = "{{%s}}" % k
        if ph in rendered:
            rendered = rendered.replace(ph, str(v))
            used.append(k)
    return {"ok": True, "skill": skill, "text": rendered[:MAX_TEXT],
            "vars": used, "bytes": len(text)}


# ------------------------------------------------------------ 工具注册
def _tool_name(pid, name):
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(name or "tool"))
    return "%s%s__%s" % (PREFIX, pid, safe)


def _register_one(pid, man, td):
    if not isinstance(td, dict) or not td.get("name"):
        return None
    name = _tool_name(pid, td.get("name"))
    ttype = str(td.get("type") or "skill").lower()
    desc = td.get("desc") or td.get("description") or ("插件 %s 的工具" % man.get("name"))
    params = td.get("parameters") or {"type": "object", "properties": {}, "required": []}
    mutating = bool(td.get("mutating", ttype == "shell"))

    if ttype == "skill":
        skill = td.get("skill") or ""
        pdef = td.get("parameters")
        if not isinstance(pdef, dict):
            pdef = {"type": "object", "properties": {"topic": {"type": "string"}}, "required": []}
            params = pdef
            desc = "[%s] 调用技能 %s" % (man.get("name"), skill or "?")

        def _fn(a, ctx, _skill=skill, _pid=pid):
            if not _skill:
                return {"ok": False, "error": "插件 %s 的工具未配置 skill" % _pid}
            return invoke_skill(_skill, a or {})

    elif ttype == "shell":
        cmd = td.get("command") or ""

        def _fn(a, ctx, _cmd=cmd, _pid=pid):
            c = _cmd
            for k, v in (a or {}).items():
                c = c.replace("{{%s}}" % k, str(v))
            if not c.strip():
                return {"ok": False, "error": "插件 %s 的 shell 工具未配置 command" % _pid}
            t0 = time.time()
            try:
                p = subprocess.run(c, shell=True, capture_output=True,
                                   timeout=int((a or {}).get("timeout") or 120),
                                   cwd=(a or {}).get("cwd") or os.getcwd())
                out = E._decode(p.stdout or b"")      # utf-8 / gbk / cp936 自适应（无控制台时 cmd 输出 GBK）
                err = E._decode(p.stderr or b"")
                return {"ok": p.returncode == 0,
                        "text": (out + (("\n" + err) if err else ""))[:MAX_TEXT],
                        "code": p.returncode, "ms": int((time.time() - t0) * 1000)}
            except Exception as e:
                return {"ok": False, "text": "%s: %s" % (type(e).__name__, e)}
    else:
        return None

    E.TOOLS[name] = {"name": name, "group": "插件",
                     "desc": "[%s] %s" % (man.get("name"), desc),
                     "parameters": params, "fn": _wrap(_fn), "mutating": mutating,
                     "_plugin": pid}
    return name


def unload(pid=None):
    dead = [n for n, t in list(E.TOOLS.items())
            if t.get("_plugin") and (pid is None or t.get("_plugin") == pid)]
    for n in dead:
        E.TOOLS.pop(n, None)
    return dead


def load_all():
    _ensure_dir()
    unload()
    loaded = {}
    for pid in sorted(os.listdir(PLUGIN_DIR)):
        if not os.path.isdir(os.path.join(PLUGIN_DIR, pid)):
            continue
        man = read_manifest(pid)
        if not man:
            continue
        man["_tools_registered"] = []
        if man.get("enabled", True) and not man.get("_error"):
            for td in (man.get("tools") or []):
                n = _register_one(pid, man, td)
                if n:
                    man["_tools_registered"].append(n)
        loaded[pid] = man
    with LOCK:
        _LOADED.clear()
        _LOADED.update(loaded)
    return loaded


# ------------------------------------------------------------ 管理 API
def state():
    if not _LOADED:
        load_all()
    items = []
    for pid, man in sorted(_LOADED.items()):
        items.append({"id": pid, "name": man.get("name"), "version": man.get("version"),
                      "description": man.get("description"),
                      "enabled": bool(man.get("enabled", True)),
                      "skills": man.get("skills") or [],
                      "tools": [t.get("name") for t in (man.get("tools") or []) if isinstance(t, dict)],
                      "registered": man.get("_tools_registered") or [],
                      "error": man.get("_error") or "",
                      "path": os.path.join(PLUGIN_DIR, pid)})
    return {"ok": True, "items": items, "dir": PLUGIN_DIR, "total": len(items),
            "tools": len([n for n in E.TOOLS if n.startswith(PREFIX)])}


def read_plugin(pid):
    pid = _safe_id(pid)
    man = read_manifest(pid)
    if not man:
        return {"ok": False, "error": "插件不存在：" + str(pid)}
    out = {"id": pid, "name": man.get("name"), "version": man.get("version"),
           "description": man.get("description"), "enabled": bool(man.get("enabled", True)),
           "skills": man.get("skills") or [], "tools": man.get("tools") or [],
           "error": man.get("_error") or "", "path": os.path.join(PLUGIN_DIR, pid)}
    try:
        out["files"] = sorted(os.listdir(os.path.join(PLUGIN_DIR, pid)))[:50]
    except Exception:
        out["files"] = []
    return {"ok": True, "plugin": out, "text": json.dumps(out, ensure_ascii=False, indent=2)}


def set_enabled(pid, enabled):
    pid = _safe_id(pid)
    man = read_manifest(pid)
    if not man:
        return {"ok": False, "error": "插件不存在：" + str(pid)}
    man["enabled"] = bool(enabled)
    _write_manifest(pid, man)
    load_all()
    return {"ok": True, "id": pid, "enabled": bool(enabled),
            "text": "插件 %s 已%s" % (pid, "启用" if enabled else "停用")}


def delete_plugin(pid):
    pid = _safe_id(pid)
    d = os.path.join(PLUGIN_DIR, pid)
    if not pid or not os.path.isdir(d):
        return {"ok": False, "error": "插件不存在：" + str(pid)}
    unload(pid)
    shutil.rmtree(d, ignore_errors=True)
    load_all()
    E.log("info", "plugins", "插件删除 %s" % pid)
    return {"ok": True, "id": pid, "text": "已删除插件 %s" % pid}


def open_root():
    _ensure_dir()
    try:
        if os.name == "nt":
            os.startfile(PLUGIN_DIR)
        else:
            subprocess.Popen(["xdg-open", PLUGIN_DIR])
        return {"ok": True, "dir": PLUGIN_DIR, "text": "已打开插件根目录：" + PLUGIN_DIR}
    except Exception as e:
        return {"ok": False, "dir": PLUGIN_DIR, "error": "%s: %s" % (type(e).__name__, e)}


def call_tool(tool, args_json):
    tool = str(tool or "")
    t = E.TOOLS.get(tool)
    if not t or not str(t.get("name", "")).startswith(PREFIX):
        return {"ok": False, "error": "插件工具不存在：" + tool}
    try:
        args = json.loads(args_json) if isinstance(args_json, str) and args_json.strip() else (args_json or {})
    except Exception:
        args = {"_raw": args_json}
    if not isinstance(args, dict):
        args = {"value": args}
    res = t["fn"](args, {})
    res["tool"] = tool
    return res


# ------------------------------------------------------------ 示例插件（调用技能）
EXAMPLE_JS = """// index.js —— 插件前端 UI 扩展点（可选）
// 本基座（Python）不执行 JS，仅登记展示；接入前端 plugins/ui 运行时时可在此注册面板与命令。
export default {
  id: "%(pid)s",
  panels: [],
  commands: [
    {
      id: "run-skill",
      title: "调用技能 %(skill)s",
      run: (ctx) => ctx.invokeTool("plugin_%(pid)s__run_skill", { topic: "来自插件 UI" })
    },
    {
      id: "run-shell",
      title: "执行 shell 模板",
      run: (ctx) => ctx.invokeTool("plugin_%(pid)s__run_shell", { text: "来自插件 UI" })
    }
  ]
};
"""


def create_example(pid="skill-caller", skill="", overwrite=False):
    """创建示例插件：声明 skill + shell 两个工具 —— 调用具体技能并按参数注入 {{占位}}，以及执行本地命令模板。"""
    core = _core()
    _ensure_dir()
    pid = _safe_id(pid) or "skill-caller"
    skill = _safe_id(skill) or "hello-plugin-skill"

    # 1) 确保被调用技能存在
    if not core.skill_read(skill).get("ok"):
        body = (
            "# %s\n\n"
            "本技能由「本地插件基座」示例创建，用于演示插件调用技能。\n\n"
            "## 用途\n接收插件传入的参数并输出执行说明。\n\n"
            "## 参数\n- topic：调用主题，缺省「PI 插件 × 技能」。\n\n"
            "## 步骤\n"
            "1. 读取技能说明（本文件）；\n"
            "2. 按 topic 产出结果：**{{topic}}**；\n"
            "3. 回报结论与依据。\n"
        ) % skill
        core.skill_create(skill, "演示技能：供本地插件基座调用", body)

    # 2) 写入插件目录（skill + shell 组合示例）
    d = os.path.join(PLUGIN_DIR, pid)
    if os.path.isfile(os.path.join(d, "plugin.json")) and not overwrite:
        return {"ok": False, "error": "插件已存在：%s（overwrite=true 可覆盖）" % pid}
    os.makedirs(d, exist_ok=True)
    t_skill = _tool_name(pid, "run_skill")
    t_shell = _tool_name(pid, "run_shell")
    man = {
        "id": pid,
        "name": "技能 + Shell 示例",
        "version": "1.1.0",
        "description": "示例插件：run_skill 调用技能库中的技能（{{topic}} 传参注入），run_shell 执行本地命令模板（{{text}} 注入，mutating 走引擎审批门）。",
        "enabled": True,
        "skills": [skill],
        "tools": [
            {"name": "run_skill", "type": "skill", "skill": skill, "mutating": False,
             "desc": "调用技能 %s（topic 传参：注入 {{topic}} 占位）" % skill,
             "parameters": {"type": "object",
                            "properties": {"topic": {"type": "string", "description": "调用主题"}},
                            "required": []}},
            {"name": "run_shell", "type": "shell", "mutating": True,
             "command": "echo PI-plugin %s :: {{text}}" % pid,
             "desc": "执行本地命令模板（{{text}} 注入；mutating=true，对话内调用走审批门）",
             "parameters": {"type": "object",
                            "properties": {"text": {"type": "string", "description": "注入 {{text}} 的文本"},
                                           "timeout": {"type": "number", "description": "超时秒数"}},
                            "required": []}}
        ]
    }
    _write_manifest(pid, man)
    with open(os.path.join(d, "index.js"), "w", encoding="utf-8") as f:
        f.write(EXAMPLE_JS % {"pid": pid, "skill": skill})
    load_all()
    E.log("info", "plugins", "示例插件创建 %s（技能 %s + shell 模板）" % (pid, skill))
    return {"ok": True, "id": pid, "skill": skill, "tools": [t_skill, t_shell],
            "tool": t_skill, "dir": d,
            "text": "已创建示例插件 %s：注册工具 %s（调用技能 %s）、%s（shell 模板）" % (pid, t_skill, skill, t_shell)}


# ------------------------------------------------------------ 工具入口 / 注册
def _fn_plugins(a, ctx):
    act = str(a.get("action") or "list").lower()
    if act in ("list", "state"):
        return state()
    if act == "reload":
        load_all()
        st = state()
        st["text"] = "插件已重载：%d 个插件 / %d 个工具" % (st["total"], st["tools"])
        return st
    if act == "read":
        return read_plugin(a.get("id"))
    if act == "enable":
        return set_enabled(a.get("id"), True)
    if act == "disable":
        return set_enabled(a.get("id"), False)
    if act == "delete":
        return delete_plugin(a.get("id"))
    if act == "open_root":
        return open_root()
    if act == "create_example":
        return create_example(a.get("id") or "skill-caller", a.get("skill") or "",
                             overwrite=bool(a.get("overwrite")))
    if act == "call":
        return call_tool(a.get("tool"), a.get("args_json"))
    return {"ok": False, "error": "未知 action：" + act}


def register():
    E.TOOLS["plugins"] = {
        "name": "plugins", "group": "核心",
        "desc": "本地插件基座：list/read/reload/enable/disable/delete/create_example/open_root/call"
                "（plugin.json 声明式工具；type=skill 注入 {{参数}} 调用技能，type=shell 执行命令模板）",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "read", "reload", "enable", "disable",
                                                  "delete", "create_example", "open_root", "call"]},
            "id": {"type": "string", "description": "插件 id"},
            "tool": {"type": "string", "description": "call 时的插件工具名（plugin_<id>__<tool>）"},
            "args_json": {"type": "string", "description": "call 时的参数 JSON"},
            "skill": {"type": "string", "description": "create_example 时被调用的技能名"},
            "overwrite": {"type": "boolean"}}, "required": ["action"]},
        "fn": _wrap(_fn_plugins), "mutating": False}
    load_all()
    return {"ok": True, "plugins": len(_LOADED),
            "tools": len([n for n in E.TOOLS if n.startswith(PREFIX)])}


# ------------------------------------------------------------ 自检
def selftest():
    items = []

    def chk(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        items.append({"name": "plugins·" + name, "ok": bool(ok), "detail": str(detail)[:200]})

    def t_round():
        pid, sk = "_selftest-plugin", "_selftest-plugin-skill"
        r = create_example(pid, sk, overwrite=True)
        tname = _tool_name(pid, "run_skill")
        sname = _tool_name(pid, "run_shell")
        st = state()
        found = any(x["id"] == pid for x in st["items"])
        res = E.TOOLS[tname]["fn"]({"topic": "自检"} , {}) if tname in E.TOOLS else {}
        called = bool(res.get("ok")) and "自检" in (res.get("text") or "")
        res2 = E.TOOLS[sname]["fn"]({"text": "自检-shell"}, {}) if sname in E.TOOLS else {}
        shell_ok = bool(res2.get("ok")) and "自检-shell" in (res2.get("text") or "")
        delete_plugin(pid)
        _core().skill_delete(sk)
        gone = not any(x["id"] == pid for x in state()["items"])
        return (r.get("ok") and found and called and shell_ok and gone), \
            "创建/注册/调用技能/跑 shell/删除（%s · %s）" % (tname, sname)

    chk("插件 创建·注册·调用技能·跑 shell·删除", t_round)
    return {"passed": len([x for x in items if x["ok"]]), "total": len(items), "items": items}


def format_selftest(r):
    lines = ["PI 插件基座自检：%d/%d 通过" % (r["passed"], r["total"]), ""]
    for it in r["items"]:
        lines.append("%s %-28s %s" % ("OK" if it["ok"] else "X ", it["name"], it["detail"]))
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys_stdout = __import__("sys").stdout
        sys_stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    E.ensure_home()
    print(format_selftest(selftest()))
