#!/usr/bin/env python3
"""
강변북로 ITS 교통 데이터 통합 수집기 (MISAN-TempoDrivingGuide)

수집 대상 (국가교통정보센터 ITS OpenAPI)
    traffic : 교통소통정보   - 링크별 속도·통행시간
    events  : 돌발상황정보   - 사고·공사 ('사고 없는 정체' 판별)
    vds     : VDS 정보       - 차량검지기 속도·교통량·점유율
    caution : 주의운전구간   - 사고 위험·주의 구간
    vsl     : VSL 정보       - 가변 제한속도

※ 엔드포인트 이름·파라미터는 아래 SOURCES 에서 수정합니다.
  처음에는 `python collect.py probe` 로 API마다 1번씩 호출해 응답 구조를 확인하세요.

외부 패키지 없이 Python 3.8+ 표준 라이브러리만 사용합니다.

사용 예)
    python collect.py probe                       # API별 엔드포인트·필드 분석 → data/probe/probe_report.txt
    python collect.py probe --try --sources caution   # 엔드포인트 후보를 차례로 시도
    python collect.py probe --offline             # 저장된 probe 파일만 다시 분석 (키 불필요)
    python collect.py probe --endpoint caution=실제이름 --sources caution
    python collect.py collect --once              # 1회 수집
    python collect.py collect                     # 연속 수집 (q 키로 종료 → 세션 CSV 저장)
    python collect.py collect --sources traffic,vds
    python collect.py export                      # 전체 누적 데이터 CSV
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# ===========================================================================
# 설정
# ===========================================================================
ITS_BASE = "https://openapi.its.go.kr:9443"

# 강변북로 전 구간(구리 토평 ~ 고양 가양대교 북단)을 덮는 좌표 범위 (WGS84)
BBOX = {"minX": 126.80, "maxX": 127.16, "minY": 37.51, "maxY": 37.62}

DEFAULT_ROAD = "강변북로"
DEFAULT_INTERVAL = 60  # 초

# endpoint : ITS_BASE 뒤에 붙는 경로. 승인 메일/마이페이지의 '요청 URL'과 다르면 여기만 고치세요.
#            (명령줄 --endpoint caution=실제이름 으로 임시 변경도 가능)
# candidates : 엔드포인트를 모를 때 `probe --try` 가 차례로 시도해 볼 후보 이름
# params   : 공통 파라미터(apiKey, getType=json) 외에 추가로 보낼 값
# bbox     : True면 좌표 범위(minX, maxX, minY, maxY) 파라미터를 함께 보냄
# road_filter : 'exact'  → roadName 이 강변북로와 정확히 일치하는 항목만
#               'links'  → 강변북로 링크ID(교통소통정보에서 확보)와 겹치는 항목만
#                          (VDS·VSL처럼 도로명이 없는 API용. link_field 에 링크 필드명)
#               'auto'   → roadName 일치 → 링크ID 일치 → 좌표 범위 순으로 첫 번째로 걸리는 방식 사용
#               'none'   → 범위 안 전체 저장
# mode     : 'series' → 같은 내용이라도 시각이 다르면 새 행 (VDS처럼 시간에 따라 바뀌는 값)
#            'state'  → 내용이 같으면 한 행 유지, 마지막 확인 시각만 갱신 (돌발, 주의구간처럼 상태 정보)
# ts_field : 정보 생성 시각 필드명. 있으면 그 값으로 중복 판단, 없으면 수집 시각 기준
# max_age_min : ts_field 가 이 시간(분)보다 오래된 항목은 버림 (VDS는 수년 전 값이 섞여 옴)
#
# ※ 2026-10-17 probe 결과로 확인한 사항
#   - vdsInfo  : 26,462건. 필드 vdsId, linkIds(쉼표 구분), laneNo, colctedDate, speed, volume, occupancy
#                도로명·좌표 없음, BBOX 무시(전국), 측정불가 값은 -1, 2020~2025년 묵은 값 섞임
#   - vslInfo  : 3,917건. 필드 vslId, sectionCode, createdDate, limitSpeed, defLmtSpeed, roadNo, linkId,
#                coordX, coordY, registedDate. 도로공사 구간은 coordX/coordY 위경도가 뒤바뀌어 옴
#   - 두 API 모두 고속도로(도로공사) 중심이라 강변북로 링크와 겹치는 항목이 0건이었음
#   - cautionInfo : 요청 실패(엔드포인트 이름 확인 필요)
SOURCES = {
    "traffic": {
        "label": "교통소통정보",
        "endpoint": "trafficInfo",
        "params": {"type": "all", "drcType": "all"},
        "bbox": True,
        "road_filter": "exact",
        "mode": "series",
        "ts_field": "createdDate",
    },
    "events": {
        "label": "돌발상황정보",
        "endpoint": "eventInfo",
        "params": {"type": "all", "eventType": "all"},
        "bbox": True,
        "road_filter": "none",   # 주변 도로(올림픽대로 등) 돌발도 정체 원인 판단에 쓰이므로 전체 보관
        "mode": "state",
        "ts_field": None,
    },
    "vds": {
        "label": "VDS 정보",
        "endpoint": "vdsInfo",        # probe 확인 완료
        "params": {"type": "all"},
        "bbox": True,
        "road_filter": "links",
        "link_field": "linkIds",
        "mode": "series",
        "ts_field": "colctedDate",    # API 철자 그대로 (collected 아님)
        "max_age_min": 30,
    },
    "caution": {
        "label": "주의운전구간정보",
        "endpoint": "cautionInfo",    # ← 확인 필요: probe --try 로 후보 시도
        "candidates": ["cautionInfo", "cautionDriveInfo", "cautionSectionInfo", "carefulDriveInfo",
                       "dangerInfo", "dangerousRoadInfo", "attentionInfo", "warningInfo",
                       "riskInfo", "safeDriveInfo"],
        "params": {"type": "all"},
        "bbox": True,
        "road_filter": "auto",
        "mode": "state",
        "ts_field": None,
    },
    "vsl": {
        "label": "VSL 정보",
        "endpoint": "vslInfo",        # probe 확인 완료
        "params": {"type": "all"},
        "bbox": True,
        "road_filter": "auto",        # 링크ID 일치 → (없으면) 좌표 범위
        "link_field": "linkId",
        "mode": "series",
        "ts_field": "createdDate",
        "max_age_min": 30,
    },
}

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DB_PATH = DATA_DIR / "its_data.db"   # 이전 버전의 gangbyeon.db 는 건드리지 않음

log = logging.getLogger("collector")


# ===========================================================================
# HTTP / 응답 처리
# ===========================================================================
def http_get_json(url, params, retries=3, timeout=30):
    full = f"{url}?{urllib.parse.urlencode(params)}"
    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "MISAN-collector/2.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # JSON이 아니면(XML 오류 페이지 등) 원문을 그대로 돌려줘서 확인할 수 있게 함
                return {"_non_json": raw[:2000]}
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}"
            if e.code in (400, 401, 403, 404):  # 재시도해도 소용없는 오류
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        log.warning("요청 실패 (%d/%d): %s", attempt, retries, last_err)
        time.sleep(2 * attempt)
    raise RuntimeError(f"API 요청 실패: {last_err}")


def find_items(obj, depth=0):
    """응답에서 item 목록(dict 리스트)을 찾는다. 구조가 API마다 달라도 동작하도록 탐색."""
    if depth > 5:
        return None
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return obj
        return None
    if isinstance(obj, dict):
        for key in ("body", "items", "item", "data", "list", "result", "response"):
            if key in obj:
                found = find_items(obj[key], depth + 1)
                if found is not None:
                    return found
                if isinstance(obj[key], dict) and key in ("item", "items"):
                    return [obj[key]]  # 항목이 1개라 dict로 온 경우
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = find_items(v, depth + 1)
                if found is not None:
                    return found
    return None


def result_status(payload):
    """(성공 여부, 메시지)"""
    if not isinstance(payload, dict):
        return False, "알 수 없는 응답"
    if "_non_json" in payload:
        return False, "JSON이 아닌 응답: " + payload["_non_json"][:200].replace("\n", " ")
    header = payload.get("header") or payload.get("response", {}).get("header") or {}
    code = str(header.get("resultCode", "0"))
    if code not in ("0", "00"):
        return False, f"오류 코드 {code}: {header.get('resultMsg')}"
    return True, header.get("resultMsg", "OK")


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_xy(it):
    """(경도, 위도). 위경도가 뒤바뀐 응답(VSL 등)은 값 크기로 판단해 바로잡는다 (한국: 경도≈124~132, 위도≈33~39)."""
    x = to_float(it.get("coordX") or it.get("xcoord") or it.get("lon") or it.get("longitude"))
    y = to_float(it.get("coordY") or it.get("ycoord") or it.get("lat") or it.get("latitude"))
    if x is None or y is None:
        return None
    if x < 90 < y:  # coordX 에 위도, coordY 에 경도가 들어온 경우
        x, y = y, x
    return x, y


def in_bbox(it, keep_no_coord=True):
    xy = get_xy(it)
    if xy is None:
        return keep_no_coord  # 좌표가 없으면 기본적으로 거르지 않음
    x, y = xy
    return BBOX["minX"] <= x <= BBOX["maxX"] and BBOX["minY"] <= y <= BBOX["maxY"]


def item_links(it, link_field):
    if not link_field:
        return set()
    return {s.strip() for s in str(it.get(link_field) or "").split(",") if s.strip()}


def apply_filter(items, how, road, links=None, link_field=None):
    """반환: (거른 항목, 실제 사용한 방식)"""
    links = links or set()
    if how == "none":
        return items, "none"
    if how == "exact":
        return [it for it in items if str(it.get("roadName", "")).strip() == road], "roadName"
    if how == "links":
        return [it for it in items if item_links(it, link_field) & links], "links"
    # auto: 도로명 → 링크ID → 좌표 범위
    named = [it for it in items if str(it.get("roadName", "")).strip() == road]
    if named:
        return named, "roadName"
    if links and link_field:
        by_link = [it for it in items if item_links(it, link_field) & links]
        if by_link:
            return by_link, "links"
    has_coord = any(get_xy(it) for it in items)
    return [it for it in items if in_bbox(it, keep_no_coord=not has_coord)], "bbox"


def drop_stale(items, ts_field, max_age_min, now_dt):
    """ts_field 가 max_age_min 분보다 오래된 항목 제거 (VDS의 수년 전 값 등)"""
    if not (ts_field and max_age_min):
        return items
    limit = now_dt.timestamp() - max_age_min * 60
    out = []
    for it in items:
        try:
            t = datetime.strptime(str(it.get(ts_field))[:14], "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            out.append(it)  # 시각을 못 읽으면 남겨 둠
            continue
        if t >= limit:
            out.append(it)
    return out


# ===========================================================================
# 강변북로 링크 목록 (VDS·VSL을 링크ID로 거르기 위해 필요)
# ===========================================================================
def links_path(road):
    return DATA_DIR / f"links_{road}.json"


def load_road_links(road, api_key=None, store=None, refresh=False):
    """우선순위: 저장된 파일 → DB의 traffic 테이블 → 교통소통정보 API 1회 호출"""
    path = links_path(road)
    if path.exists() and not refresh:
        return set(json.loads(path.read_text(encoding="utf-8")))
    links = set()
    if store is not None and "traffic" in store.tables() and "linkId" in store.columns("traffic"):
        links = {r[0] for r in store.con.execute(
            'SELECT DISTINCT linkId FROM traffic WHERE roadName = ?', (road,)) if r[0]}
    if not links and api_key:
        payload = call_source("traffic", api_key)
        items = find_items(payload) or []
        links = {str(it.get("linkId")) for it in items
                 if str(it.get("roadName", "")).strip() == road and it.get("linkId")}
    if links:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(links)), encoding="utf-8")
        log.info("%s 링크 %d개 확보 → %s", road, len(links), path)
    else:
        log.warning("%s 링크 목록을 만들지 못했습니다. 링크 기준 필터는 0건이 됩니다.", road)
    return links


# ===========================================================================
# DB (API별 테이블, 응답 필드에 맞춰 컬럼 자동 추가)
# ===========================================================================
META_COLS = ("key", "first_seen", "last_seen")


def safe_col(name):
    col = re.sub(r"\W", "_", str(name)).strip("_") or "field"
    if col.lower() in META_COLS:
        col = "f_" + col
    return col


def flatten(it):
    out = {}
    for k, v in it.items():
        out[safe_col(k)] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
    return out


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.cols = {}

    def ensure_table(self, table, fields):
        if table not in self.cols:
            self.con.execute(
                f'CREATE TABLE IF NOT EXISTS "{table}" '
                "(key TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT)")
            self.cols[table] = {r[1] for r in self.con.execute(f'PRAGMA table_info("{table}")')}
        for f in fields:
            if f not in self.cols[table]:
                self.con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{f}" TEXT')
                self.cols[table].add(f)

    def save(self, table, items, now, mode, ts_field):
        rows = [flatten(it) for it in items]
        fields = sorted({k for r in rows for k in r})
        self.ensure_table(table, fields)
        new = 0
        for it, r in zip(items, rows):
            body = json.dumps(it, ensure_ascii=False, sort_keys=True)
            if mode == "series" and not (ts_field and it.get(ts_field)):
                body += now  # 생성 시각 필드가 없으면 수집 시각마다 별도 행
            key = hashlib.sha1(body.encode("utf-8")).hexdigest()
            cols = list(r)
            cur = self.con.execute(
                f'INSERT INTO "{table}" (key, first_seen, last_seen, '
                + ", ".join(f'"{c}"' for c in cols)
                + ") VALUES (?, ?, ?" + ", ?" * len(cols) + ") "
                "ON CONFLICT(key) DO UPDATE SET last_seen = excluded.last_seen",
                [key, now, now, *[r[c] for c in cols]])
            new += 1 if cur.rowcount and self._is_new(table, key, now) else 0
        self.con.commit()
        return new

    def _is_new(self, table, key, now):
        row = self.con.execute(f'SELECT first_seen FROM "{table}" WHERE key=?', (key,)).fetchone()
        return row is not None and row[0] == now

    def tables(self):
        return [r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def columns(self, table):
        return [r[1] for r in self.con.execute(f'PRAGMA table_info("{table}")')]


# ===========================================================================
# 수집
# ===========================================================================
def call_source(name, api_key, endpoint=None):
    cfg = SOURCES[name]
    params = {"apiKey": api_key, "getType": "json", **cfg.get("params", {})}
    if cfg.get("bbox"):
        params.update(BBOX)
    return http_get_json(f"{ITS_BASE}/{endpoint or cfg['endpoint']}", params)


def set_endpoint_overrides(pairs):
    """--endpoint caution=xxx,vsl=yyy → SOURCES 의 endpoint 를 이번 실행에만 바꿈"""
    for p in [s for s in (pairs or "").split(",") if s.strip()]:
        if "=" not in p:
            sys.exit(f"--endpoint 형식 오류: {p} (예: caution=cautionInfo)")
        k, v = (s.strip() for s in p.split("=", 1))
        if k not in SOURCES:
            sys.exit(f"알 수 없는 소스: {k}")
        SOURCES[k]["endpoint"] = v


def collect_source(store, name, api_key, road, now, raw_dir, links=None, keep_all=False):
    cfg = SOURCES[name]
    payload = call_source(name, api_key)
    ok, msg = result_status(payload)
    if not ok:
        log.error("[%s] %s", cfg["label"], msg)
        return
    items = find_items(payload) or []
    fresh = drop_stale(items, cfg.get("ts_field"), cfg.get("max_age_min"),
                       datetime.strptime(now, "%Y%m%d%H%M%S"))
    if keep_all:
        kept, how = fresh, "none(--keep-all)"
    else:
        kept, how = apply_filter(fresh, cfg.get("road_filter", "none"), road,
                                 links, cfg.get("link_field"))
    if raw_dir is not None:  # 원본은 거른 항목만 보관 (용량 절약)
        d = raw_dir / now[:8]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}_{now}.json").write_text(
            json.dumps({"header": payload.get("header"), "items": kept}, ensure_ascii=False),
            encoding="utf-8")
    new = store.save(name, kept, now, cfg.get("mode", "series"), cfg.get("ts_field")) if kept else 0

    extra = ""
    if name == "traffic":
        avg = road_avg_speed(kept)
        extra = f" | 구간평균 {avg:.1f} km/h" if avg else ""
    stale = len(items) - len(fresh)
    log.info("[%s] %d건 저장대상 (응답 %d건, 오래된 값 %d건 제외, 필터=%s, 신규 %d)%s",
             cfg["label"], len(kept), len(items), stale, how, new, extra)
    if not kept and items:
        log.warning("[%s] %s 해당 항목이 0건입니다. `probe` 보고서에서 범위를 확인하세요.", cfg["label"], road)


def road_avg_speed(items):
    """구간 평균속도 = 총 길이 / 총 통행시간 (링크 길이 = 속도 × 통행시간)"""
    dist = tt = 0.0
    for it in items:
        s, t = to_float(it.get("speed")), to_float(it.get("travelTime"))
        if s and t:
            dist += s * t / 3.6
            tt += t
    return (dist / tt * 3.6) if tt else None


def collect_once(store, api_key, road, sources, raw_dir, links=None, keep_all=()):
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    for name in sources:
        try:
            collect_source(store, name, api_key, road, now, raw_dir, links, name in keep_all)
        except Exception as e:  # 한 API가 실패해도 나머지는 계속
            log.error("[%s] 수집 실패: %s", SOURCES[name]["label"], e)


# ===========================================================================
# 종료 키 감지 (Windows / macOS / Linux)
# ===========================================================================
def start_key_listener(stop_event, quit_key):
    if not sys.stdin.isatty():
        log.info("터미널 입력이 없어 키 종료를 쓸 수 없습니다. Ctrl+C로 종료하세요.")
        return None

    def listen_windows():
        import msvcrt
        while not stop_event.is_set():
            if msvcrt.kbhit() and msvcrt.getwch().lower() == quit_key:
                stop_event.set()
            time.sleep(0.1)

    def listen_unix():
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not stop_event.is_set():
                r, _, _ = select.select([sys.stdin], [], [], 0.2)
                if r and sys.stdin.read(1).lower() == quit_key:
                    stop_event.set()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    th = threading.Thread(target=listen_windows if os.name == "nt" else listen_unix, daemon=True)
    th.start()
    return th


# ===========================================================================
# CSV 내보내기
# ===========================================================================
def fmt_ts(s):
    s = str(s or "")
    if len(s) >= 14 and s[:14].isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
    return s


def write_csv(path, header, rows):
    import csv
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # 엑셀 한글 호환
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return len(rows)


def dump_table(store, table, path, since=None):
    cols = [c for c in store.columns(table) if c != "key"]
    sql = f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{table}"'
    args = ()
    if since:
        sql += " WHERE last_seen >= ?"
        args = (since,)
    sql += " ORDER BY first_seen"
    rows = [[fmt_ts(v) if c in ("first_seen", "last_seen") else v for c, v in zip(cols, r)]
            for r in store.con.execute(sql, args)]
    header = ["처음수집" if c == "first_seen" else "마지막수집" if c == "last_seen" else c
              for c in cols]
    return write_csv(path, header, rows)


def traffic_extras(store, out_dir, tag, since):
    """교통소통정보 정리: 링크별 요약 + 시각별 구간 평균속도"""
    cols = set(store.columns("traffic"))
    if not {"linkId", "speed", "travelTime", "createdDate"} <= cols:
        return
    where = "WHERE last_seen >= ?" if since else ""
    args = (since,) if since else ()

    rows = store.con.execute(
        f"""SELECT roadName, linkId, COUNT(*),
                   ROUND(AVG(CAST(speed AS REAL)),1), MIN(CAST(speed AS REAL)), MAX(CAST(speed AS REAL)),
                   ROUND(100.0*SUM(CASE WHEN CAST(speed AS REAL) < 30 THEN 1 ELSE 0 END)/COUNT(*),1)
            FROM traffic {where} GROUP BY roadName, linkId ORDER BY linkId""", args).fetchall()
    write_csv(out_dir / f"traffic_summary_{tag}.csv",
              ["도로명", "링크ID", "관측수", "평균속도", "최저속도", "최고속도", "정체비율(%,<30km/h)"], rows)

    rows = store.con.execute(
        f"""SELECT createdDate, COUNT(*),
                   ROUND(AVG(CAST(speed AS REAL)),1),
                   ROUND(SUM(CAST(speed AS REAL)*CAST(travelTime AS REAL)/3.6)
                         / SUM(CAST(travelTime AS REAL)) * 3.6, 1),
                   SUM(CASE WHEN CAST(speed AS REAL) < 30 THEN 1 ELSE 0 END)
            FROM traffic {where} GROUP BY createdDate ORDER BY createdDate""", args).fetchall()
    write_csv(out_dir / f"traffic_avgspeed_{tag}.csv",
              ["정보생성시각", "링크수", "단순평균속도", "구간평균속도", "30km/h미만링크수"],
              [(fmt_ts(r[0]), *r[1:]) for r in rows])


def export_session(store, session_start):
    tag = session_start.strftime("%Y%m%d_%H%M%S")
    since = session_start.strftime("%Y%m%d%H%M%S")
    out_dir = DATA_DIR / "sessions" / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for table in store.tables():
        n = dump_table(store, table, out_dir / f"{table}_{tag}.csv", since)
        parts.append(f"{table} {n}행")
    if "traffic" in store.tables():
        traffic_extras(store, out_dir, tag, since)
    log.info("세션 CSV 저장 → %s", out_dir)
    log.info("  %s", " / ".join(parts) or "저장된 데이터 없음")


# ===========================================================================
# 명령
# ===========================================================================
def get_key(args):
    key = args.its_key or os.environ.get("ITS_API_KEY")
    if not key:
        sys.exit("ITS API 키가 필요합니다: --its-key 또는 환경변수 ITS_API_KEY")
    return key


def parse_sources(text):
    names = [s.strip() for s in text.split(",") if s.strip()]
    bad = [n for n in names if n not in SOURCES]
    if bad:
        sys.exit(f"알 수 없는 소스: {bad}. 사용 가능: {list(SOURCES)}")
    return names


# ===========================================================================
# probe: 엔드포인트·필드 확정용 분석
# ===========================================================================
TS_RE = re.compile(r"^\d{14}$")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def parse_ts(v):
    try:
        return datetime.strptime(str(v)[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def guess_type(name, values):
    vals = [str(v) for v in values if v not in (None, "")]
    if not vals:
        return "빈 값"
    if all(TS_RE.match(v) for v in vals):
        return "시각(YYYYMMDDhhmmss)"
    low = name.lower()
    if "coord" in low or low in ("lat", "lon", "latitude", "longitude", "xcoord", "ycoord"):
        return "좌표"
    if "link" in low:
        return "링크ID" + (" 목록(쉼표)" if any("," in v for v in vals) else "")
    if all(NUM_RE.match(v) for v in vals):
        return "숫자"
    return "문자"


def analyze_items(name, cfg, items, road, links, now_dt):
    """필드별 통계와 추천 설정을 담은 보고서 줄 목록 + 필드 표 행"""
    lines, field_rows = [], []
    n = len(items)
    keys = []
    for it in items:
        for k in it:
            if k not in keys:
                keys.append(k)

    ts_candidates = []
    for k in keys:
        vals = [it.get(k) for it in items]
        filled = [v for v in vals if v not in (None, "")]
        uniq = len({json.dumps(v, ensure_ascii=False) for v in filled})
        typ = guess_type(k, filled[:500])
        samples = []
        for v in filled:
            s = str(v)[:40]
            if s not in samples:
                samples.append(s)
            if len(samples) == 3:
                break
        extra = ""
        if typ.startswith("시각"):
            ts = [t for t in (parse_ts(v) for v in filled) if t]
            if ts:
                fresh = sum(1 for t in ts if 0 <= (now_dt - t).total_seconds() <= 30 * 60)
                extra = f"범위 {min(ts):%Y-%m-%d %H:%M} ~ {max(ts):%Y-%m-%d %H:%M}, 최근30분 {fresh}/{len(ts)}"
                ts_candidates.append((k, uniq, extra))
        if typ == "숫자":
            nums = [float(v) for v in filled if NUM_RE.match(str(v))]
            neg = sum(1 for x in nums if x < 0)
            if nums:
                extra = f"최소 {min(nums):g} / 최대 {max(nums):g}" + (f", 음수(-1 등) {neg}건" if neg else "")
        field_rows.append([k, typ, f"{100.0 * len(filled) / n:.0f}" if n else "0", uniq,
                           " | ".join(samples), extra])

    lines.append(f"항목 {n}건, 필드 {len(keys)}개")
    for r in field_rows:
        lines.append(f"  - {r[0]:<16} {r[1]:<22} 채움 {r[2]:>3}%  고유 {r[3]:<6} 예: {r[4]}"
                     + (f"  [{r[5]}]" if r[5] else ""))

    # 좌표
    xy = [get_xy(it) for it in items]
    with_xy = [p for p in xy if p]
    if with_xy:
        swapped = sum(1 for it in items
                      if to_float(it.get("coordX")) is not None and to_float(it.get("coordX")) < 90)
        inb = sum(1 for it in items if get_xy(it) and in_bbox(it))
        lines.append(f"좌표: {len(with_xy)}/{n}건 보유, 위경도 뒤바뀜 {swapped}건(자동 보정), 강변북로 범위 안 {inb}건")
    else:
        lines.append("좌표: 없음 → 좌표 범위로 거를 수 없음")

    # 도로 매칭
    named = sum(1 for it in items if str(it.get("roadName", "")).strip() == road)
    has_rn = any("roadName" in it for it in items)
    lines.append(f"도로명 '{road}' 일치: {named}건" + ("" if has_rn else " (roadName 필드 없음)"))
    link_field = cfg.get("link_field") or next(
        (k for k in keys if k.lower() in ("linkid", "linkids")), None)
    link_hits = 0
    if link_field:
        link_hits = sum(1 for it in items if item_links(it, link_field) & links)
        lines.append(f"링크 필드 '{link_field}' 로 강변북로 링크({len(links)}개)와 겹침: {link_hits}건")

    # 추천
    lines.append("추천 설정:")
    if cfg.get("mode") == "state":
        lines.append("  ts_field = None (상태 정보라 내용 기준으로 중복 판단, mode='state' 유지)")
    elif ts_candidates:
        pref = [c for c in ts_candidates
                if any(w in c[0].lower() for w in ("creat", "colct", "collect"))]
        best = (pref or sorted(ts_candidates, key=lambda c: -c[1]))[0]
        lines.append(f"  ts_field = '{best[0]}'   ({best[2]})")
        if len(ts_candidates) > 1:
            lines.append(f"    (다른 시각 필드: {', '.join(c[0] for c in ts_candidates if c[0] != best[0])})")
    else:
        lines.append("  ts_field = None (시각 필드 없음 → 수집 시각 기준)")
    if named:
        lines.append("  road_filter = 'exact'")
    elif link_hits:
        lines.append(f"  road_filter = 'links', link_field = '{link_field}'")
    elif with_xy:
        lines.append("  road_filter = 'auto' (좌표 범위)")
    else:
        lines.append("  ※ 강변북로로 거를 방법이 없음 (도로명·링크·좌표 모두 해당 없음)")
    if not (named or link_hits) and n:
        lines.append(f"  ※ 이 API 응답에는 {road} 항목이 없습니다. 다른 출처(서울시 TOPIS 등)가 필요할 수 있습니다.")
    return lines, field_rows, ts_candidates


def probe_one(name, api_key, endpoint=None):
    """(payload 또는 None, 상태 문자열)"""
    try:
        payload = call_source(name, api_key, endpoint)
    except Exception as e:
        return None, f"요청 실패: {e}"
    ok, msg = result_status(payload)
    return (payload if ok else None), (msg if ok else f"응답 오류: {msg}")


def cmd_probe(args):
    """API마다 1번 호출해 필드 구조를 분석하고, data/probe 에 원본·보고서·필드표를 저장.
    --offline : 이미 저장된 data/probe/*.json 만 다시 분석 (키 불필요)
    --try     : 엔드포인트 후보(candidates)를 차례로 시도해 맞는 이름을 찾음"""
    out = DATA_DIR / "probe"
    out.mkdir(parents=True, exist_ok=True)
    api_key = None if args.offline else get_key(args)
    store = Store(DB_PATH) if DB_PATH.exists() else None
    links = load_road_links(args.road, api_key, store, refresh=args.refresh_links) \
        if (api_key or links_path(args.road).exists() or store) else set()
    if not links and args.offline:
        # 오프라인이면 data/raw 의 교통소통정보 원본에서 링크 목록 확보
        for f in sorted((DATA_DIR / "raw").glob("*/traffic_*.json"), reverse=True):
            items = find_items(json.loads(f.read_text(encoding="utf-8"))) or []
            links = {str(it.get("linkId")) for it in items
                     if str(it.get("roadName", "")).strip() == args.road and it.get("linkId")}
            if links:
                break
    now_dt = datetime.now()
    report = [f"ITS OpenAPI probe 보고서  {now_dt:%Y-%m-%d %H:%M:%S}  도로={args.road}  "
              f"강변북로 링크 {len(links)}개", "=" * 78]
    summary = []

    for name in parse_sources(args.sources):
        cfg = SOURCES[name]
        payload, status, used = None, "", cfg["endpoint"]
        raw_file = out / f"{name}.json"

        src_now = now_dt
        if args.offline:
            if raw_file.exists():
                # 오래된 값 판단은 probe 파일을 저장한 시각 기준
                src_now = datetime.fromtimestamp(raw_file.stat().st_mtime)
                payload = json.loads(raw_file.read_text(encoding="utf-8"))
                ok, status = result_status(payload)
                status = "저장된 파일 분석" if ok else status
                if not ok:
                    payload = None
            else:
                status = "저장된 probe 파일 없음"
        else:
            tries = [cfg["endpoint"]]
            if args.try_candidates:
                tries += [c for c in cfg.get("candidates", []) if c not in tries]
            for ep in tries:
                payload, status = probe_one(name, api_key, ep)
                print(f"  [{name}] {ITS_BASE}/{ep} → {'✓ ' if payload else '✗ '}{status}")
                if payload:
                    used = ep
                    break
            if payload:
                raw_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            else:
                (out / f"{name}_error.txt").write_text(
                    f"{now_dt:%Y-%m-%d %H:%M:%S}\n시도: {', '.join(tries)}\n마지막 결과: {status}\n",
                    encoding="utf-8")

        report.append(f"\n## {name} ({cfg['label']})  endpoint={used}")
        report.append(f"상태: {status}")
        if payload is None:
            report.append("  → 엔드포인트 이름을 ITS 마이페이지의 '요청 URL'과 맞춰 주세요."
                          " (probe --try 또는 --endpoint " + name + "=이름)")
            summary.append([name, cfg["label"], used, "실패", "", "", "", status])
            continue
        if used != cfg["endpoint"]:
            report.append(f"  ★ 맞는 엔드포인트 발견: SOURCES['{name}']['endpoint'] = '{used}' 로 바꾸세요")
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        items = find_items(payload) or []
        if body.get("totalCount") is not None:
            report.append(f"totalCount={body.get('totalCount')} / 받은 항목 {len(items)}건")
        lines, rows, ts_c = analyze_items(name, cfg, items, args.road, links, src_now)
        report += lines
        write_csv(out / f"fields_{name}.csv",
                  ["필드", "추정유형", "채움비율(%)", "고유값수", "예시", "비고"], rows)

        fresh = drop_stale(items, cfg.get("ts_field"), cfg.get("max_age_min"), src_now)
        kept, how = apply_filter(fresh, cfg.get("road_filter", "none"), args.road,
                                 links, cfg.get("link_field"))
        report.append(f"현재 설정으로 저장될 항목: {len(kept)}건 (필터={how}, "
                      f"ts_field={cfg.get('ts_field')}, max_age={cfg.get('max_age_min')}분)")
        summary.append([name, cfg["label"], used, "성공", len(items), len(kept), how,
                        ts_c[0][0] if ts_c else ""])

    report.append("\n" + "=" * 78)
    report.append("요약")
    for s in summary:
        report.append("  " + " | ".join(str(x) for x in s))
    text = "\n".join(report)
    print("\n" + text)
    (out / "probe_report.txt").write_text(text, encoding="utf-8")
    write_csv(out / "probe_summary.csv",
              ["소스", "이름", "엔드포인트", "결과", "응답건수", "저장대상", "필터", "시각필드/상태"], summary)
    print(f"\n보고서: {out / 'probe_report.txt'}")
    print(f"필드표: {out / 'fields_<소스>.csv'},  요약: {out / 'probe_summary.csv'}")


def cmd_collect(args):
    api_key = get_key(args)
    sources = parse_sources(args.sources)
    store = Store(DB_PATH)
    raw_dir = None if args.no_raw else DATA_DIR / "raw"
    quit_key = args.quit_key.lower()

    links = set()
    if any(SOURCES[s].get("road_filter") in ("links", "auto") for s in sources):
        links = load_road_links(args.road, api_key, store, refresh=args.refresh_links)
    keep_all = parse_sources(args.keep_all) if args.keep_all else []

    session_start = datetime.now()
    stop_event = threading.Event()
    log.info("수집 시작: %s | 도로=%s | 간격=%s",
             ", ".join(SOURCES[s]["label"] for s in sources), args.road,
             "1회" if args.once else f"{args.interval}초")
    if not args.once:
        start_key_listener(stop_event, quit_key)
        log.info("▶ '%s' 키를 누르면 수집을 멈추고 CSV를 저장합니다. (Ctrl+C도 가능)", quit_key)

    rounds = 0
    try:
        while not stop_event.is_set():
            started = time.time()
            collect_once(store, api_key, args.road, sources, raw_dir, links, keep_all)
            rounds += 1
            if args.once:
                break
            wait = max(0, args.interval - (time.time() - started))
            nxt = datetime.fromtimestamp(time.time() + wait).strftime("%H:%M:%S")
            log.info("다음 수집 %s (누적 %d회)", nxt, rounds)
            stop_event.wait(wait)
    except KeyboardInterrupt:
        log.info("Ctrl+C 입력")
    finally:
        stop_event.set()
        log.info("수집 종료: %s ~ %s, 총 %d회",
                 session_start.strftime("%H:%M:%S"), datetime.now().strftime("%H:%M:%S"), rounds)
        export_session(store, session_start)
        store.con.close()


def cmd_export(args):
    store = Store(DB_PATH)
    out_dir = DATA_DIR / "export"
    out_dir.mkdir(parents=True, exist_ok=True)
    for table in store.tables():
        n = dump_table(store, table, out_dir / f"{table}.csv")
        log.info("%s → %s (%d행)", table, out_dir / f"{table}.csv", n)
    if "traffic" in store.tables():
        traffic_extras(store, out_dir, "all", None)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(description="강변북로 ITS 교통 데이터 통합 수집기")
    sub = p.add_subparsers(dest="cmd", required=True)
    all_src = ",".join(SOURCES)

    def common(sp):
        sp.add_argument("--its-key", help="ITS OpenAPI 인증키 (없으면 ITS_API_KEY 환경변수)")
        sp.add_argument("--road", default=DEFAULT_ROAD, help="도로명 (기본: 강변북로)")
        sp.add_argument("--sources", default=all_src, help=f"수집할 API (기본: {all_src})")
        sp.add_argument("--endpoint", default="",
                        help="엔드포인트 임시 변경, 예: caution=cautionInfo,vsl=vslInfo")
        sp.add_argument("--refresh-links", action="store_true",
                        help="강변북로 링크 목록(data/links_도로명.json)을 다시 만듦")

    pr = sub.add_parser("probe", help="API별 엔드포인트·필드 확인 → data/probe/probe_report.txt")
    common(pr)
    pr.add_argument("--try", dest="try_candidates", action="store_true",
                    help="실패하면 SOURCES 의 candidates 엔드포인트 후보를 차례로 시도")
    pr.add_argument("--offline", action="store_true",
                    help="API 호출 없이 저장된 data/probe/*.json 만 다시 분석 (키 불필요)")
    pr.set_defaults(func=cmd_probe)

    c = sub.add_parser("collect", help="데이터 수집")
    common(c)
    c.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                   help=f"수집 간격(초), 기본 {DEFAULT_INTERVAL}")
    c.add_argument("--once", action="store_true", help="1회만 수집")
    c.add_argument("--quit-key", default="q", help="수집 종료 키 (기본: q)")
    c.add_argument("--no-raw", action="store_true", help="원본 JSON 저장 안 함")
    c.add_argument("--keep-all", default="",
                   help="도로 필터 없이 전체 저장할 소스, 예: vsl (vds는 회당 2만여 건이라 비추천)")
    c.set_defaults(func=cmd_collect)

    e = sub.add_parser("export", help="전체 누적 데이터 CSV로 내보내기")
    e.set_defaults(func=cmd_export)

    args = p.parse_args()
    set_endpoint_overrides(getattr(args, "endpoint", ""))
    args.func(args)


if __name__ == "__main__":
    main()
