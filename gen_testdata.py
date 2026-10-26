#!/usr/bin/env python3
"""
강변북로 합성 테스트 데이터 생성기 (MISAN-TempoDrivingGuide)

실제 수집한 교통소통정보 1회분(링크 358개)의 링크ID·노드 연결·길이·야간 속도를 바탕으로
하루치(24시간) 교통 상황을 합성한다. 실측이 쌓이기 전에 분석·AI·Unity·화면 개발을 먼저 하기 위한 자료.

  - 출퇴근 수요 곡선 (가양 방향은 오전, 구리 방향은 오후가 혼잡)
  - 병목 지점의 반복 정체 (대기행렬이 상류로 늘었다 줄어듦)
  - 유령정체 파동: 혼잡할 때 임의 지점에서 생겨 시속 15~20km로 상류(뒤쪽)로 퍼지며 커졌다가 사라짐
  - 사고 1건 (퇴근길 구리 방향): 차로 차단 → 정체 → 해소

출력 (기본 data/testdata/YYYYMMDD/)
  links_order.csv      방향별 본선 링크 순서·길이·누적거리 (Unity 도로·히트맵 거리축)
  traffic_test.csv     교통소통정보 형식 (collect.py 세션 CSV와 같은 칸), 5분 단위
  vds_test.csv         VDS 형식 가상 검지기, 1km 간격·3차로·1분 단위
  events_test.csv      돌발상황정보 형식 (실제 공사 정보 + 합성 사고 1건)
  grid_speed_W.csv / grid_speed_E.csv   시간(1분) × 위치(100m) 속도표 (히트맵·AI 학습용)
  truth_jams.csv       정답표: 합성한 정체마다 유형(유령/병목/사고)·시각·위치·파동속도·최저속도
  heatmap.png          (matplotlib 이 있으면) 방향별 시공간 속도 히트맵

표준 라이브러리만 사용. 사용 예)
    python gen_testdata.py
    python gen_testdata.py --date 2026-10-31 --seed 7 --weekend
"""

import argparse
import csv
import glob
import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ROAD = "강변북로"

DX = 0.1          # km, 위치 격자
DT = 30           # 초, 시간 격자
STEPS = 24 * 3600 // DT

# 본선 경로 (2026-10-17 교통소통정보의 노드 연결로 복원; 진행 방향 순서)
# W: 구리 → 가양 방향(도심 방향, 오전 혼잡) / E: 가양 → 구리 방향(오후 혼잡)
# 경로 복원 시 출발점·회차점은 램프 고리를 제외하고 잘랐다. 표준노드링크로 확정 전까지는 가정값.
MAINLINE = {
    "W": ["2210046600", "2210068800", "2210069000", "2210067800", "2210066800", "1040004101",
          "1040002102", "1040001502", "1040001501", "1040001104", "1040001103", "1040000502",
          "1040018900", "1040040000", "1040019000", "1040039900", "1040017900", "1040000201",
          "1040000803", "1040000802", "1030000403", "1030000402", "1030066801", "1030066501",
          "1030000602", "1030000601", "1030005101", "1030005200", "1030025800", "1030001001",
          "1030001100", "1020000902", "1020000901", "1020000304", "1020002302", "1020002402",
          "1020000301", "1020000202", "1020000201", "1020000602", "1020000601", "1020001201",
          "1020001202", "1020001203", "1020001401", "1020001403", "1020001404", "1130000401",
          "1130000402", "1130000403", "1130000404", "1130000405", "1130034300", "1130036500",
          "1130001400", "1130001001", "1130039100", "1130039000", "1130001013", "1130001023",
          "1130001033", "1130001005", "1130002001", "1130002002", "1130002003", "1130002401",
          "1130002402", "1130003401", "1130003402", "1130004401", "1130004402", "1130004404",
          "1130059800", "1130006401"],
    "E": ["1130006301", "1130061400", "1130060800", "1130005802", "1130062900", "1130063800",
          "1130007001", "1130007002", "1130007300", "1130007400", "1130006903", "1130006902",
          "1130004304", "1130004303", "1130004301", "1130002303", "1130002302", "1130001902",
          "1130001901", "1130000902", "1130000901", "1130036400", "1130000304", "1130000303",
          "1130000302", "1130000301", "1020001305", "1020001304", "1020001302", "1020001301",
          "1020001100", "1020000701", "1020029200", "1020029000", "1020000501", "1020000101",
          "1020023700", "1020000401", "1020000403", "1020001001", "1020001002", "1020001003",
          "1020001004", "1030001200", "1030000902", "1030000700", "1030000500", "1030000304",
          "1030000303", "1030000302", "1040000701", "1040000702", "1040000101", "1040000102",
          "1040017400", "1040017700", "1040018300", "1040018500", "1040020400", "1040000601",
          "1040001201", "1040001601", "1040001602", "1040002201", "1040004201", "2210066900",
          "2210067900", "2210069200", "2210069500"],
}
DIR_NAME = {"W": "가양방향(서행)", "E": "구리방향(동행)"}

# 반복 병목: (위치 km, 처리용량 비율). 수요 비율이 용량을 넘으면 대기행렬이 생김 (가정값)
BOTTLENECKS = {"W": [(11.5, 0.97), (21.0, 1.00)], "E": [(9.0, 0.98), (20.5, 0.99)]}
# 사고: 방향, 위치 km, 시작 시각(시), 지속(분), 사고 중 용량 비율
INCIDENT = ("E", 15.0, 18.2, 25, 0.55)


# ---------------------------------------------------------------------------
def load_links():
    """가장 최근의 강변북로 교통소통정보 원본에서 링크 정보 읽기"""
    files = sorted(glob.glob(str(DATA / "raw" / "*" / "traffic_*.json")), reverse=True)
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        items = (d.get("body") or {}).get("items") or d.get("items") or []
        items = [it for it in items if it.get("roadName") == ROAD]
        if len(items) > 100:
            return {it["linkId"]: it for it in items}, f
    raise SystemExit("data/raw/*/traffic_*.json 에서 강변북로 링크를 찾지 못했습니다. collect.py 로 1회 수집하세요.")


def load_events():
    files = sorted(glob.glob(str(DATA / "raw" / "*" / "events_*.json")), reverse=True)
    files += [str(DATA / "probe" / "events.json")]
    for f in files:
        if Path(f).exists():
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            items = (d.get("body") or {}).get("items") or d.get("items") or []
            if items:
                return items
    return []


def gauss(h, mu, sd):
    return math.exp(-0.5 * ((h - mu) / sd) ** 2)


def sigmoid(z):
    return 1 / (1 + math.exp(-z))


def demand(h, d, weekend):
    """도로 용량 대비 수요 비율 (1.0 = 용량)"""
    plateau = sigmoid((h - 6.0) / 0.6) * sigmoid((22.5 - h) / 0.8)
    if weekend:
        return 0.12 + 0.62 * plateau + 0.22 * gauss(h, 14.5, 2.5)
    am, pm = (0.45, 0.25) if d == "W" else (0.20, 0.45)
    return 0.12 + 0.55 * plateau + am * gauss(h, 8.0, 1.0) + pm * gauss(h, 18.5, 1.3)


def round_half(v):
    return round(v * 2) / 2


# ---------------------------------------------------------------------------
def build_road(links, d):
    """방향별 셀 배열: 셀별 자유속도, 소속 링크"""
    rows, cum = [], 0.0
    for i, lid in enumerate(MAINLINE[d]):
        it = links[lid]
        s, tt = float(it["speed"]), float(it["travelTime"])
        length = s * tt / 3.6 / 1000  # km
        vf = 75.0 if s < 40 else min(90.0, max(60.0, s + 3))  # 야간 속도 기반 자유속도 (40 미만은 야간공사 등으로 보고 75)
        rows.append({"dir": d, "seq": i + 1, "linkId": lid, "startNodeId": it["startNodeId"],
                     "endNodeId": it["endNodeId"], "len": length, "start": cum, "end": cum + length,
                     "vf": vf})
        cum += length
    n = int(math.ceil(cum / DX))
    cell_link, vf = [], []
    j = 0
    for c in range(n):
        x = (c + 0.5) * DX
        while j < len(rows) - 1 and x > rows[j]["end"]:
            j += 1
        cell_link.append(j)
        vf.append(rows[j]["vf"])
    # 링크 경계에서 속도가 계단처럼 바뀌지 않게 이동평균
    sm = [sum(vf[max(0, c - 3):c + 4]) / len(vf[max(0, c - 3):c + 4]) for c in range(n)]
    return rows, cell_link, sm, cum


def simulate(d, vf, length, weekend, rng, truth):
    """속도장 v[t][x] (km/h) 생성"""
    n = len(vf)
    bns = [dict(x=x, cap=cap, L=0.0, active=False, start=None, minv=99) for x, cap in BOTTLENECKS[d]]
    inc = None
    if INCIDENT[0] == d and not weekend:
        _, ix, ih, imin, icap = INCIDENT
        inc = dict(x=ix, cap=icap, L=0.0, t0=ih, t1=ih + imin / 60, active=False, start=None, minv=99)
    waves, wid = [], 0
    G = 60.0  # 수요 초과 0.1 당 대기행렬 6 km/h 증가
    v = []
    for k in range(STEPS):
        h = k * DT / 3600
        D = demand(h, d, weekend) * (1 + rng.gauss(0, 0.01))
        base = [vf[c] * (1 - 0.15 * max(0.0, D - 0.55)) for c in range(n)]
        row = base[:]

        # 반복 병목·사고 대기행렬
        queues = []
        for b in bns + ([inc] if inc else []):
            cap = b["cap"]
            if b is inc:
                if not (b["t0"] <= h < b["t1"]):
                    cap = 1.05  # 사고 처리 후에는 평소보다 조금 더 잘 빠짐
            b["L"] = min(b["x"], max(0.0, b["L"] + G * (D - cap) * DT / 3600))
            if b["L"] > 0.3:
                vq = max(8.0, 28 - 60 * max(0.0, D - cap)) if D > cap else 32.0
                queues.append((b, vq))
        for b, vq in queues:
            head, tail = b["x"], b["x"] - b["L"]
            c0, c1 = max(0, int((tail - 0.3) / DX)), min(n - 1, int((head + 0.3) / DX))
            for c in range(c0, c1 + 1):
                x = (c + 0.5) * DX
                w_tail = sigmoid((x - tail) / 0.08)       # 대기행렬 꼬리: 급하게 감속
                w_head = sigmoid((head - x) / 0.15)       # 병목 지나면 서서히 회복
                w = w_tail * w_head
                row[c] = row[c] * (1 - w) + min(row[c], vq + rng.gauss(0, 2)) * w

        # 유령정체 파동 발생
        rate = 8.0 * max(0.0, D - 0.75) / 0.3  # 시간당 발생 수
        if rng.random() < rate * DT / 3600:
            wid += 1
            in_q = [b for b, _ in queues]
            note = ""
            if in_q and rng.random() < 0.5:
                b = rng.choice(in_q)
                x0 = b["x"] - rng.uniform(0, b["L"])
                note = "사고 정체 안에서 발생" if b is inc else "병목 대기행렬 안 가다서다 파동"
            else:
                x0 = rng.uniform(2.0, length - 0.5)
            life = rng.uniform(10, 40) * 60
            a0 = rng.uniform(12, 30)
            waves.append(dict(id=wid, x0=x0, t0=k * DT, life=life, a0=a0,
                              ap=a0 * rng.uniform(1.3, 1.9), w=rng.uniform(15, 20),
                              sig=rng.uniform(0.18, 0.3), minv=99.0, note=note))
        alive = []
        for w in waves:
            age = k * DT - w["t0"]
            if age > w["life"]:
                if w["minv"] < 45:
                    truth.append(dict(유형="유령정체", 방향=d, id=f"{d}-G{w['id']:03d}",
                                      시작초=w["t0"], 종료초=w["t0"] + w["life"],
                                      발생위치_km=w["x0"], 소멸위치_km=max(0, w["x0"] - w["w"] * w["life"] / 3600),
                                      파동속도_kmh=-w["w"], 최저속도=w["minv"],
                                      비고=w["note"]))
                continue
            alive.append(w)
            p = age / w["life"]
            amp = w["a0"] + (w["ap"] - w["a0"]) * (p / 0.35) if p < 0.35 else w["ap"] * (1 - (p - 0.35) / 0.65)
            xc = w["x0"] - w["w"] * age / 3600
            sig = w["sig"] * (1 + 0.8 * p)
            c0, c1 = max(0, int((xc - 4 * sig) / DX)), min(n - 1, int((xc + 4 * sig) / DX))
            for c in range(c0, c1 + 1):
                x = (c + 0.5) * DX
                drop = amp * math.exp(-0.5 * ((x - xc) / sig) ** 2)
                row[c] = max(3.0, row[c] - drop)
                if drop > amp * 0.8:
                    w["minv"] = min(w["minv"], row[c])
        waves = alive

        # 병목·사고 기록
        for b in bns + ([inc] if inc else []):
            on = b["L"] > 0.5
            if on and not b["active"]:
                b["active"], b["start"], b["maxL"] = True, k * DT, 0.0
            if on:
                b["maxL"] = max(b["maxL"], b["L"])
                c = min(n - 1, int((b["x"] - 0.2) / DX))
                b["minv"] = min(b["minv"], row[c])
            if b["active"] and not on:
                b["active"] = False
                kind = "사고" if b is inc else "병목정체"
                truth.append(dict(유형=kind, 방향=d, id=f"{d}-{'A' if b is inc else 'B'}{int(b['x']*10):03d}-{b['start']//60:04d}",
                                  시작초=b["start"], 종료초=k * DT, 발생위치_km=b["x"],
                                  소멸위치_km=b["x"] - b["maxL"], 파동속도_kmh="",
                                  최저속도=b["minv"], 비고=f"최대 대기행렬 {b['maxL']:.1f}km"))
                b["minv"] = 99

        # 측정 잡음
        v.append([max(3.0, s * (1 + rng.gauss(0, 0.015))) for s in row])
    return v


# ---------------------------------------------------------------------------
def write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def ts(day, sec, fmt="%Y%m%d%H%M%S"):
    return (day + timedelta(seconds=sec)).strftime(fmt)


def hm(day, sec):
    return ts(day, sec, "%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description="강변북로 합성 테스트 데이터 생성")
    ap.add_argument("--date", default="2026-10-28", help="데이터 날짜 (기본 2026-10-28 수요일)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--weekend", action="store_true", help="주말 수요 곡선 사용 (출퇴근 피크·사고 없음)")
    ap.add_argument("--out", default=None, help="출력 폴더 (기본 data/testdata/YYYYMMDD)")
    args = ap.parse_args()

    day = datetime.strptime(args.date, "%Y-%m-%d")
    out = Path(args.out) if args.out else DATA / "testdata" / day.strftime("%Y%m%d")
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    links, src = load_links()
    print(f"링크 원본: {src} ({len(links)}개)")

    truth, all_rows, traffic, vds, fields = [], [], [], [], {}
    for d in ("W", "E"):
        rows, cell_link, vf, length = build_road(links, d)
        all_rows += rows
        print(f"[{d}] {DIR_NAME[d]}: 링크 {len(rows)}개, {length:.1f} km, 셀 {len(vf)}개 → 시뮬레이션 중...")
        v = simulate(d, vf, length, args.weekend, rng, truth)
        fields[d] = (v, rows, cell_link, length)

        # 1분 × 100m 속도표
        grid = []
        for m in range(1440):
            a, b = v[2 * m], v[2 * m + 1]
            grid.append([hm(day, m * 60 + 60)] + [round(2 / (1 / a[c] + 1 / b[c]), 1) for c in range(len(a))])
        write_csv(out / f"grid_speed_{d}.csv",
                  ["시각"] + [f"{(c + 0.5) * DX:.2f}km" for c in range(len(vf))], grid)

        # 교통소통정보 (5분, 링크 구간평균 = 거리 / 시간)
        per = 300 // DT
        link_cells = [[c for c, lj in enumerate(cell_link) if lj == j] or
                      [min(len(vf) - 1, int(r["start"] / DX))] for j, r in enumerate(rows)]
        for p in range(288):
            stamp = ts(day, (p + 1) * 300)
            for j, r in enumerate(rows):
                cells = link_cells[j]
                inv = sum(1 / v[k][c] for k in range(p * per, (p + 1) * per) for c in cells) / (per * len(cells))
                spd = max(1.0, round_half(1 / inv))
                tt = round(r["len"] * 1000 / (spd / 3.6), 2)
                seen = hm(day, (p + 1) * 300 + 40)
                traffic.append([seen, seen, stamp, r["endNodeId"], r["linkId"], "", "", ROAD,
                                f"{spd:g}", r["startNodeId"], f"{tt:g}"])

        # 가상 VDS: 1km 간격, 3차로, 1분
        for i in range(int(length)):
            x = i + 0.5
            c = min(len(vf) - 1, int(x / DX))
            lid = rows[cell_link[c]]["linkId"]
            vid = f"TEST{d}VDS{i + 1:03d}"
            for m in range(1440):
                vm = (v[2 * m][c] + v[2 * m + 1][c]) / 2
                D = demand((m + 0.5) / 60, d, args.weekend)
                for lane, (lf, share) in enumerate([(1.05, 0.36), (1.0, 0.34), (0.93, 0.30)], start=1):
                    sp = vm * lf
                    if rng.random() < 0.01:  # 실제 VDS처럼 측정불가(-1) 1%
                        vds.append([hm(day, m * 60 + 60), hm(day, m * 60 + 60), ts(day, m * 60 + 60),
                                    lane, lid, -1, -1, vid, -1])
                        continue
                    if sp >= 0.85 * vf[c]:  # 자유류: 교통량은 수요에 비례
                        q = min(2000.0, D * 1850 * share * 3)
                        k = q / sp
                    else:                   # 혼잡류: 삼각형 기본도 (혼잡 파동속도 18km/h, 혼잡밀도 130대/km)
                        k = 18 * 130 / (sp + 18)
                        q = sp * k
                    vol = max(0, int(round(rng.gauss(q / 60, math.sqrt(max(q / 60, 0.1))))))
                    occ = round_half(min(100.0, k * 5.5 / 10))
                    vds.append([hm(day, m * 60 + 60), hm(day, m * 60 + 60), ts(day, m * 60 + 60),
                                lane, lid, f"{occ:g}", f"{round_half(sp):g}", vid, vol])

    # 링크 순서표
    write_csv(out / "links_order.csv",
              ["방향", "방향이름", "순서", "linkId", "startNodeId", "endNodeId", "길이_m", "시작_km", "끝_km", "자유속도_kmh"],
              [[r["dir"], DIR_NAME[r["dir"]], r["seq"], r["linkId"], r["startNodeId"], r["endNodeId"],
                round(r["len"] * 1000), round(r["start"], 3), round(r["end"], 3), r["vf"]] for r in all_rows])

    write_csv(out / "traffic_test.csv",
              ["처음수집", "마지막수집", "createdDate", "endNodeId", "linkId", "linkNo", "roadDrcType",
               "roadName", "speed", "startNodeId", "travelTime"], traffic)
    write_csv(out / "vds_test.csv",
              ["처음수집", "마지막수집", "colctedDate", "laneNo", "linkIds", "occupancy", "speed", "vdsId", "volume"], vds)

    # 정답표 (위치 → 링크ID)
    def link_at(d, x):
        for r in all_rows:
            if r["dir"] == d and r["start"] <= x <= r["end"]:
                return r["linkId"]
        return ""
    truth.sort(key=lambda t: (t["시작초"], t["방향"]))
    write_csv(out / "truth_jams.csv",
              ["id", "유형", "방향", "시작", "종료", "지속_분", "발생위치_km", "발생링크", "소멸위치_km",
               "파동속도_kmh", "최저속도_kmh", "비고"],
              [[t["id"], t["유형"], t["방향"], hm(day, t["시작초"]), hm(day, t["종료초"]),
                round((t["종료초"] - t["시작초"]) / 60), round(t["발생위치_km"], 2), link_at(t["방향"], t["발생위치_km"]),
                round(t["소멸위치_km"], 2), t["파동속도_kmh"] if t["파동속도_kmh"] == "" else round(t["파동속도_kmh"], 1),
                round(t["최저속도"], 1), t["비고"]] for t in truth])

    # 돌발: 실제 강변북로 공사 + 합성 사고
    ev_cols = ["처음수집", "마지막수집", "coordX", "coordY", "endDate", "eventDetailType", "eventType",
               "lanesBlockType", "lanesBlocked", "linkId", "message", "roadDrcType", "roadName", "roadNo",
               "startDate", "type"]
    ev_rows = []
    for e in load_events():
        if e.get("roadName") != ROAD:
            continue
        if not (e.get("startDate", "") <= day.strftime("%Y%m%d235959") and e.get("endDate", "9") >= day.strftime("%Y%m%d")):
            continue
        ev_rows.append([hm(day, 0), hm(day, 86399)] + [e.get(c, "") for c in ev_cols[2:]])
    if not args.weekend:
        d, ix, ih, imin, _ = INCIDENT
        s0, s1 = int(ih * 3600), int(ih * 3600 + imin * 60)
        lid = link_at(d, ix)
        ev_rows.append([hm(day, s0 + 120), hm(day, s1), "", "", ts(day, s1), "", "교통사고", "", "2개 차로",
                        lid, f"<사고>::강변북로::[합성 테스트] {DIR_NAME[d]} {ix:.1f}km 지점 추돌사고, 2개 차로 통제",
                        "", ROAD, "70", ts(day, s0), "고속도로"])
    write_csv(out / "events_test.csv", ev_cols, ev_rows)

    # 히트맵 (선택)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        for name in ("Malgun Gothic", "NanumGothic", "AppleGothic", "Noto Sans CJK KR"):
            if any(name in f.name for f in font_manager.fontManager.ttflist):
                plt.rcParams["font.family"] = name
                break
        plt.rcParams["axes.unicode_minus"] = False
        fig, axes = plt.subplots(2, 1, figsize=(14, 9), sharex=True)
        for ax, d in zip(axes, ("W", "E")):
            v, rows, _, length = fields[d]
            grid = [[(v[2 * m][c] + v[2 * m + 1][c]) / 2 for m in range(1440)] for c in range(len(v[0]))]
            im = ax.imshow(grid, aspect="auto", origin="lower", cmap="RdYlGn", vmin=0, vmax=90,
                           extent=[0, 24, 0, length])
            for t in truth:
                if t["방향"] == d and t["유형"] == "유령정체":
                    ax.plot(t["시작초"] / 3600, t["발생위치_km"], "k.", ms=3)
            ax.set_ylabel(f"{DIR_NAME[d]}\n진행 거리 (km)")
            ax.set_xticks(range(0, 25, 2))
        axes[-1].set_xlabel("시각 (시)")
        fig.colorbar(im, ax=axes, label="속도 (km/h)")
        fig.suptitle(f"강변북로 합성 테스트 데이터 {args.date} (점: 유령정체 파동 발생 지점)")
        fig.savefig(out / "heatmap.png", dpi=110, bbox_inches="tight")
        print("히트맵: heatmap.png")
    except ImportError:
        print("matplotlib 이 없어 히트맵은 건너뜀 (pip install matplotlib)")

    print(f"\n완료 → {out}")
    print(f"  traffic_test.csv {len(traffic):,}행 / vds_test.csv {len(vds):,}행 / events_test.csv {len(ev_rows)}행")
    kinds = {}
    for t in truth:
        kinds[t["유형"]] = kinds.get(t["유형"], 0) + 1
    print(f"  truth_jams.csv {len(truth)}건: " + ", ".join(f"{k} {n}" for k, n in kinds.items()))


if __name__ == "__main__":
    main()
