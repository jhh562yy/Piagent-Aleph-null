# -*- coding: utf-8 -*-
"""
river_repair.py —— 断裂长条形状（河流）修复 + 中心线提取算法库

问题设定
--------
输入是二值栅格掩膜：一条"长条形"目标（河流）远看连续，实际被随机切断成若干碎片，
旁边还有若干规则/不规则的干扰形状（椭圆、矩形、随机块、盐噪声点）。

算法主线（方法 B：碎片链重建 / fragment chaining）
------------------------------------------------
 1. 去噪            去掉面积小于阈值的孤立斑点
 2. 碎片几何分析     连通域标记 -> 每块的 质心/主轴(PCA)/长度/厚度/伸长率/两个端点
 3. 候选配对         端点距离 / 出射方向夹角 / 横向偏移 / 厚度一致性 四道几何约束
 4. 最优链搜索       在"碎片图"上做带记忆的 DFS，最大化链的总长度（即最长条）
 5. 长条判据         链的 长度/厚度 ≥ STRIP_RATIO 才认定为"长条河流"，否则弃用
 6. 桥接             在相邻碎片端点间画等宽"连接管"，得到拓扑连通的修复掩膜
 7. 骨架化           Zhang-Suen 细化
 8. 中心线           骨架图上双端 BFS 求测地最长路径 + 毛刺剪枝 + 重采样 + 滑动平均平滑
 9. 宽度             掩膜欧氏距离变换沿中心线采样（垂直于走向取局部极大），得到宽度剖面

对照方法（方法 A：形态学闭合 + 形状筛选）
----------------------------------------
直接对掩膜做圆盘闭运算（膨胀+腐蚀）把断裂"糊"起来，再用 伸长率/长度 挑出长条连通域。
实现简单，但闭运算会把距离近的干扰形状一起并进来（本文可视化里可见其副作用）。

依赖：numpy（必需）。无其它依赖。
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

# ---------------------------------------------------------------- 参数常量
AREA_MIN_FRAG = 18.0      # 碎片最小面积（去噪阈值）
AREA_MIN_LINK = 20.0      # 参与链接的碎片最小面积
ELONG_MIN_LINK = 1.55     # 参与链接的碎片最小伸长率 长度/厚度
GAP_MAX = 24.0            # 允许桥接的最大端点间距(px)
ANGLE_MAX = math.radians(75.0)   # 端点出射方向与连接方向的最大夹角（宽松硬约束）
PRED_REACH = 16.0         # 端点外推时向内取用的骨架弧长(px)
ERR_HARD_K = 2.2          # 外推吻合硬上限 = ERR_HARD_K*平均厚度 + ERR_HARD_B
ERR_HARD_B = 8.0
ERR_TOL_K = 0.95          # 外推吻合"理想"容差（用于打分/解释）
ERR_TOL_B = 3.5
THICK_RATIO_MAX = 2.60    # 相邻碎片厚度比上限
STRIP_RATIO = 9.0         # 长条判据：链长度/平均厚度


# ================================================================ 基础形态学
def disk_offsets(r: int):
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    m = (y * y + x * x) <= r * r
    return np.stack([y[m], x[m]], axis=1)


def dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """圆盘膨胀（边界外视为背景）。"""
    if r <= 0:
        return mask.copy()
    H, W = mask.shape
    out = np.zeros_like(mask)
    for dy, dx in disk_offsets(r):
        ys, yd = slice(max(0, dy), H + min(0, dy)), slice(max(0, -dy), H + min(0, -dy))
        xs, xd = slice(max(0, dx), W + min(0, dx)), slice(max(0, -dx), W + min(0, -dx))
        out[yd, xd] |= mask[ys, xs]
    return out


def erode(mask: np.ndarray, r: int) -> np.ndarray:
    """圆盘腐蚀（边界外视为背景，等价于零填充）。"""
    if r <= 0:
        return mask.copy()
    H, W = mask.shape
    out = mask.copy()
    for dy, dx in disk_offsets(r):
        sh = np.zeros_like(mask)
        ys, yd = slice(max(0, dy), H + min(0, dy)), slice(max(0, -dy), H + min(0, -dy))
        xs, xd = slice(max(0, dx), W + min(0, dx)), slice(max(0, -dx), W + min(0, -dx))
        sh[yd, xd] = mask[ys, xs]
        out &= sh
    return out


def closing(mask: np.ndarray, r: int) -> np.ndarray:
    """形态学闭运算：先膨胀后腐蚀，用来桥接小断裂。"""
    if r <= 0:
        return mask.copy()
    return erode(dilate(mask, r), r)


def label(mask: np.ndarray):
    """8 邻域连通域标记 -> (id 图, 个数)，id 从 1 开始。"""
    H, W = mask.shape
    ids = np.zeros((H, W), np.int32)
    n = 0
    for y0 in range(H):
        row = mask[y0]
        for x0 in range(W):
            if row[x0] and ids[y0, x0] == 0:
                n += 1
                ids[y0, x0] = n
                stack = [(y0, x0)]
                while stack:
                    cy, cx = stack.pop()
                    for dy in (-1, 0, 1):
                        ny = cy + dy
                        if ny < 0 or ny >= H:
                            continue
                        for dx in (-1, 0, 1):
                            nx = cx + dx
                            if nx < 0 or nx >= W or ids[ny, nx] or not mask[ny, nx]:
                                continue
                            ids[ny, nx] = n
                            stack.append((ny, nx))
    return ids, n


# ================================================================ 距离变换
def _dt1d(f: np.ndarray) -> np.ndarray:
    """一维平方距离变换（Felzenszwalb & Huttenlocher 抛物线包络）。"""
    n = len(f)
    d = np.empty(n, dtype=np.float64)
    v = np.zeros(n, dtype=np.int64)
    z = np.empty(n + 1, dtype=np.float64)
    k = 0
    v[0] = 0
    z[0] = -np.inf
    z[1] = np.inf
    for q in range(1, n):
        while True:
            vk = v[k]
            s = ((f[q] + q * q) - (f[vk] + vk * vk)) / (2.0 * q - 2.0 * vk)
            if s <= z[k]:
                k -= 1
            else:
                break
        k += 1
        v[k] = q
        z[k] = s
        z[k + 1] = np.inf
    k = 0
    for q in range(n):
        while z[k + 1] < q:
            k += 1
        dq = q - v[k]
        d[q] = dq * dq + f[v[k]]
    return d


# ---------------------------------------------------------------- 可选加速后端
# 距离变换只是工具，不该是瓶颈：真实影像 2569x2302 逐条算 40 条河的中心线时，
# 纯 Python 的 _dt1d 会被调上万次（约 52s / 62s）。三个后端语义一致（到最近背景像素的距离）：
#   scipy.ndimage.distance_transform_edt -> 精确，首选
#   cv2.distanceTransform(DIST_L2, 5)    -> 近似 5x5 掩膜，无 scipy 时用
#   纯 numpy 两趟一维变换                -> 精确，零第三方依赖时的兜底
try:
    from scipy import ndimage as _sndi
except Exception:                                    # pragma: no cover
    _sndi = None
try:
    import cv2 as _cv2
except Exception:                                    # pragma: no cover
    _cv2 = None

EDT_BACKEND = "scipy" if _sndi is not None else ("cv2" if _cv2 is not None else "python")


def edt(mask: np.ndarray, backend: str = "auto") -> np.ndarray:
    """掩膜内像素 -> 到掩膜外部的欧氏距离。backend="auto"/"scipy"/"cv2"/"python"。"""
    m = np.asarray(mask, dtype=bool)
    if m.size == 0:
        return np.zeros(m.shape, dtype=np.float64)
    b = EDT_BACKEND if backend in (None, "auto") else backend
    if b == "scipy" and _sndi is not None:
        return _sndi.distance_transform_edt(m).astype(np.float64)
    if b == "cv2" and _cv2 is not None:
        return _cv2.distanceTransform(m.astype(np.uint8), _cv2.DIST_L2, 5).astype(np.float64)
    return _edt_python(m)


def _edt_python(mask: np.ndarray) -> np.ndarray:
    """纯 numpy 精确实现（两趟一维变换，Felzenszwalb-Huttenlocher）。"""
    H, W = mask.shape
    INF = 1e12
    f = np.where(mask, 0.0, INF)
    for x in range(W):
        f[:, x] = _dt1d(f[:, x])
    for y in range(H):
        f[y, :] = _dt1d(f[y, :])
    return np.sqrt(f)


# ================================================================ 骨架化
def skeletonize(mask: np.ndarray) -> np.ndarray:
    """Zhang-Suen 细化，返回单像素宽骨架。"""
    img = mask.astype(np.uint8).copy()
    while True:
        changed = False
        for step in (0, 1):
            P = np.pad(img, 1)
            p2, p3, p4 = P[:-2, 1:-1], P[:-2, 2:], P[1:-1, 2:]
            p5, p6, p7 = P[2:, 2:], P[2:, 1:-1], P[2:, :-2]
            p8, p9 = P[1:-1, :-2], P[:-2, :-2]
            B = p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9
            seq = [p2, p3, p4, p5, p6, p7, p8, p9, p2]
            A = np.zeros_like(B)
            for k in range(8):
                A += ((seq[k] == 0) & (seq[k + 1] == 1)).astype(np.uint8)
            if step == 0:
                c1, c2 = p2 * p4 * p6, p4 * p6 * p8
            else:
                c1, c2 = p2 * p4 * p8, p2 * p6 * p8
            cond = (img == 1) & (B >= 2) & (B <= 6) & (A == 1) & (c1 == 0) & (c2 == 0)
            if cond.any():
                img[cond] = 0
                changed = True
        if not changed:
            break
    return img.astype(bool)


NEI8 = [(-1, -1, math.sqrt(2)), (-1, 0, 1.0), (-1, 1, math.sqrt(2)), (0, -1, 1.0),
        (0, 1, 1.0), (1, -1, math.sqrt(2)), (1, 0, 1.0), (1, 1, math.sqrt(2))]


def skeleton_neighbors(sk: np.ndarray, y: int, x: int):
    H, W = sk.shape
    out = []
    for dy, dx, w in NEI8:
        ny, nx = y + dy, x + dx
        if 0 <= ny < H and 0 <= nx < W and sk[ny, nx]:
            out.append((ny, nx, w))
    return out


def dijkstra(sk: np.ndarray, src):
    """骨架上的单源最短路（8 邻域，对角权 sqrt2）。返回 dist 与 prev。"""
    H, W = sk.shape
    INF = float("inf")
    dist = {}
    prev = {}
    import heapq
    pq = [(0.0, src)]
    dist[src] = 0.0
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, INF):
            continue
        for ny, nx, w in skeleton_neighbors(sk, *u):
            v = (ny, nx)
            nd = d + w
            if nd < dist.get(v, INF):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, prev


def skeleton_endpoints(sk: np.ndarray):
    pts = np.argwhere(sk)
    ends = []
    for y, x in pts:
        if len(skeleton_neighbors(sk, int(y), int(x))) == 1:
            ends.append((int(y), int(x)))
    return ends


def spur_prune(sk: np.ndarray, min_len_fn) -> np.ndarray:
    """剪掉短毛刺：从端点走到第一个分叉点，长度不足则删除该支。"""
    sk = sk.copy()
    for _ in range(6):
        removed = 0
        for e in skeleton_endpoints(sk):
            branch = [e]
            cur = e
            prev = None
            while True:
                nbs = [n for n in skeleton_neighbors(sk, *cur) if prev is None or (n[0], n[1]) != prev]
                if len(nbs) != 1:
                    break
                prev = cur
                cur = (nbs[0][0], nbs[0][1])
                if len(skeleton_neighbors(sk, *cur)) != 2:
                    break
                branch.append(cur)
                if len(branch) > 400:
                    break
            # cur 是分叉点（度>=3）
            if len(skeleton_neighbors(sk, *cur)) >= 3 and len(branch) < min_len_fn(cur):
                for p in branch:
                    sk[p] = False
                removed += len(branch)
        if removed == 0:
            break
    return sk


def longest_path(sk: np.ndarray):
    """骨架测地最长路径（双次 Dijkstra）。返回 [(y,x), ...]。"""
    ends = skeleton_endpoints(sk)
    if not ends:
        pts = np.argwhere(sk)
        if len(pts) == 0:
            return []
        ends = [(int(pts[0][0]), int(pts[0][1]))]
    d0, _ = dijkstra(sk, ends[0])
    far = max(d0, key=d0.get)
    d1, prev = dijkstra(sk, far)
    far2 = max(d1, key=d1.get)
    path = [far2]
    while path[-1] != far:
        path.append(prev[path[-1]])
    path.reverse()
    return path


def path_to_xy(path, offset=0.5):
    return np.array([[x + offset, y + offset] for y, x in path], dtype=np.float64)


def resample_polyline(pts: np.ndarray, step: float) -> np.ndarray:
    if len(pts) < 2:
        return pts.copy()
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = s[-1]
    if total <= 0:
        return pts.copy()
    n = max(2, int(round(total / step)) + 1)
    t = np.linspace(0, total, n)
    return np.stack([np.interp(t, s, pts[:, 0]), np.interp(t, s, pts[:, 1])], axis=1)


def smooth_polyline(pts: np.ndarray, win: int = 7, iters: int = 2, keep_ends: bool = True) -> np.ndarray:
    out = pts.copy()
    n = len(out)
    if n < win:
        return out
    k = win // 2
    for _ in range(iters):
        pad = np.vstack([np.repeat(out[:1], k, 0), out, np.repeat(out[-1:], k, 0)])
        cs = np.cumsum(np.vstack([np.zeros((1, 2)), pad]), axis=0)
        sm = (cs[win:] - cs[:-win]) / win
        if keep_ends:
            sm[0], sm[-1] = out[0], out[-1]
        out = sm
    return out


# ================================================================ 几何描述
def analyze_fragment(mask_local: np.ndarray, oy: int, ox: int, fid: int, area: int):
    """
    单碎片的骨架化几何描述：
      长度   = 骨架测地最长路径的弧长
      厚度   = 面积 / 长度（细长带状的平均宽度，天然抗弯折）
      伸长率 = 长度 / 厚度
      端点与端切向 = 最长路径两端点及其外指方向（弯曲碎片也准确）
    """
    if mask_local.sum() < 2:
        return None
    sk = skeletonize(mask_local)
    pts = np.argwhere(sk)
    if len(pts) < 2:
        # 退化小团块：用质心 + 零长度
        yx = np.argwhere(mask_local)
        cy, cx = yx.mean(axis=0)
        return dict(id=fid, area=float(area), length=0.0, thick=float(area) ** 0.5,
                    elong=0.0, cx=float(ox + cx), cy=float(oy + cy),
                    end0=(ox + cx, oy + cy), end1=(ox + cx, oy + cy),
                    tan0=None, tan1=None, need_skel=True,
                    skel=[(float(ox + cx), float(oy + cy))])
    path = longest_path(sk)
    if len(path) < 2:
        return None
    xy = path_to_xy(path) + np.array([ox, oy], dtype=np.float64)
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    L = float(seg.sum())
    if L < 1e-6:
        return None
    arc = np.concatenate([[0.0], np.cumsum(seg)])

    def tangent(which: int):
        reach = min(5.0, max(1.6, 0.35 * L))
        if which == 0:
            idx = np.where(arc <= reach)[0]
            i_in = int(min(idx.max(), len(xy) - 1))
            v = xy[0] - xy[i_in]
        else:
            idx = np.where(arc >= arc[-1] - reach)[0]
            i_in = int(max(idx.min(), 0))
            v = xy[-1] - xy[i_in]
        n = float(np.linalg.norm(v))
        return (v / n) if n > 1e-9 else None

    t0, t1 = tangent(0), tangent(1)
    if t0 is None and len(xy) >= 2:
        v = xy[0] - xy[min(2, len(xy) - 1)]
        n = float(np.linalg.norm(v))
        t0 = v / n if n > 1e-9 else None
    if t1 is None and len(xy) >= 2:
        v = xy[-1] - xy[max(len(xy) - 3, 0)]
        n = float(np.linalg.norm(v))
        t1 = v / n if n > 1e-9 else None

    thick = float(area) / L
    yx = np.argwhere(mask_local)
    cy, cx = yx.mean(axis=0)
    return dict(id=fid, area=float(area), length=L, thick=thick,
                elong=L / max(thick, 1e-6), cx=float(ox + cx), cy=float(oy + cy),
                end0=(float(xy[0][0]), float(xy[0][1])), end1=(float(xy[-1][0]), float(xy[-1][1])),
                tan0=((float(t0[0]), float(t0[1])) if t0 is not None else None),
                tan1=((float(t1[0]), float(t1[1])) if t1 is not None else None),
                need_skel=False,
                skel=[(float(p[0]), float(p[1])) for p in xy])


def analyze_fragments(ids: np.ndarray, n: int):
    """对每个连通域做骨架化几何描述（只在包围盒内计算，快）。"""
    frags = []
    H, W = ids.shape
    for k in range(1, n + 1):
        yx = np.argwhere(ids == k)
        if len(yx) == 0:
            continue
        y0, x0 = yx.min(axis=0)
        y1, x1 = yx.max(axis=0) + 1
        pad = 2
        ly0, lx0 = max(0, y0 - pad), max(0, x0 - pad)
        ly1, lx1 = min(H, y1 + pad), min(W, x1 + pad)
        local = (ids[ly0:ly1, lx0:lx1] == k)
        d = analyze_fragment(local, int(ly0), int(lx0), int(k), int(local.sum()))
        if d:
            frags.append(d)
    return frags


def _perp(u, w):
    return abs(u[0] * w[1] - u[1] * w[0])


def _unit(v):
    n = float(np.linalg.norm(v))
    return None if n < 1e-9 else v / n


def _wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


def _circle_fit(P: np.ndarray):
    """Kasa 代数圆拟合 -> (cx, cy, r)；退化时返回 None。"""
    x, y = P[:, 0], P[:, 1]
    A = np.stack([x, y, np.ones_like(x)], axis=1)
    b = x * x + y * y
    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    except Exception:
        return None
    cx, cy = sol[0] / 2.0, sol[1] / 2.0
    r2 = sol[2] + cx * cx + cy * cy
    if not np.isfinite(r2) or r2 <= 0:
        return None
    return float(cx), float(cy), float(math.sqrt(r2))


def predict_forward(f: dict, which: int, dist: float):
    """
    把碎片骨架从端点沿曲率方向"外推" dist 像素，返回 (预测点, 外指单位切向)。
    弯曲碎片用圆弧拟合外推，近似直线时退化为线性外推。
    这是修复算法的核心判据：两个碎片若真是同一条河的两截，
      各自外推跨过缝隙后应当恰好落在对方端点上。
    """
    sk = np.asarray(f.get("skel", []), dtype=np.float64)
    if len(sk) < 2:
        return None, None
    seq = sk if which == 0 else sk[::-1]           # 统一成"从本端点往碎片内部"
    seg = np.linalg.norm(np.diff(seq, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    if s[-1] < 1e-6:
        return None, None
    k = int(np.searchsorted(s, min(s[-1], PRED_REACH)))
    k = max(min(k, len(seq) - 1), 2)
    P, S = seq[:k + 1], s[:k + 1]

    # —— 模型选择：圆弧 vs 直线（按拟合残差择优），避免直线被硬套成小半径圆弧
    fit = _circle_fit(P) if (k >= 5 and S[-1] >= 4.0) else None
    line_dir = None
    Xc = P - P.mean(axis=0)
    try:
        _, _, Vt = np.linalg.svd(Xc, full_matrices=False)
        line_dir = Vt[0] / np.linalg.norm(Vt[0])
        line_res = float(np.max(np.abs(Xc @ np.array([-line_dir[1], line_dir[0]]))))
    except Exception:
        line_res = 1e9
    circle_res = 1e9
    if fit is not None:
        cx, cy, R = fit
        res = np.abs(np.linalg.norm(P - np.array([cx, cy]), axis=1) - R)
        circle_res = float(res.max())
    if (fit is not None and 6.0 < fit[2] < 400.0
            and circle_res < 0.65 and circle_res < 0.6 * line_res):
        cx, cy, R = fit
        v0 = P[0] - np.array([cx, cy])
        v1 = P[-1] - np.array([cx, cy])
        a0 = math.atan2(v0[1], v0[0])
        a1 = math.atan2(v1[1], v1[0])
        d_in = _wrap(a1 - a0)
        if abs(d_in) > 1e-4:
            a_out = a0 - math.copysign(1.0, d_in) * (dist / R)
            pred = np.array([cx + R * math.cos(a_out), cy + R * math.sin(a_out)])
            tan = np.array([math.sin(a_out), -math.cos(a_out)]) * math.copysign(1.0, d_in)
            return pred, tan
    # 直线外推
    if line_dir is None:
        return None, None
    dirv = _unit(np.array([line_dir[0], line_dir[1]]))
    if dirv is None:
        return None, None
    if float(np.dot(dirv, P[0] - P[-1])) < 0:
        dirv = -dirv
    return P[0] + dirv * dist, dirv


def link_check_end(fa: dict, ea: str, fb: dict, eb: str):
    """
    判断两个碎片端点能否桥接（几何一致性 4 要素）。
    返回 (ok, cost, reasons)
      ok      —— 是否通过全部硬约束
      cost    —— 代价（间距 + 外推残差 + 方向偏差），链搜索时作为惩罚
      reasons —— 未通过原因（可视化里直接展示，解释"为什么拒绝"）
    """
    end_a = fa[ea]
    end_b = fb[eb]
    reasons = []
    pa = np.array(end_a, dtype=np.float64)
    pb = np.array(end_b, dtype=np.float64)
    dvec = pb - pa
    d = float(np.linalg.norm(dvec))
    if d < 1e-6:
        return False, 1e6, ["端点重合"]
    if d > GAP_MAX:
        return False, d, ["间距超限 (%.1f>%.1f)" % (d, GAP_MAX)]
    u = dvec / d
    which_a = 0 if ea == "end0" else 1
    which_b = 0 if eb == "end0" else 1

    qa, ta = predict_forward(fa, which_a, d)
    qb, tb = predict_forward(fb, which_b, d)
    if qa is None or qb is None:
        return False, d, ["端点外推不可用"]
    err_a = float(np.linalg.norm(qa - pb))
    err_b = float(np.linalg.norm(qb - pa))
    mean_t = 0.5 * (fa["thick"] + fb["thick"])
    tol_ideal = ERR_TOL_K * mean_t + ERR_TOL_B          # 理想容差（用于解释）
    tol_hard = ERR_HARD_K * mean_t + ERR_HARD_B         # 硬上限
    if max(err_a, err_b) > tol_hard:
        reasons.append("外推不吻合 (%.1f/%.1f > %.1f)" % (err_a, err_b, tol_hard))
    ang_a = math.degrees(math.acos(float(np.clip(np.dot(ta, u), -1, 1))))
    ang_b = math.degrees(math.acos(float(np.clip(np.dot(tb, -u), -1, 1))))
    if ang_a > math.degrees(ANGLE_MAX) or ang_b > math.degrees(ANGLE_MAX):
        reasons.append("方向偏离 (%.0f°/%.0f° > %.0f°)" % (ang_a, ang_b, math.degrees(ANGLE_MAX)))
    ratio = max(fa["thick"], fb["thick"]) / max(1e-6, min(fa["thick"], fb["thick"]))
    if ratio > THICK_RATIO_MAX:
        reasons.append("厚度差异过大 (%.2f > %.2f)" % (ratio, THICK_RATIO_MAX))
    cost = d + 0.60 * (err_a + err_b) + 0.25 * (ang_a + ang_b)
    return (len(reasons) == 0), cost, reasons


def build_candidate_links(frags, gap_max=None):
    """生成候选连接（含被拒绝的，便于可视化解释）。"""
    gap_max = GAP_MAX if gap_max is None else gap_max
    cands = []
    for i in range(len(frags)):
        for j in range(len(frags)):
            if i == j:
                continue
            fa, fb = frags[i], frags[j]
            if fa["area"] < AREA_MIN_LINK or fb["area"] < AREA_MIN_LINK:
                continue
            for ea, end_a in (("end0", fa["end0"]), ("end1", fa["end1"])):
                for eb, end_b in (("end0", fb["end0"]), ("end1", fb["end1"])):
                    dd = float(np.linalg.norm(np.array(end_a) - np.array(end_b)))
                    if dd > gap_max or dd < 1e-6:
                        continue
                    ok, cost, reasons = link_check_end(fa, ea, fb, eb)
                    cands.append(dict(a=fa["id"], b=fb["id"], ea=ea, eb=eb,
                                      pa=[float(end_a[0]), float(end_a[1])],
                                      pb=[float(end_b[0]), float(end_b[1])],
                                      d=dd, cost=float(cost), ok=bool(ok), reasons=reasons))
    return cands


def match_links(cands):
    """
    全局最小代价互斥配对：按代价升序贪心，每个端点最多匹配一次。
    （比"固定阈值 + 指数级链搜索"稳得多：河流自身的延续永远是代价最小的候选，
      而横向偏出去的干扰形状代价天然偏高。）
    """
    ok = sorted([c for c in cands if c["ok"]], key=lambda c: c["cost"])
    used = set()
    matches = []
    for c in ok:
        ka = (c["a"], c["ea"])
        kb = (c["b"], c["eb"])
        if ka in used or kb in used:
            continue
        used.add(ka)
        used.add(kb)
        matches.append(c)
    return matches


def chains_from_matches(matches, frags):
    """由配对关系串出所有碎片链（每碎片度 ≤2，结构是一堆路径/环）。"""
    adj = {}
    for c in matches:
        adj.setdefault(c["a"], []).append(c)
        adj.setdefault(c["b"], []).append(c)

    def partner(fid, end):
        for c in adj.get(fid, []):
            if c["a"] == fid and c["ea"] == end:
                return c["b"], c["eb"], c
            if c["b"] == fid and c["eb"] == end:
                return c["a"], c["ea"], c
        return None, None, None

    by_id = {f["id"]: f for f in frags}
    chains = []
    visited = set()
    # 先走"端点"（度为 1 的碎片），再处理闭环
    starts = [fid for fid in adj if len(adj[fid]) <= 1]
    for st in starts + [fid for fid in adj if len(adj[fid]) == 2]:
        if st in visited:
            continue
        chain = [st]
        visited.add(st)
        cur = st
        entry = None
        while True:
            ends = [e for e in ("end0", "end1") if entry is None or e != entry]
            nxt, nxt_entry = None, None
            for e in ends:
                nb, nb_end, c = partner(cur, e)
                if nb is not None and nb not in visited:
                    nxt, nxt_entry = nb, nb_end
                    break
            if nxt is None:
                break
            chain.append(nxt)
            visited.add(nxt)
            entry = nxt_entry
            cur = nxt
        if len(chain) >= 2:
            chains.append(chain)
    return chains


def best_chain(frags, cands, max_frag=60):
    """返回总长度最大的碎片链（长度/厚度需满足长条判据由调用方检查）。"""
    matches = match_links(cands)
    chains = chains_from_matches(matches, frags)
    by_id = {f["id"]: f for f in frags}
    if not chains:
        return []
    best = max(chains, key=lambda ch: sum(by_id[i]["length"] for i in ch))
    return best


def chain_links_ordered(cands, chain_ids):
    """返回链上相邻碎片实际使用的连接（按链顺序）。"""
    out = []
    for a, b in zip(chain_ids[:-1], chain_ids[1:]):
        good = [c for c in cands if c["ok"] and ((c["a"] == a and c["b"] == b) or (c["a"] == b and c["b"] == a))]
        good.sort(key=lambda c: c["cost"])
        if good:
            out.append(good[0])
    return out


def chain_stats(frags, chain_ids, links):
    by_id = {f["id"]: f for f in frags}
    total_len = sum(by_id[i]["length"] for i in chain_ids)
    thick = float(np.median([by_id[i]["thick"] for i in chain_ids])) if chain_ids else 0.0
    return dict(total_len=float(total_len), thick=thick,
                strip_ratio=float(total_len / thick) if thick else 0.0,
                gap_sum=float(sum(l["d"] for l in links)),
                links=links, n_frag=len(chain_ids))


# ================================================================ 桥接
def draw_tube(shape, p0, p1, radius):
    """在两个端点之间画等半径"连接管"（胶囊），把断裂处接上。"""
    H, W = shape
    x0, y0 = p0
    x1, y1 = p1
    n = int(max(2, math.hypot(x1 - x0, y1 - y0) * 2.0)) + 1
    out = np.zeros(shape, bool)
    rr_ = int(math.ceil(radius)) + 1
    for cx, cy in zip(np.linspace(x0, x1, n), np.linspace(y0, y1, n)):
        ix, iy = int(cx), int(cy)
        x_lo, x_hi = max(0, ix - rr_), min(W, ix + rr_ + 1)
        y_lo, y_hi = max(0, iy - rr_), min(H, iy + rr_ + 1)
        if x_lo >= x_hi or y_lo >= y_hi:
            continue
        gy, gx = np.mgrid[y_lo:y_hi, x_lo:x_hi]
        out[y_lo:y_hi, x_lo:x_hi] |= ((gx + 0.5 - cx) ** 2 + (gy + 0.5 - cy) ** 2) <= radius * radius
    return out


def draw_tube_path(shape, pts, r0, r1):
    """沿给定折线画变半径"连接管"（半径从 r0 线性过渡到 r1）。"""
    H, W = shape
    out = np.zeros(shape, bool)
    pts = np.asarray(pts, dtype=np.float64)
    if len(pts) < 2:
        return out
    dense = resample_polyline(pts, 0.5)
    n = len(dense)
    for i, (cx, cy) in enumerate(dense):
        r = r0 + (r1 - r0) * (i / max(n - 1, 1))
        rr_ = int(math.ceil(r)) + 1
        ix, iy = int(cx), int(cy)
        x0, x1 = max(0, ix - rr_), min(W, ix + rr_ + 1)
        y0, y1 = max(0, iy - rr_), min(H, iy + rr_ + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        gy, gx = np.mgrid[y0:y1, x0:x1]
        out[y0:y1, x0:x1] |= ((gx + 0.5 - cx) ** 2 + (gy + 0.5 - cy) ** 2) <= r * r
    return out


def bridge_polyline(fa, ea, fb, eb, n: int = 9):
    """用两端各自的外推曲线做加权融合，得到跨缝隙的弧形桥接线（而不是直连）。"""
    which_a = 0 if ea == "end0" else 1
    which_b = 0 if eb == "end0" else 1
    pa = np.array(fa[ea], dtype=np.float64)
    pb = np.array(fb[eb], dtype=np.float64)
    d = float(np.linalg.norm(pb - pa))
    out = []
    for t in np.linspace(0.0, 1.0, n):
        qa, _ = predict_forward(fa, which_a, t * d) if t > 0 else (pa, None)
        qb, _ = predict_forward(fb, which_b, (1.0 - t) * d) if t < 1 else (pb, None)
        if qa is None:
            qa = pa
        if qb is None:
            qb = pb
        out.append((1.0 - t) * qa + t * qb)
    out[0], out[-1] = pa, pb
    return np.array(out)


# ================================================================ 两种修复方法
def repair_method_A(mask: np.ndarray, r: int):
    """方法 A：去噪 + 形态学闭运算 + 长条连通域筛选（"一视同仁地糊"，作对照基线）。"""
    ids0, n0 = label(mask)
    clean = mask.copy()
    noise_ids = []
    for k in range(1, n0 + 1):
        m = ids0 == k
        if m.sum() < AREA_MIN_FRAG:
            clean &= ~m
            noise_ids.append(k)
    closed = closing(clean, r)
    ids, n = label(closed)
    frags = analyze_fragments(ids, n)
    if not frags:
        return dict(mask=np.zeros_like(mask), r=r, comps=0, chosen=None, desc=None, ids=ids)
    cand = [f for f in frags if f["elong"] >= 3.0 and f["length"] >= 40.0]
    cand.sort(key=lambda f: -f["length"])
    if cand:
        chosen = cand[0]
        out = ids == chosen["id"]
    else:
        big = max(frags, key=lambda f: f["area"])
        chosen = big
        out = ids == big["id"]
    return dict(mask=out, r=r, comps=n, chosen=chosen, desc=frags, ids=ids,
                noise_ids=noise_ids)


def repair_method_B(mask: np.ndarray):
    """方法 B：碎片链重建（去噪 -> 几何分析 -> 互斥配对 -> 最优链 -> 等宽桥接）。"""
    ids, n = label(mask)
    all_frags = analyze_fragments(ids, n)
    noise_ids = [f["id"] for f in all_frags if f["area"] < AREA_MIN_FRAG]
    frags = [f for f in all_frags if f["area"] >= AREA_MIN_FRAG]
    cands = build_candidate_links(frags)
    matches = match_links(cands)
    chains = chains_from_matches(matches, frags)
    by_id = {f["id"]: f for f in frags}
    if chains:
        chain = max(chains, key=lambda ch: sum(by_id[i]["length"] for i in ch))
    else:
        chain = []
    links = chain_links_ordered(cands, chain)
    st = chain_stats(frags, chain, links)
    accepted = bool(chain) and len(chain) >= 2 and st["strip_ratio"] >= STRIP_RATIO

    rep = np.zeros_like(mask)
    for i in chain:
        rep |= (ids == i)
    tubes = np.zeros_like(mask)
    tube_radius = []
    bridges = []
    for c in links:
        fa, fb = by_id[c["a"]], by_id[c["b"]]
        r = float(np.clip(0.5 * (fa["thick"] + fb["thick"]) * 0.525, 1.4, 12.0))
        pa = np.array(c["pa"], dtype=np.float64)
        pb = np.array(c["pb"], dtype=np.float64)
        tubes |= draw_tube(mask.shape, pa, pb, r)
        tube_radius.append(r)
        wa = 0 if c["ea"] == "end0" else 1
        wb = 0 if c["eb"] == "end0" else 1
        arc_a = [list(map(float, q)) for q in _arc_samples(fa, wa, c["d"])]
        arc_b = [list(map(float, q)) for q in _arc_samples(fb, wb, c["d"])]
        bridges.append(dict(a=c["a"], b=c["b"], d=c["d"], cost=c["cost"], r=r,
                            pa=[float(pa[0]), float(pa[1])], pb=[float(pb[0]), float(pb[1])],
                            ea=c["ea"], eb=c["eb"], arc_a=arc_a, arc_b=arc_b))
    if accepted:
        rep = rep | tubes
    return dict(mask=rep, frags=frags, cands=cands, matches=matches, chains=chains,
                chain=chain, links=links, stats=st, accepted=accepted, ids=ids,
                tubes=tubes, tube_radius=tube_radius, bridges=bridges,
                noise_ids=noise_ids, n_all=n)


def _arc_samples(f, which, dist, n=8):
    """碎片端点沿曲率外推的采样点（仅用于可视化解释）。"""
    out = []
    for t in np.linspace(0.0, 1.0, n):
        q, _ = predict_forward(f, which, max(t * dist, 1e-6))
        out.append(q if q is not None else np.array(f["end0" if which == 0 else "end1"]))
    return out


def _bilinear(a: np.ndarray, x, y):
    """把数组 a 当作"定义在像元中心上的连续场"，在连续坐标 (x, y) 处双线性采样。

    坐标约定与全库一致：像元 i 覆盖 [i, i+1)，其中心在 i+0.5。
    """
    H, W = a.shape
    x = np.clip(np.asarray(x, dtype=np.float64) - 0.5, 0.0, W - 1.0)
    y = np.clip(np.asarray(y, dtype=np.float64) - 0.5, 0.0, H - 1.0)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.minimum(x0 + 1, W - 1)
    y1 = np.minimum(y0 + 1, H - 1)
    fx = x - x0
    fy = y - y0
    return (a[y0, x0] * (1 - fx) * (1 - fy) + a[y0, x1] * fx * (1 - fy) +
            a[y1, x0] * (1 - fx) * fy + a[y1, x1] * fx * fy)


def normal_profile(mask: np.ndarray, xy: np.ndarray, step: float = 0.25,
                   max_reach: float = 40.0, dt: np.ndarray = None, reach_k: float = 1.3,
                   snap: bool = True, mode: str = "ridge", ridge_reach_max: float = 6.0):
    """
    沿中心线每个点的法向做亚像素测量，返回 (细化后的中心点, 该处宽度)。

    两种模式：
      mode="ridge"（默认，稳）：在受限小窗口内取 EDT 最大处（= 脊线）作为新中心点，
                                宽度 = 2*dt(脊线点)。窗口半径 clip(0.5*dt+1, 1.5, 6)：
                                骨架本来就在脊线上，只需修正亚像素/漂移，窗口小 -> 不会走偏。
      mode="span"（对照）：扫描窗口内"包含中心点的连续掩膜区间"，宽度 = 区间长度，
                                中心点 = 区间中点。宽水体 + 法向偏斜时会高估（见补丁9注释）。

    共同点：中心点不在掩膜内时先沿法向**吸附**到最近的掩膜像元（细河的亚像素抖动很常见），
    而不是直接记 0 宽度。
    """
    H, W = mask.shape
    pts = np.asarray(xy, dtype=np.float64).copy()
    widths = np.zeros(len(pts))
    n_pts = len(pts)
    if dt is None:
        dt = edt(mask)
    for i in range(n_pts):
        j0 = max(0, i - 3)
        j1 = min(n_pts - 1, i + 3)
        t = pts[j1] - pts[j0]
        nt = float(np.linalg.norm(t))
        if nt < 1e-9:
            n = np.array([0.0, 1.0])
        else:
            n = np.array([-t[1], t[0]]) / nt
        cix = int(min(max(pts[i, 0], 0.0), W - 1))
        ciy = int(min(max(pts[i, 1], 0.0), H - 1))
        d_here = float(dt[ciy, cix]) if mask[ciy, cix] else 0.0
        if mode == "ridge":
            reach = min(float(max_reach), 2.0 * d_here + 2.0)
        else:
            reach = min(float(max_reach), float(reach_k) * 2.0 * d_here + 1.0)
        reach = max(reach, 1.5)
        s = np.arange(-reach, reach + 1e-9, step)
        q = pts[i] + np.outer(s, n)
        # 注意：用 floor 取"包含该连续坐标的像素"，用 round 会带来 0.5px 系统偏移
        ix = np.clip(np.floor(q[:, 0]).astype(int), 0, W - 1)
        iy = np.clip(np.floor(q[:, 1]).astype(int), 0, H - 1)
        inside = mask[iy, ix]
        k0 = int(np.argmin(np.abs(s)))
        if not inside[k0]:
            if not snap:
                widths[i] = 0.0
                continue
            cand = np.nonzero(inside)[0]
            if cand.size == 0:
                widths[i] = 0.0
                continue
            k0 = int(cand[np.argmin(np.abs(s[cand]))])
            pts[i] = q[k0]
        # 连续掩膜区间（中心点所在的这一段"里面"）
        lo, hi = k0, k0
        while lo - 1 >= 0 and inside[lo - 1]:
            lo -= 1
        while hi + 1 < len(s) and inside[hi + 1]:
            hi += 1
        span = float(s[hi] - s[lo])
        if mode == "ridge":
            # 中心：区间中点（亚像素；限幅防跑偏）
            s_mid = 0.5 * (s[hi] + s[lo])
            cap = 0.5 * d_here + 1.0
            s_mid = float(np.clip(s_mid, -cap, cap))
            pts[i] = pts[i] + s_mid * n
            # 宽度：EDT 场峰值 ×2（与法向是否偏斜无关，宽水体也不会被放大）
            ok = np.nonzero(inside)[0]
            if ok.size:
                dv = _bilinear(dt, q[ok, 0], q[ok, 1])
                jbest = int(np.argmax(dv))
                kbest = int(ok[jbest])
                d_star = float(dv[jbest])
                if 0 < kbest < len(s) - 1:            # 抛物线插值到亚像素峰值
                    d0 = float(_bilinear(dt, q[kbest - 1, 0], q[kbest - 1, 1]))
                    d2 = float(_bilinear(dt, q[kbest + 1, 0], q[kbest + 1, 1]))
                    den = (d0 - 2.0 * d_star + d2)
                    if abs(den) > 1e-9:
                        u = float(np.clip(0.5 * (d0 - d2) / den, -1.0, 1.0))
                        d_star = d_star - 0.25 * (d0 - d2) * u
                widths[i] = 2.0 * max(d_star, 0.0)
            else:
                widths[i] = max(span, 0.0)
            continue
        widths[i] = span
        pts[i] = q[k0] + n * (0.5 * (s[hi] + s[lo]) - s[k0])
        lo, hi = k0, k0
        while lo - 1 >= 0 and inside[lo - 1]:
            lo -= 1
        while hi + 1 < len(s) and inside[hi + 1]:
            hi += 1
        widths[i] = float(s[hi] - s[lo])
        pts[i] = q[k0] + n * (0.5 * (s[hi] + s[lo]) - s[k0])
    return pts, widths


def refine_centerline(mask: np.ndarray, xy: np.ndarray, iters: int = 4, dt: np.ndarray = None,
                      mode: str = "ridge"):
    """迭代法向细化：让中心线收敛到掩膜的"脊线"（每轮都用小窗口限幅，防止越跑越偏）。"""
    if dt is None:
        dt = edt(mask)
    pts = np.asarray(xy, dtype=np.float64)
    widths = np.zeros(len(pts))
    for _ in range(iters):
        pts, widths = normal_profile(mask, pts, dt=dt, mode=mode)
    return pts, widths


# ================================================================ 中心线
def centerline_from_mask(mask: np.ndarray, spur_k: float = 1.15, spur_min: float = 4.0,
                         smooth_win: int = 9, smooth_iter: int = 2, resample_step: float = 1.5,
                         refine: bool = True):
    """骨架 -> 剪毛刺 -> 测地最长路径 -> 平滑 -> 法向剖面细化；给出中心线与宽度剖面。"""
    dist = edt(mask)
    sk = skeletonize(mask)
    if not sk.any():
        return dict(points=np.zeros((0, 2)), width=np.zeros(0), skeleton=sk,
                    length=0.0, raw_len=0.0, arc=np.zeros(0))

    def min_len_fn(pt):
        return max(spur_min, spur_k * 2.0 * float(dist[pt]))

    sk2 = spur_prune(sk, min_len_fn)
    path = longest_path(sk2)
    if len(path) < 2:
        return dict(points=np.zeros((0, 2)), width=np.zeros(0), skeleton=sk2,
                    length=0.0, raw_len=0.0, arc=np.zeros(0))
    xy = path_to_xy(path)
    raw_len = float(np.sum(np.linalg.norm(np.diff(xy, axis=0), axis=1)))
    xy = resample_polyline(xy, resample_step)
    xy = smooth_polyline(xy, win=smooth_win, iters=smooth_iter)

    if refine:
        xy, width = refine_centerline(mask, xy, iters=4, dt=dist)
        xy = resample_polyline(xy, resample_step)
        xy = smooth_polyline(xy, win=smooth_win, iters=1)
        xy, width = normal_profile(mask, xy, dt=dist)
        width = np.convolve(np.pad(width, 3, mode="edge"), np.ones(7) / 7.0, mode="valid")
    else:
        H, W = mask.shape
        width = np.zeros(len(xy))
        for i, (x, y) in enumerate(xy):
            j0 = max(0, i - 3)
            j1 = min(len(xy), i + 4)
            tx = xy[j1 - 1] - xy[j0]
            nt = math.hypot(tx[0], tx[1])
            nx, ny = (-tx[1] / nt, tx[0] / nt) if nt > 1e-9 else (1.0, 0.0)
            best = 0.0
            for s in np.arange(-2.0, 2.01, 0.5):
                ix, iy = int(x + nx * s), int(y + ny * s)
                if 0 <= ix < W and 0 <= iy < H and mask[iy, ix]:
                    best = max(best, float(dist[iy, ix]))
            width[i] = 2.0 * best
        width = np.convolve(np.pad(width, 3, mode="edge"), np.ones(7) / 7.0, mode="valid")
    arc = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))])
    return dict(points=xy, width=width, skeleton=sk2, length=float(arc[-1]),
                raw_len=raw_len, arc=arc)


# ================================================================ 评估
class PolyDist:
    """点到折线的距离查询（带均匀网格加速）。"""

    def __init__(self, pts: np.ndarray, cell: float = 4.0):
        self.pts = pts
        self.cell = cell
        self.grid = {}
        for i, (x, y) in enumerate(pts):
            key = (int(x // cell), int(y // cell))
            self.grid.setdefault(key, []).append(i)

    def nearest(self, x, y):
        """返回 (到最近真值点的距离, 下标)。逐环扩张，best 跨环保留（不能重置）。"""
        c = self.cell
        kx, ky = int(x // c), int(y // c)
        best, bi = float("inf"), -1
        for ring in range(0, 40):
            for gx in range(kx - ring, kx + ring + 1):
                for gy in range(ky - ring, ky + ring + 1):
                    if ring > 0 and max(abs(gx - kx), abs(gy - ky)) != ring:
                        continue
                    for i in self.grid.get((gx, gy), ()):
                        d = math.hypot(self.pts[i, 0] - x, self.pts[i, 1] - y)
                        if d < best:
                            best, bi = d, i
            if best <= ring * c:
                return best, bi
        return best, bi


def eval_centerline(pred_xy: np.ndarray, gt_xy: np.ndarray, gt_width: np.ndarray):
    """预测中心线 vs 真值折线：偏差统计 + 覆盖率。"""
    if len(pred_xy) == 0:
        return dict(mean=float("nan"), median=float("nan"), p95=float("nan"),
                    max=float("nan"), cov2=0.0, cov3=0.0, n=0)
    gq = PolyDist(gt_xy)
    dists = []
    for x, y in pred_xy:
        d, _ = gq.nearest(x, y)
        dists.append(d)
    dists = np.array(dists)
    pq = PolyDist(pred_xy)
    gd = np.array([pq.nearest(x, y)[0] for x, y in gt_xy])
    return dict(mean=float(dists.mean()), median=float(np.median(dists)),
                p95=float(np.percentile(dists, 95)), max=float(dists.max()),
                cov2=float((dists <= 2.0).mean()), cov3=float((dists <= 3.0).mean()),
                n=int(len(dists)), gt_cover2=float((gd <= 2.0).mean()),
                gt_max=float(np.nanmax(gd)) if len(gd) else float("nan"))


def width_profile_error(pred_xy, pred_w, gt_xy, gt_w):
    """"把真值宽度按最近点取过来与预测宽度比较。"""
    if len(pred_xy) == 0:
        return dict(rmse=float("nan"), mae=float("nan"), mean_gt=float("nan"), mean_pred=float("nan"))
    gq = PolyDist(gt_xy)
    gw = []
    for x, y in pred_xy:
        _, i = gq.nearest(x, y)
        gw.append(gt_w[i] if i >= 0 else np.nan)
    gw = np.array(gw)
    ok = ~np.isnan(gw)
    if ok.sum() == 0:
        return dict(rmse=float("nan"), mae=float("nan"), mean_gt=float("nan"), mean_pred=float("nan"))
    err = pred_w[ok] - gw[ok]
    return dict(rmse=float(np.sqrt((err ** 2).mean())), mae=float(np.abs(err).mean()),
                mean_gt=float(gw[ok].mean()), mean_pred=float(pred_w[ok].mean()))


# ================================================================ 多目标扩展
def component_bboxes(ids, n=None):
    """每个连通域的像素包围盒 -> {id: (x0, y0, x1, y1)}（x1/y1 为**独占**上界）。

    为什么需要：多目标模式下要按"每条河"裁剪出小图再算中心线，
    否则在 2500x2300 全图上给几十条河反复骨架化，纯 numpy 实现会慢到不可接受。
    """
    ids = np.asarray(ids)
    if n is None:
        n = int(ids.max(initial=0))
    n = int(n)
    out = {}
    ys, xs = np.nonzero(ids)
    if len(ys) == 0 or n <= 0:
        return out
    lbl = ids[ys, xs].astype(np.int64)
    order = np.argsort(lbl, kind="stable")
    lbl, ys, xs = lbl[order], ys[order], xs[order]
    keys = np.arange(1, n + 1)
    lo = np.searchsorted(lbl, keys, "left")
    hi = np.searchsorted(lbl, keys, "right")
    for k, s, e in zip(keys, lo, hi):
        if e <= s:
            continue
        out[int(k)] = (int(xs[s:e].min()), int(ys[s:e].min()),
                       int(xs[s:e].max()) + 1, int(ys[s:e].max()) + 1)
    return out


def extract_rivers(mask, min_len=40.0, min_area=60.0, min_elong=3.0, max_rivers=500,
                   crop_pad=None, refine=True, spur_k=1.15, spur_min=4.0, smooth_win=9,
                   smooth_iter=2, resample_step=1.5):
    """多目标提取：一次给出**多条**河流的修复掩膜 + 中心线（方法 B 的多对象扩展）。

    与 repair_method_B 的区别：
      * repair_method_B 只返回"最长的那条链"（单河流场景，例如 300x200 演示图）；
      * 这里把**所有**碎片链都当候选，再把没进链的独立长条也各自当一条河，
        逐条在包围盒内修复 + 骨架化 + 细化，因此天然支持"一张图里不止一条河"。

    参数（默认值针对一般线状水系）
      min_len / min_area : 接受为一条河的最小中心线长度(px) / 最小水体面积(px)
      min_elong          : 长条判据 中心线长/平均宽度，低于它记为 kind="waterbody"（面状水体）
      crop_pad           : 每条河裁剪外扩像素；缺省 max(4, GAP_MAX+2)

    返回 dict(rivers=[...], rejected=[...], mask=修复掩膜, label=河流编号栅格, ...)
      每个 river: river_id / kind / chain / n_frag / area_px / length_px / thick_px /
                 strip_ratio / points((N,2) 像素坐标) / width / arc / bbox / bridges
    """
    mask = np.asarray(mask, dtype=bool)
    H, W = mask.shape
    ids, n_all = label(mask)
    all_frags = analyze_fragments(ids, n_all)
    noise_ids = [int(f["id"]) for f in all_frags if f["area"] < AREA_MIN_FRAG]
    frags = [f for f in all_frags if f["area"] >= AREA_MIN_FRAG]
    by_id = {f["id"]: f for f in frags}
    bbox = component_bboxes(ids, n_all)
    cands = build_candidate_links(frags)
    matches = match_links(cands)
    chains = chains_from_matches(matches, frags)

    # 1) 成链的先按"链上碎片总长"降序排；2) 没进链的独立长条各自成一组
    chains = sorted(chains, key=lambda ch: -sum(by_id[i]["length"] for i in ch if i in by_id))
    used, groups = set(), []
    for ch in chains:
        groups.append(dict(chain=[int(i) for i in ch], links=chain_links_ordered(cands, ch)))
        used.update(ch)
    for f in sorted(frags, key=lambda d: -d["length"]):
        if f["id"] in used:
            continue
        groups.append(dict(chain=[int(f["id"])], links=[]))

    pad = int(crop_pad) if crop_pad is not None else int(max(4, GAP_MAX + 2))
    rivers, rejected = [], []
    mask_repair = np.zeros_like(mask)
    label_out = np.zeros(mask.shape, np.int32)

    for g in groups:
        if len(rivers) >= int(max_rivers):
            break
        cids = [i for i in g["chain"] if i in bbox]
        links = g["links"]
        if not cids:
            continue
        x0 = min(bbox[i][0] for i in cids)
        y0 = min(bbox[i][1] for i in cids)
        x1 = max(bbox[i][2] for i in cids)
        y1 = max(bbox[i][3] for i in cids)
        cy0, cx0 = max(0, y0 - pad), max(0, x0 - pad)
        cy1, cx1 = min(H, y1 + pad), min(W, x1 + pad)
        sub_shape = (cy1 - cy0, cx1 - cx0)

        rep = np.zeros(sub_shape, bool)
        for i in cids:
            rep |= (ids[cy0:cy1, cx0:cx1] == i)
        area_px = int(rep.sum())
        bridges = []
        for c in links:
            fa, fb = by_id.get(c["a"]), by_id.get(c["b"])
            if fa is None or fb is None:
                continue
            r = float(np.clip(0.5 * (fa["thick"] + fb["thick"]) * 0.525, 1.4, 12.0))
            pa = np.array(c["pa"], dtype=np.float64) - np.array([cx0, cy0], dtype=np.float64)
            pb = np.array(c["pb"], dtype=np.float64) - np.array([cx0, cy0], dtype=np.float64)
            rep |= draw_tube(sub_shape, pa, pb, r)
            bridges.append(dict(a=int(c["a"]), b=int(c["b"]), d=float(c["d"]), cost=float(c["cost"]),
                                r=r, pa=[float(c["pa"][0]), float(c["pa"][1])],
                                pb=[float(c["pb"][0]), float(c["pb"][1])], ea=c["ea"], eb=c["eb"]))

        cl = centerline_from_mask(rep, spur_k=spur_k, spur_min=spur_min, smooth_win=smooth_win,
                                  smooth_iter=smooth_iter, resample_step=resample_step, refine=refine)
        length_px = float(cl["length"])
        thick_px = float(area_px) / max(length_px, 1e-9)
        strip = float(length_px / max(thick_px, 1e-9))
        reason = []
        if length_px < float(min_len):
            reason.append("中心线 %.1fpx < min_len %.1f" % (length_px, min_len))
        if area_px < float(min_area):
            reason.append("水体面积 %dpx < min_area %.1f" % (area_px, min_area))
        if reason:
            rejected.append(dict(chain=[int(i) for i in cids], n_frag=len(cids), area_px=area_px,
                                 length_px=round(length_px, 2), reason="；".join(reason)))
            continue

        kind = "river" if (strip >= float(min_elong) or len(cids) >= 2) else "waterbody"
        rid = len(rivers) + 1
        pts = np.asarray(cl["points"], dtype=np.float64) + np.array([cx0, cy0], dtype=np.float64)
        rivers.append(dict(
            river_id=int(rid), kind=kind, chain=[int(i) for i in cids], n_frag=len(cids),
            area_px=int(area_px), bbox=[int(cx0), int(cy0), int(sub_shape[1]), int(sub_shape[0])],
            points=pts, width=np.asarray(cl["width"], dtype=np.float64),
            arc=np.asarray(cl["arc"], dtype=np.float64), raw_len_px=float(cl["raw_len"]),
            length_px=length_px, thick_px=thick_px, strip_ratio=strip,
            chain_len_px=float(sum(by_id[i]["length"] for i in cids if i in by_id)),
            n_bridges=len(bridges), bridges=bridges, links=links,
            gap_sum_px=float(sum(float(c["d"]) for c in links)),
            min_elong=float(min_elong),
        ))
        mask_repair[cy0:cy1, cx0:cx1] |= rep
        sub_lbl = label_out[cy0:cy1, cx0:cx1]
        sub_lbl[rep] = rid

    rivers.sort(key=lambda r: -r["length_px"])
    # 排序后重编号：river_id 1 = 最长河；label 栅格同步重映射，保证两者一致
    remap = np.zeros(int(label_out.max(initial=0)) + 2, np.int32)
    for i, r in enumerate(rivers, start=1):
        remap[int(r["river_id"])] = int(i)
        r["river_id"] = int(i)
    if remap.size > 2:
        label_out = remap[label_out]
    return dict(rivers=rivers, rejected=rejected, mask=mask_repair, label=label_out,
                ids=ids, all_frags=all_frags, frags=frags, cands=cands, chains=chains,
                noise_ids=noise_ids, n_in=int(n_all), n_clean=int(n_all - len(noise_ids)),
                n_groups=len(groups))
