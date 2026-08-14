# -*- coding: utf-8 -*-
"""실 라벨 자리 잡기 — 방 안에서 경계로부터 가장 먼 점(내접 최대점 근사).

rect·bbox·면적중심은 오목한 실에서 틀린다: 키즈짐이 라커(남)을 ㄷ자로
감싸는 도면에서 셋 다 구멍 근처에 떨어져 옆 실 라벨과 겹쳐 보였다.
격자 표본으로 폴리곤 내부 점 중 경계 최소거리가 최대인 곳을 고르면
어느 모양이든 방의 가장 트인 자리에 라벨이 앉는다.

plan_law_report · evac_report · fire_layout 이 공용으로 쓴다.
"""
import math


def _inside(px, py, pts):
    hit = False
    for i in range(len(pts)):
        x0, y0 = pts[i][0], pts[i][1]
        x1, y1 = pts[(i + 1) % len(pts)][0], pts[(i + 1) % len(pts)][1]
        if (y0 > py) != (y1 > py) and \
                px < (x1 - x0) * (py - y0) / (y1 - y0) + x0:
            hit = not hit
    return hit


def _edge_d(px, py, pts):
    best = float("inf")
    for i in range(len(pts)):
        x0, y0 = pts[i][0], pts[i][1]
        x1, y1 = pts[(i + 1) % len(pts)][0], pts[(i + 1) % len(pts)][1]
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = 0 if L2 == 0 else max(0.0, min(1.0, ((px - x0) * dx
                                                 + (py - y0) * dy) / L2))
        best = min(best, math.hypot(px - (x0 + t * dx), py - (y0 + t * dy)))
    return best


def label_spot(room: dict, n: int = 24):
    """room = {'rect': [x0,y0,x1,y1], 'poly': [[x,y],...]|None} → (x, y)."""
    pts = room.get("poly")
    if pts and len(pts) >= 3:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
        best, bp = -1.0, None
        for i in range(1, n):
            for j in range(1, n):
                px = bx0 + (bx1 - bx0) * i / n
                py = by0 + (by1 - by0) * j / n
                if not _inside(px, py, pts):
                    continue
                d = _edge_d(px, py, pts)
                if d > best:
                    best, bp = d, (px, py)
        if bp:
            return bp
    x0, y0, x1, y1 = room["rect"]
    return ((x0 + x1) / 2, (y0 + y1) / 2)
