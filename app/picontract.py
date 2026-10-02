# -*- coding: utf-8 -*-
"""PI 契约系统（自治理）：全周期状态机 draft→signed→active→checking→settled|breached→closed。

每个契约包含：目标 goal / 验收项 evals[] / 允许 allow[] 与禁止 deny[] 清单 / 成本预算 cost /
守卫记录 guards[] / 检查记录 checks[] / 结算 settled{} / 报告（Markdown 落盘）。

数据：~/.pistudio/core/contracts/contracts.json + reports/<id>.md
接入：picore.init() 注册工具 `contract`；REST /api/contracts；「契约」面板；原生「治理中心」。
"""
import json
import os
import re
import time

import piengine as E

DIR = os.path.join(E.HOME, "core", "contracts")
PATH = os.path.join(DIR, "contracts.json")
REPORT_DIR = os.path.join(DIR, "reports")
STATES = ("draft", "signed", "active", "checking", "settled", "breached", "closed")
MAX_ITEMS = 300


def _dirs():
    os.makedirs(REPORT_DIR, exist_ok=True)


def _load():
    _dirs()
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        db = {}
    db.setdefault("seq", 0)
    db.setdefault("items", [])
    return db


def _save(db):
    db["items"] = (db.get("items") or [])[-MAX_ITEMS:]
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def _now():
    return time.time()


def _txt(rec):
    return "%s [%s] %s" % (rec.get("id"), rec.get("state"), rec.get("goal"))


def get(cid):
    for c in _load()["items"]:
        if c.get("id") == cid:
            return c
    return None


def _setstate(c, st, note=""):
    if st not in STATES:
        raise ValueError("未知状态：" + st)
    c["state"] = st
    c["updated"] = _now()
    c.setdefault("timeline", []).append({"ts": _now(), "state": st, "note": note})


def create(goal, evals=None, allow=None, deny=None, cost=None):
    goal = str(goal or "").strip()
    if not goal:
        return {"ok": False, "error": "缺少 goal"}
    ev = []
    for i, x in enumerate(evals or []):
        if isinstance(x, dict):
            ev.append({"k": str(x.get("k") or ("e%d" % (i + 1))), "desc": str(x.get("desc") or ""),
                       "status": "pending", "note": ""})
        else:
            ev.append({"k": "e%d" % (i + 1), "desc": str(x), "status": "pending", "note": ""})
    db = _load()
    db["seq"] = int(db.get("seq") or 0) + 1
    c = {"id": "c%d" % db["seq"], "goal": goal, "evals": ev,
         "allow": [str(x) for x in (allow or [])], "deny": [str(x) for x in (deny or [])],
         "cost": cost, "guards": [], "checks": [], "settled": None,
         "state": "draft", "created": _now(), "updated": _now(), "timeline": []}
    _setstate(c, "draft", "创建")
    db["items"].append(c)
    _save(db)
    E.log("info", "contract", "契约创建 %s：%s" % (c["id"], goal[:60]))
    return {"ok": True, "id": c["id"], "contract": c, "text": _txt(c) + "（验收 %d 项）" % len(ev)}


def sign(cid):
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    _setstate(c, "signed", "签署")
    _touch(c)
    return {"ok": True, "id": cid, "text": _txt(c)}


def start(cid):
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    if c.get("state") in ("closed", "settled", "breached"):
        return {"ok": False, "error": "契约已结束（%s）" % c.get("state")}
    _setstate(c, "active", "开始执行")
    _touch(c)
    return {"ok": True, "id": cid, "text": _txt(c)}


def _match(pat, text):
    try:
        return bool(re.search(pat, text))
    except Exception:
        return str(pat) in str(text)


def guard(cid, targets):
    """对目标清单做守卫检查：命中 deny → 违规；allow 非空且全部未命中 → 警告。"""
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    targets = [str(t) for t in (targets or [])]
    if not targets:
        return {"ok": False, "error": "缺少 targets"}
    viol, warn = [], []
    for t in targets:
        hit = next((d for d in (c.get("deny") or []) if _match(d, t)), None)
        if hit:
            viol.append({"target": t, "deny": hit})
            continue
        al = c.get("allow") or []
        if al and not any(_match(a, t) for a in al):
            warn.append({"target": t, "why": "不在 allow 清单"})
    rec = {"ts": _now(), "targets": targets, "violations": viol, "warnings": warn}
    c.setdefault("guards", []).append(rec)
    _touch(c)
    ok = not viol
    return {"ok": True, "pass": ok, "violations": viol, "warnings": warn, "checked": len(targets),
            "text": ("守卫通过（%d 项目标）" if ok else "守卫拦截：%d 项违规") % (len(targets) if ok else len(viol))}


def amend(cid, delta="", goal=None, allow=None, deny=None):
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    if goal:
        c["goal"] = str(goal)
    if isinstance(allow, list):
        c["allow"] = [str(x) for x in allow]
    if isinstance(deny, list):
        c["deny"] = [str(x) for x in deny]
    c.setdefault("timeline", []).append({"ts": _now(), "state": c.get("state"), "note": "修订：" + str(delta)})
    _touch(c)
    return {"ok": True, "id": cid, "text": "已修订 %s：%s" % (cid, str(delta)[:80])}


def check(cid, results=None):
    """进入检查：results=[{k,status:pass|fail|pending,note}]；缺省把未决项标记 fail 之外的保持 pending。"""
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        k = str(r.get("k") or "")
        for e in c.get("evals") or []:
            if e.get("k") == k:
                e["status"] = "pass" if str(r.get("status")) == "pass" else ("pending" if str(r.get("status")) == "pending" else "fail")
                e["note"] = str(r.get("note") or "")
    done = sum(1 for e in (c.get("evals") or []) if e.get("status") == "pass")
    total = len(c.get("evals") or [])
    c.setdefault("checks", []).append({"ts": _now(), "pass": done, "total": total})
    _setstate(c, "checking", "检查 %d/%d" % (done, total))
    _touch(c)
    return {"ok": True, "id": cid, "pass": done, "total": total, "text": "%s 检查：%d/%d 项通过" % (cid, done, total)}


def settle(cid, ai=None, user=None, notes=""):
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    done = sum(1 for e in (c.get("evals") or []) if e.get("status") == "pass")
    total = len(c.get("evals") or [])
    c["settled"] = {"ts": _now(), "ai": [str(x) for x in (ai or [])], "user": [str(x) for x in (user or [])],
                    "pass": done, "total": total, "notes": str(notes or "")}
    _setstate(c, "settled" if done >= total and total > 0 else "checking",
              "结算 %d/%d" % (done, total))
    _touch(c)
    return {"ok": True, "id": cid, "pass": done, "total": total, "state": c["state"],
            "text": "结算 %s：%d/%d（%s）" % (cid, done, total, c["state"])}


def breach(cid, reason=""):
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    _setstate(c, "breached", "违约：" + str(reason or "")[:120])
    _touch(c)
    return {"ok": True, "id": cid, "text": "已标记违约：%s（%s）" % (cid, str(reason or "")[:60])}


def terminate(cid, note=""):
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    _setstate(c, "closed", "关闭：" + str(note or "")[:80])
    _touch(c)
    return {"ok": True, "id": cid, "text": "已关闭：" + cid}


def score(cid, clarity=0, accuracy=0):
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    c["score"] = {"clarity": int(clarity or 0), "accuracy": int(accuracy or 0), "ts": _now()}
    _touch(c)
    return {"ok": True, "id": cid, "text": "评分：清晰度 %s / 准确度 %s" % (clarity, accuracy)}


def report(cid):
    c = get(cid)
    if not c:
        return {"ok": False, "error": "契约不存在：" + str(cid)}
    ev = c.get("evals") or []
    lines = ["# 契约报告 %s" % c["id"], "",
             "- 目标：%s" % c.get("goal"), "- 状态：%s" % c.get("state"),
             "- 创建：%s ｜ 更新：%s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(c.get("created") or 0)),
                                      time.strftime("%Y-%m-%d %H:%M", time.localtime(c.get("updated") or 0))),
             "- 允许：%s" % (", ".join(c.get("allow") or []) or "—"),
             "- 禁止：%s" % (", ".join(c.get("deny") or []) or "—"),
             "- 成本预算：%s" % (c.get("cost") if c.get("cost") is not None else "—"), "", "## 验收项"]
    for e in ev:
        lines.append("- [%s] %s — %s%s" % ("x" if e.get("status") == "pass" else ("!" if e.get("status") == "fail" else " "),
                                           e.get("k"), e.get("desc") or "", ("（" + e.get("note") + "）") if e.get("note") else ""))
    g = c.get("guards") or []
    if g:
        lines += ["", "## 守卫记录"]
        for x in g[-8:]:
            lines.append("- %s：违规 %d / 警告 %d ｜ %s" % (
                time.strftime("%H:%M:%S", time.localtime(x.get("ts") or 0)),
                len(x.get("violations") or []), len(x.get("warnings") or []),
                ", ".join(str(t)[:50] for t in (x.get("targets") or [])[:3])))
    st = c.get("settled")
    if st:
        lines += ["", "## 结算", "- 通过：%d/%d" % (st.get("pass", 0), st.get("total", 0)),
                  "- AI 卷宗：%s" % ("；".join(st.get("ai") or []) or "—"),
                  "- 用户反馈：%s" % ("；".join(st.get("user") or []) or "—"),
                  "- 备注：%s" % (st.get("notes") or "—")]
    if c.get("score"):
        lines += ["", "## 评分", "- 清晰度：%s ｜ 准确度：%s" % (c["score"].get("clarity"), c["score"].get("accuracy"))]
    lines += ["", "## 时间线"]
    for t in (c.get("timeline") or [])[-20:]:
        lines.append("- %s %s %s" % (time.strftime("%m-%d %H:%M:%S", time.localtime(t.get("ts") or 0)),
                                     t.get("state"), t.get("note") or ""))
    text = "\n".join(lines)
    _dirs()
    p = os.path.join(REPORT_DIR, "%s.md" % c["id"])
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    c["report"] = p
    _touch(c)
    return {"ok": True, "id": cid, "path": p, "text": text}


def _touch(c):
    db = _load()
    for i, x in enumerate(db["items"]):
        if x.get("id") == c.get("id"):
            db["items"][i] = c
            break
    _save(db)


def list_(limit=50):
    items = _load()["items"]
    items = sorted(items, key=lambda c: c.get("updated") or 0, reverse=True)[:limit]
    return {"ok": True, "items": [{"id": c.get("id"), "goal": c.get("goal"), "state": c.get("state"),
                                   "evals": [{"k": e.get("k"), "desc": e.get("desc"), "status": e.get("status")}
                                             for e in (c.get("evals") or [])],
                                   "updated": c.get("updated"),
                                   "pass": sum(1 for e in (c.get("evals") or []) if e.get("status") == "pass"),
                                   "total": len(c.get("evals") or []),
                                   "report": c.get("report") or "",
                                   "allow": c.get("allow") or [], "deny": c.get("deny") or []}
                                  for c in items],
            "dir": DIR, "total": len(_load()["items"])}


def state():
    db = _load()
    items = db.get("items") or []
    return {"total": len(items),
            "active": len([c for c in items if c.get("state") in ("signed", "active", "checking")]),
            "settled": len([c for c in items if c.get("state") == "settled"]),
            "dir": DIR}


# ------------------------------------------------------------ 工具入口
def _fn_contract(a, ctx):
    act = str(a.get("action") or "list").lower()
    if act in ("list", "state"):
        return list_()
    if act == "get":
        c = get(a.get("id"))
        return ({"ok": True, "contract": c, "text": json.dumps(c, ensure_ascii=False)[:4000]}
                if c else {"ok": False, "error": "契约不存在"})
    if act == "create":
        return create(a.get("goal"), a.get("evals"), a.get("allow"), a.get("deny"), a.get("cost"))
    if act == "sign":
        return sign(a.get("id"))
    if act == "start":
        return start(a.get("id"))
    if act == "guard":
        return guard(a.get("id"), a.get("targets"))
    if act == "amend":
        return amend(a.get("id"), a.get("delta") or a.get("note") or "", a.get("goal"), a.get("allow"), a.get("deny"))
    if act == "check":
        return check(a.get("id"), a.get("results"))
    if act == "settle":
        return settle(a.get("id"), a.get("ai"), a.get("user"), a.get("note") or a.get("notes") or "")
    if act == "breach":
        return breach(a.get("id"), a.get("reason") or a.get("note") or "")
    if act == "report":
        return report(a.get("id"))
    if act == "score":
        return score(a.get("id"), a.get("clarity"), a.get("accuracy"))
    if act == "terminate":
        return terminate(a.get("id"), a.get("note") or "")
    return {"ok": False, "error": "未知 action：" + act}


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


def register():
    E.TOOLS["contract"] = {
        "name": "contract", "group": "核心",
        "desc": "契约系统（自治理）：create/sign/start/guard/amend/check/settle/breach/report/score/terminate/list/get"
                "——目标+验收项+允许/禁止清单的全周期状态机，报告落盘 core/contracts/reports/",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "get", "create", "sign", "start", "guard", "amend",
                                                  "check", "settle", "breach", "report", "score", "terminate", "state"]},
            "id": {"type": "string"}, "goal": {"type": "string"},
            "evals": {"type": "array", "items": {"type": "string"}, "description": "验收项（字符串数组）"},
            "allow": {"type": "array", "items": {"type": "string"}},
            "deny": {"type": "array", "items": {"type": "string"}},
            "targets": {"type": "array", "items": {"type": "string"}, "description": "guard 检查的目标清单"},
            "results": {"type": "array", "items": {"type": "object"}, "description": "check 结果 [{k,status,note}]"},
            "note": {"type": "string"}, "reason": {"type": "string"},
            "ai": {"type": "array", "items": {"type": "string"}}, "user": {"type": "array", "items": {"type": "string"}},
            "clarity": {"type": "number"}, "accuracy": {"type": "number"}, "cost": {"type": "number"},
            "delta": {"type": "string"}}, "required": ["action"]},
        "fn": _wrap(_fn_contract), "mutating": False}
    return {"ok": True}


def selftest():
    items = []

    def chk(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        items.append({"name": "契约·" + name, "ok": bool(ok), "detail": str(detail)[:200]})

    def t_full():
        r = create("自检契约：验证全周期", ["创建", "守卫", "结算"], allow=["echo *", "fs_write work*"],
                   deny=["rm -rf", "format *"])
        cid = r["id"]
        sign(cid); start(cid)
        g1 = guard(cid, ["rm -rf /", "echo hello"])
        g2 = guard(cid, ["echo ok"])
        check(cid, [{"k": "e1", "status": "pass"}, {"k": "e2", "status": "pass"}, {"k": "e3", "status": "pass"}])
        s = settle(cid, ai=["实现完成"], user=["满意"])
        rep = report(cid)
        ok = (r["ok"] and g1["violations"] and g1["pass"] is False and g2["pass"] and
              s["state"] == "settled" and os.path.isfile(rep["path"]))
        db = _load()
        db["items"] = [c for c in db["items"] if c.get("id") != cid]
        _save(db)
        return ok, "创建→签署→开始→守卫拦截→检查→结算→报告（%s）" % cid
    chk("全周期 创建·守卫·检查·结算·报告", t_full)

    def t_state():
        st = state()
        return "total" in st and "active" in st, "统计：共 %d 个契约" % st.get("total", 0)
    chk("状态统计", t_state)
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
        print("PI 契约系统自检：%d/%d 通过" % (r["passed"], r["total"]))
        for it in r["items"]:
            print(("OK " if it["ok"] else "X  ") + it["name"] + "  " + it["detail"])
        __import__("sys").exit(0 if r["passed"] == r["total"] else 1)
    print("PI 契约系统模块。用 --selftest 自检。")
