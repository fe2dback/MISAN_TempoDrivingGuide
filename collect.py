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
    python collect.py probe                       # API별 연결·응답 구조 확인
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
# params   : 공통 파라미터(apiKey, getType=json) 외에 추가로 보낼 값
# bbox     : True면 좌표 범위(minX, maxX, minY, maxY) 파라미터를 함께 보냄
# road_filter : 'exact'  → roadName 이 강변북로와 정확히 일치하는 항목만
#               'auto'   → roadName 필드가 있고 일치 항목이 있으면 거르고, 없으면 좌표 범위로만 거름
#               'none'   → 범위 안 전체 저장
# mode     : 'series' → 같은 내용이라도 시각이 다르면 새 행 (VDS처럼 시간에 따라 바뀌는 값)
#            'state'  → 내용이 같으면 한 행 유지, 마지막 확인 시각만 갱신 (돌발, 주의구간처럼 상태 정보)
# ts_field : 정보 생성 시각 필드명. 있으면 그 값으로 중복 판단, 없으면 수집 시각 기준
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
        "endpoint": "vdsInfo",        # ← 확인 필요
        "params": {"type": "all"},
        "bbox": True,
        "road_filter": "auto",
        "mode": "series",
        "ts_field": None,             # probe 결과에서 시각 필드를 확인하면 지정 (예: "collectDate")
    },
    "caution": {
        "label": "주의운전구간정보",
        "endpoint": "cautionInfo",    # ← 확인 필요
        "params": {"type": "all"},
        "bbox": True,
        "road_filter": "auto",
        "mode": "state",
        "ts_field": None,
    },
    "vsl": {
        "label": "VSL 정보",
        "endpoint": "vslInfo",        # ← 확인 필요
        "params": {"type": "all"},
        "bbox": True,
        "road_filter": "auto",
        "mode": "series",
        "ts_field": None,
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


def in_bbox(it):
    x = to_float(it.get("coordX") or it.get("xcoord") or it.get("lon") or it.get("longitude"))
    y = to_float(it.get("coordY") or it.get("ycoord") or it.get("lat") or it.get("latitude"))
    if x is None or y is None:
        return True  # 좌표가 없으면 거르지 않음
    return BBOX["minX"] <= x <= BBOX["maxX"] and BBOX["minY"] <= y <= BBOX["maxY"]


def apply_filter(items, how, road):
    if how == "none":
        return items
    if how == "exact":
        return [it for it in items if str(it.get("roadName", "")).strip() == road]
    # auto
    named = [it for it in items if str(it.get("roadName", "")).strip() == road]
    if named:
        return named
    return [it for it in items if in_bbox(it)]


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
def call_source(name, api_key):
    cfg = SOURCES[name]
    params = {"apiKey": api_key, "getType": "json", **cfg.get("params", {})}
    if cfg.get("bbox"):
        params.update(BBOX)
    return http_get_json(f"{ITS_BASE}/{cfg['endpoint']}", params)


def collect_source(store, name, api_key, road, now, raw_dir):
    cfg = SOURCES[name]
    payload = call_source(name, api_key)
    ok, msg = result_status(payload)
    if not ok:
        log.error("[%s] %s", cfg["label"], msg)
        return
    items = find_items(payload) or []
    kept = apply_filter(items, cfg.get("road_filter", "none"), road)
    if raw_dir is not None:  # 원본은 거른 항목만 보관 (용량 절약)
        d = raw_dir / now[:8]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}_{now}.json").write_text(
            json.dumps({"header": payload.get("header"), "items": kept}, ensure_ascii=False),
            encoding="utf-8")
    new = store.save(name, kept, now, cfg.get("mode", "series"), cfg.get("ts_field"))

    extra = ""
    if name == "traffic":
        avg = road_avg_speed(kept)
        extra = f" | 구간평균 {avg:.1f} km/h" if avg else ""
    log.info("[%s] %d건 (응답 %d건, 신규 %d)%s", cfg["label"], len(kept), len(items), new, extra)


def road_avg_speed(items):
    """구간 평균속도 = 총 길이 / 총 통행시간 (링크 길이 = 속도 × 통행시간)"""
    dist = tt = 0.0
    for it in items:
        s, t = to_float(it.get("speed")), to_float(it.get("travelTime"))
        if s and t:
            dist += s * t / 3.6
            tt += t
    return (dist / tt * 3.6) if tt else None


def collect_once(store, api_key, road, sources, raw_dir):
    now = datetime.now().strftime("%Y%m%d%H%M%S")
    for name in sources:
        try:
            collect_source(store, name, api_key, road, now, raw_dir)
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


def cmd_probe(args):
    """API마다 1번 호출해 상태·항목 수·필드 목록을 보여주고 원본 전체를 data/probe 에 저장"""
    api_key = get_key(args)
    out = DATA_DIR / "probe"
    out.mkdir(parents=True, exist_ok=True)
    for name in parse_sources(args.sources):
        cfg = SOURCES[name]
        print(f"\n=== {name} ({cfg['label']}) : {ITS_BASE}/{cfg['endpoint']}")
        try:
            payload = call_source(name, api_key)
        except Exception as e:
            print(f"  ✗ 요청 실패: {e}")
            continue
        (out / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
        ok, msg = result_status(payload)
        items = find_items(payload) or []
        kept = apply_filter(items, cfg.get("road_filter", "none"), args.road)
        print(f"  {'✓' if ok else '✗'} {msg} | 응답 {len(items)}건 → 저장 대상 {len(kept)}건")
        if items:
            print(f"  필드: {', '.join(items[0].keys())}")
            print(f"  예시: {json.dumps(items[0], ensure_ascii=False)[:300]}")
        print(f"  원본: {out / (name + '.json')}")


def cmd_collect(args):
    api_key = get_key(args)
    sources = parse_sources(args.sources)
    store = Store(DB_PATH)
    raw_dir = None if args.no_raw else DATA_DIR / "raw"
    quit_key = args.quit_key.lower()

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
            collect_once(store, api_key, args.road, sources, raw_dir)
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

    pr = sub.add_parser("probe", help="API별 연결·응답 구조 확인")
    common(pr)
    pr.set_defaults(func=cmd_probe)

    c = sub.add_parser("collect", help="데이터 수집")
    common(c)
    c.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                   help=f"수집 간격(초), 기본 {DEFAULT_INTERVAL}")
    c.add_argument("--once", action="store_true", help="1회만 수집")
    c.add_argument("--quit-key", default="q", help="수집 종료 키 (기본: q)")
    c.add_argument("--no-raw", action="store_true", help="원본 JSON 저장 안 함")
    c.set_defaults(func=cmd_collect)

    e = sub.add_parser("export", help="전체 누적 데이터 CSV로 내보내기")
    e.set_defaults(func=cmd_export)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
