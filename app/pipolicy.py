# -*- coding: utf-8 -*-
"""PI 审批策略与自动确认：规则（工具 × 正则 → allow/deny/ask）+ 自动确认开关 + 决策历史。

规则匹配对象 subject：shell→command、fs_write/fs_edit→path、py_run→code、http_get→url、
插件 shell 工具→command（若有）、其余→参数 JSON。工具名支持 "*" 与 前缀通配（如 "plugin_*"）。

数据：~/.pistudio/core/approvals.json（auto / rules / history，历史保留 400 条）
接入：原生 app 与后端的审批钩子先过 decide()；工具 `approvals`；REST /api/approvals；「审批」面板。
"""
import json
import os
import re
import time

import piengine as E

PATH = os.path.join(E.HOME, "core", "approvals.json")
ACTIONS = ("allow", "deny", "ask")
MAX_HISTORY = 400


def _load():
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("auto", False)
    d.setdefault("rules", [])
    d.setdefault("history", [])
    return d


def _save(d):
    d["history"] = (d.get("history") or [])[-MAX_HISTORY:]
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def set_auto(on):
    d = _load()
    d["auto"] = bool(on)
    _save(d)
    E.log("info", "policy", "自动确认 %s" % ("开启" if on else "关闭"))
    return {"ok": True, "auto": d["auto"], "text": "自动确认已%s" % ("开启（所有写操作免审批）" if on else "关闭")}


def auto(cfg=None):
    return bool(_load().get("auto") or ((cfg or {}).get("prefs") or {}).get("policy_auto"))


def add_rule(tool, pattern, action="allow", note=""):
    tool = str(tool or "*").strip() or "*"
    action = str(action or "allow").lower()
    if action not in ACTIONS:
        return {"ok": False, "error": "action 需为 allow/deny/ask"}
    d = _load()
    rid = "r%d" % (int(time.time() * 1000) % 100000000)
    d["rules"].append({"id": rid, "tool": tool, "pattern": str(pattern or ""), "action": action,
                       "note": str(note or ""), "ts": time.time()})
    _save(d)
    E.log("info", "policy", "审批规则新增：%s × %s → %s" % (tool, pattern, action))
    return {"ok": True, "id": rid, "text": "规则已添加：%s × /%s/ → %s" % (tool, pattern, action)}


def del_rule(rid):
    d = _load()
    n = len(d["rules"])
    d["rules"] = [r for r in d["rules"] if r.get("id") != rid]
    _save(d)
    return {"ok": len(d["rules"]) < n, "text": "规则已删除：%s" % rid}


def clear_history():
    d = _load()
    d["history"] = []
    _save(d)
    return {"ok": True, "text": "审批历史已清空"}


def history(limit=80):
    d = _load()
    n = max(1, min(int(limit or 80), 400))
    return {"ok": True, "items": list(reversed(d["history"]))[:n]}


def _tool_match(rule_tool, name):
    rt = str(rule_tool or "*")
    if rt in ("*", ""):
        return True
    if rt.endswith("*"):
        return str(name).startswith(rt[:-1])
    return rt == name


def subject(name, args):
    a = args if isinstance(args, dict) else {}
    for k in ("command", "cmd", "path", "file", "url", "code"):
        if a.get(k):
            return str(a[k])
    try:
        return json.dumps(a, ensure_ascii=False)[:400]
    except Exception:
        return str(a)[:400]


def _match(pattern, text):
    try:
        return bool(re.search(pattern, text))
    except Exception:
        return str(pattern) in str(text)


def _rec(name, subj, decision, rule):
    d = _load()
    d["history"].append({"ts": time.time(), "tool": name, "subject": str(subj)[:160],
                         "decision": decision, "rule": rule})
    _save(d)


def decide(cfg, name, args, meta=None):
    """返回 True=自动放行 / False=自动拒绝 / None=需要人工审批。"""
    try:
        if auto(cfg):
            _rec(name, subject(name, args), "allow", "auto")
            return True
        subj = subject(name, args)
        for r in reversed(_load().get("rules") or []):
            if not _tool_match(r.get("tool"), name):
                continue
            pat = r.get("pattern") or ""
            if not pat or _match(pat, subj):
                act = r.get("action") or "ask"
                if act in ("allow", "deny"):
                    _rec(name, subj, act, "rule:" + str(r.get("id")))
                    return act == "allow"
                return None
        return None
    except Exception:
        return None


def record(name, args, decision, rule="interactive"):
    try:
        _rec(name, subject(name, args), "allow" if decision else "deny", rule)
    except Exception:
        pass


def state(cfg=None):
    d = _load()
    return {"ok": True, "auto": auto(cfg), "rules": d.get("rules") or [],
            "rules_n": len(d.get("rules") or []),
            "history_n": len(d.get("history") or []),
            "history": list(reversed(d.get("history") or []))[:60],
            "path": PATH}


def _wrap(fn):
    def w(a, ctx):
        try:
            r = fn(a or {}, ctx or {})
            if isinstance(r, dict):
                r.setdefault("ok", True)
                if "text" not in r:
                    r["text"] = json.dumps({k: v for k, v in r.items() if k != "text"},
                                           ensure_ascii=False)[:3000]
                return r
            return {"ok": True, "text": json.dumps(r, ensure_ascii=False)[:3000]}
        except Exception as e:
            return {"ok": False, "text": "%s: %s" % (type(e).__name__, e)}
    return w


def _fn_approvals(a, ctx):
    act = str(a.get("action") or "list").lower()
    if act in ("list", "state"):
        return state()
    if act == "add":
        return add_rule(a.get("tool"), a.get("pattern"),
                        a.get("rule_action") or a.get("mode") or "allow", a.get("note"))
    if act == "delete":
        return del_rule(a.get("id"))
    if act == "auto":
        return set_auto(bool(a.get("on")))
    if act == "history":
        return history(a.get("limit") or 80)
    if act == "clear":
        return clear_history()
    return {"ok": False, "error": "未知 action：" + act}


def register():
    E.TOOLS["approvals"] = {
        "name": "approvals", "group": "核心",
        "desc": "审批策略与自动确认：list/add/delete/auto/history/clear。规则=工具×正则→allow/deny/ask；"
                "auto=true 时所有写操作自动放行（谨慎）。命中即生效于原生与网页两端。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "add", "delete", "auto", "history", "clear"]},
            "tool": {"type": "string", "description": "工具名，* 或前缀通配（plugin_*）"},
            "pattern": {"type": "string", "description": "正则；留空=该工具全部命中"},
            "rule_action": {"type": "string", "enum": ["allow", "deny", "ask"]},
            "note": {"type": "string"},
            "id": {"type": "string"}, "on": {"type": "boolean"}, "limit": {"type": "number"}},
            "required": ["action"]},
        "fn": _wrap(_fn_approvals), "mutating": False}
    return {"ok": True}


def selftest():
    items = []
    prev_auto = bool(_load().get("auto"))
    set_auto(False)

    def chk(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        items.append({"name": "审批·" + name, "ok": bool(ok), "detail": str(detail)[:200]})

    def t_rules():
        d0 = _load()
        r1 = add_rule("shell", r"^rm -rf", "deny", "自检")
        r2 = add_rule("fs_write", r"work", "allow", "自检")
        deny = decide({}, "shell", {"command": "rm -rf /"})
        allow = decide({}, "fs_write", {"path": "work/x.txt"})
        other = decide({}, "shell", {"command": "echo hi"})
        del_rule(r1["id"]); del_rule(r2["id"])
        d1 = _load()
        d1["history"] = d0.get("history") or []
        _save(d1)
        return (deny is False and allow is True and other is None), "deny/allow/未命中→询问"
    chk("规则 命中与放行/拒绝", t_rules)

    def t_auto():
        set_auto(True)
        r = decide({}, "shell", {"command": "whatever"}) is True
        set_auto(False)
        r2 = decide({}, "shell", {"command": "whatever"}) is None
        return r and r2, "自动确认开/关"
    chk("自动确认开关", t_auto)

    def t_subject():
        return subject("shell", {"command": "ls"}) == "ls" and "a" in subject("x", {"a": 1}), "subject 提取"
    chk("subject 提取", t_subject)
    set_auto(prev_auto)
    return {"passed": len([x for x in items if x["ok"]]), "total": len(items), "items": items}


if __name__ == "__main__":
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--selftest" in __import__("sys").argv:
        E.ensure_home()
        r = selftest()
        print("PI 审批策略自检：%d/%d 通过" % (r["passed"], r["total"]))
        for it in r["items"]:
            print(("OK " if it["ok"] else "X  ") + it["name"] + "  " + it["detail"])
        __import__("sys").exit(0 if r["passed"] == r["total"] else 1)
    print("PI 审批策略模块。用 --selftest 自检。")
