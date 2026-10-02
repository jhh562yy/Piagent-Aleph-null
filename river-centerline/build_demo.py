# -*- coding: utf-8 -*-
"""
build_demo.py —— 跑一遍算法并把所有中间结果打包进自包含可视化网页。

  输入：river_scene 合成的断裂河流场景
  输出：demo.json（结构化数据） + river-centerline-viz.html（内嵌数据，双击即看）
"""
from __future__ import annotations

import base64
import json
import time

import numpy as np

import river_repair as rr
from river_scene import make_scene

OUT_HTML = "river-centerline-viz.html"
OUT_JSON = "demo.json"
TEMPLATE = "viewer_template.html"


# ---------------------------------------------------------------- 编码工具
def bits_b64(mask: np.ndarray) -> str:
    packed = np.packbits(mask.reshape(-1).astype(np.uint8), bitorder="little")
    return base64.b64encode(packed.tobytes()).decode("ascii")


def rle_grid(grid: np.ndarray) -> str:
    """uint8 栅格 -> 行游程编码 "s,l,v;..." 行间用 | 连接。"""
    rows = []
    for y in range(grid.shape[0]):
        row = grid[y]
        out = []
        i = 0
        n = len(row)
        while i < n:
            v = int(row[i])
            j = i + 1
            while j < n and int(row[j]) == v:
                j += 1
            if v:
                out.append("%d,%d,%d" % (i, j - i, v))
            i = j
        rows.append(";".join(out))
    return "|".join(rows)


def pts_json(arr) -> list:
    if arr is None or len(arr) == 0:
        return []
    return [[round(float(x), 3), round(float(y), 3)] for x, y in np.asarray(arr)]


def r3(x):
    try:
        v = float(x)
    except Exception:
        return None
    return None if not np.isfinite(v) else round(v, 4)


def metrics_dict(ev, we, cl, extra=None):
    d = dict(mean=r3(ev.get("mean")), median=r3(ev.get("median")), p95=r3(ev.get("p95")),
             max=r3(ev.get("max")), cov2=r3(ev.get("cov2")), cov3=r3(ev.get("cov3")),
             gt_cover2=r3(ev.get("gt_cover2")), gt_max=r3(ev.get("gt_max")),
             n=int(ev.get("n", 0)),
             w_rmse=r3(we.get("rmse")), w_mae=r3(we.get("mae")),
             w_gt=r3(we.get("mean_gt")), w_pred=r3(we.get("mean_pred")),
             cl_len=r3(cl.get("length")), cl_raw=r3(cl.get("raw_len")))
    if extra:
        d.update(extra)
    return d


def centerline_err_profile(pred_xy, gt_xy):
    if len(pred_xy) == 0:
        return []
    gq = rr.PolyDist(gt_xy)
    return [round(float(gq.nearest(x, y)[0]), 3) for x, y in pred_xy]


# ---------------------------------------------------------------- 主流程
def main():
    t_start = time.time()
    scene = make_scene(seed=7)
    W, H = scene["W"], scene["H"]
    inp = scene["input_mask"]

    # —— 全输入连通域（用于可视化：含噪声点/干扰形状）
    ids_all, n_all = rr.label(inp)
    all_frags = rr.analyze_fragments(ids_all, n_all)
    meta_all = {f["id"]: f for f in all_frags}

    # —— 方法 B
    t0 = time.perf_counter()
    B = rr.repair_method_B(inp)
    t_b = time.perf_counter() - t0
    clB = rr.centerline_from_mask(B["mask"])
    evB = rr.eval_centerline(clB["points"], scene["true_center"], scene["true_width"])
    weB = rr.width_profile_error(clB["points"], clB["width"], scene["true_center"], scene["true_width"])
    ids_rep, n_rep = rr.label(B["mask"])

    # —— 方法 A（不同闭运算半径）
    A_list = []
    for r in range(1, 9):
        res = rr.repair_method_A(inp, r)
        cl = rr.centerline_from_mask(res["mask"])
        ev = rr.eval_centerline(cl["points"], scene["true_center"], scene["true_width"])
        we = rr.width_profile_error(cl["points"], cl["width"], scene["true_center"], scene["true_width"])
        ch = res["chosen"]
        A_list.append(dict(
            r=r,
            maskBits=bits_b64(res["mask"]),
            center=pts_json(cl["points"]),
            width=[round(float(w), 3) for w in cl["width"]],
            metrics=metrics_dict(ev, we, cl, dict(
                comps=int(res["comps"]),
                sel_len=r3(ch["length"]) if ch else 0.0,
                sel_elong=r3(ch["elong"]) if ch else 0.0,
                contam=int((res["mask"] & scene["obst_mask"]).sum()),
                mask_px=int(res["mask"].sum()))),
        ))

    # —— 真值：河流带 + 中心线（GT 中心线用算法从"完整河流"里提，作为对照基线）
    clGT = rr.centerline_from_mask(scene["true_mask"])
    evGT = rr.eval_centerline(clGT["points"], scene["true_center"], scene["true_width"])

    # —— 碎片表（角色标注）
    obst_meta = scene["obst_meta"]
    chain_set = set(B["chain"])
    noise_set = set(B["noise_ids"])
    used_pairs = set()
    for c in B["links"]:
        used_pairs.add((c["a"], c["b"]))
        used_pairs.add((c["b"], c["a"]))
    frags_json = []
    for f in all_frags:
        fid = f["id"]
        if fid in chain_set and f["area"] >= rr.AREA_MIN_FRAG:
            role = "chain"
        elif fid in noise_set:
            role = "noise"
        else:
            role = "distractor"
        kind = obst_meta.get(fid, {}).get("kind", "碎片" if role == "chain" else "形状") \
            if role != "chain" else "河流碎片"
        frags_json.append(dict(
            id=fid, area=int(f["area"]), length=r3(f["length"]), thick=r3(f["thick"]),
            elong=r3(f["elong"]), cx=r3(f["cx"]), cy=r3(f["cy"]),
            end0=pts_json([f["end0"]])[0], end1=pts_json([f["end1"]])[0],
            tan0=pts_json([f["tan0"]])[0] if f["tan0"] else None,
            tan1=pts_json([f["tan1"]])[0] if f["tan1"] else None,
            skel=pts_json(f["skel"]), role=role, kind=kind,
            eligible=bool(f["area"] >= rr.AREA_MIN_LINK),
            accepted_link_count=sum(1 for c in B["links"] if c["a"] == fid or c["b"] == fid)))

    cands_json = []
    for c in B["cands"]:
        cands_json.append(dict(a=c["a"], b=c["b"], ea=c["ea"], eb=c["eb"], pa=c["pa"], pb=c["pb"],
                               d=r3(c["d"]), cost=r3(c["cost"]), ok=bool(c["ok"]),
                               reasons=list(c["reasons"]),
                               chosen=bool((c["a"], c["b"]) in used_pairs
                                           and any(l["a"] == c["a"] and l["b"] == c["b"] and
                                                   l["ea"] == c["ea"] and l["eb"] == c["eb"]
                                                   for l in B["links"]))))

    bridges_json = []
    for br in B["bridges"]:
        bridges_json.append(dict(a=br["a"], b=br["b"], d=r3(br["d"]), r=r3(br["r"]),
                                 pa=br["pa"], pb=br["pb"], ea=br["ea"], eb=br["eb"],
                                 arcA=pts_json(br["arc_a"]), arcB=pts_json(br["arc_b"])))

    data = dict(
        meta=dict(
            W=W, H=H, seed=7, n_breaks=len(scene["cuts"]),
            gap_min=5.0, gap_max=11.0,
            n_input_comp=int(n_all), n_noise=len(B["noise_ids"]), n_frag=len(B["frags"]),
            n_obst=int(scene["n_obst"]), time_b=round(t_b, 4),
            time_total=round(time.time() - t_start, 3),
            params=dict(AREA_MIN_FRAG=rr.AREA_MIN_FRAG, AREA_MIN_LINK=rr.AREA_MIN_LINK,
                        ELONG_MIN_LINK=rr.ELONG_MIN_LINK, GAP_MAX=rr.GAP_MAX,
                        ANGLE_MAX=round(np.degrees(rr.ANGLE_MAX), 1),
                        PRED_REACH=rr.PRED_REACH,
                        THICK_RATIO_MAX=rr.THICK_RATIO_MAX, STRIP_RATIO=rr.STRIP_RATIO,
                        ERR_HARD_K=rr.ERR_HARD_K, ERR_HARD_B=rr.ERR_HARD_B),
            cuts=[[int(a), int(b)] for a, b in scene["cuts"]],
        ),
        bits=dict(input=bits_b64(inp), obst=bits_b64(scene["obst_mask"]),
                  salt=bits_b64(scene["obst_salt"]), true=bits_b64(scene["true_mask"]),
                  bMask=bits_b64(B["mask"]), bSkel=bits_b64(clB["skeleton"]),
                  bTube=bits_b64(B["tubes"])),
        rle=dict(fragIds=rle_grid(ids_all.astype(np.uint8))),
        frags=frags_json,
        cands=cands_json,
        bridges=bridges_json,
        A=[dict(r=a["r"], mask=a["maskBits"], center=a["center"], width=a["width"],
                metrics=a["metrics"]) for a in A_list],
        B=dict(center=pts_json(clB["points"]), width=[round(float(w), 3) for w in clB["width"]],
               err=centerline_err_profile(clB["points"], scene["true_center"]),
               metrics=metrics_dict(evB, weB, clB, dict(
                   chain=list(B["chain"]), n_chain=len(B["chain"]),
                   strip_ratio=r3(B["stats"]["strip_ratio"]),
                   gap_sum=r3(B["stats"]["gap_sum"]), n_bridge=len(B["links"]),
                   comps=int(len(set(np.unique(ids_rep)) - {0})),
                   contam=int((B["mask"] & scene["obst_mask"]).sum()),
                   mask_px=int(B["mask"].sum()),
                   accepted=bool(B["accepted"])))),
        gt=dict(center=pts_json(scene["true_center"]),
                width=[round(float(w), 3) for w in scene["true_width"]],
                cl_center=pts_json(clGT["points"]),
                metrics=metrics_dict(evGT, rr.width_profile_error(
                    clGT["points"], clGT["width"], scene["true_center"], scene["true_width"]), clGT),
                len=r3(float(np.sum(np.linalg.norm(np.diff(scene["true_center"], axis=0), axis=1))))),
    )

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    with open(TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = html.replace("__DATA__", payload)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print("已生成 %s (%.1f KB) 与 %s (%.1f KB)"
          % (OUT_HTML, len(html.encode("utf-8")) / 1024, OUT_JSON,
             len(json.dumps(data).encode("utf-8")) / 1024))
    print("方法B: 链%s 长%.1fpx 误差均值%.3f P95 %.3f 覆盖@2px %.1f%% 宽度RMSE %.3f 耗时%.3fs"
          % (B["chain"], clB["length"], evB["mean"], evB["p95"], 100 * evB["cov2"],
             weB["rmse"], t_b))


if __name__ == "__main__":
    main()
