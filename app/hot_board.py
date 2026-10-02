# -*- coding: utf-8 -*-
"""热点榜数据抓取器。

复用 piengine 里 hot_topics 工具的真实抓取实现（百度/头条/抖音/贴吧/B站/GitHub），
把结果落盘为两份文件，供展示页面使用：

  app/web/hot-topics.json   结构化数据（API/其它程序读取）
  app/web/hot-topics.js     window.HOT_DATA = {...}（页面用 <script> 加载，
                            这样通过 file:// 直接双击打开也不会被 CORS 拦住）

说明：这里直接调用内部函数 _fetch_hub，而不是工具入口 t_hot_topics——后者为适配
模型消息有 13000 字符预算，会按比例截断条目（实测每源只剩 6 条）；展示页需要完整数据。

用法：
    python app/hot_board.py                    # 全部源，每源 10 条，实时榜
    python app/hot_board.py --sources baidu,toutiao --top 12
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import piengine as E  # noqa: E402  项目自带引擎，提供真实抓取实现

WEB_DIR = os.path.join(HERE, "web")
JSON_OUT = os.path.join(WEB_DIR, "hot-topics.json")
JS_OUT = os.path.join(WEB_DIR, "hot-topics.js")

ALIAS = {"weibo": "baidu", "微博": "baidu", "百度": "baidu", "热搜": "baidu",
         "头条": "toutiao", "今日头条": "toutiao", "抖音": "douyin",
         "贴吧": "tieba", "b站": "bili", "哔哩哔哩": "bili", "bilibili": "bili",
         "github": "github", "gh": "github"}


def pick_sources(raw):
    """解析源名（与 hot_topics 工具一致的别名规则），返回确定存在的源 key 列表。"""
    names = [x.strip() for x in re.split(r"[,，;；\s]+", str(raw or "")) if x.strip()]
    if not names or any(n.lower() in ("all", "*", "全部", "所有", "全部源") for n in names):
        return list(E.HUBS)
    low = {k.lower(): k for k in E.HUBS}
    out, seen = [], set()
    for n in names:
        k = low.get(n.lower()) or ALIAS.get(n.lower())
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def grab(top=10, days=0, sources="all", timeout=12):
    """抓取并返回结构化数据（字段与 hot_topics format=json 的 data 一致，但不截断）。"""
    cfg = E.load_config()
    ctx = {"proxy": (cfg.get("prefs") or {}).get("proxy") or "", "cfg": cfg}
    top = max(1, min(int(top or 10), 50))
    timeout = max(3, min(int(timeout or 12), 60))
    sources_out, errors, ok_n = [], [], 0
    for k in pick_sources(sources):
        hub = E.HUBS[k]
        r = E._fetch_hub(hub, ctx, timeout, top)
        if not r["ok"]:
            errors.append("%s：%s" % (hub["label"], r["text"]))
            sources_out.append({"source": k, "label": hub["label"], "ok": False,
                                "endpoint": r.get("url") or hub["url"], "count": 0,
                                "items": [], "error": r["text"]})
            continue
        ok_n += 1
        items = [{"rank": it["rank"], "title": it["title"], "hot": it.get("hot") or "",
                  "hot_text": E._num(it.get("hot")), "url": it.get("url") or "",
                  "extra": it.get("extra") or ""} for it in r["items"]]
        sources_out.append({"source": k, "label": hub["label"], "tip": hub["tip"],
                            "ok": True, "endpoint": r["url"], "count": len(items),
                            "items": items})
    return {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "days": int(days or 0), "top": top, "sources": sources_out,
            "errors": errors, "ok_count": ok_n, "fail_count": len(errors)}


def save(data, json_path=JSON_OUT, js_path=JS_OUT):
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    txt = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(txt)
    os.replace(tmp, json_path)                      # 原子替换，避免页面读到半个文件
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.HOT_DATA = " + txt + ";\n")
    return json_path, js_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="抓取实时热点榜并落盘")
    ap.add_argument("--top", type=int, default=10, help="每源条数 1-50")
    ap.add_argument("--days", type=int, default=0, help="0=实时/今天，1=近24小时")
    ap.add_argument("--sources", default="all", help="逗号分隔，all=全部源")
    ap.add_argument("--timeout", type=int, default=12, help="单源超时秒")
    ap.add_argument("--json", default=JSON_OUT)
    ap.add_argument("--js", default=JS_OUT)
    a = ap.parse_args(argv)

    data = grab(a.top, a.days, a.sources, a.timeout)
    json_p, js_p = save(data, a.json, a.js)
    print("ok=%s fail=%s sources=%s fetched_at=%s"
          % (data.get("ok_count"), data.get("fail_count"),
             len(data.get("sources") or []), data.get("fetched_at")))
    for s in data.get("sources") or []:
        print("  - %-8s %-12s %s 条" % (s.get("source"), s.get("label"),
                                        s.get("count") or 0))
    for e in data.get("errors") or []:
        print("  ! " + str(e))
    print("写入 %s" % json_p)
    print("写入 %s" % js_p)
    return 0 if data.get("ok_count") else 1


if __name__ == "__main__":
    sys.exit(main())
