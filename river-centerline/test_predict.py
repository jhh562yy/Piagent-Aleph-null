# -*- coding: utf-8 -*-
"""单元自测：圆弧外推 predict_forward 的正确性（修复算法的核心判据）。"""
import math

import numpy as np

import river_repair as rr


def test_arc_extrapolation():
    """一段标准圆弧上，向两端外推 12px 应当落在弧的延长线上（误差 < 1e-6）。"""
    R, cx, cy = 40.0, 100.0, 100.0
    ang = np.linspace(-1.0, 1.0, 200)
    pts = np.stack([cx + R * np.cos(ang), cy + R * np.sin(ang)], axis=1)
    f = dict(skel=[tuple(p) for p in pts], thick=6.0)
    for which in (0, 1):
        pred, tan = rr.predict_forward(f, which, 12.0)
        a_true = ang[0] - 12.0 / R if which == 0 else ang[-1] + 12.0 / R
        true = np.array([cx + R * math.cos(a_true), cy + R * math.sin(a_true)])
        t_true = (np.array([math.sin(a_true), -math.cos(a_true)]) if which == 0
                  else np.array([-math.sin(a_true), math.cos(a_true)]))
        err = float(np.linalg.norm(pred - true))
        ang_err = math.degrees(math.acos(float(np.clip(np.dot(tan, t_true), -1, 1))))
        assert err < 1e-6, "圆弧外推位置误差 %.6f px" % err
        assert ang_err < 1e-6, "圆弧外推切向误差 %.6f°" % ang_err
    print("[OK] 圆弧外推：位置与切向误差均 < 1e-6")


def test_straight_line():
    """近直线（Kasa 圆拟合退化）必须退化为线性外推，不能硬套小半径圆弧。"""
    line = np.stack([np.arange(20) + 50.0, np.full(20, 30.0)], axis=1)
    g = dict(skel=[tuple(p) for p in line], thick=6.0)
    for which, expect in ((0, (40.0, 30.0)), (1, (79.0, 30.0))):
        pred, tan = rr.predict_forward(g, which, 10.0)
        err = float(np.linalg.norm(pred - np.array(expect)))
        assert err < 1e-6, "直线外推误差 %.6f px (which=%d)" % (err, which)
    print("[OK] 直线外推：退化处理正确，误差 < 1e-6")


def test_centerline_subpixel():
    """理想直带（宽 7px，中心 y=30）中心线应落在 30.0 附近（<0.2px 偏差）。"""
    H, W = 60, 200
    gy = np.arange(H)[:, None] + 0.5
    band = np.repeat(np.abs(gy - 30.0) <= 3.5, W, axis=1)
    cl = rr.centerline_from_mask(band)
    bias = float(np.abs(cl["points"][:, 1] - 30.0).mean())
    assert bias < 0.2, "直带中心线偏差 %.3f px" % bias
    print("[OK] 法向剖面细化：直带中心偏差 %.3f px" % bias)


if __name__ == "__main__":
    test_arc_extrapolation()
    test_straight_line()
    test_centerline_subpixel()
    print("全部通过")
