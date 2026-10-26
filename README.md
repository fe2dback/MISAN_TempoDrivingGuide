# 강변북로 ITS 교통 데이터 통합 수집기

국가교통정보센터(ITS) OpenAPI에서 아래 5가지 정보를 주기적으로 수집해 SQLite(`data/its_data.db`)에 저장합니다. Python 3.8 이상만 있으면 되고, 별도 패키지 설치는 필요 없습니다.

| 이름 | API | 저장 범위 |
|---|---|---|
| `traffic` | 교통소통정보 | 강변북로 링크만 (도로명 정확히 일치) |
| `events` | 돌발상황정보 | 좌표 범위 안 전체 (주변 도로 포함) |
| `vds` | VDS 정보 | 강변북로 항목. 도로명 필드가 없으면 좌표 범위 안 전체 |
| `caution` | 주의운전구간정보 | 위와 같음 |
| `vsl` | VSL(가변 제한속도) 정보 | 위와 같음 |

## 1. 엔드포인트·필드 확정 (probe)
```bash
python collect.py probe --its-key 발급받은키                         # 5개 API 전체 분석
python collect.py probe --its-key 키 --sources caution --try          # 주의운전구간: 엔드포인트 후보 차례로 시도
python collect.py probe --its-key 키 --sources caution --endpoint caution=요청URL의이름
python collect.py probe --offline                                    # 저장된 probe 파일만 다시 분석 (키 불필요)
```
결과는 `data/probe/`에 저장됩니다.

| 파일 | 내용 |
|---|---|
| `probe_report.txt` | API별 상태, 필드별 유형·채움비율·예시, 시각 필드 범위, 좌표·도로명·링크 매칭 건수, **추천 설정(ts_field, road_filter)** |
| `fields_소스.csv` | 필드표 (엑셀로 열어 팀 공유용) |
| `probe_summary.csv` | 소스별 성공 여부, 응답 건수, 저장 대상 건수 |
| `소스.json` / `소스_error.txt` | 원본 응답 / 실패 사유 |

- 강변북로 링크 목록은 교통소통정보에서 뽑아 `data/links_강변북로.json`에 저장하고, 도로명이 없는 VDS·VSL을 링크ID로 거를 때 씁니다. 다시 만들려면 `--refresh-links`.
- 보고서의 추천값을 `collect.py` 맨 위 `SOURCES`에 옮겨 적으면 확정입니다. 엔드포인트는 ITS 마이페이지의 **요청 URL** 마지막 부분과 같아야 합니다.

### 2026-10-17 probe로 확인된 내용
| API | 엔드포인트 | 시각 필드 | 강변북로 거르는 방법 | 결과 |
|---|---|---|---|---|
| 교통소통정보 | trafficInfo | createdDate | roadName | 358개 링크 |
| 돌발상황정보 | eventInfo | (상태 정보) | 범위 전체 보관 | 74건, 강변북로 20건 |
| VDS | vdsInfo | colctedDate (철자 그대로) | linkIds ∩ 강변북로 링크 | **0건**: 고속도로(도로공사) 검지기만 있음, 수년 전 값·-1(측정불가) 섞임 |
| VSL | vslInfo | createdDate | linkId → 좌표 범위 | **0건**: 고속도로 VSL만 있음, 일부 위경도 뒤바뀜(자동 보정) |
| 주의운전구간 | cautionInfo? | - | - | 요청 실패, 엔드포인트 확인 필요 |

→ 강변북로 VDS·VSL은 ITS가 아닌 서울시(TOPIS, 서울시설공단) 자료가 필요할 수 있습니다. 근처 고속도로 자료라도 받아 두려면 `collect --keep-all vsl`처럼 필터 없이 저장할 수 있습니다 (VDS는 회당 2만여 건이라 비추천).

## 2. 수집
```bash
# 전체 API, 60초 간격 연속 수집 → q 키(또는 Ctrl+C)로 종료하면 세션 CSV 자동 저장
python collect.py collect --its-key 발급받은키

# 일부만 / 간격 변경 / 1회만
python collect.py collect --its-key 키 --sources traffic,vds --interval 300
python collect.py collect --its-key 키 --once

# 환경변수로 키 지정 (Windows)
set ITS_API_KEY=발급받은키
```
옵션: `--quit-key x`(종료 키 변경), `--no-raw`(원본 JSON 저장 안 함), `--road`(도로명 변경), `--endpoint 소스=이름`(엔드포인트 임시 변경), `--keep-all 소스`(도로 필터 없이 저장), `--refresh-links`

- VDS·VSL은 `max_age_min`(기본 30분)보다 오래된 값은 버립니다.

## 3. 저장 구조
```
data/
├── its_data.db                   # API별 테이블 (응답 필드에 맞춰 컬럼 자동 생성)
├── probe/                        # probe 결과 원본
├── raw/YYYYMMDD/                 # 회차별 원본 (거른 항목만)
├── sessions/20261017_083000/     # 실행 1회마다 시작 시각 이름의 폴더
│   ├── traffic_…csv              # 링크 속도
│   ├── traffic_summary_…csv      # 링크별 평균/최저/최고 속도, 정체비율(<30km/h)
│   ├── traffic_avgspeed_…csv     # 시각별 구간 평균속도 (총 길이 ÷ 총 통행시간)
│   ├── events_…csv / vds_…csv / caution_…csv / vsl_…csv
└── export/                       # export 명령: 전체 누적 CSV
```
- 모든 CSV에는 `처음수집`, `마지막수집` 시각이 붙습니다. 세션 CSV에는 그 실행 중에 확인된 데이터가 모두 들어갑니다. 이전 실행에서 이미 받은 자료도 이번 실행에서 다시 확인되면 포함됩니다.
- 중복 처리:
  - `traffic`은 정보생성시각이 같으면 한 행으로 저장합니다.
  - `vds`, `vsl`은 수집할 때마다 새 행을 추가합니다.
  - `events`, `caution`은 내용이 같으면 한 행을 유지하고 마지막 확인 시각만 갱신합니다.
- 이전 버전의 `data/gangbyeon.db`는 그대로 남겨 두었습니다. 새 데이터는 `its_data.db`에 쌓입니다.

## 참고
- 교통소통정보는 5분 단위로 갱신되므로, 60초 간격으로 수집해도 새 행은 5분마다 생깁니다.
- 터미널 창이 아닌 곳에서 실행하면 키 입력을 받을 수 없으니 Ctrl+C를 쓰세요. 강제로 종료하면 세션 CSV는 안 만들어지지만, 데이터는 DB에 남고 `export`로 꺼낼 수 있습니다.
