# -*- coding: utf-8 -*-
"""run_check.py —— 本地跑通校验：打印一致性/精度指标，并输出 PNG 预览。"""
from __future__ import annotations

import time

import numpy as np
from PIL import Image

import river_repair as rr
from river_scene import make_scene


def despeckle(mask, area_min=rr.AREA_MIN_FRAG):
    ids, n = rr.label(mask)
    out = np.zeros_like(mask)
    kept = []
    for k in range(1, n + 1):
        m = ids == k
        if m.sum() >= area_min:
            out |= m
            kept.append(k)
    return out, len(kept), n


def main():
    scene = make_scene(seed=7)
    W, H = scene["W"], scene["H"]
    gt_xy, gt_w = scene["true_center"], scene["true_width"]

    t0 = time.time()
    inp, n_keep, n0 = despeckle(scene["input_mask"])
    t_desp = time.time() - t0
    print("场景 %dx%d  输入连通域 %d 个（去噪后 %d 个）  说明:GT 河流被切成 %d 段"
          % (W, H, n0, n_keep, len(scene["cuts"]) + 1))

    # 碎片几何描述一览
    ids_i, n_i = rr.label(inp)
    frags_dbg = rr.analyze_fragments(ids_i, n_i)
    print("\n-- 输入连通域几何描述 --")
    for f in sorted(frags_dbg, key=lambda d: -d["length"]):
        print("  #%2d area=%5d 长度=%6.1f 厚度=%5.2f 伸长率=%5.2f 端点(%.0f,%.0f)-(%.0f,%.0f)"
              % (f["id"], f["area"], f["length"], f["thick"], f["elong"],
                 f["end0"][0], f["end0"][1], f["end1"][0], f["end1"][1]))

    def obst_pixels(mask):
        return int((mask & scene["obst_mask"]).sum())

    print("\n=== 方法A：形态学闭运算 + 长条连通域筛选 ===")
    print(" r | 连通域 | 选中长度 | 伸长率 | 误并入干扰px | 中心线长 | 均值误差 | P95 | 覆盖率@2px | 宽度RMSE")
    A = {}
    for r in range(1, 9):
        res = rr.repair_method_A(inp, r)
        cl = rr.centerline_from_mask(res["mask"])
        ev = rr.eval_centerline(cl["points"], gt_xy, gt_w)
        we = rr.width_profile_error(cl["points"], cl["width"], gt_xy, gt_w)
        A[r] = dict(res=res, cl=cl, ev=ev, we=we)
        ch = res["chosen"]
        print("%2d | %6d | %8.1f | %6.2f | %12d | %8.1f | %8.2f | %5.2f | %10.3f | %7.2f"
              % (r, res["comps"], ch["length"] if ch else 0, ch["elong"] if ch else 0,
                 obst_pixels(res["mask"]), cl["length"], ev["mean"], ev["p95"], ev["cov2"],
                 we["rmse"]))

    print("\n=== 方法B：碎片链重建（几何约束 + 最优链 + 桥接）===")
    t0 = time.time()
    B = rr.repair_method_B(inp)
    t_b = time.time() - t0
    st = B["stats"]
    print("碎片 %d 个；候选连接 %d 条（合法 %d 条）；选链长度 %d 段"
          % (len(B["frags"]), len(B["cands"]), sum(c["ok"] for c in B["cands"]), len(B["chain"])))
    print("链: 总长 %.1f px, 平均厚度 %.2f px, 长条比 长度/厚度 = %.1f (阈值 %.1f) -> %s"
          % (st["total_len"], st["thick"], st["strip_ratio"], rr.STRIP_RATIO,
             "接受" if B["accepted"] else "拒绝"))
    print("桥接 %d 处；跨接缝隙总长 %.1f px" % (len(B["links"]), st["gap_sum"]))
    for c in B["cands"]:
        if not c["ok"]:
            print("   拒绝连接 碎片%d->%d  d=%.1fpx  理由: %s" % (c["a"], c["b"], c["d"], "; ".join(c["reasons"])))
    clB = rr.centerline_from_mask(B["mask"])
    evB = rr.eval_centerline(clB["points"], gt_xy, gt_w)
    weB = rr.width_profile_error(clB["points"], clB["width"], gt_xy, gt_w)
    idsR, nR = rr.label(B["mask"])
    included = sorted(set(np.unique(idsR[B["mask"]])) - {0})
    print("修复掩膜: 连通域 %d 个（理想=1）；残留碎片 %d 个；误并入干扰像素 %d px"
          % (len(included), max(0, len(included) - 1), obst_pixels(B["mask"])))
    print("中心线: 长度 %.1f px（GT %.1f）；误差 均值%.2f 中位%.2f P95 %.2f 最大%.2f px；"
          "覆盖率@2px %.1f%%；宽度 RMSE %.2f px；耗时 %.2f s"
          % (clB["length"], float(np.sum(np.linalg.norm(np.diff(gt_xy, axis=0), axis=1))),
             evB["mean"], evB["median"], evB["p95"], evB["max"], 100 * evB["cov2"],
             weB["rmse"], t_b))
    print("去噪耗时 %.2fs" % t_desp)

    # ---------- 预览 PNG ----------
    scale = 3
    img = np.zeros((H, W, 3), np.uint8)
    img[scene["obst_mask"]] = (90, 60, 60)
    img[inp & ~scene["obst_mask"]] = (110, 150, 190)
    img[B["mask"] & ~inp] = (40, 120, 90)
    img[clB["skeleton"]] = (250, 250, 250)
    for x, y in clB["points"]:
        ix, iy = int(x), int(y)
        if 0 <= ix < W and 0 <= iy < H:
            img[max(0, iy - 0):iy + 1, ix] = (255, 170, 40)
    for x, y in gt_xy[::4]:
        ix, iy = int(x), int(y)
        if 0 <= ix < W and 0 <= iy < H:
            img[iy, ix] = (60, 220, 255)
    Image.fromarray(img).resize((W * scale, H * scale), Image.NEAREST).save("preview.png")
    print("\n已输出 preview.png")


if __name__ == "__main__":
    main()
