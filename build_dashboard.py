#!/usr/bin/env python3
"""
강변북로 교통 대시보드 생성기 (MISAN-TempoDrivingGuide)

gen_testdata.py 가 만든 테스트 데이터 폴더를 읽어, 브라우저로 여는 HTML 한 장(dashboard.html)을 만든다.
  - 지도: 시간 슬라이더·재생, 링크별 정체 상태(초록/노랑/빨강), 방향별 구간 평균속도·통행시간
  - 정체 이벤트: 유령정체 파동·병목·사고(정답표)와 공사·사고(돌발정보)를 지도와 목록에 표시
  - 시공간 히트맵: 시간 × 거리 속도, 현재 시각 선
  - VDS 차로별 그래프: 검지기를 고르면 차로별 속도·교통량·점유율

※ 링크 좌표가 아직 없어서(표준노드링크 미확보) 한강 북안 주요 지점으로 그린 경로에
  돌발정보의 실제 좌표(링크ID 포함)를 기준점으로 맞춰 링크 위치를 근사한다.

사용:  python build_dashboard.py                      (data/testdata 안 가장 최근 폴더, OpenStreetMap 배경)
       python build_dashboard.py data/testdata/20261028
       python build_dashboard.py --serve      (naver_key.txt 가 있으면 네이버 지도, 로컬 웹서버로 열기)
결과:  <데이터폴더>/dashboard.html

네이버 지도 쓰기
  1) 네이버 클라우드 플랫폼 콘솔 → Maps → Application 등록
     - API 선택: Dynamic Map
     - Web 서비스 URL: http://localhost:8000 과 http://127.0.0.1:8000 (--port 를 바꾸면 그 번호로)
  2) 발급된 인증 정보의 Client ID 를 프로젝트 폴더의 naver_key.txt 에 한 줄로 저장
     (또는 --naver-key 옵션, 환경변수 NAVER_MAP_KEY)
  3) 반드시 --serve 로 열기: 네이버 지도는 등록된 주소(http://localhost...)에서만 인증되며,
     파일을 더블클릭해 연 file:// 주소에서는 인증에 실패해 OpenStreetMap 으로 자동 전환됨
  ※ 키 파라미터 이름은 신규 콘솔 기준 ncpKeyId. 예전 방식 키라면 --naver-param ncpClientId
"""

import argparse
import base64
import csv
import functools
import http.server
import json
import math
import os
import sys
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 강변북로 근사 경로 (서→동, 경도, 위도): 자유로 접속부 ~ 가양대교 북단 ~ ... ~ 구리 토평
PATH = [(126.8530, 37.5840), (126.8640, 37.5768), (126.8740, 37.5712), (126.8812, 37.5658),
        (126.8882, 37.5621), (126.8967, 37.5556), (126.9008, 37.5524), (126.9105, 37.5439),
        (126.9200, 37.5418), (126.9300, 37.5397), (126.9423, 37.5372), (126.9530, 37.5320),
        (126.9640, 37.5222), (126.9750, 37.5175), (126.9850, 37.5166), (126.9958, 37.5203),
        (127.0020, 37.5230), (127.0061, 37.5276), (127.0115, 37.5302), (127.0190, 37.5375),
        (127.0300, 37.5420), (127.0370, 37.5435), (127.0500, 37.5420), (127.0615, 37.5385),
        (127.0700, 37.5340), (127.0790, 37.5296), (127.0881, 37.5286), (127.0990, 37.5340),
        (127.1040, 37.5410), (127.1088, 37.5455), (127.1150, 37.5530), (127.1230, 37.5630),
        (127.1320, 37.5730), (127.1420, 37.5800), (127.1500, 37.5860)]
KX = 111.32 * math.cos(math.radians(37.55))
KY = 110.95
OFFSET_KM = 0.03  # VDS 표시 위치를 방향별로 살짝 띄우는 간격
DIR_NAME = {"W": "가양방향", "E": "구리방향"}


def read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# 좌표 근사
# ---------------------------------------------------------------------------
P = [(x * KX, y * KY) for x, y in PATH]
CUM = [0.0]
for a, b in zip(P, P[1:]):
    CUM.append(CUM[-1] + math.dist(a, b))
PLEN = CUM[-1]


def project(lon, lat):
    """경로 위에서 가장 가까운 지점까지의 누적거리 s(km), 떨어진 거리(km)"""
    q = (lon * KX, lat * KY)
    best = (1e9, 0.0)
    for i, (a, b) in enumerate(zip(P, P[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        l2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / l2))
        d = math.dist((a[0] + t * dx, a[1] + t * dy), q)
        if d < best[0]:
            best = (d, CUM[i] + t * math.sqrt(l2))
    return best[1], best[0]


def point_at(s, side=0.0):
    """누적거리 s 의 (위도, 경도). side>0 이면 서→동 진행 기준 왼쪽(북쪽)으로 이동"""
    s = max(0.0, min(PLEN, s))
    i = max(0, min(len(P) - 2, next((k for k in range(len(CUM) - 1) if CUM[k + 1] >= s), len(P) - 2)))
    a, b = P[i], P[i + 1]
    seg = CUM[i + 1] - CUM[i]
    t = (s - CUM[i]) / seg if seg else 0
    x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
    nx, ny = -(b[1] - a[1]) / seg, (b[0] - a[0]) / seg
    x, y = x + nx * side, y + ny * side
    return [round(y / KY, 6), round(x / KX, 6)]


def build_mapping(links, events):
    """방향별 (진행 km → 경로 s) 기준점. 돌발정보 좌표와 링크ID 로 맞춤"""
    mid = {}
    for r in links:
        mid.setdefault(r["방향"], {})[r["linkId"]] = (float(r["시작_km"]) + float(r["끝_km"])) / 2
    total = {d: max(float(r["끝_km"]) for r in links if r["방향"] == d) for d in mid}
    anchors = {d: [] for d in mid}
    for e in events:
        if not e.get("coordX"):
            continue
        s, off = project(float(e["coordX"]), float(e["coordY"]))
        if off > 0.3:
            continue
        for d in mid:
            if e["linkId"] in mid[d]:
                anchors[d].append((mid[d][e["linkId"]], s))
    maps = {}
    for d, a in anchors.items():
        g = {}
        for km, s in a:  # 같은 링크의 기준점은 평균
            g.setdefault(round(km, 3), []).append(s)
        a = sorted((km, sum(v) / len(v)) for km, v in g.items())
        sign = -1 if d == "W" else 1  # W(가양방향)는 동→서로 진행: km 가 늘면 s 는 줄어듦
        clean = []
        for km, s in a:  # 단조성이 깨지는 기준점 제거
            if not clean or (s - clean[-1][1]) * sign > 0:
                clean.append((km, s))
        if len(clean) < 2:  # 기준점이 부족하면 길이 비율로
            clean = [(0, PLEN if sign < 0 else 0), (total[d], 0 if sign < 0 else PLEN)]
        maps[d] = (clean, sign, total[d])
        print(f"  [{d}] 기준점 {len(clean)}개: " + ", ".join(f"{km:.1f}km→{s:.1f}" for km, s in clean))
    return maps


def km_to_s(m, km):
    pts, sign, _ = m
    if km <= pts[0][0]:
        return pts[0][1] + sign * (km - pts[0][0])
    if km >= pts[-1][0]:
        return pts[-1][1] + sign * (km - pts[-1][0])
    for (k0, s0), (k1, s1) in zip(pts, pts[1:]):
        if k0 <= km <= k1:
            return s0 + (s1 - s0) * (km - k0) / (k1 - k0)


def side_of(d):
    # 우측통행: 서쪽으로 가는 차는 북쪽, 동쪽으로 가는 차는 남쪽(한강 쪽)
    return OFFSET_KM if d == "W" else -OFFSET_KM


# ---------------------------------------------------------------------------
def b64(byte_list):
    return base64.b64encode(bytes(byte_list)).decode("ascii")


def to_min(text):
    hh, mm, ss = text[-8:].split(":")
    return int(hh) * 60 + int(mm) + int(ss) / 60


def main():
    ap = argparse.ArgumentParser(description="강변북로 교통 대시보드 생성")
    ap.add_argument("folder", nargs="?", help="테스트 데이터 폴더 (기본: data/testdata 안 가장 최근)")
    key_file = HERE / "naver_key.txt"
    default_key = os.environ.get("NAVER_MAP_KEY", "") or \
        (key_file.read_text(encoding="utf-8").strip() if key_file.exists() else "")
    ap.add_argument("--naver-key", default=default_key,
                    help="네이버 지도 Client ID (없으면 OpenStreetMap 배경). 환경변수 NAVER_MAP_KEY 나 "
                         "프로젝트 폴더의 naver_key.txt 에 넣어 두면 자동으로 사용")
    ap.add_argument("--naver-param", default="ncpKeyId", choices=["ncpKeyId", "ncpClientId"],
                    help="지도 스크립트 키 파라미터 이름 (기본 ncpKeyId)")
    ap.add_argument("--serve", action="store_true", help="만든 뒤 로컬 웹서버를 띄우고 브라우저로 열기")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    if args.folder:
        src = Path(args.folder).resolve()
    else:
        cands = sorted((HERE / "data" / "testdata").glob("*/links_order.csv"))
        if not cands:
            sys.exit("data/testdata 에 테스트 데이터가 없습니다. 먼저 python gen_testdata.py")
        src = cands[-1].parent
    print(f"데이터: {src}")
    links = read_csv(src / "links_order.csv")
    events = read_csv(src / "events_test.csv")
    maps = build_mapping(links, events)

    # 링크 + 좌표
    L, idx = [], {}
    for r in links:
        d = r["방향"]
        k0, k1 = float(r["시작_km"]), float(r["끝_km"])
        n = max(2, int((k1 - k0) / 0.05) + 1)
        coords = [point_at(km_to_s(maps[d], k0 + (k1 - k0) * i / (n - 1))) for i in range(n)]  # 중심선 (방향별 간격은 화면에서)
        idx[(d, r["linkId"])] = len(L)
        L.append({"d": d, "seq": int(r["순서"]), "id": r["linkId"], "len": int(r["길이_m"]),
                  "k0": round(k0, 3), "k1": round(k1, 3), "vf": float(r["자유속도_kmh"]), "c": coords})

    # 5분 링크 속도
    sp = [[None] * 288 for _ in L]
    date = None
    for r in read_csv(src / "traffic_test.csv"):
        key = None
        for d in ("W", "E"):
            if (d, r["linkId"]) in idx:
                key = idx[(d, r["linkId"])]
        if key is None:
            continue
        cd = r["createdDate"]
        date = date or cd[:8]
        p = (int(cd[8:10]) * 60 + int(cd[10:12])) // 5 - 1
        if p < 0:
            p = 287
        sp[key][p] = float(r["speed"])
    for i, row in enumerate(L):
        row["sp"] = sp[i]

    # 방향별 100m 경로 좌표, 1분×100m 속도 격자
    geo, grid = {}, {}
    for d in ("W", "E"):
        rows = read_csv(src / f"grid_speed_{d}.csv")
        nx = len(rows[0]) - 1
        total = maps[d][2]
        geo[d] = [point_at(km_to_s(maps[d], min(total, c * 0.1))) for c in range(nx + 1)]
        vals = []
        for r in rows:
            vals += [min(254, int(round(float(v)))) for v in list(r.values())[1:]]
        grid[d] = {"nx": nx, "nt": len(rows), "b": b64(vals), "len": round(total, 3)}

    # 정답 정체
    truth = []
    for r in read_csv(src / "truth_jams.csv"):
        truth.append({"id": r["id"], "type": r["유형"], "d": r["방향"], "t0": round(to_min(r["시작"]), 2),
                      "t1": round(to_min(r["종료"]), 2), "x0": float(r["발생위치_km"]),
                      "x1": float(r["소멸위치_km"]), "w": float(r["파동속도_kmh"]) if r["파동속도_kmh"] else None,
                      "vmin": float(r["최저속도_kmh"]), "link": r["발생링크"], "note": r["비고"]})

    # 돌발
    ev = []
    for e in events:
        if e["coordX"]:
            lat, lon = float(e["coordY"]), float(e["coordX"])
        else:  # 좌표 없는 합성 사고: 링크 위치로
            key = next(((d, e["linkId"]) for d in ("W", "E") if (d, e["linkId"]) in idx), None)
            if not key:
                continue
            c = L[idx[key]]["c"]
            lat, lon = c[len(c) // 2]
        parts = e["message"].split("::")
        title = " ".join(p for p in parts[1:4] if p.strip())
        st, en = e["startDate"], e["endDate"]
        same_day = st[:8] == date
        ev.append({"type": e["eventType"], "lat": lat, "lon": lon, "link": e["linkId"], "title": title,
                   "msg": e["message"].replace("::", " · "),
                   "t0": int(st[8:10]) * 60 + int(st[10:12]) if same_day else 0,
                   "t1": int(en[8:10]) * 60 + int(en[10:12]) if en[:8] == date else 1440,
                   "lanes": e["lanesBlocked"]})

    # VDS: 검지기 목록 + 차로별 1분 값 (속도, 교통량, 점유율×2; 측정불가=255)
    det, series = {}, {}
    for r in read_csv(src / "vds_test.csv"):
        vid, lane = r["vdsId"], int(r["laneNo"])
        if vid not in det:
            d = "W" if "TESTW" in vid else "E"
            km = int(vid[-3:]) - 0.5
            det[vid] = {"id": vid, "d": d, "km": km, "link": r["linkIds"],
                        "ll": point_at(km_to_s(maps[d], km), side_of(d) * 1.5)}
            series[vid] = {ln: ([255] * 1440, [255] * 1440, [255] * 1440) for ln in (1, 2, 3)}
        m = int(r["colctedDate"][8:10]) * 60 + int(r["colctedDate"][10:12]) - 1
        if m < 0:
            m = 1439
        s, v, o = float(r["speed"]), int(r["volume"]), float(r["occupancy"])
        arr = series[vid][lane]
        if s >= 0:
            arr[0][m] = min(254, int(round(s)))
            arr[1][m] = min(254, v)
            arr[2][m] = min(254, int(round(o * 2)))
    vds = []
    for vid, info in det.items():
        info["b"] = {str(ln): [b64(a) for a in series[vid][ln]] for ln in (1, 2, 3)}
        vds.append(info)

    data = {"date": f"{date[:4]}-{date[4:6]}-{date[6:8]}", "links": L, "geo": geo, "grid": grid,
            "truth": truth, "events": ev, "vds": vds, "dirName": DIR_NAME,
            "source": str(src.name), "naverKey": bool(args.naver_key)}
    html = TEMPLATE.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    if args.naver_key:
        html = html.replace("<!--__NAVER__-->", '<script src="https://oapi.map.naver.com/openapi/v3/maps.js?'
                            f'{args.naver_param}={args.naver_key.strip()}"></script>')
    out = src / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"완료 → {out} ({out.stat().st_size / 1e6:.1f} MB), 지도: {'네이버' if args.naver_key else 'OpenStreetMap'}")
    if args.serve:
        serve(out, args.port)
    elif args.naver_key:
        print("※ 네이버 지도는 --serve 로 열어야 인증됩니다 (파일 더블클릭은 OpenStreetMap 으로 전환)")


def serve(out, port):
    """프로젝트 폴더를 http://localhost:port 로 열고 대시보드 주소를 브라우저로 띄움 (Ctrl+C 로 종료)"""
    root = HERE
    try:
        rel = out.resolve().relative_to(root).as_posix()
    except ValueError:  # 프로젝트 폴더 밖이면 그 폴더를 바로 엶
        root, rel = out.resolve().parent, out.name
    url = f"http://localhost:{port}/{rel}"
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"웹서버 실행: {url}\n  (종료: Ctrl+C)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("웹서버 종료")


TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>강변북로 교통 대시보드</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<!--__NAVER__-->
<style>
:root{
  --bg:#f4f5f7; --panel:#ffffff; --ink:#1d2330; --muted:#667085; --line:#e3e6eb; --accent:#2f5bd3;
  --free:#1f9d55; --slow:#f0b400; --jam:#d6312b; --none:#a0a6b1;
  --ghost:#7b3fe4; --bottle:#e2711d; --acc:#c81e1e; --work:#5b6472;
  --radius:10px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 "Pretendard","Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;padding:14px 20px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0;font-weight:700}
.badge{font-size:12px;padding:3px 8px;border-radius:99px;background:#eef2ff;color:var(--accent);font-weight:600}
.badge.warn{background:#fff4e5;color:#a35a00}
.controls{position:sticky;top:0;z-index:1000;display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;padding:10px 20px;background:rgba(255,255,255,.96);backdrop-filter:blur(4px);border-bottom:1px solid var(--line)}
.clock{font-variant-numeric:tabular-nums;font-size:26px;font-weight:700;min-width:84px}
button,select{font:inherit;border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 10px;cursor:pointer;color:var(--ink)}
button:hover{border-color:#c5cad3}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;min-width:74px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;border-radius:0;border-right:1px solid var(--line)}
.seg button:last-child{border-right:0}
.seg button.on{background:var(--ink);color:#fff}
#slider{flex:1 1 280px;accent-color:var(--accent)}
label.chk{display:inline-flex;align-items:center;gap:5px;color:var(--muted);cursor:pointer}
main{padding:16px 20px 40px;display:grid;gap:16px;grid-template-columns:minmax(0,1fr) 360px}
main>*{min-width:0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;min-width:0}
.panel h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.panel h2 small{font-weight:400;color:var(--muted);font-size:12px}
#map{height:560px;border-radius:8px}
.full{grid-column:1/-1}
.kpis{display:grid;gap:10px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.kpi .hd{display:flex;justify-content:space-between;align-items:center;font-weight:700}
.kpi .big{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.15}
.kpi .big span{font-size:14px;color:var(--muted);font-weight:500}
.kpi .row{display:flex;justify-content:space-between;color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
.kpi .row b{color:var(--ink);font-weight:600}
.bar{display:flex;height:8px;border-radius:4px;overflow:hidden;margin:8px 0 4px;background:#eee}
.bar i{display:block;height:100%}
.status{font-size:12px;font-weight:700;padding:2px 8px;border-radius:99px;color:#fff}
.legend{display:flex;flex-wrap:wrap;gap:6px 12px;font-size:12px;color:var(--muted);margin-top:4px}
.legend span{display:inline-flex;align-items:center;gap:5px}
.sw{width:18px;height:5px;border-radius:3px;display:inline-block}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
#active{max-height:220px;overflow:auto;margin:0;padding:0;list-style:none}
#active li{padding:6px 0;border-bottom:1px dashed var(--line);cursor:pointer;font-size:13px}
#active li:last-child{border-bottom:0}
.empty{color:var(--muted);font-size:13px}
.heat-wrap{position:relative;margin-bottom:12px}
.heat-wrap .lbl{font-size:12px;font-weight:600;margin:2px 0 4px;display:flex;justify-content:space-between;color:var(--muted)}
.heat-wrap canvas{width:100%;height:190px;display:block;border-radius:6px;cursor:crosshair}
.tip{position:fixed;pointer-events:none;background:#1d2330;color:#fff;font-size:12px;padding:5px 8px;border-radius:6px;display:none;z-index:2000;white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chips button{padding:3px 10px;border-radius:99px;font-size:12px}
.chips button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.tbl-wrap{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{position:sticky;top:0;background:#fafbfc;font-weight:600;color:var(--muted)}
tbody tr{cursor:pointer}
tbody tr:hover{background:#f6f8ff}
tbody tr.live{background:#fff7e6}
.tag{font-size:11px;font-weight:700;padding:1px 7px;border-radius:99px;color:#fff}
.vds-charts{display:grid;gap:8px}
.vds-charts canvas{height:150px!important}
.note{font-size:12px;color:var(--muted);margin-top:8px}
.two{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:16px}
.leaflet-tooltip{font:12px/1.4 inherit}
.maperr{display:none;background:#fff4e5;color:#7a4300;border:1px solid #f5d6a8;border-radius:8px;padding:8px 10px;font-size:13px;margin-bottom:8px}
@keyframes pulse{0%{transform:scale(.8);opacity:.9}70%{transform:scale(2.2);opacity:0}100%{opacity:0}}
.pulse{position:relative}
.pulse::after{content:"";position:absolute;inset:0;border-radius:50%;background:inherit;animation:pulse 1.6s infinite}
@media (max-width:1100px){main{grid-template-columns:minmax(0,1fr)}.two{grid-template-columns:minmax(0,1fr)}#map{height:440px}}
@media (max-width:600px){main{padding:12px}.controls{padding:8px 12px}header{padding:12px}#map{height:360px}.clock{font-size:22px}}
</style>
</head>
<body>
<header>
  <h1>강변북로 교통 대시보드</h1>
  <span class="badge" id="dateBadge"></span>
  <span class="badge warn">합성 테스트 데이터 · 링크 위치는 근사치</span>
</header>

<div class="controls">
  <button class="primary" id="play">▶ 재생</button>
  <button id="back" title="5분 전 (←)">−5분</button>
  <button id="fwd" title="5분 후 (→)">+5분</button>
  <div class="clock" id="clock">00:00</div>
  <input type="range" id="slider" min="0" max="1439" step="1" value="480" aria-label="시각">
  <select id="speed" title="재생 속도">
    <option value="1">1분/틱</option>
    <option value="2" selected>2분/틱</option>
    <option value="5">5분/틱</option>
    <option value="10">10분/틱</option>
  </select>
  <div class="seg" id="dirSeg">
    <button data-d="all" class="on">양방향</button>
    <button data-d="W">가양방향</button>
    <button data-d="E">구리방향</button>
  </div>
  <label class="chk"><input type="checkbox" id="showEv" checked>정체·돌발 표시</label>
  <label class="chk"><input type="checkbox" id="showVds">VDS 검지기</label>
  <label class="chk" id="trafficWrap" style="display:none" title="네이버 실시간 교통정보(지금 시각 기준, 테스트 데이터와 무관)"><input type="checkbox" id="showTraffic">네이버 실시간 교통</label>
</div>

<main>
  <section class="panel">
    <h2>시간별 정체 지도 <small id="mapSub"></small> <span class="badge" id="mapProvider" style="margin-left:auto"></span></h2>
    <div class="maperr" id="mapWarn"></div>
    <div id="map"></div>
    <div class="legend">
      <span><i class="sw" style="background:var(--free)"></i>원활 60km/h 이상</span>
      <span><i class="sw" style="background:var(--slow)"></i>서행 30~60</span>
      <span><i class="sw" style="background:var(--jam)"></i>정체 30 미만</span>
      <span><i class="dot" style="background:var(--ghost)"></i>유령정체 파동</span>
      <span><i class="dot" style="background:var(--bottle)"></i>병목 정체</span>
      <span><i class="dot" style="background:var(--acc)"></i>사고</span>
      <span><i class="dot" style="background:var(--work)"></i>공사</span>
    </div>
    <p class="note">선 두 줄 중 북쪽이 가양방향(서쪽으로 진행), 남쪽(한강 쪽)이 구리방향입니다. 링크 좌표가 없어 돌발정보 좌표를 기준점으로 위치를 맞췄습니다.</p>
  </section>

  <aside class="kpis">
    <div class="panel kpi" id="kpiW"></div>
    <div class="panel kpi" id="kpiE"></div>
    <div class="panel">
      <h2>지금 진행 중인 정체 <small id="activeCnt"></small></h2>
      <ul id="active"></ul>
    </div>
  </aside>

  <section class="panel full">
    <h2>구간 평균속도 추이 <small>총 길이 ÷ 총 통행시간, 5분 단위 · 그래프를 누르면 그 시각으로 이동</small></h2>
    <div style="height:220px"><canvas id="avgChart"></canvas></div>
  </section>

  <section class="panel full">
    <h2>시공간 속도 히트맵 <small>가로 시간 · 세로 진행 거리(아래가 출발점) · 비스듬한 줄무늬가 뒤로 퍼지는 정체 파동 · 누르면 그 시각으로 이동</small>
      <label class="chk" style="margin-left:auto"><input type="checkbox" id="showGhost" checked>유령정체 발생점</label></h2>
    <div class="heat-wrap"><div class="lbl"><span>가양방향 (구리 → 가양)</span><span id="lenW"></span></div><canvas id="heatW"></canvas></div>
    <div class="heat-wrap"><div class="lbl"><span>구리방향 (가양 → 구리)</span><span id="lenE"></span></div><canvas id="heatE"></canvas></div>
    <div class="legend" id="heatLegend"></div>
  </section>

  <section class="full two">
    <div class="panel">
      <h2>정체·돌발 목록 <small>행을 누르면 그 시각과 위치로 이동</small></h2>
      <div class="chips" id="chips" style="margin-bottom:10px"></div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>시작</th><th>종료</th><th>유형</th><th>방향</th><th>위치</th><th>최저속도</th><th>비고</th></tr></thead>
        <tbody id="evBody"></tbody>
      </table></div>
    </div>
    <div class="panel">
      <h2>VDS 차로별 그래프 <select id="vdsSel" style="margin-left:auto"></select></h2>
      <div class="vds-charts">
        <div style="height:150px"><canvas id="vSpeed"></canvas></div>
        <div style="height:150px"><canvas id="vVol"></canvas></div>
        <div style="height:150px"><canvas id="vOcc"></canvas></div>
      </div>
      <p class="note">가상 검지기(1km 간격, 3차로, 1분). 끊긴 곳은 측정불가(-1). 지도에서 VDS 검지기를 켜고 눌러도 선택됩니다.</p>
    </div>
  </section>
</main>
<div class="tip" id="tip"></div>

<script>
const DATA = /*__DATA__*/null;
const $ = s => document.querySelector(s);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C = {free:css('--free'), slow:css('--slow'), jam:css('--jam'), none:css('--none'),
  ghost:css('--ghost'), bottle:css('--bottle'), acc:css('--acc'), work:css('--work')};
const dec = b => Uint8Array.from(atob(b), c => c.charCodeAt(0));
const hhmm = m => { m = Math.max(0, Math.min(1439.99, m)); const h = Math.floor(m/60), mm = Math.floor(m%60);
  return String(h).padStart(2,'0') + ':' + String(mm).padStart(2,'0'); };
const level = v => v == null ? 'none' : v >= 60 ? 'free' : v >= 30 ? 'slow' : 'jam';
const LV = {free:'원활', slow:'서행', jam:'정체', none:'자료 없음'};
const TYPE = {'유령정체':{c:C.ghost}, '병목정체':{c:C.bottle}, '사고':{c:C.acc}, '교통사고':{c:C.acc}, '공사':{c:C.work}, '기타':{c:C.work}};

let T = 480, DIR = 'all', playing = null;
$('#dateBadge').textContent = DATA.date + ' (' + DATA.source + ')';

// ---------- 집계 ----------
const linksBy = {W: DATA.links.filter(l => l.d === 'W'), E: DATA.links.filter(l => l.d === 'E')};
function stats(d, p) {
  let len = 0, tt = 0, ft = 0, jam = 0, slow = 0, free = 0;
  for (const l of linksBy[d]) {
    const v = l.sp[p]; if (v == null) continue;
    len += l.len; tt += l.len / (v / 3.6); ft += l.len / (l.vf / 3.6);
    const lv = level(v); if (lv === 'jam') jam += l.len; else if (lv === 'slow') slow += l.len; else free += l.len;
  }
  return {avg: tt ? len / tt * 3.6 : null, tt: tt / 60, ft: ft / 60, jam: jam / 1000, slow: slow / 1000, free: free / 1000, len: len / 1000};
}
const SERIES = {W: [], E: []};
for (const d of ['W', 'E']) for (let p = 0; p < 288; p++) SERIES[d].push(stats(d, p));

// ---------- 지도 (네이버 지도 또는 Leaflet) ----------
const KX = 111320 * Math.cos(37.55 * Math.PI / 180), KY = 110950, OFF_PX = 3.6;
// 두 방향을 화면에서 나란히 보이도록 줌에 맞춰 진행방향 오른쪽으로 몇 픽셀 띄움 (우측통행)
function offsetLine(c, meters) {
  const P = c.map(([la, lo]) => [lo * KX, la * KY]);
  return P.map((p, i) => {
    const a = P[Math.max(0, i - 1)], b = P[Math.min(P.length - 1, i + 1)];
    const dx = b[0] - a[0], dy = b[1] - a[1], n = Math.hypot(dx, dy) || 1;
    return [(p[1] - dx / n * meters) / KY, (p[0] + dy / n * meters) / KX];
  });
}
const tipEl = $('#tip');
function showTip(x, y, html) { tipEl.innerHTML = html; tipEl.style.display = 'block'; tipEl.style.left = x + 12 + 'px'; tipEl.style.top = y + 12 + 'px'; }
function hideTip() { tipEl.style.display = 'none'; }

function leafletAdapter() {
  const map = L.map('map', {zoomControl: true});
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    maxZoom: 19, subdomains: 'abcd',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>'
  }).addTo(map);
  return {
    name: 'OpenStreetMap',
    line(c, tip) { const pl = L.polyline(c, {weight: 5, opacity: .95, lineCap: 'butt'}).addTo(map);
      pl.bindTooltip(() => tip(), {sticky: true});
      return {setStyle: (color, opacity) => pl.setStyle({color, opacity}), setPath: p => pl.setLatLngs(p)}; },
    marker(ll, html, size, tip, onClick) {
      const m = L.marker(ll, {icon: L.divIcon({className: '', html, iconSize: [size, size]})});
      if (tip) m.bindTooltip(typeof tip === 'function' ? () => tip() : tip);
      if (onClick) m.on('click', onClick);
      return {show: b => b ? m.addTo(map) : m.remove(), remove: () => m.remove()}; },
    fit(cs) { map.fitBounds(L.latLngBounds(cs), {padding: [20, 20]}); },
    flyTo(ll, z) { map.flyTo(ll, z); },
    zoom: () => map.getZoom(),
    onZoom(fn) { map.on('zoomend', fn); },
    traffic: null
  };
}

function naverAdapter() {
  const nm = naver.maps, LL = ([a, b]) => new nm.LatLng(a, b);
  const map = new nm.Map('map', {center: new nm.LatLng(37.55, 127.0), zoom: 12, scaleControl: true, mapDataControl: false,
    zoomControl: true, zoomControlOptions: {position: nm.Position.TOP_LEFT, style: nm.ZoomControlStyle.SMALL}});
  const hover = (obj, tip) => {
    const h = e => { const d = e.domEvent || e.pointerEvent;
      if (d && d.clientX != null) showTip(d.clientX, d.clientY, tip());
      else if (e.offset) { const r = $('#map').getBoundingClientRect(); showTip(r.left + e.offset.x, r.top + e.offset.y, tip()); } };
    nm.Event.addListener(obj, 'mouseover', h); nm.Event.addListener(obj, 'mousemove', h);
    nm.Event.addListener(obj, 'mouseout', hideTip);
  };
  let traffic = null;
  return {
    name: '네이버 지도',
    line(c, tip) { const pl = new nm.Polyline({map, path: c.map(LL), strokeWeight: 5, strokeOpacity: .95, strokeColor: '#a0a6b1', strokeLineCap: 'butt', clickable: true});
      hover(pl, tip);
      return {setStyle: (color, opacity) => pl.setOptions({strokeColor: color, strokeOpacity: opacity}), setPath: p => pl.setPath(p.map(LL))}; },
    marker(ll, html, size, tip, onClick) {
      const m = new nm.Marker({position: LL(ll), map: null, clickable: true, icon: {content: html, anchor: new nm.Point(size / 2 + 2, size / 2 + 2)}});
      if (tip) hover(m, typeof tip === 'function' ? tip : () => tip);
      if (onClick) nm.Event.addListener(m, 'click', onClick);
      return {show: b => m.setMap(b ? map : null), remove: () => { m.setMap(null); hideTip(); }}; },
    fit(cs) { const b = new nm.LatLngBounds(LL(cs[0]), LL(cs[0])); cs.forEach(c => b.extend(LL(c)));
      map.fitBounds(b, {top: 20, right: 20, bottom: 20, left: 20}); },
    flyTo(ll, z) { map.morph(LL(ll), z); },
    zoom: () => map.getZoom(),
    onZoom(fn) { nm.Event.addListener(map, 'zoom_changed', fn); },
    traffic(on) { if (!traffic) traffic = new nm.TrafficLayer({interval: 300000}); traffic.setMap(on ? map : null); }
  };
}

const dotHtml = (c, r, pulse) => `<div class="${pulse ? 'pulse' : ''}" style="width:${r}px;height:${r}px;border-radius:50%;background:${c};border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.15)"></div>`;
function linkTip(l) {
  const p = Math.floor(T / 5), v = l.sp[p], lv = level(v);
  const tt = v ? (l.len / (v / 3.6)).toFixed(0) + '초' : '-';
  return `<b>${DATA.dirName[l.d]} ${l.seq}번 링크</b> (${l.id})<br>${l.k0.toFixed(1)}~${l.k1.toFixed(1)}km · 길이 ${l.len}m<br>` +
    `속도 <b>${v ?? '-'} km/h</b> <span style="color:${C[lv]}">● ${LV[lv]}</span> · 통행 ${tt}<br>자유속도 ${l.vf} km/h · ${hhmm(p*5)}~${hhmm(p*5+5)} 자료`;
}
function geoAt(d, km) { const g = DATA.geo[d]; const i = Math.max(0, Math.min(g.length - 1, Math.round(km * 10))); return g[i]; }
function jamPos(j, t) { return j.type === '유령정체' ? j.x0 + j.w * (t - j.t0) / 60 : j.x0; }

let MAP, lines = [], evMarkers = [], vdsMarkers = [], liveMarkers = [];
function relayout() {
  const mpp = 40075016.686 * Math.cos(37.55 * Math.PI / 180) / Math.pow(2, MAP.zoom() + 8);
  DATA.links.forEach((l, i) => lines[i].setPath(offsetLine(l.c, OFF_PX * mpp)));
}
function buildMap(kind) {
  let el = $('#map');
  const fresh = el.cloneNode(false); fresh.className = ''; el.replaceWith(fresh);
  MAP = kind === 'naver' ? naverAdapter() : leafletAdapter();
  $('#mapProvider').textContent = MAP.name;
  $('#trafficWrap').style.display = MAP.traffic ? '' : 'none';
  lines = DATA.links.map(l => MAP.line(l.c, () => linkTip(l)));
  MAP.fit(DATA.links.flatMap(l => l.c));
  MAP.onZoom(relayout); relayout();
  evMarkers = DATA.events.map(e => {
    const c = (TYPE[e.type] || TYPE['기타']).c, r = e.type === '공사' ? 10 : 16;
    return {e, m: MAP.marker([e.lat, e.lon], dotHtml(c, r), r,
      `<b>${e.type}</b> ${e.title}<br>${e.lanes ? e.lanes + ' 통제 · ' : ''}${e.t0 ? hhmm(e.t0) : '종일'}${e.t1 < 1440 ? '~' + hhmm(e.t1) : ''}<br><span style="opacity:.75">${e.msg.slice(0, 80)}</span>`)};
  });
  vdsMarkers = DATA.vds.map(v => MAP.marker(v.ll,
    '<div style="width:9px;height:9px;background:#2f5bd3;border:2px solid #fff;box-shadow:0 0 0 1px #2f5bd3"></div>', 9,
    `VDS ${v.id}<br>${DATA.dirName[v.d]} ${v.km}km · 링크 ${v.link}`, () => { $('#vdsSel').value = v.id; drawVds(); }));
  vdsMarkers.forEach(m => m.show($('#showVds').checked));
}
$('#showVds').onchange = e => vdsMarkers.forEach(m => m.show(e.target.checked));
$('#showTraffic').onchange = e => MAP.traffic && MAP.traffic(e.target.checked);
const USE_NAVER = !!(DATA.naverKey && window.naver && window.naver.maps);
let fellBack = false;
window.navermap_authFailure = () => {
  if (fellBack) return; fellBack = true;
  $('#mapWarn').style.display = 'block';
  $('#mapWarn').innerHTML = location.protocol === 'file:'
    ? '파일을 더블클릭해 열면(file://) 네이버 지도 인증이 되지 않아 OpenStreetMap으로 보여 줍니다. 폴더에서 <b>python build_dashboard.py --serve</b> 로 실행하세요.'
    : '네이버 지도 인증에 실패해 OpenStreetMap으로 바꿨습니다. 네이버 클라우드 콘솔의 Maps Application에서 <b>Dynamic Map</b>이 선택돼 있는지, Web 서비스 URL에 <b>' +
      location.origin + '</b> 이 등록돼 있는지, 인증 정보(Client ID)가 맞는지 확인하세요.';
  buildMap('leaflet'); if (typeof update === 'function' && window.__ready) update();
};
buildMap(USE_NAVER ? 'naver' : 'leaflet');
if (DATA.naverKey && !USE_NAVER) setTimeout(window.navermap_authFailure, 0);
$('#showEv').onchange = () => update();

// ---------- KPI ----------
function kpi(d) {
  const s = SERIES[d][Math.floor(T / 5)];
  const lv = level(s.avg);
  const delay = s.tt - s.ft;
  const tot = s.free + s.slow + s.jam || 1;
  $('#kpi' + d).innerHTML = `
    <div class="hd">${DATA.dirName[d]} <span class="status" style="background:${C[lv]}">${LV[lv]}</span></div>
    <div class="big">${s.avg ? s.avg.toFixed(1) : '-'} <span>km/h 구간평균</span></div>
    <div class="bar"><i style="width:${s.free / tot * 100}%;background:${C.free}"></i><i style="width:${s.slow / tot * 100}%;background:${C.slow}"></i><i style="width:${s.jam / tot * 100}%;background:${C.jam}"></i></div>
    <div class="row"><span>정체 <b>${s.jam.toFixed(1)}km</b> · 서행 <b>${s.slow.toFixed(1)}km</b></span><span>전체 ${s.len.toFixed(1)}km</span></div>
    <div class="row"><span>통행시간 <b>${s.tt.toFixed(1)}분</b></span><span>평소 대비 ${delay >= 0.05 ? '+' + delay.toFixed(1) + '분' : '지연 없음'}</span></div>`;
}

// ---------- 갱신 ----------
function activeJams(t) { return DATA.truth.filter(j => t >= j.t0 && t <= j.t1 && (DIR === 'all' || j.d === DIR)); }
function update() {
  const p = Math.floor(T / 5);
  $('#clock').textContent = hhmm(T);
  $('#slider').value = T;
  $('#mapSub').textContent = `${hhmm(p * 5)}~${hhmm(p * 5 + 5)} 교통소통정보`;
  DATA.links.forEach((l, i) => {
    const show = DIR === 'all' || l.d === DIR;
    lines[i].setStyle(C[level(l.sp[p])], show ? .95 : .08);
  });
  kpi('W'); kpi('E');
  // 돌발
  liveMarkers.forEach(m => m.remove()); liveMarkers = [];
  const showEv = $('#showEv').checked;
  evMarkers.forEach(({e, m}) => m.show(showEv && T >= e.t0 && T <= e.t1));
  // 진행 중 정체
  const act = activeJams(T);
  $('#activeCnt').textContent = act.length + '건';
  $('#active').innerHTML = act.length ? act.map(j => {
    const km = jamPos(j, T);
    return `<li data-id="${j.id}"><span class="tag" style="background:${TYPE[j.type].c}">${j.type}</span> ${DATA.dirName[j.d]} ${km.toFixed(1)}km
      <span style="color:#667085">· ${hhmm(j.t0)}부터 · 최저 ${j.vmin.toFixed(0)}km/h${j.w ? ' · 뒤로 ' + Math.abs(j.w).toFixed(0) + 'km/h' : ''}</span></li>`;
  }).join('') : '<li class="empty">진행 중인 정체 이벤트가 없습니다.</li>';
  if (showEv) act.forEach(j => {
    const ll = geoAt(j.d, Math.max(0, jamPos(j, T)));
    const c = TYPE[j.type].c, r = j.type === '유령정체' ? 12 : 16;
    const m = MAP.marker(ll, dotHtml(c, r, true), r,
      `<b>${j.type}</b> ${j.id}<br>${DATA.dirName[j.d]} ${jamPos(j, T).toFixed(1)}km · ${hhmm(j.t0)}~${hhmm(j.t1)}<br>최저 ${j.vmin.toFixed(1)} km/h${j.note ? '<br>' + j.note : ''}`);
    m.show(true); liveMarkers.push(m);
  });
  document.querySelectorAll('#evBody tr').forEach(tr => tr.classList.toggle('live', +tr.dataset.t0 <= T && T <= +tr.dataset.t1));
  avgChart.update('none');
  drawHeatOverlay();
  vCharts.forEach(c => c.update('none'));
}
function setT(t) { T = Math.max(0, Math.min(1439, Math.round(t))); update(); }
$('#active').onclick = e => { const li = e.target.closest('li[data-id]'); if (!li) return;
  const j = DATA.truth.find(x => x.id === li.dataset.id); MAP.flyTo(geoAt(j.d, Math.max(0, jamPos(j, T))), 14); };

// ---------- 컨트롤 ----------
$('#slider').oninput = e => setT(+e.target.value);
$('#back').onclick = () => setT(T - 5);
$('#fwd').onclick = () => setT(T + 5);
$('#play').onclick = togglePlay;
function togglePlay() {
  if (playing) { clearInterval(playing); playing = null; $('#play').textContent = '▶ 재생'; return; }
  if (T >= 1439) T = 0;
  $('#play').textContent = '❚❚ 정지';
  playing = setInterval(() => { if (T >= 1439) { togglePlay(); return; } setT(T + +$('#speed').value); }, 140);
}
document.querySelectorAll('#dirSeg button').forEach(b => b.onclick = () => {
  document.querySelectorAll('#dirSeg button').forEach(x => x.classList.toggle('on', x === b));
  DIR = b.dataset.d; update();
});
document.addEventListener('keydown', e => {
  if (['INPUT', 'SELECT'].includes(e.target.tagName) && e.target.type !== 'range') return;
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  if (e.key === 'ArrowRight') { e.preventDefault(); setT(T + (e.shiftKey ? 30 : 5)); }
  if (e.key === 'ArrowLeft') { e.preventDefault(); setT(T - (e.shiftKey ? 30 : 5)); }
});

// ---------- 차트 공통 ----------
Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.font.size = 11;
Chart.defaults.color = '#667085';
const nowLine = {id: 'nowLine', afterDatasetsDraw(c) {
  const x = c.scales.x.getPixelForValue(T), {top, bottom} = c.chartArea, ctx = c.ctx;
  ctx.save(); ctx.strokeStyle = '#1d2330'; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
  ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
}};
const timeAxis = {type: 'linear', min: 0, max: 1440, ticks: {stepSize: 120, callback: v => hhmm(v).slice(0, 2) + '시'}, grid: {color: '#eef0f3'}};
const clickTime = (evt, el, chart) => setT(chart.scales.x.getValueForPixel(evt.x));

const avgChart = new Chart($('#avgChart'), {
  type: 'line', plugins: [nowLine],
  data: {datasets: ['W', 'E'].map((d, k) => ({label: DATA.dirName[d],
    data: SERIES[d].map((s, p) => ({x: p * 5 + 2.5, y: s.avg && +s.avg.toFixed(1)})),
    borderColor: k ? '#2f5bd3' : '#e2711d', backgroundColor: k ? '#2f5bd3' : '#e2711d', borderWidth: 2, pointRadius: 0, tension: .2}))},
  options: {maintainAspectRatio: false, animation: false, onClick: clickTime, interaction: {mode: 'index', intersect: false},
    scales: {x: timeAxis, y: {min: 0, max: 90, title: {display: true, text: 'km/h'}, grid: {color: '#eef0f3'}}},
    plugins: {legend: {position: 'top', align: 'end'}, tooltip: {callbacks: {title: it => hhmm(it[0].parsed.x - 2.5) + ' 기준'}}}}
});

// ---------- 히트맵 ----------
const STOPS = [[0, [122, 0, 0]], [15, [200, 30, 30]], [30, [236, 98, 52]], [45, [250, 190, 70]], [60, [206, 225, 120]], [75, [96, 184, 96]], [90, [20, 130, 70]]];
function ramp(v) {
  if (v >= 254) return [200, 200, 200];
  for (let i = 1; i < STOPS.length; i++) if (v <= STOPS[i][0]) {
    const [a, ca] = STOPS[i - 1], [b, cb] = STOPS[i], t = (v - a) / (b - a);
    return ca.map((x, k) => Math.round(x + (cb[k] - x) * t));
  }
  return STOPS[STOPS.length - 1][1];
}
$('#heatLegend').innerHTML = STOPS.map(([v, c]) => `<span><i class="sw" style="width:22px;height:10px;background:rgb(${c})"></i>${v}</span>`).join('') + '<span>km/h</span>';
const HEAT = {};
for (const d of ['W', 'E']) {
  const g = DATA.grid[d], a = dec(g.b), off = document.createElement('canvas');
  off.width = g.nt; off.height = g.nx;
  const img = off.getContext('2d').createImageData(g.nt, g.nx);
  for (let t = 0; t < g.nt; t++) for (let x = 0; x < g.nx; x++) {
    const [r, gg, b] = ramp(a[t * g.nx + x]), o = ((g.nx - 1 - x) * g.nt + t) * 4;
    img.data[o] = r; img.data[o + 1] = gg; img.data[o + 2] = b; img.data[o + 3] = 255;
  }
  off.getContext('2d').putImageData(img, 0, 0);
  HEAT[d] = {g, a, off, cv: $('#heat' + d)};
  $('#len' + d).textContent = `0 ~ ${g.len.toFixed(1)} km`;
}
const PAD = {l: 34, b: 18, t: 4, r: 6};
function drawHeatOverlay() {
  for (const d of ['W', 'E']) {
    const {g, off, cv} = HEAT[d], dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth, H = cv.clientHeight;
    if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
    const ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const pw = W - PAD.l - PAD.r, ph = H - PAD.t - PAD.b;
    ctx.imageSmoothingEnabled = false;
    ctx.globalAlpha = DIR === 'all' || DIR === d ? 1 : .35;
    ctx.drawImage(off, PAD.l, PAD.t, pw, ph); ctx.globalAlpha = 1;
    ctx.fillStyle = '#667085'; ctx.font = '11px sans-serif';
    for (let h = 0; h <= 24; h += 2) { const x = PAD.l + pw * h / 24; ctx.fillText(h + '시', x - 8, H - 4); }
    for (let k = 0; k <= g.len; k += 10) { const y = PAD.t + ph * (1 - k / g.len); ctx.fillText(k + 'km', 0, y + 4); }
    if ($('#showGhost').checked) {
      ctx.fillStyle = '#111';
      DATA.truth.filter(j => j.d === d && j.type === '유령정체').forEach(j => {
        ctx.beginPath(); ctx.arc(PAD.l + pw * j.t0 / 1440, PAD.t + ph * (1 - j.x0 / g.len), 2.4, 0, 7); ctx.fill(); });
      DATA.truth.filter(j => j.d === d && j.type !== '유령정체').forEach(j => {
        ctx.strokeStyle = TYPE[j.type].c; ctx.lineWidth = 2;
        ctx.strokeRect(PAD.l + pw * j.t0 / 1440, PAD.t + ph * (1 - j.x0 / g.len), pw * (j.t1 - j.t0) / 1440, ph * (j.x0 - j.x1) / g.len); });
    }
    const x = PAD.l + pw * T / 1440;
    ctx.strokeStyle = '#1d2330'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x, PAD.t); ctx.lineTo(x, PAD.t + ph); ctx.stroke();
  }
}
$('#showGhost').onchange = drawHeatOverlay;
window.addEventListener('resize', drawHeatOverlay);
const tip = tipEl;
for (const d of ['W', 'E']) {
  const {g, a, cv} = HEAT[d];
  const pos = e => { const r = cv.getBoundingClientRect(), pw = r.width - PAD.l - PAD.r, ph = r.height - PAD.t - PAD.b;
    return {t: (e.clientX - r.left - PAD.l) / pw * 1440, km: (1 - (e.clientY - r.top - PAD.t) / ph) * g.len}; };
  cv.addEventListener('mousemove', e => {
    const {t, km} = pos(e); if (t < 0 || t >= 1440 || km < 0 || km > g.len) { tip.style.display = 'none'; return; }
    const v = a[Math.floor(t) * g.nx + Math.min(g.nx - 1, Math.floor(km * 10))];
    tip.style.display = 'block'; tip.style.left = e.clientX + 12 + 'px'; tip.style.top = e.clientY + 12 + 'px';
    tip.textContent = `${hhmm(t)} · ${km.toFixed(1)}km · ${v >= 254 ? '-' : v + ' km/h'}`;
  });
  cv.addEventListener('mouseleave', () => tip.style.display = 'none');
  cv.addEventListener('click', e => { const {t, km} = pos(e); setT(t);
    if (km >= 0 && km <= g.len) MAP.flyTo(geoAt(d, km), 14); });
}

// ---------- 목록 ----------
const ROWS = [
  ...DATA.truth.map(j => ({t0: j.t0, t1: j.t1, type: j.type, d: j.d, where: `${j.x0.toFixed(1)}km (${j.link})`,
    vmin: j.vmin.toFixed(0), note: j.note || (j.w ? `뒤로 ${Math.abs(j.w).toFixed(0)}km/h 전파` : ''), ll: () => geoAt(j.d, j.x0)})),
  ...DATA.events.map(e => ({t0: e.t0, t1: e.t1, type: e.type, d: '', where: `${e.title} (${e.link})`, vmin: '',
    note: e.lanes ? e.lanes + ' 통제' : (e.t0 === 0 ? '종일 공사' : ''), ll: () => [e.lat, e.lon]}))
].sort((a, b) => ((a.type === '공사' && a.t0 === 0) - (b.type === '공사' && b.t0 === 0)) || a.t0 - b.t0);
let FILTER = '전체';
const KINDS = ['전체', '유령정체', '병목정체', '사고', '공사'];
$('#chips').innerHTML = KINDS.map(k => `<button data-k="${k}" class="${k === FILTER ? 'on' : ''}">${k} ${k === '전체' ? ROWS.length : ROWS.filter(r => r.type.includes(k === '사고' ? '사고' : k)).length}</button>`).join('');
$('#chips').onclick = e => { const b = e.target.closest('button'); if (!b) return; FILTER = b.dataset.k;
  document.querySelectorAll('#chips button').forEach(x => x.classList.toggle('on', x === b)); renderRows(); };
function renderRows() {
  const rows = ROWS.filter(r => FILTER === '전체' || r.type.includes(FILTER === '사고' ? '사고' : FILTER));
  $('#evBody').innerHTML = rows.map((r, i) => `<tr data-i="${ROWS.indexOf(r)}" data-t0="${r.t0}" data-t1="${r.t1}">
    <td>${r.t0 === 0 && r.type === '공사' ? '종일' : hhmm(r.t0)}</td><td>${r.t1 >= 1440 ? '-' : hhmm(r.t1)}</td>
    <td><span class="tag" style="background:${(TYPE[r.type] || TYPE['기타']).c}">${r.type}</span></td>
    <td>${r.d ? DATA.dirName[r.d] : ''}</td><td>${r.where}</td><td>${r.vmin && r.vmin + ' km/h'}</td><td>${r.note}</td></tr>`).join('');
  document.querySelectorAll('#evBody tr').forEach(tr => tr.classList.toggle('live', +tr.dataset.t0 <= T && T <= +tr.dataset.t1));
}
$('#evBody').onclick = e => { const tr = e.target.closest('tr'); if (!tr) return; const r = ROWS[+tr.dataset.i];
  if (!(r.type === '공사' && r.t0 === 0)) setT(r.t0 + 0.5); MAP.flyTo(r.ll(), 14);
  window.scrollTo({top: 0, behavior: 'smooth'}); };
renderRows();

// ---------- VDS ----------
$('#vdsSel').innerHTML = ['W', 'E'].map(d => `<optgroup label="${DATA.dirName[d]}">` +
  DATA.vds.filter(v => v.d === d).map(v => `<option value="${v.id}">${v.id} · ${v.km}km</option>`).join('') + '</optgroup>').join('');
const LANE_C = ['#2f5bd3', '#1f9d55', '#e2711d'];
const vMeta = [['vSpeed', '속도 (km/h)', 0, 1], ['vVol', '교통량 (대/분)', 1, 1], ['vOcc', '점유율 (%)', 2, .5]];
const vCharts = vMeta.map(([id, title]) => new Chart($('#' + id), {
  type: 'line', plugins: [nowLine], data: {datasets: [1, 2, 3].map(n => ({label: n + '차로', data: [], borderColor: LANE_C[n - 1],
    borderWidth: 1.2, pointRadius: 0, spanGaps: false}))},
  options: {maintainAspectRatio: false, animation: false, onClick: clickTime, parsing: false, normalized: true,
    interaction: {mode: 'index', intersect: false},
    scales: {x: timeAxis, y: {beginAtZero: true, grid: {color: '#eef0f3'}}},
    plugins: {title: {display: true, text: title, align: 'start', font: {weight: '600'}},
      legend: {position: 'top', align: 'end', labels: {boxWidth: 12}},
      tooltip: {callbacks: {title: it => hhmm(it[0].parsed.x)}}}}
}));
function drawVds() {
  const v = DATA.vds.find(x => x.id === $('#vdsSel').value);
  vMeta.forEach(([, , k, sc], ci) => {
    vCharts[ci].data.datasets.forEach((ds, li) => {
      const arr = dec(v.b[String(li + 1)][k]);
      ds.data = Array.from(arr, (y, m) => ({x: m + 1, y: y === 255 ? null : y * sc}));
    });
    vCharts[ci].update('none');
  });
}
$('#vdsSel').onchange = drawVds;
$('#vdsSel').value = (DATA.vds.find(v => v.d === 'W' && v.km > 10) || DATA.vds[0]).id;
drawVds();
window.__ready = true;
update();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
