#!/usr/bin/env python3
"""
설산 산행지 추천용 적설 데이터 수집 스크립트 (기상청 API허브 사용)

하는 일
  1) (선택) --snap : 좌표가 정상에서 벗어난 산을 Open-Meteo 고도 API로 주변 최고점에 맞춤
  2) 현재 적설깊이(sd_tot)      : 기상청 고해상도(500m) 격자 관측분석
  3) 지난 7일 신적설 합계(sd_24h) : 같은 격자자료, 매일 00시 값 7개 합산
  4) 목표일 예보 : 산악예보(정상 기준, mountainNum) 우선, 없으면 단기예보(5km 격자)
     - 목표일 = 이번 토요일(토 11시부터는 일요일, 일 11시부터는 다음 주 토요일).
       그날 예보가 아직 없으면 예보가 있는 마지막 날 (--day 로 지정 가능)
     - 목표일 09시까지 신적설, 산행시간(08~15시) 눈 오는 시간, 영상 기온 시간, 비
  5) 점수 = 09시 예상적설 − 녹을 위험(0.5×영상시간 + 2×비mm, 단 쌓인 눈 이하) + 0.3×7일신적설 − 1.5×산행중 눈시간 − 0.05×강수확률(%)

준비
  - 기상청 API허브(apihub.kma.go.kr) 인증키를 환경변수로 넣습니다. (코드/채팅에 붙여넣지 마세요)
      export KMA_AUTH_KEY=발급받은키
  - API허브에서 아래 API를 각각 '활용신청' 해야 합니다.
      · 융합기상 > 고해상도 격자자료 > 1.3 특정지점 다중요소
      · 융합기상 > 산업 > 5. 산악예보 조회(getMountainWeather)
      · 예특보 > 단기예보 > 동네예보 조회(getVilageFcst)  ← mountainNum 없는 산(함백산 대체 등)용
  - pip install requests

실행 예
  python snow_report.py --csv top20_snow_mountains.csv             # 수·금 저녁, 토 새벽 실행
  python snow_report.py --csv ... --only 설악산 --debug             # 응답 원문 확인용
"""
import argparse, csv, html as H, json, math, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
HUB = "https://apihub.kma.go.kr/api"
DEBUG = False
LOGFILE = None
FAILS = 0            # 연속 실패 횟수 (접속 자체가 막혔을 때 빨리 멈추기 위함)
HUB_SLOTS = threading.BoundedSemaphore(4)   # 기상청 API허브 동시 요청 최대 4건(서버 부담·504 줄이기)
WORKERS = 6                                 # 산 여러 곳을 동시에 처리

# ---------------------------------------------------------------- 실행 간 캐시
# 지난 날짜의 일 신적설·찾아 둔 사진은 바뀌지 않으므로 지난 실행 결과를 다시 씀.
# 캐시는 웹페이지와 함께 cache.json 으로 배포되고, 다음 실행 때 내려받아 사용.
# 저장소 주인·이름은 GitHub Actions가 알려줌(GITHUB_REPOSITORY="계정/저장소") → 계정을 옮겨도 고칠 필요 없음
_REPO = os.environ.get("GITHUB_REPOSITORY", "leontrail/hikeplan")
CACHE_URL = f"https://{_REPO.split('/')[0].lower()}.github.io/{_REPO.split('/')[1]}/cache.json"
CACHE = {"obs": {}, "photo": {}}
CACHE_LOCK = threading.Lock()


def load_cache(local=None):
    global CACHE
    try:
        if local and os.path.exists(local):
            CACHE = json.load(open(local, encoding="utf-8"))
        else:
            CACHE = requests.get(CACHE_URL, timeout=10).json()
        CACHE.setdefault("obs", {})
        CACHE.setdefault("photo", {})
        log(f"캐시 불러옴: 관측 {len(CACHE['obs'])}건, 사진 {len(CACHE['photo'])}건")
    except Exception as e:
        log(f"캐시 없음(처음 실행이거나 내려받기 실패): {str(e)[:80]}")
        CACHE = {"obs": {}, "photo": {}}


def save_cache(path):
    cut = (datetime.now(KST) - timedelta(days=10)).strftime("%Y%m%d")
    CACHE["obs"] = {k: v for k, v in CACHE["obs"].items() if k.split("|")[1][:8] >= cut}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(CACHE, f, ensure_ascii=False)


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, file=sys.stderr)
    if LOGFILE:                                   # PowerShell 리다이렉트 대신 UTF-8 파일로 직접 기록
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


# ---------------------------------------------------------------- 좌표 변환
def latlon_to_grid(lat, lon):
    """위경도 → 단기예보 격자(nx, ny). 기상청 Lambert Conformal Conic 공식."""
    RE, GRID, SLAT1, SLAT2, OLON, OLAT, XO, YO = 6371.00877, 5.0, 30.0, 60.0, 126.0, 38.0, 43, 136
    d = math.pi / 180.0
    re = RE / GRID
    s1, s2, olon, olat = SLAT1 * d, SLAT2 * d, OLON * d, OLAT * d
    sn = math.log(math.cos(s1) / math.cos(s2)) / math.log(
        math.tan(math.pi * 0.25 + s2 * 0.5) / math.tan(math.pi * 0.25 + s1 * 0.5))
    sf = math.tan(math.pi * 0.25 + s1 * 0.5) ** sn * math.cos(s1) / sn
    ro = re * sf / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    ra = re * sf / math.tan(math.pi * 0.25 + lat * d * 0.5) ** sn
    theta = lon * d - olon
    theta = (theta + math.pi) % (2 * math.pi) - math.pi
    theta *= sn
    return int(ra * math.sin(theta) + XO + 0.5), int(ro - ra * math.cos(theta) + YO + 0.5)


# ---------------------------------------------------------------- 좌표 보정
def snap_to_summit(lat, lon, radius_m=1200, step_m=200):
    """주변 격자점 고도를 조회해 가장 높은 지점으로 이동 (Open-Meteo Elevation, 키 불필요)."""
    n = int(radius_m // step_m)
    dlat = step_m / 111_000
    dlon = step_m / (111_000 * math.cos(math.radians(lat)))
    pts = [(lat + i * dlat, lon + j * dlon) for i in range(-n, n + 1) for j in range(-n, n + 1)
           if (i * i + j * j) * step_m ** 2 <= radius_m ** 2]
    best = None
    for k in range(0, len(pts), 100):
        chunk = pts[k:k + 100]
        r = requests.get("https://api.open-meteo.com/v1/elevation", timeout=30, params={
            "latitude": ",".join(f"{p[0]:.5f}" for p in chunk),
            "longitude": ",".join(f"{p[1]:.5f}" for p in chunk)})
        r.raise_for_status()
        for p, e in zip(chunk, r.json()["elevation"]):
            if best is None or e > best[2]:
                best = (p[0], p[1], e)
    return best


# ---------------------------------------------------------------- 응답 파싱 도우미
def num(v):
    try:
        f = float(v)
        return None if f <= -90 else f      # -99, -999 등 결측값
    except (TypeError, ValueError):
        return None


def find_values(payload, key):
    """JSON/CSV 어느 형태든 key 컬럼 값을 모두 찾아 숫자 리스트로 반환."""
    out = []
    try:
        data = json.loads(payload)
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                for k, v in x.items():
                    if k.lower() == key and not isinstance(v, (dict, list)):
                        out.append(num(v))
                    else:
                        stack.append(v)
            elif isinstance(x, list):
                stack.extend(x)
        return [v for v in out if v is not None]
    except ValueError:
        pass
    header = None                                  # CSV 형태
    for line in payload.splitlines():
        cells = [c.strip() for c in re.split(r"[,\s]+", line.strip("# ").strip()) if c.strip()]
        if not cells:
            continue
        low = [c.lower() for c in cells]
        if key in low:
            header = low
            continue
        if header and len(cells) == len(header):
            v = num(cells[header.index(key)])
            if v is not None:
                out.append(v)
    return out


# ---------------------------------------------------------------- 기상청 API
def hub_get(path, params, key, tries=4):
    """API허브 호출. 504·시간초과는 서버가 잠깐 바쁜 경우가 많아 간격을 늘려가며 재시도."""
    global FAILS
    shown = {k: v for k, v in params.items()}
    params = dict(params, authKey=key)
    name = path.rsplit("/", 1)[-1]
    conn_err = False
    for attempt in range(tries):
        t0 = time.time()
        try:
            with HUB_SLOTS:
                r = requests.get(f"{HUB}/{path}", params=params, timeout=(10, 30))
            r.encoding = "utf-8"                     # 한글 예보값(강수없음 등)이 깨지지 않도록 고정
            if r.status_code == 200:
                if DEBUG:
                    log(f"--- {name} {shown} ({time.time() - t0:.1f}s)\n{r.text[:3000]}\n---")
                FAILS = 0
                return r.text
            log(f"  [{name}] HTTP {r.status_code} ({attempt + 1}/{tries}) {r.text[:200].strip()}")
            if r.status_code in (401, 403):          # 키·권한 문제는 재시도해도 소용없음
                break
        except requests.RequestException as e:
            conn_err = isinstance(e, (requests.ConnectionError, requests.ConnectTimeout))
            log(f"  [{name}] 요청 오류({attempt + 1}/{tries}, {time.time() - t0:.0f}s): {type(e).__name__}")
        if attempt < tries - 1:
            time.sleep([2, 4, 8][min(attempt, 2)])
    log(f"  [{name}] {tries}번 모두 실패 → 이 항목은 빈 값으로 처리")
    if not conn_err:          # 504 등 서버가 바쁜 경우는 건너뛰고 계속 (접속 자체가 안 될 때만 중단)
        return ""
    FAILS += 1
    if FAILS >= 3:
        sys.exit("기상청 API허브에 연속으로 접속하지 못해 중단합니다. 잠시 후 다시 실행해 보세요.")
    return ""


def grid_obs(lat, lon, tm, obs, key, cache=False):
    """고해상도 격자 관측분석값 (특정지점 다중요소). tm 시각(30분 단위) 1개 값.
    cache=True: 지난 시각 값(일 신적설 등)은 바뀌지 않으므로 캐시에서 꺼내 씀."""
    t = tm.strftime("%Y%m%d%H%M")
    ck = f"{lat:.4f},{lon:.4f}|{t}|{','.join(obs)}"
    if cache and ck in CACHE["obs"]:
        return CACHE["obs"][ck]
    txt = hub_get("typ01/url/sfc_nc_var.php", {
        "tm1": t, "tm2": t, "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
        "obs": ",".join(obs), "itv": 30, "help": 0, "disp": 1}, key)
    vals = {o: (find_values(txt, o) or [None])[-1] for o in obs}
    out = {o: (None if v is None else max(0.0, v)) for o, v in vals.items()}   # 분석값의 -0.1 같은 음수는 0으로
    if cache and all(v is not None for v in out.values()):
        with CACHE_LOCK:
            CACHE["obs"][ck] = out
    return out


def sno_to_cm(v, step_h=1):
    """눈의양(SNO) 문자열 → cm.
    정량값: '적설없음', '1.0cm 미만', '1.0~5.0cm', '5.0~10.0cm', '10.0~20.0cm', '20.0cm 이상', '2.5cm'
    연장기간(3시간 간격) 정성코드: 0 강설없음, 1 시간당 1cm 미만, 2 시간당 1cm 이상"""
    v = str(v).replace(" ", "")
    if not v or "없음" in v or v in ("-", "null"):
        return 0.0
    if v in ("0", "1", "2"):                       # 정성코드 → 보수적으로 환산
        return {"0": 0.0, "1": 0.5, "2": 1.0}[v] * step_h
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", v)]
    if not nums:
        return 0.0
    if "미만" in v:
        return nums[0] / 2
    if len(nums) >= 2:                               # 범위 → 중간값
        return (nums[0] + nums[1]) / 2
    return nums[0]


def pcp_to_mm(v):
    v = str(v).replace(" ", "")
    if not v or "없음" in v or v in ("-", "null", "0"):
        return 0.0
    if v in ("1", "2", "3"):                         # 연장기간 정성코드
        return {"1": 1.0, "2": 5.0, "3": 15.0}[v]
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", v)]
    if not nums:
        return 0.0
    if "미만" in v:
        return nums[0] / 2
    return sum(nums[:2]) / min(len(nums), 2)


FC_BASES = []      # 실제로 받아온 예보의 발표시각(화면 표시용)


def latest_base(now, lag_min=10):
    """발표 후 약 10분 뒤부터 API로 받을 수 있음.
    (실행을 매 발표시각 15분에 하는 이유: 07분에 돌리면 그 시각 발표분이 아직 없어 3시간 전 발표분을 씀)"""
    base = now - timedelta(minutes=lag_min)
    while base.hour not in (2, 5, 8, 11, 14, 17, 20, 23):
        base -= timedelta(hours=1)
    return base


def items_from(txt):
    """응답에서 category/fcstDate/fcstTime/fcstValue 가진 항목만 추출 (JSON 구조가 달라도 동작)."""
    try:
        data = json.loads(txt)
    except ValueError:
        return []
    out, stack = [], [data]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if "category" in x and "fcstValue" in x:
                out.append(x)
            else:
                stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return out


def to_series(items):
    """{category: {datetime: value}}"""
    ser = {}
    for it in items:
        d = it.get("fcstDate") or it.get("fcstBase")
        t = it.get("fcstTime")
        if not d or not t:
            continue
        when = datetime.strptime(f"{d}{int(t):04d}", "%Y%m%d%H%M").replace(tzinfo=KST)
        ser.setdefault(it["category"], {})[when] = it["fcstValue"]
    return ser


def fetch_fcst(label, path, params_of, key):
    """가장 최근 발표분을 먼저 시도하고, 아직 안 나왔으면 직전 발표분(3시간 전)으로."""
    base = latest_base(datetime.now(KST))
    txt = ""
    for b in (base, base - timedelta(hours=3)):
        txt = hub_get(path, params_of(b), key)
        items = items_from(txt)
        if items:
            FC_BASES.append(b.replace(minute=0, second=0, microsecond=0))
            return to_series(items)
        log(f"  {label}: {b:%m-%d %H}시 발표분 없음, 직전 발표분 시도")
    log(f"  {label} 응답 해석 실패 {txt[:200]}")
    return to_series([])


def mountain_fcst(mnum, now, key):
    """산악예보(정상 기준). 응답: 일자별 SNO/PCP/TMP/POP/SKY …"""
    return fetch_fcst(f"산악예보 mountainNum={mnum}", "typ08/getMountainWeather",
                      lambda b: {"mountainNum": mnum, "base_date": b.strftime("%Y%m%d"),
                                 "base_time": f"{b.hour:02d}00"}, key)


def vilage_fcst(nx, ny, now, key):
    """단기예보(5km 격자) — 산악예보 지점이 없을 때 대안."""
    return fetch_fcst(f"단기예보 nx={nx},ny={ny}",
                      "typ02/openApi/VilageFcstInfoService_2.0/getVilageFcst",
                      lambda b: {"pageNo": 1, "numOfRows": 2000, "dataType": "JSON",
                                 "base_date": b.strftime("%Y%m%d"), "base_time": f"{b.hour:02d}00",
                                 "nx": nx, "ny": ny}, key)


def summarize_fcst(ser, now, day):
    """예보 시계열 → 목표일(day) 판단용 요약. 1시간/3시간 간격이 섞여 있어도 시간 가중치로 계산."""
    sno, pcp = ser.get("SNO", {}), ser.get("PCP", {})
    tmp, pop = ser.get("TMP", {}), ser.get("POP", {})
    d9 = datetime(day.year, day.month, day.day, 9, tzinfo=KST)
    h0, h1 = d9 - timedelta(hours=1), d9 + timedelta(hours=6)          # 산행시간 08~15시
    times = sorted(set(sno) | set(pcp) | set(tmp))
    w = {t: (min(3, max(1, int((t - times[i - 1]).total_seconds() // 3600))) if i else 1)
         for i, t in enumerate(times)}                                  # 각 시점이 대표하는 시간 수
    t_of = lambda t: num(tmp.get(t))
    is_snow = lambda t: sno_to_cm(sno.get(t, 0)) > 0 or (pcp_to_mm(pcp.get(t, 0)) > 0 and t_of(t) is not None and t_of(t) <= 0)
    snow_to_d9 = sum(sno_to_cm(sno[t], w[t]) for t in sno if now < t <= d9)
    hike_t = [t for t in times if h0 <= t <= h1]
    hike_snow_h = sum(w[t] for t in hike_t if is_snow(t))
    hike_tmp = [t_of(t) for t in hike_t if t_of(t) is not None]
    melt_h = sum(w[t] for t in tmp if now < t <= d9 and (t_of(t) if t_of(t) is not None else -99) > 0)
    rain_mm = sum(pcp_to_mm(pcp[t]) * (w[t] if str(pcp[t]).strip() in ("1", "2", "3") else 1)
                  for t in pcp if now < t <= d9 and (t_of(t) if t_of(t) is not None else -99) > 0)
    return {
        "fcst_new_snow_to_day09_cm": round(snow_to_d9, 1),
        "hike_snow_hours": hike_snow_h,
        "hike_max_pop": max((num(pop.get(t)) or 0 for t in hike_t), default=0),
        "hike_tmp_min": min(hike_tmp) if hike_tmp else None,
        "hike_tmp_max": max(hike_tmp) if hike_tmp else None,
        "melt_hours_to_day09": melt_h,
        "rain_mm_to_day09": round(rain_mm, 1),
    }


def target_hike_day(now):
    """산행 목표일: 평일엔 이번 토요일, 토요일 11시부터는 일요일,
    일요일 11시부터는 다음 주 토요일."""
    wd, h = now.weekday(), now.hour             # 월=0 … 토=5, 일=6
    if wd == 5:
        add = 0 if h < 11 else 1
    elif wd == 6:
        add = 0 if h < 11 else 6
    else:
        add = 5 - wd
    return (now + timedelta(days=add)).date()


def last_full_day(ser):
    """산행시간(15시)까지 예보가 있는 가장 늦은 날짜."""
    last = max((t for c in ser.values() for t in c), default=None)
    if last is None:
        return None
    d = last.date()
    return d if last.hour >= 15 else d - timedelta(days=1)



# ---------------------------------------------------------------- 설경 사진 (위키미디어 공용, 자유 라이선스)
COMMONS = "https://commons.wikimedia.org/w/api.php"
OK_LIC = ("CC BY", "CC0", "PUBLIC DOMAIN", "PD")


def _strip_tags(v):
    return re.sub(r"<[^>]+>", "", v or "").strip()


def commons_photo(name, photo_q):
    """photo_q: '검색어1|검색어2|…' (영문 로마자 등). 첫 검색어의 핵심 단어가 제목에 들어간
    자유 라이선스 jpg를 고른다. 실패하면 None(사진 없이 표시)."""
    queries = [q.strip() for q in (photo_q or "").split("|") if q.strip()]
    if not queries:
        return None
    rom = queries[0].split()[0].lower()                    # 첫 검색어의 첫 단어 = 산 이름(로마자)
    keys = [rom, name.split("(")[0]] + ([rom[:-3]] if rom.endswith("san") and len(rom) >= 8 else [])
    keys += [q.split()[0] for q in queries if re.search("[가-힣]", q)]  # 한글 검색어(노고단·곰배령 등)
    for q in queries:
        try:
            r = requests.get(COMMONS, timeout=(5, 10), headers={"User-Agent": f"snow-report/1.0 (github.com/{_REPO})"},
                             params={"action": "query", "format": "json", "generator": "search",
                                     "gsrsearch": f"{q} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 12,
                                     "prop": "imageinfo", "iiprop": "url|extmetadata|size", "iiurlwidth": 360,
                                     "iiextmetadatafilter": "LicenseShortName|Artist"})
            pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda x: x.get("index", 99))
        except Exception as e:
            log(f"  사진 검색 실패 {name}: {e}")
            continue
        for pg in pages:
            t = pg.get("title", "")
            ii = (pg.get("imageinfo") or [{}])[0]
            md = ii.get("extmetadata", {})
            lic = (md.get("LicenseShortName", {}).get("value") or "").upper()
            if not t.lower().endswith((".jpg", ".jpeg")) or not lic.startswith(OK_LIC):
                continue
            if not any(k.lower() in t.lower() for k in keys):
                continue
            if re.search(r"map|지도|logo|sign|안내", t, re.I):
                continue
            return {"src": ii.get("thumburl") or ii.get("url"), "page": ii.get("descriptionurl"),
                    "credit": _strip_tags(md.get("Artist", {}).get("value"))[:40],
                    "lic": md.get("LicenseShortName", {}).get("value", "")}
    log(f"  사진 없음: {name}")
    return None


# ---------------------------------------------------------------- 설경 사진 (한국관광공사 관광사진, 공공누리 1유형)
TOUR = "https://apis.data.go.kr/B551011/PhotoGalleryService1"
WINTER_WORDS = ("설경", "눈꽃", "상고대", "겨울", "설산", "눈")


TOUR_DOWN = False    # 관광공사 API가 응답하지 않으면 이번 실행에서는 더 묻지 않음


def _load_chosen():
    """사진 고르기 페이지에서 직접 고른 사진 (photos.json). 하나라도 있으면 자동 검색은 쓰지 않음."""
    try:
        return json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos.json"), encoding="utf-8")) or {}
    except Exception:
        return {}


CHOSEN = _load_chosen()


def photo_html(ph, cls="ph", empty="❄"):
    """카드 사진: 링크 없이 이미지만, 아래에 작게 출처(자유 이용 사진의 표시 조건)."""
    if not ph:
        return f'<div class="{cls} none">{empty}</div>'
    cr = " · ".join(x for x in (ph.get("credit"), ph.get("lic")) if x)
    return (f'<div class="{cls}"><img src="{H.escape(ph["src"])}" alt="" loading="lazy">'
            + (f'<span>© {H.escape(cr)}</span>' if cr else "") + '</div>')


# 사진 클릭 → 확대 보기 (원본 크기까지, 비율 유지). 바깥이나 ✕ 누르거나 Esc로 닫기
LIGHTBOX = """<div id="lb" onclick="this.style.display='none'"><span>✕</span><img alt=""></div>
<style>.ph img{cursor:zoom-in}#lb{display:none;position:fixed;inset:0;z-index:99;background:#000d;align-items:center;justify-content:center;cursor:zoom-out}#lb img{max-width:96vw;max-height:94vh;width:auto;height:auto;border-radius:6px;box-shadow:0 4px 20px #000a}#lb span{position:absolute;top:10px;right:16px;color:#fff;font-size:28px}</style>
<script>document.addEventListener('click',function(e){var t=e.target;if(t.tagName==='IMG'&&t.closest('.ph')){var b=document.getElementById('lb');b.querySelector('img').src=t.src;b.style.display='flex';}});document.addEventListener('keydown',function(e){if(e.key==='Escape')document.getElementById('lb').style.display='none';});</script>"""

IMG_DIR = None        # 고른 사진을 저장할 폴더 (site/img). main에서 정함


def _slug(key):
    import hashlib
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:10]


def find_file(path):
    """파일이 그 자리에 없으면 같은 폴더 아래(하위 폴더 포함)에서 같은 이름을 찾음.
    (GitHub에 폴더째 올리다 photos/photos/ 처럼 한 겹 더 들어가도 찾도록)"""
    if os.path.exists(path):
        return path
    top = os.path.dirname(path)
    base = os.path.basename(path)
    while top and not os.path.isdir(top):
        top = os.path.dirname(top)
    for root, _, files in os.walk(top or "."):
        if base in files:
            return os.path.join(root, base)
    return None


def chosen_photo(key):
    """photos.json 에서 고른 사진을 웹페이지 폴더(img/)에 저장하고 그 경로를 돌려줌.
    항목 형식: {"file": "위키미디어 파일 이름"} 또는 {"local": "photos/내사진.jpg", "credit": "직접 촬영"}"""
    e = CHOSEN.get(key)
    if not e or not IMG_DIR:
        return None
    os.makedirs(IMG_DIR, exist_ok=True)
    fn = f"{_slug(key)}.jpg"
    dst = os.path.join(IMG_DIR, fn)
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        if e.get("local"):                                   # 직접 올린 사진
            import shutil
            src = find_file(os.path.join(here, e["local"]))
            if not src:
                raise FileNotFoundError(e["local"])
            shutil.copyfile(src, dst)
            return {"src": f"img/{fn}", "credit": e.get("credit", ""), "lic": e.get("lic", "")}
        title = e["file"]
        ck = "commons|" + title
        meta = CACHE["photo"].get(ck)
        if not meta:                                         # 촬영자·라이선스는 한 번만 조회해 캐시
            r = requests.get(COMMONS, timeout=(5, 15), headers={"User-Agent": f"snow-report/1.0 (github.com/{_REPO})"},
                             params={"action": "query", "format": "json", "titles": "File:" + title,
                                     "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 900,
                                     "iiextmetadatafilter": "LicenseShortName|Artist"})
            pg = next(iter(r.json()["query"]["pages"].values()))
            ii = pg["imageinfo"][0]
            md = ii.get("extmetadata", {})
            meta = {"url": ii.get("thumburl") or ii["url"],
                    "credit": _strip_tags(md.get("Artist", {}).get("value"))[:40],
                    "lic": md.get("LicenseShortName", {}).get("value", "")}
            with CACHE_LOCK:
                CACHE["photo"][ck] = meta
        img = requests.get(meta["url"], timeout=(5, 30), headers={"User-Agent": "snow-report/1.0"})
        img.raise_for_status()
        with open(dst, "wb") as f:
            f.write(img.content)
        return {"src": f"img/{fn}", "credit": meta["credit"], "lic": meta["lic"]}
    except Exception as ex:
        log(f"  고른 사진 저장 실패 {key}: {str(ex)[:120]}")
        return None


def photo_for(name, photo_q=None, must=None, key=None):
    if (key or name) in CHOSEN:                      # 직접 고른 사진이 있으면 그것만
        return chosen_photo(key or name)
    """산 사진 1장 (캐시 → 관광공사 → 위키미디어). 찾은 사진은 14일, 못 찾은 결과는 1일 동안 다시 찾지 않음."""
    ck = name + ("|" + ",".join(must) if must else "")
    today = datetime.now(KST).date()
    c = CACHE["photo"].get(ck)
    if c:
        age = (today - datetime.strptime(c["t"], "%Y-%m-%d").date()).days
        if age < (14 if c["p"] else 1):
            return c["p"]
    p = tour_photo(name, os.environ.get("TOUR_API_KEY"), must) or commons_photo(name, photo_q)
    if p or not TOUR_DOWN:                 # 관광공사가 먹통이라 못 찾은 경우는 저장하지 않음
        with CACHE_LOCK:
            CACHE["photo"][ck] = {"p": p, "t": f"{today}"}
    return p if p else (c["p"] if c else None)


def tour_photo(name, key, must=None):
    """관광공사 관광사진 갤러리에서 산 이름으로 검색 → 겨울(12~2월)·설경 사진 우선.
    must: 사진 설명에 이 중 하나가 들어 있어야 함(같은 이름의 다른 지역 산 제외)."""
    global TOUR_DOWN
    if not key or TOUR_DOWN:
        return None
    from urllib.parse import unquote
    key = unquote(key.strip())            # 인코딩 키를 넣어도 동작하게
    base = name.split("(")[0]
    if "(" in name:                       # 지리산(노고단) → 노고단, 점봉산(곰배령) → 곰배령
        inner = name[name.index("(") + 1:-1]
        if inner not in ("천안",):
            base = inner
    best = None
    for kw in (f"{base} 설경", f"{base} 겨울", base):
        items = []
        for op in ("gallerySearchList1", "gallerySearchList2"):
            try:
                r = requests.get(f"{TOUR}/{op}", timeout=(5, 10), params={
                    "serviceKey": key, "MobileOS": "ETC", "MobileApp": "snowreport", "_type": "json",
                    "keyword": kw, "numOfRows": 50, "pageNo": 1, "arrange": "C"})
                if r.status_code == 404:
                    continue
                body = (r.json().get("response") or {}).get("body") or {}
                it = (body.get("items") or {}) if isinstance(body.get("items"), dict) else {}
                items = it.get("item") or []
                if isinstance(items, dict):
                    items = [items]
                break
            except Exception as e:
                log(f"  관광사진 검색 실패 {name} ({kw}): {str(e)[:120]}")
                if isinstance(e, requests.Timeout):
                    TOUR_DOWN = True
                    log("  관광공사 API 응답이 없어 이번 실행에서는 관광사진을 건너뜀")
                    return None
                items = []
        for x in items:
            url = (x.get("galWebImageUrl") or "").replace("http://", "https://")
            txt = " ".join(str(x.get(k) or "") for k in ("galTitle", "galSearchKeyword", "galPhotographyLocation"))
            if not url or base not in txt:
                continue
            if base == "광덕산" and not any(w in txt for w in ("천안", "아산")):
                continue                  # 강원·경기 광덕산 사진 제외
            if must and not any(w in txt for w in must):
                continue
            month = str(x.get("galPhotographyMonth") or "")[-2:]
            score = (2 if month in ("12", "01", "02") else 0) + (2 if any(w in txt for w in WINTER_WORDS) else 0)
            if best is None or score > best[0]:
                best = (score, {"src": url, "page": "https://conlab.visitkorea.or.kr",
                                "credit": f"한국관광공사 {x.get('galPhotographer') or ''}".strip()[:40],
                                "lic": "공공누리 1유형"})
        if best and best[0] >= 2:
            break
    return best[1] if best else None



BASE_CSS = """body{font-family:-apple-system,"Malgun Gothic",sans-serif;margin:0;padding:12px;background:#f4f6f8;color:#222}
h1{font-size:18px;margin:4px 0}.meta{font-size:12px;color:#666;margin-bottom:10px}
.card{background:#fff;border-radius:10px;margin:8px 0;box-shadow:0 1px 2px #0002;display:flex;overflow:hidden}.tx{flex:1;min-width:0;padding:10px 12px}.ph{flex:0 0 110px;position:relative;background:#dfe8f1;display:block}.ph img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.ph span{position:absolute;left:0;right:0;bottom:0;font-size:9px;font-weight:normal;color:#fff;background:#0007;padding:2px 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ph.none{display:flex;align-items:center;justify-content:center;font-size:28px;color:#fff}@media(min-width:600px){.ph{flex-basis:180px}}
.card.top{box-shadow:0 0 0 2px #2a7de1}.hd{display:flex;justify-content:space-between;font-size:16px}
.sc{color:#2a7de1;font-weight:bold}.bd{font-size:13px;line-height:1.6;margin-top:4px}a{color:#2a7de1;text-decoration:none;font-weight:bold}.w{color:#c0392b}.info{background:#fff;border-radius:10px;padding:8px 12px;font-size:13px;line-height:1.7;margin-bottom:6px}.s{color:#888;font-size:11px}.rt{color:#2e5d3a;font-size:12px}.tabs{display:flex;gap:6px;margin:0 0 10px;position:sticky;top:0;background:#f4f6f8;padding:6px 0;z-index:5}.tabs a{flex:1;text-align:center;padding:8px 4px;border-radius:8px;background:#fff;color:#555;font-size:13px;box-shadow:0 1px 2px #0002}.tabs a.on{background:#2a7de1;color:#fff}"""

TABS = [("snow", "index.html", "❄ 설산"), ("hamyang", "hamyang.html", "⛰ 오르GO 함양"),
        ("yeongnam", "yeongnam.html", "🏔 영남알프스")]


def nav_html(active):
    """페이지 위쪽 탭(설산 / 오르GO 함양 / 영남알프스)."""
    return '<nav class="tabs">' + "".join(
        f'<a href="{href}"{" class=\"on\"" if key == active else ""}>{label}</a>'
        for key, href, label in TABS) + "</nav>"


def next_slot(now, schedule):
    slots = sorted(tuple(map(int, x.split(":"))) for x in schedule.split(","))
    cands = [datetime(now.year, now.month, now.day, h, m, tzinfo=KST) + timedelta(days=d)
             for d in (0, 1) for h, m in slots]
    return min(t for t in cands if t > now)


# ---------------------------------------------------------------- 모바일용 출력
def write_html(path, rank, day, wk, obs_tm, note, now, fc_base=None, next_up=None):
    import html as H
    def g(v):
        return "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))
    rows = []
    for i, x in enumerate(rank, 1):
        tr = "-" if x["hike_tmp_min"] is None else f"{x['hike_tmp_min']:.0f}~{x['hike_tmp_max']:.0f}℃"
        snowing = (f"<span class=\"w\">산행 중 눈 {x['hike_snow_hours']}h</span>" if x["hike_snow_hours"]
                   else "☀ 산행 중 눈 없음")
        warn = []
        if x["melt_hours_to_day09"]:
            warn.append(f"영상 {x['melt_hours_to_day09']}h")
        if x["rain_mm_to_day09"]:
            warn.append(f"비 {x['rain_mm_to_day09']}mm")
        cls = "top" if i <= 3 else ""
        links = []
        if x.get("mtId"):
            links.append(f'<a href="https://www.weather.go.kr/w/forecast/life/mountain.do?mtId={x["mtId"]}" '
                         f'target="_blank" rel="noopener">기상청 산악예보 보기 ›</a>')
        if x.get("extra_url"):
            links.append(f'<a href="{H.escape(x["extra_url"])}" target="_blank" rel="noopener">'
                         f'{H.escape(x.get("extra_label") or "바로가기")} ›</a>')
        link = ""
        if x.get("route"):
            link += f'<br><span class="rt">🥾 {H.escape(x["route"])}</span>'
        link += ("<br>" + " · ".join(links)) if links else ""
        if x.get("extra_note"):
            link += f'<br><span class="s">{H.escape(x["extra_note"])}</span>'
        ph = x.get("photo")
        pic = photo_html(ph)
        rows.append(f"""<div class="card {cls}">{pic}<div class="tx"><div class="hd"><b>{i}. {H.escape(x['name'])}{'' if x['obs_ok'] == 'Y' else ' <small class="w">관측없음</small>'}</b><span class="sc">{g(x['score'])}점</span></div>
<div class="bd">지금 <b>{g(x['snow_depth_now_cm'])}cm</b> · 7일 신적설 {g(x['new_snow_7d_cm'])}cm<br>
{wk}09시 예상 <b>{g(x['est_depth_day09_cm'])}cm</b> (예보 +{g(x['fcst_new_snow_to_day09_cm'])}) {snowing}<br>
산행기온 {tr} · 강수확률 {int(x['hike_max_pop'] or 0)}%{' · <span class="w">' + ', '.join(warn) + '</span>' if warn else ''}{link}</div></div></div>""")
    doc = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>설산 추천 {day}</title>
<style>{BASE_CSS}</style></head><body>
{nav_html("snow")}
<h1>❄ {day}({wk}) 설산 추천</h1>
<div class="info">
<div>📡 예보: <b>{fc_base:%m월 %d일 %H시}</b> 발표분</div>
<div>❄ 적설: <b>{obs_tm:%m월 %d일 %H:%M}</b> 분석값</div>
<div>🕒 페이지 생성: {now:%m월 %d일 %H:%M}</div>
<div>⏭ 다음 업데이트: <b>{next_up + timedelta(minutes=20):%m월 %d일 %H:%M}~{next_up + timedelta(minutes=30):%H:%M}</b> 확인 가능 <span class="s">({next_up:%H:%M} 실행 시작 · 자료 수집과 페이지 생성에 20~30분 걸려요)</span></div>
</div>{('<div class="meta">' + H.escape(note.strip()) + '</div>') if note else ''}
{''.join(rows)}
<div class="meta">점수 = 09시 예상적설 − 녹을 위험(쌓인 눈 이하) + 0.3×7일신적설 − 1.5×산행중 눈시간 − 0.05×강수확률(%)</div>
{LIGHTBOX}
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def send_telegram(rank, day, wk, obs_tm, note):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        log("텔레그램 설정(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)이 없어 전송을 건너뜀")
        return
    g = lambda v: "-" if v is None else (f"{v:.0f}" if isinstance(v, float) else str(v))
    lines = [f"❄ {day}({wk}) 설산 추천 (관측 {obs_tm:%m-%d %H:%M})"]
    if note:
        lines.append(note.strip())
    for i, x in enumerate(rank[:10], 1):
        s = f"{i}. {x['name']} {g(x['score'])}점 | 지금 {g(x['snow_depth_now_cm'])}cm → {wk}09 {g(x['est_depth_day09_cm'])}cm"
        if x["hike_snow_hours"]:
            s += f" | 산행중 눈 {x['hike_snow_hours']}h"
        if x["rain_mm_to_day09"]:
            s += f" | 비 {x['rain_mm_to_day09']}mm"
        lines.append(s)
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": "\n".join(lines)}, timeout=30)
        log("텔레그램 전송 " + ("완료" if r.ok else f"실패: {r.text[:200]}"))
    except requests.RequestException as e:
        log(f"텔레그램 전송 실패: {e}")


def telegram_setup():
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        sys.exit("TELEGRAM_BOT_TOKEN 이 없습니다. set_telegram.bat 으로 먼저 토큰을 저장하세요.")
    r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=30).json()
    ids = {u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u}
    if not ids:
        print("대화 기록이 없습니다. 휴대폰 텔레그램에서 만든 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
    for i in ids:
        print(f"chat id: {i}")


# ---------------------------------------------------------------- 메인
def main():
    global DEBUG, LOGFILE, IMG_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="kma_mountains_coords.csv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--snap", action="store_true", help="좌표를 주변 최고점으로 보정해 *_snapped.csv 저장")
    ap.add_argument("--snap-all", action="store_true", help="OK 표시된 산까지 모두 보정")
    ap.add_argument("--only", help="쉼표로 구분한 산 이름만 처리")
    ap.add_argument("--debug", action="store_true", help="API 응답 원문 일부를 snow_debug.txt 에 기록")
    ap.add_argument("--skip-obs", action="store_true", help="적설 관측(500m 격자) 조회 생략, 예보만")
    ap.add_argument("--day", default="auto",
                    help="목표일: auto(토요일 예보가 있으면 토요일, 없으면 예보가 있는 마지막 날), sat, 또는 YYYY-MM-DD")
    ap.add_argument("--report-dir", help="모바일용 HTML(snow_latest.html + 날짜별 사본)을 저장할 폴더")
    ap.add_argument("--telegram", action="store_true", help="상위 10곳 요약을 텔레그램으로 전송")
    ap.add_argument("--schedule", default="05:17,08:17,11:17,14:17,17:17,20:17,23:17",
                    help="자동 실행 시각(KST). 웹페이지의 '다음 업데이트' 표시에 사용")
    ap.add_argument("--telegram-hours", help="이 시각(KST, 쉼표 구분)에 실행될 때만 텔레그램 전송. 예: 17,18")
    ap.add_argument("--telegram-setup", action="store_true", help="텔레그램 chat id 확인")
    a = ap.parse_args()
    DEBUG = a.debug
    if a.telegram_setup:
        telegram_setup()
        return
    if a.debug:
        LOGFILE = "snow_debug.txt"
        open(LOGFILE, "w", encoding="utf-8").close()
        log(f"디버그 기록 시작: {datetime.now(KST):%Y-%m-%d %H:%M}")

    with open(a.csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if a.only:
        names = {s.strip() for s in a.only.split(",")}
        rows = [r for r in rows if r["name"] in names]

    if a.snap:
        for r in rows:
            if not r["lat"] or (r["coord_check"] == "OK" and not a.snap_all):
                continue
            lat, lon, e = snap_to_summit(float(r["lat"]), float(r["lon"]))
            log(f"{r['name']}: {r['lat']},{r['lon']} → {lat:.5f},{lon:.5f} (DEM {e:.0f} m)")
            r.update(lat=f"{lat:.5f}", lon=f"{lon:.5f}", dem_elev_m=int(e),
                     note=(r["note"] + " / 최고점 보정").strip(" /"))
        out = a.out or a.csv.replace(".csv", "_snapped.csv")
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        log(f"저장: {out}")
        return

    key = os.environ.get("KMA_AUTH_KEY")
    if not key:
        sys.exit("환경변수 KMA_AUTH_KEY 가 없습니다.")

    now = datetime.now(KST)
    obs_tm = (now - timedelta(minutes=40)).replace(second=0, microsecond=0)
    obs_tm = obs_tm.replace(minute=0 if obs_tm.minute < 30 else 30)
    sat = target_hike_day(now)
    midnights = [datetime(now.year, now.month, now.day, tzinfo=KST) - timedelta(days=i) for i in range(7)]

    IMG_DIR = os.path.join(a.report_dir, "img") if a.report_dir else None
    # 1단계: 산별로 관측·예보 수집 (여러 산을 동시에)
    load_cache(os.path.join(a.report_dir, "cache.json") if a.report_dir and os.path.exists(os.path.join(a.report_dir, "cache.json")) else None)
    t_start = time.time()

    def collect(i_r):
        i, r = i_r
        if not r["lat"]:
            log(f"[{i}/{len(rows)}] {r['name']}: 좌표 없음, 건너뜀")
            return None
        lat, lon = float(r["lat"]), float(r["lon"])
        log(f"[{i}/{len(rows)}] {r['name']}")
        # ① 관측: 지금 쌓인 눈 + 최근 7일 신적설 (500m 격자 분석, 지난 날짜는 캐시)
        if a.skip_obs:
            cur, daily = None, [None] * 7
        else:
            cur = grid_obs(lat, lon, obs_tm, ["sd_tot"], key)["sd_tot"]
            daily = [grid_obs(lat, lon, m, ["sd_24h"], key, cache=True)["sd_24h"] for m in midnights]
        week = sum(v for v in daily if v is not None) if any(v is not None for v in daily) else None
        # ② 예보: 산악예보(정상) 우선, 없으면 단기예보
        mnum = r.get("mountainNum", "").strip()
        if mnum:
            ser, src = mountain_fcst(mnum, now, key), f"산악예보#{mnum}"
        else:
            nx, ny = latlon_to_grid(lat, lon)
            ser, src = vilage_fcst(nx, ny, now, key), f"단기예보({nx},{ny})"
        return (r, mnum, src, cur, week, ser)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        collected = [c for c in ex.map(collect, enumerate(rows, 1)) if c]
        photos = dict(zip([c[0]["name"] for c in collected],
                          ex.map(lambda c: photo_for(c[0]["name"], c[0].get("photo_q")), collected)))
    log(f"자료 수집 {time.time() - t_start:.0f}초")

    # 2단계: 목표일 결정 (모든 산에 공통 적용)
    # 산악예보가 있는 산 기준으로 목표일을 정함(단기예보 대체 산이 범위를 줄이지 않게)
    base_set = [c for c in collected if c[1]] or collected
    covers = [d for d in (last_full_day(c[5]) for c in base_set) if d]
    max_day = min(covers) if covers else now.date()
    if a.day == "sat":
        day = sat
    elif a.day == "auto":
        day = sat if sat <= max_day else max_day
    else:
        day = datetime.strptime(a.day, "%Y-%m-%d").date()
    if day > max_day:
        log(f"※ {day} 은(는) 아직 예보 범위 밖입니다(산행시간까지 예보가 있는 마지막 날: {max_day}).")
    if day < now.date():
        day = now.date()
    wk = "월화수목금토일"[day.weekday()]
    note = "" if day == sat else f"  ※ {'월화수목금토일'[sat.weekday()]}요일({sat}) 예보 전이라 예보가 있는 마지막 날 기준"

    # 3단계: 점수 계산
    results = []
    for r, mnum, src, cur, week, ser in collected:
        f = summarize_fcst(ser, now, day)
        est = None if cur is None else max(0.0, cur + f["fcst_new_snow_to_day09_cm"])
        # 점수: 목표일 아침 예상 적설 − 녹을 위험 − 산행 중 눈·강수확률 감점
        # (적설 관측을 못 받은 산은 예보만으로 계산하고 '관측없음' 표시)
        base = est if est is not None else f["fcst_new_snow_to_day09_cm"]
        # 녹을 위험 감점은 '녹을 눈'이 있을 때만, 그리고 그 양을 넘지 않게
        penalty = min(base, 0.5 * f["melt_hours_to_day09"] + 2 * f["rain_mm_to_day09"])
        # 산행 당일(08~15시)에는 눈·비가 안 오는 날이 좋음 → 눈 오는 시간·강수확률은 감점
        score = (base - penalty + 0.3 * (week or 0)
                 - 1.5 * f["hike_snow_hours"] - 0.05 * (f["hike_max_pop"] or 0))
        results.append({
            "target_day": f"{day}({wk})", "name": r["name"], "mountainNum": mnum, "fcst_source": src,
            "mtId": (r.get("mtId") or "").strip(),
            "route": (r.get("route") or "").strip(),
            "photo": photos.get(r["name"]),
            "extra_url": (r.get("extra_url") or "").strip(),
            "extra_label": (r.get("extra_label") or "").strip(),
            "extra_note": (r.get("extra_note") or "").strip(),
            "snow_depth_now_cm": cur,
            "new_snow_7d_cm": None if week is None else round(week, 1),
            **f,
            "est_depth_day09_cm": None if est is None else round(est, 1),
            "score": round(score, 1),
            "obs_ok": "Y" if cur is not None else "관측없음",
            "obs_time": obs_tm.strftime("%Y-%m-%d %H:%M"),
        })

    if not results:
        sys.exit("결과가 없습니다. snow_debug.txt 를 확인하세요.")
    out = a.out or f"snow_{now:%Y%m%d_%H%M}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=[k for k in results[0] if k != "photo"], extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    rank = sorted(results, key=lambda x: x["score"] if x["score"] is not None else -999, reverse=True)
    print(f"\n{day}({wk}) 추천 순위  (예보 {(min(FC_BASES) if FC_BASES else latest_base(now)):%m-%d %H}시 발표 · 적설 {obs_tm:%m-%d %H:%M} 분석){note}")
    hdr = ["산", "현재적설", "7일신적설", f"{wk}09까지", f"{wk}09예상", "산행중눈h", "산행기온", "녹는h", "비mm", "점수"]
    print("  ".join(hdr))
    g = lambda v: "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))
    for x in rank:
        tr = "-" if x["hike_tmp_min"] is None else f"{x['hike_tmp_min']:.0f}~{x['hike_tmp_max']:.0f}"
        print("  ".join([x["name"] + ("" if x["obs_ok"] == "Y" else "(관측없음)"), g(x["snow_depth_now_cm"]), g(x["new_snow_7d_cm"]),
                         g(x["fcst_new_snow_to_day09_cm"]), g(x["est_depth_day09_cm"]),
                         g(x["hike_snow_hours"]), tr, g(x["melt_hours_to_day09"]),
                         g(x["rain_mm_to_day09"]), g(x["score"])]))
    print(f"\n전체 결과: {out}")
    if a.report_dir:
        fc_base = min(FC_BASES) if FC_BASES else latest_base(now)
        next_up = next_slot(now, a.schedule)
        os.makedirs(a.report_dir, exist_ok=True)
        for fn in ("snow_latest.html", f"snow_{now:%Y%m%d_%H%M}.html"):
            write_html(os.path.join(a.report_dir, fn), rank, day, wk, obs_tm, note, now, fc_base, next_up)
        print(f"모바일용 리포트: {os.path.join(a.report_dir, 'snow_latest.html')}")
        save_cache(os.path.join(a.report_dir, "cache.json"))
    if a.telegram:
        hours = {int(h) for h in a.telegram_hours.split(",")} if a.telegram_hours else None
        if hours is None or now.hour in hours:
            send_telegram(rank, day, wk, obs_tm, note)
        else:
            log(f"텔레그램은 {sorted(hours)}시 실행 때만 보냄 → 이번엔 생략")


if __name__ == "__main__":
    main()
