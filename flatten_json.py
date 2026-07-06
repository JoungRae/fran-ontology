"""
CAD JSON 평탄화(컬럼형) 변환: 키 반복을 제거해 토큰/용량을 크게 줄인다.

핵심 아이디어
- 엔티티마다 반복되던 키("Type"/"Layer"/"Start"...)를 타입별 1회 스키마로 모음
- 각 엔티티는 순수 값 배열(row)로만 저장 (스프레드시트의 행처럼)
- 무의미 필드 제거: H(핸들, 대부분 "0")
- Layer 문자열 -> Layers 배열의 정수 인덱스(L)로 치환
- 좌표는 정수 mm로 반올림(compact 단계와 동일)

출력 포맷
{
  "Version", "File", "ExportedAt",
  "Layers": [...원본 레이어 정의...],
  "Schema": { "Line": ["L","Color","x1","y1","x2","y2"], ... },
  "Entities": { "Line": [[...],[...]], "Polyline": [...], ... },
  "Dropped": { "Hatch": 39 }   # 제외한 타입과 개수(참고용)
}

원본/뷰어 호환: render.py 가 이 포맷을 자동 인식해 읽는다.

사용법:
    python flatten_json.py [입력.json] [-o 출력.json]
기본: data/ 의 (compact 아닌) 첫 JSON -> '<원본명>_flat.json'
"""

import argparse
import glob
import json
import os
import sys


# 타입별 스키마 정의. (Verts 는 가변길이라 평탄 숫자배열로 저장)
SCHEMA = {
    "Line": ["L", "Color", "x1", "y1", "x2", "y2"],
    "Polyline": ["L", "Color", "Closed", "Verts"],
    "Circle": ["L", "Color", "cx", "cy", "r"],
    "Arc": ["L", "Color", "cx", "cy", "r", "a0", "a1"],
    "DBText": ["L", "Color", "x", "y", "Height", "Rotation", "Text"],
    "BlockReference": ["L", "Color", "x", "y", "Rotation", "sx", "sy", "sz", "BlockName"],
    "RotatedDimension": ["L", "Color", "x", "y", "Measurement", "DimText"],
    "LineAngularDimension2": ["L", "Color", "x", "y", "Measurement", "DimText"],
}

# 평탄화에서 제외할 타입(형상 좌표가 없어 그릴 수 없는 타입)
# Hatch/Ellipse/Spline 은 익스포트에 좌표가 없음(키: H/Type/Layer/Color 뿐)
DROP_TYPES = {"Hatch", "Ellipse", "Spline"}


def ri(v):
    """정수 mm 반올림."""
    return int(round(v)) if isinstance(v, float) else v


def row_for(e, layer_index):
    """엔티티 dict -> 값 배열(row). 지원하지 않는 타입이면 None."""
    t = e["Type"]
    L = layer_index.get(e.get("Layer"), -1)
    c = e.get("Color")
    if t == "Line":
        s, en = e["Start"], e["End"]
        return [L, c, ri(s[0]), ri(s[1]), ri(en[0]), ri(en[1])]
    if t == "Polyline":
        verts = []
        for v in e["Verts"]:
            verts.append(ri(v[0]))
            verts.append(ri(v[1]))
        return [L, c, 1 if e.get("Closed") else 0, verts]
    if t == "Circle":
        ce = e["Center"]
        return [L, c, ri(ce[0]), ri(ce[1]), ri(e["Radius"])]
    if t == "Arc":
        ce = e["Center"]
        # 각도는 라디안 실수 -> 4자리로만 줄여 정밀도 유지하며 토큰 절감
        return [L, c, ri(ce[0]), ri(ce[1]), ri(e["Radius"]),
                round(e.get("StartAngle", 0), 4), round(e.get("EndAngle", 0), 4)]
    if t == "DBText":
        p = e["Pos"]
        return [L, c, ri(p[0]), ri(p[1]), ri(e.get("Height", 0)),
                e.get("Rotation", 0), e.get("Text", "")]
    if t == "BlockReference":
        p = e["Pos"]
        sc = e.get("Scale", [1, 1, 1])
        return [L, c, ri(p[0]), ri(p[1]), e.get("Rotation", 0),
                sc[0], sc[1], sc[2] if len(sc) > 2 else 1, e.get("BlockName", "")]
    if t in ("RotatedDimension", "LineAngularDimension2"):
        p = e["Pos"]
        return [L, c, ri(p[0]), ri(p[1]), ri(e.get("Measurement", 0)),
                e.get("DimText", "")]
    return None


def main():
    ap = argparse.ArgumentParser(description="CAD JSON 평탄화(컬럼형) 변환")
    ap.add_argument("input", nargs="?", help="입력 JSON (생략 시 data/ 의 첫 비-flat/비-compact JSON)")
    ap.add_argument("-o", "--output", help="출력 경로 (생략 시 <원본명>_flat.json)")
    ap.add_argument("--drop", default="",
                    help="추가로 제거할 엔티티 Type (쉼표 구분). 예: --drop Line")
    ap.add_argument("--min-line-len", type=float, default=0,
                    help="이 길이(mm) 미만의 Line 은 잡음으로 제거 (예: 50). 0이면 제거 안 함")
    ap.add_argument("--micro-len", type=float, default=0,
                    help="이 길이(mm) 미만의 미세조각(Line 길이 + Arc 호길이)을 모두 제거")
    args = ap.parse_args()

    drop_types = set(DROP_TYPES) | {
        t.strip() for t in args.drop.split(",") if t.strip()
    }

    input_path = args.input
    if not input_path:
        cands = [
            f for f in sorted(glob.glob(os.path.join("data", "*.json")))
            if not f.endswith("_flat.json") and not f.endswith("_compact.json")
        ]
        if not cands:
            print("data/ 에서 원본 JSON을 찾지 못했습니다.", file=sys.stderr)
            sys.exit(1)
        input_path = cands[0]

    output_path = args.output
    if not output_path:
        base, ext = os.path.splitext(input_path)
        output_path = base + "_flat" + ext

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    layers = data.get("Layers", [])
    layer_index = {ly["Name"]: i for i, ly in enumerate(layers)}
    entities = data.get("Entities", [])
    has_layers = len(layers) > 0
    has_color = any("Color" in e for e in entities)

    buckets = {t: [] for t in SCHEMA}
    dropped = {}
    skipped = {}
    import math
    for e in entities:
        t = e.get("Type")
        if t in drop_types:
            dropped[t] = dropped.get(t, 0) + 1
            continue
        # 짧은 Line 제거(잡음: 해치 빗금·미세 조각). 벽 등 긴 선은 유지
        line_th = max(args.min_line_len, args.micro_len)
        if t == "Line" and line_th > 0:
            s, en = e["Start"], e["End"]
            if math.hypot(en[0] - s[0], en[1] - s[1]) < line_th:
                key = f"Line(<{line_th:g}mm)"
                dropped[key] = dropped.get(key, 0) + 1
                continue
        # 짧은 Arc(미세 모서리 라운딩 등) 제거 — 호 길이 기준
        if t == "Arc" and args.micro_len > 0:
            a0 = e.get("StartAngle", 0.0)
            a1 = e.get("EndAngle", 0.0)
            arclen = e.get("Radius", 0) * ((a1 - a0) % (2 * math.pi))
            if arclen < args.micro_len:
                key = f"Arc(<{args.micro_len:g}mm)"
                dropped[key] = dropped.get(key, 0) + 1
                continue
        row = row_for(e, layer_index)
        if row is None:
            skipped[t] = skipped.get(t, 0) + 1
            continue
        buckets[t].append(row)

    # --- 미사용 레이어 제거 + 인덱스 재정렬 (레이어가 있을 때만) -------------
    # L 은 SCHEMA 0번 컬럼. 실제 참조된 레이어만 남기고 다시 매긴다.
    pruned_layers = []
    removed_layers = 0
    if has_layers:
        used = set()
        for rows in buckets.values():
            for r in rows:
                used.add(r[0])
        kept = [i for i in range(len(layers)) if i in used]
        remap = {old: new for new, old in enumerate(kept)}
        for rows in buckets.values():
            for r in rows:
                r[0] = remap[r[0]]
        pruned_layers = [layers[i] for i in kept]
        removed_layers = len(layers) - len(pruned_layers)

    # --- 비어있는 컬럼 제거: 레이어 없으면 L, 색 없으면 Color 컬럼 삭제 ------
    drop_cols = set()
    if not has_layers:
        drop_cols.add("L")
    if not has_color:
        drop_cols.add("Color")

    final_schema = {}
    final_entities = {}
    for t in SCHEMA:
        if not buckets[t]:
            continue
        cols = SCHEMA[t]
        keep_idx = [i for i, c in enumerate(cols) if c not in drop_cols]
        final_schema[t] = [cols[i] for i in keep_idx]
        final_entities[t] = [[r[i] for i in keep_idx] for r in buckets[t]]

    out = {
        "Version": data.get("Version"),
        "File": data.get("File"),
        "ExportedAt": data.get("ExportedAt"),
        "Format": "flat-v1",
        "Schema": final_schema,
        "Entities": final_entities,
        "Dropped": dropped,
    }
    if has_layers:
        out["Layers"] = pruned_layers
    if "Reference" in data:
        out["Reference"] = data["Reference"]  # 유저가 뽑은 기준 벽체 보존

    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)

    in_bytes = os.path.getsize(input_path)
    out_bytes = os.path.getsize(output_path)
    print(f"입력: {input_path} ({in_bytes:,} bytes)")
    print(f"출력: {output_path} ({out_bytes:,} bytes)")
    for t in SCHEMA:
        if buckets[t]:
            print(f"  {t}: {len(buckets[t])} rows")
    if has_layers:
        print(f"  레이어: {len(layers)} -> {len(pruned_layers)} (미사용 {removed_layers}개 제거)")
    else:
        print("  레이어 정보 없음 -> L 컬럼 생략")
    if not has_color:
        print("  Color 정보 없음 -> Color 컬럼 생략")
    if "Reference" in data:
        print(f"  Reference(기준 벽체) 보존: {data['Reference'].get('BlockName','?')}")
    if dropped:
        print(f"  제외(Hatch 등): {dropped}")
    if skipped:
        print(f"  미지원 타입 건너뜀: {skipped}")
    print(f"용량: {out_bytes / in_bytes * 100:.1f}% (원본 대비), 절감 {100 - out_bytes / in_bytes * 100:.1f}%")


if __name__ == "__main__":
    main()
