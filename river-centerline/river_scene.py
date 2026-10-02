# -*- coding: utf-8 -*-
"""
river_scene.py —— 合成测试场景：断裂的长条河流 + 干扰形状 + 噪声

产出（栅格真值，仅用于评估与可视化对照）：
  true_mask       完整（未断裂）河流带状掩膜
  true_center     GT 中心线折线 (N,2) 像素坐标
  true_width      GT 中心线各点对应的河宽
  frag_mask       随机切断后的碎片掩膜（算法输入的一部分）
  obst_mask       干扰形状掩膜（椭圆/矩形/随机块/近岸湖/盐噪声）
  obst_meta       每个干扰连通域的 类型/面积
  input_mask      frag_mask | obst_mask —— 算法唯一可见的输入

干扰形状放置规则：自动外推偏移量，保证与河流、与其它干扰至少留 2px 空档
（唯一例外是"近岸湖"，故意留 2px，用来展示闭运算会把近邻形状误并进来）。
"""

from __future__ import annotations

import math

import numpy as np

from river_repair import dilate, label, resample_polyline


def _river_path(W: int, H: int):
    t = np.linspace(0.0, 1.0, 4000)
    x = 16.0 + (W - 32.0) * t + 5.0 * np.sin(2 * np.pi * 2.1 * t + 1.2)
    y = H * 0.5 + 0.30 * H * np.sin(2 * np.pi * 1.15 * t + 0.7) + 0.07 * H * np.sin(2 * np.pi * 3.4 * t + 2.1)
    return resample_polyline(np.stack([x, y], axis=1), 1.0)


def _normals(path: np.ndarray):
    d = np.gradient(path, axis=0)
    n = np.stack([-d[:, 1], d[:, 0]], axis=1)
    ln = np.linalg.norm(n, axis=1)
    ln[ln == 0] = 1.0
    return n / ln[:, None]


def _rasterize_band(path, width, shape):
    H, W = shape
    gx, gy = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
    mask = np.zeros(shape, bool)
    seg = np.full(shape, -1, np.int32)
    for i in range(len(path) - 1):
        ax, ay = path[i]
        bx, by = path[i + 1]
        vx, vy = bx - ax, by - ay
        L2 = vx * vx + vy * vy
        if L2 <= 0:
            continue
        tt = np.clip(((gx - ax) * vx + (gy - ay) * vy) / L2, 0.0, 1.0)
        px, py = ax + tt * vx, ay + tt * vy
        hit = np.hypot(gx - px, gy - py) <= 0.5 * width[i]
        new = hit & ~mask
        mask |= hit
        seg[new] = i
    return mask, seg


def make_scene(seed: int = 7, W: int = 300, H: int = 200,
               n_breaks: int = 6, gap_min: float = 5.0, gap_max: float = 11.0,
               n_salt: int = 52):
    rng = np.random.default_rng(seed)
    path = _river_path(W, H)
    nrm = _normals(path)
    L = len(path)
    arc = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(path, axis=0), axis=1))])
    u = arc / arc[-1]

    width = 7.0 + 2.0 * np.sin(2 * np.pi * 2.3 * u + 0.4)
    width *= (1.0 - 0.30 * np.clip((arc[-1] - arc) / 14.0, 0, 1))     # 末端收窄
    width *= (1.0 - 0.45 * np.clip((12.0 - arc) / 12.0, 0, 1))        # 起端收窄
    width = np.maximum(width, 3.2)

    true_mask, seg = _rasterize_band(path, width, (H, W))

    # ---- 随机切断（保证切口间距，避免出现零星碎屑）
    keep = np.ones(L, bool)
    cuts = []
    last_end = -99
    for p in np.sort(rng.uniform(0.10, 0.88, size=n_breaks)):
        i0 = int(p * (L - 1))
        if i0 - last_end < 30:
            i0 = last_end + 30
        if i0 >= L - 20:
            continue
        gl = int(round(rng.uniform(gap_min, gap_max)))
        i1 = min(L - 1, i0 + gl)
        keep[i0:i1] = False
        cuts.append((i0, i1))
        last_end = i1
    frag_mask = true_mask & keep[seg]

    # ---- 干扰形状（自动保持间距）
    H_, W_ = H, W
    gx, gy = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)

    def ellipse(c, a, b, th):
        X = (gx - c[0]) * math.cos(th) + (gy - c[1]) * math.sin(th)
        Y = -(gx - c[0]) * math.sin(th) + (gy - c[1]) * math.cos(th)
        return (X / a) ** 2 + (Y / b) ** 2 <= 1.0

    def rect(c, w, h, th):
        X = (gx - c[0]) * math.cos(th) + (gy - c[1]) * math.sin(th)
        Y = -(gx - c[0]) * math.sin(th) + (gy - c[1]) * math.cos(th)
        return (np.abs(X) <= w / 2) & (np.abs(Y) <= h / 2)

    obst = np.zeros((H, W), bool)
    specs = []      # (kind, mask)

    def place(kind, maker, frac, off, inward=False, min_gap=2):
        """在河道 frac 处沿法线偏移 off 放置形状；不足间距则自动外推。"""
        nonlocal obst
        step = -2.5 if inward else 2.5
        for k in range(24):
            cur = off + step * k
            i = int(frac * (L - 1))
            c = path[i] + nrm[i] * cur
            m = maker(c, i)
            if m.sum() == 0:
                continue
            ok_river = not (dilate(true_mask, min_gap) & m).any()
            ok_other = not (dilate(obst, max(2, min_gap)) & m).any()
            inside = m[0, :].any() or m[-1, :].any() or m[:, 0].any() or m[:, -1].any()
            if ok_river and ok_other and not inside:
                obst |= m
                specs.append((kind, m.copy()))
                return c
        # 兜底：强行放置
        i = int(frac * (L - 1))
        c = path[i] + nrm[i] * off
        m = maker(c, i)
        obst |= m
        specs.append((kind, m.copy()))
        return c

    place("椭圆", lambda c, i: ellipse(c, 13, 9, 0.4), 0.15, +32)
    place("椭圆(近)", lambda c, i: ellipse(c, 9, 7, -0.6), 0.34, -16)
    place("细长矩形", lambda c, i: rect(c, 46, 13, math.atan2(nrm[i][1], nrm[i][0]) + math.radians(12)), 0.62, +26)
    place("小条带", lambda c, i: rect(c, 26, 8, math.atan2(nrm[i][1], nrm[i][0]) + math.radians(4)), 0.45, +16)
    place("随机块", lambda c, i: _blob(rng, c, (H, W), 150, 1.6), 0.80, -28)
    place("近岸湖", lambda c, i: ellipse(c, 7, 6, 0.0), 0.90, +12, min_gap=2)

    # 盐噪声
    salt = np.zeros((H, W), bool)
    for _ in range(n_salt):
        x = int(rng.integers(3, W - 3))
        y = int(rng.integers(3, H - 3))
        s = int(rng.integers(1, 3))
        salt[y:y + s, x:x + s] = True
    obst |= salt
    specs.append(("盐噪声", salt))

    input_mask = frag_mask | obst

    # ---- 干扰连通域类型（按最大重叠归属）
    ids_o, n_o = label(obst)
    obst_meta = {}
    for k in range(1, n_o + 1):
        m = ids_o == k
        a = int(m.sum())
        best, best_ov = "未知", 0
        for kind, sm in specs:
            ov = int((m & sm).sum())
            if ov > best_ov:
                best, best_ov = kind, ov
        obst_meta[k] = dict(kind=best, area=a, is_salt=(a <= 9))

    return dict(W=W, H=H, path=path, width=width, u=u, true_mask=true_mask,
                true_center=path, true_width=width, seg=seg, keep=keep,
                frag_mask=frag_mask, obst_mask=obst, obst_salt=salt,
                input_mask=input_mask, cuts=cuts, obst_ids=ids_o, obst_meta=obst_meta,
                n_obst=n_o)


def _blob(rng, c, shape, steps, sigma):
    H, W = shape
    m = np.zeros(shape, bool)
    x, y = float(c[0]), float(c[1])
    for _ in range(steps):
        x += rng.normal(0, sigma)
        y += rng.normal(0, sigma)
        ix, iy = int(x), int(y)
        if 0 <= ix < W and 0 <= iy < H:
            m[iy, ix] = True
    return m
