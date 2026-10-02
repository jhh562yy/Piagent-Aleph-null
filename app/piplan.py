# -*- coding: utf-8 -*-
"""PI 计划与编排：会话级计划 + 子代理编排 + 「强制一次性完成」续跑包装器。

- 计划：goal + 步骤 [{id,t,status:pending|doing|done}]；跟随会话（ctx['session']），无会话时落全局
  ~/.pistudio/core/plan.json。工具 `plan`（set/add/doing/done/clear/get），REST /api/plan。
- 编排：把计划中未完成步骤交给子代理逐条执行（picore.subagent_start），汇总报告落
  ~/.pistudio/core/orchestrated/<ts>.md；工具 `orchestrate`，REST /api/plan action=orchestrate。
- 一次性完成：run_turn_auto() 在 prefs.auto_continue=true 时按「未完成（步数上限 / 计划仍有 pending）」
  自动续跑，最多 prefs.auto_segments 段（默认 4，上限 20）。
"""
import json
import os
import time

import piengine as E

GLOBAL = os.path.join(E.HOME, "core", "plan.json")
ORCH_DIR = os.path.join(E.HOME, "core", "orchestrated")


def _read_global():
    try:
        with open(GLOBAL, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_global(d):
    os.makedirs(os.path.dirname(GLOBAL), exist_ok=True)
    with open(GLOBAL, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _sess(ctx):
    return (ctx or {}).get("session") if isinstance(ctx, dict) else None


def _get(ctx=None):
    s = _sess(ctx)
    if isinstance(s, dict):
        return s.setdefault("plan", {"goal": "", "items": [], "updated": 0})
    return _read_global()


def _put(ctx, plan):
    plan["updated"] = time.time()
    s = _sess(ctx)
    if isinstance(s, dict):
        s["plan"] = plan
    else:
        _write_global(plan)


def state(ctx=None):
    p = _get(ctx)
    items = p.get("items") or []
    return {"ok": True, "goal": p.get("goal") or "",
            "items": items, "pending": len([x for x in items if x.get("status") != "done"]),
            "done": len([x for x in items if x.get("status") == "done"]), "total": len(items),
            "updated": p.get("updated") or 0}


def set_plan(ctx, goal, items):
    p = _get(ctx)
    p["goal"] = str(goal or p.get("goal") or "")
    p["items"] = [{"id": "s%d" % (i + 1), "t": str(x.get("t") if isinstance(x, dict) else x),
                   "status": "pending"} for i, x in enumerate(items or [])]
    _put(ctx, p)
    return state(ctx)


def add(ctx, text):
    p = _get(ctx)
    items = p.get("items") or []
    nid = "s%d" % (max([int(x.get("id", "s0")[1:]) for x in items] or [0]) + 1)
    items.append({"id": nid, "t": str(text), "status": "pending"})
    p["items"] = items
    _put(ctx, p)
    return state(ctx)


def mark(ctx, sid, status):
    p = _get(ctx)
    for x in p.get("items") or []:
        if x.get("id") == sid:
            x["status"] = status if status in ("pending", "doing", "done") else "done"
    _put(ctx, p)
    return state(ctx)


def clear(ctx):
    p = _get(ctx)
    p["items"] = []
    p["goal"] = ""
    _put(ctx, p)
    return state(ctx)


# ------------------------------------------------------------ 编排
def orchestrate(ctx, tasks=None, readonly=True, limit=3, max_steps=24, timeout=600):
    import picore as C
    st = state(ctx)
    pend = [x for x in st["items"] if x.get("status") != "done"]
    if tasks is None:
        tasks = [{"id": x.get("id"), "t": x.get("t")} for x in pend]
    tasks = [t if isinstance(t, dict) else {"t": t} for t in (tasks or [])]
    if not tasks:
        return {"ok": False, "error": "没有可编排的任务（计划为空或全部完成）"}
    results = []
    for i, t in enumerate(tasks[:max(1, min(int(limit or 3), 8))]):
        text = str(t.get("t") or "")
        rec = C.subagent_start(text, name="编排·%s" % (t.get("id") or (i + 1)),
                               readonly=bool(readonly), max_steps=max_steps, timeout=timeout)
        full = C.subagent_get(rec.get("id")) or {}
        results.append({"id": t.get("id"), "task": text, "sub": rec.get("id"),
                        "ok": bool((full.get("sub") or {}).get("ok")),
                        "report": str((full.get("sub") or {}).get("report") or "")[:4000]})
        if t.get("id"):
            mark(ctx, t.get("id"), "done" if results[-1]["ok"] else "doing")
    os.makedirs(ORCH_DIR, exist_ok=True)
    p = os.path.join(ORCH_DIR, "orchestrate-%s.md" % time.strftime("%Y%m%d-%H%M%S"))
    lines = ["# 编排报告 %s" % time.strftime("%Y-%m-%d %H:%M:%S"), "",
             "计划目标：%s" % (st.get("goal") or "—"), ""]
    for r in results:
        lines += ["## [%s] %s" % ("✔" if r["ok"] else "✘", r["task"]), "",
                  "子代理：%s" % r.get("sub"), "", "```text", r.get("report") or "（无报告）", "```", ""]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    E.log("info", "plan", "编排完成：%d 个任务 → %s" % (len(results), p))
    return {"ok": True, "count": len(results), "path": p, "results": results,
            "plan": state(ctx),
            "text": "已编排 %d 个任务（子代理执行），汇总报告：%s" % (len(results), p)}


# ------------------------------------------------------------ 一次性完成（续跑）
def _merge(total, r):
    total["steps"] = (total.get("steps") or 0) + (r.get("steps") or 0)
    total["tools"] = (total.get("tools") or []) + (r.get("tools") or [])
    total["checkpoints"] = (total.get("checkpoints") or []) + (r.get("checkpoints") or [])
    u1, u2 = total.get("usage") or {}, r.get("usage") or {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
        if u1.get(k) or u2.get(k):
            u1[k] = int(u1.get(k) or 0) + int(u2.get(k) or 0)
    total["usage"] = u1
    for k in ("text", "error", "ctx", "cost"):
        if r.get(k) not in (None, "", {}):
            total[k] = r.get(k)
    total["ok"] = bool(total.get("ok", True) and r.get("ok"))
    total["limit"] = bool(r.get("limit"))
    total["stopped"] = bool(r.get("stopped"))
    return total


def run_turn_auto(cfg, session, on_event, cancel=None, approve=None, ask=None):
    """强制一次性完成的段循环：未完成则自动续跑（仅在 prefs.auto_continue 打开时）。"""
    prefs = (cfg or {}).get("prefs") or {}
    segments = max(1, min(int(prefs.get("auto_segments") or 4), 20))
    total, seg = None, 0
    while seg < segments:
        seg += 1
        r = E.run_turn(cfg, session, on_event, cancel, approve, ask)
        total = r if total is None else _merge(total, r)
        if not prefs.get("auto_continue"):
            break
        if cancel is not None and cancel.is_set():
            break
        if r.get("error") or r.get("stopped"):
            break
        try:
            st = state({"session": session})
        except Exception:
            st = {"pending": 0}
        need = bool(r.get("limit")) or st.get("pending", 0) > 0
        if not need:
            break
        if seg >= segments:
            try:
                on_event("notice", "已达自动续跑段数上限（%d 段），停止续跑。" % segments)
            except Exception:
                pass
            break
        try:
            on_event("notice", "自动继续（第 %d/%d 段）：任务未完成，接着执行未完成步骤。" % (seg + 1, segments))
        except Exception:
            pass
        session["messages"].append({"role": "user", "ts": time.time(), "auto": True,
                                    "content": "（自动继续）请接着完成未完成的目标与步骤，不要重复已完成部分；全部完成后给出最终结论。"})
    if total is None:
        total = {"ok": False, "error": "未执行", "text": "", "steps": 0, "usage": {}, "tools": [], "checkpoints": []}
    total["segments"] = seg
    try:
        total["plan"] = state({"session": session})
    except Exception:
        pass
    return total


# ------------------------------------------------------------ 工具入口
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


def _fn_plan(a, ctx):
    act = str(a.get("action") or "get").lower()
    if act in ("get", "state", "list"):
        return state(ctx)
    if act == "set":
        return set_plan(ctx, a.get("goal"), a.get("items") or [])
    if act == "add":
        return add(ctx, a.get("t") or a.get("text") or "")
    if act == "doing":
        return mark(ctx, a.get("id"), "doing")
    if act == "done":
        return mark(ctx, a.get("id"), "done")
    if act == "clear":
        return clear(ctx)
    if act == "orchestrate":
        return orchestrate(ctx, a.get("tasks"), readonly=bool(a.get("readonly", True)),
                           limit=a.get("limit") or 3)
    return {"ok": False, "error": "未知 action：" + act}


def register():
    E.TOOLS["plan"] = {
        "name": "plan", "group": "核心",
        "desc": "计划与编排：get/set/add/doing/done/clear/orchestrate。set 时给目标和步骤清单；"
                "每完成一步用 done 标记；全部完成后给出结论。计划显示在「计划」面板，未完成时会驱动自动续跑。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["get", "set", "add", "doing", "done", "clear", "orchestrate"]},
            "goal": {"type": "string"},
            "items": {"type": "array", "items": {"type": "string"}, "description": "步骤清单"},
            "id": {"type": "string", "description": "步骤 id（s1/s2…）"},
            "t": {"type": "string", "description": "add 的步骤文本"},
            "tasks": {"type": "array", "items": {"type": "object"}, "description": "orchestrate 的任务 [{id,t}]"},
            "readonly": {"type": "boolean"}, "limit": {"type": "number"}}, "required": ["action"]},
        "fn": _wrap(_fn_plan), "mutating": False}
    E.TOOLS["orchestrate"] = {
        "name": "orchestrate", "group": "核心",
        "desc": "自动编排：把计划中未完成步骤交给子代理逐条执行并汇总报告（只读子代理，不改文件）。",
        "parameters": {"type": "object", "properties": {
            "tasks": {"type": "array", "items": {"type": "object"}},
            "readonly": {"type": "boolean"}, "limit": {"type": "number"}}, "required": []},
        "fn": _wrap(lambda a, ctx: orchestrate(ctx, a.get("tasks"), readonly=bool(a.get("readonly", True)),
                                               limit=a.get("limit") or 3)), "mutating": False}
    return {"ok": True}


def selftest():
    items = []

    def chk(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        items.append({"name": "计划·" + name, "ok": bool(ok), "detail": str(detail)[:200]})

    def t_plan():
        fake = {"session": {}}
        r1 = set_plan(fake, "自检目标", ["步骤 A", "步骤 B"])
        mark(fake, "s1", "done")
        st = state(fake)
        add(fake, "步骤 C")
        st2 = state(fake)
        clear(fake)
        return (r1["total"] == 2 and st["done"] == 1 and st["pending"] == 1 and st2["total"] == 3
                and state(fake)["total"] == 0), "set/add/done/clear 往返"
    chk("步骤 set·add·done·clear", t_plan)

    def t_merge():
        a = {"steps": 1, "tools": [{"name": "x"}], "usage": {"total_tokens": 5}, "ok": True}
        b = {"steps": 2, "tools": [{"name": "y"}], "usage": {"total_tokens": 7}, "ok": True, "limit": True}
        m = _merge(a, b)
        return (m["steps"] == 3 and m["usage"]["total_tokens"] == 12 and m["limit"] is True
                and len(m["tools"]) == 2), "_merge 段合并"
    chk("一次性续跑 段合并", t_merge)

    def t_orch_reg():
        return ("plan" in E.TOOLS) or (register() and "plan" in E.TOOLS), "工具已注册：plan/orchestrate"
    chk("工具注册", t_orch_reg)
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
        print("PI 计划与编排自检：%d/%d 通过" % (r["passed"], r["total"]))
        for it in r["items"]:
            print(("OK " if it["ok"] else "X  ") + it["name"] + "  " + it["detail"])
        __import__("sys").exit(0 if r["passed"] == r["total"] else 1)
    print("PI 计划与编排模块。用 --selftest 自检。")
