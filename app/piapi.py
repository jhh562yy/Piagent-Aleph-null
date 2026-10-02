# -*- coding: utf-8 -*-
"""PI 自定义接口：把任意 HTTP 端点注册成模型可调用的工具（api_<id>）。

清单：~/.pistudio/core/apis.json，每条 {id,name,desc,url,method,headers,body,params,mutating,enabled,timeout}
调用：url / headers / body 中的 {{占位}} 从工具参数替换；返回体（截断）作为工具输出。
内置管理工具 `apis`（list/add/update/delete/test/call）；REST /api/apis；「接口」面板；原生「治理中心」。
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import piengine as E

PATH = os.path.join(E.HOME, "core", "apis.json")
PREFIX = "api_"
MAX_BODY = 200 * 1024
SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def _load():
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("items", [])
    return d


def _save(d):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _safe_id(x):
    x = str(x or "").strip().strip("/")
    return x if x and SAFE.match(x) and ".." not in x else ""


def list_apis():
    return {"ok": True, "items": _load()["items"], "path": PATH,
            "tools": len([n for n in E.TOOLS if n.startswith(PREFIX)])}


def upsert(spec):
    spec = spec if isinstance(spec, dict) else {}
    sid = _safe_id(spec.get("id"))
    url = str(spec.get("url") or "").strip()
    if not sid or not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "需要 id（英文字母数字-_）与 http(s) 的 url"}
    d = _load()
    item = {"id": sid, "name": str(spec.get("name") or sid), "desc": str(spec.get("desc") or ""),
            "url": url, "method": str(spec.get("method") or "GET").upper(),
            "headers": spec.get("headers") if isinstance(spec.get("headers"), dict) else {},
            "body": str(spec.get("body") or ""), "mutating": bool(spec.get("mutating", False)),
            "enabled": bool(spec.get("enabled", True)), "timeout": int(spec.get("timeout") or 60),
            "ts": time.time()}
    d["items"] = [x for x in d["items"] if x.get("id") != sid] + [item]
    _save(d)
    load_all()
    E.log("info", "apis", "自定义接口保存 %s → %s %s" % (sid, item["method"], url[:80]))
    return {"ok": True, "id": sid, "tool": PREFIX + sid, "item": item,
            "text": "已保存接口 %s（工具 %s）" % (sid, PREFIX + sid)}


def delete_api(sid):
    sid = _safe_id(sid)
    d = _load()
    n = len(d["items"])
    d["items"] = [x for x in d["items"] if x.get("id") != sid]
    _save(d)
    load_all()
    return {"ok": len(d["items"]) < n, "text": "已删除接口 %s" % sid}


def _subst(text, args, quote=False):
    out = str(text or "")
    for k, v in (args or {}).items():
        if k == "timeout":
            continue
        val = urllib.parse.quote(str(v)) if quote else str(v)
        out = out.replace("{{%s}}" % k, val)
    return out


def call_api(sid_or_tool, args=None, timeout=None):
    args = args if isinstance(args, dict) else {}
    sid = str(sid_or_tool or "")
    if sid.startswith(PREFIX):
        sid = sid[len(PREFIX):]
    item = next((x for x in _load()["items"] if x.get("id") == sid), None)
    if not item:
        return {"ok": False, "error": "接口不存在：" + sid}
    url = _subst(item.get("url"), args, quote=True)
    method = (item.get("method") or "GET").upper()
    headers = {k: _subst(v, args) for k, v in (item.get("headers") or {}).items()}
    body = _subst(item.get("body"), args).encode("utf-8") if item.get("body") else None
    if body is not None and "content-type" not in [h.lower() for h in headers]:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if body is None and method in ("POST", "PUT", "PATCH") and args:
        body = json.dumps({k: v for k, v in args.items() if k != "timeout"}, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
    t0 = time.time()
    try:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=int(timeout or args.get("timeout") or item.get("timeout") or 60)) as resp:
            raw = resp.read(MAX_BODY)
            code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read(MAX_BODY) if hasattr(e, "read") else b""
        code = e.code
    except Exception as e:
        return {"ok": False, "text": "%s: %s" % (type(e).__name__, e), "ms": int((time.time() - t0) * 1000)}
    text = E._decode(raw)
    try:
        parsed = json.loads(text)
        text = json.dumps(parsed, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return {"ok": code < 400, "code": code, "text": text[:20000], "ms": int((time.time() - t0) * 1000),
            "url": url, "method": method}


def test_api(spec_or_id, args=None):
    if isinstance(spec_or_id, dict) and spec_or_id.get("url"):
        tmp = dict(spec_or_id)
        tmp.setdefault("id", "_test")
        d = _load()
        d["items"] = [x for x in d["items"] if x.get("id") != "_test"] + [{
            "id": "_test", "name": "测试", "desc": "", "url": tmp["url"],
            "method": str(tmp.get("method") or "GET").upper(),
            "headers": tmp.get("headers") if isinstance(tmp.get("headers"), dict) else {},
            "body": str(tmp.get("body") or ""), "mutating": False, "enabled": False,
            "timeout": int(tmp.get("timeout") or 30), "ts": time.time()}]
        _save(d)
        try:
            return call_api("_test", args)
        finally:
            d = _load()
            d["items"] = [x for x in d["items"] if x.get("id") != "_test"]
            _save(d)
    return call_api(spec_or_id, args)


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


def _fn_apis(a, ctx):
    act = str(a.get("action") or "list").lower()
    if act in ("list", "state"):
        return list_apis()
    if act in ("add", "update", "upsert"):
        return upsert(a.get("spec") or a)
    if act == "delete":
        return delete_api(a.get("id"))
    if act == "test":
        return test_api(a.get("spec") or a.get("id"), a.get("args"))
    if act == "call":
        return call_api(a.get("id") or a.get("tool"), a.get("args"))
    return {"ok": False, "error": "未知 action：" + act}


def load_all():
    for n in [x for x in list(E.TOOLS) if x.startswith(PREFIX)]:
        E.TOOLS.pop(n, None)
    n = 0
    for item in _load()["items"]:
        if not item.get("enabled", True):
            continue
        name = PREFIX + item["id"]
        E.TOOLS[name] = {
            "name": name, "group": "接口",
            "desc": "[自定义接口] %s %s — %s（参数用于替换 {{占位}}）" % (
                item.get("method"), item.get("name"), item.get("desc") or item.get("url")),
            "parameters": {"type": "object", "properties": {}, "required": []},
            "fn": _wrap(lambda a, ctx, _id=item["id"]: call_api(_id, a)),
            "mutating": bool(item.get("mutating", False)), "_api": item["id"]}
        n += 1
    return {"ok": True, "tools": n}


def register():
    E.TOOLS["apis"] = {
        "name": "apis", "group": "核心",
        "desc": "自定义接口：list/add/update/delete/test/call。把任意 HTTP 端点注册成模型可调用工具"
                "（api_<id>，url/headers/body 里的 {{占位}} 由调用参数替换）。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "add", "update", "delete", "test", "call"]},
            "id": {"type": "string"}, "spec": {"type": "object", "description": "{id,name,url,method,headers,body}"},
            "args": {"type": "object", "description": "call/test 的参数（替换占位）"}},
            "required": ["action"]},
        "fn": _wrap(_fn_apis), "mutating": False}
    r = load_all()
    return {"ok": True, "tools": r["tools"]}


def selftest():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    items = []

    def chk(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        items.append({"name": "接口·" + name, "ok": bool(ok), "detail": str(detail)[:200]})

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if not self.path.startswith("/echo"):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            b = json.dumps({"echo": q.get("q", [""])[0] or "none", "ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def t_round():
        r = upsert({"id": "_selftest_api", "name": "自检接口",
                    "url": "http://127.0.0.1:%d/echo?q={{q}}" % port, "method": "GET"})
        reg = PREFIX + "_selftest_api" in E.TOOLS
        res = E.TOOLS[PREFIX + "_selftest_api"]["fn"]({"q": "hello"}, {}) if reg else {}
        ok = r["ok"] and reg and res.get("ok") and "hello" in (res.get("text") or "")
        delete_api("_selftest_api")
        gone = PREFIX + "_selftest_api" not in E.TOOLS
        return (ok and gone), "注册→调用→删除（%s）" % res.get("code")
    chk("接口 注册·调用·删除", t_round)

    def t_http_error():
        upsert({"id": "_selftest_err", "name": "错误路径", "url": "http://127.0.0.1:%d/nope?q=x" % port})
        res = call_api("_selftest_err")
        delete_api("_selftest_err")
        return (res.get("ok") is False and res.get("code") == 404) or res.get("ok") is False, \
            "404 处理（code=%s）" % res.get("code")
    chk("异常/404 处理", t_http_error)

    srv.shutdown()
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
        print("PI 自定义接口自检：%d/%d 通过" % (r["passed"], r["total"]))
        for it in r["items"]:
            print(("OK " if it["ok"] else "X  ") + it["name"] + "  " + it["detail"])
        __import__("sys").exit(0 if r["passed"] == r["total"] else 1)
    print("PI 自定义接口模块。用 --selftest 自检。")
