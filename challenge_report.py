#!/usr/bin/env python3
"""
완등 챌린지(오르GO 함양 15좌, 영남알프스 7봉) 날씨 점검 페이지.

- 각 산의 산행시간(08~15시) 비·눈·낙뢰 예보를 날짜별로 점검해 경고 표시
  · 비/눈: 기상청 산악예보(지점 있는 산) 또는 단기예보(5km 격자) — 약 4일
  · 낙뢰 : Open-Meteo 날씨코드 95/96/99(뇌우) — 7일 (기상청 단기예보에는 낙뢰 항목이 없음)
  · 기상청 예보가 없는 5~7일째는 Open-Meteo 강수 예보로 '참고' 표시
- 완등 기록: challenges.csv 의 done 칸에 날짜(예: 2027-03-13)를 적으면 완등으로 표시

사용: python challenge_report.py --csv challenges.csv --report-dir site
"""
import argparse, csv, html as H, json, os, re
from datetime import datetime, timedelta

import requests

import snow_report as S

KST = S.KST
GROUPS = {
    "hamyang": {"title": "오르GO 함양 15좌", "file": "hamyang.html", "tab": "hamyang",
                "info": "함양군 해발 1,000m 이상 15개 명산 완등 인증 (2025·2026년 기준 목록, 2027년 공지 확인 필요)",
                "link": ("오르GO 함양 안내", "https://www.hygn.go.kr/06421/06422.web"),
                "region": ("함양", "지리산", "덕유산", "산청", "거창")},
    "yeongnam": {"title": "영남알프스 7봉", "file": "yeongnam.html", "tab": "yeongnam",
                 "info": "영남알프스 완등 인증 (2025·2026년 7봉 기준, 2027년 대상·기간은 공지 확인 필요)",
                 "link": None,
                 "region": ("울산", "울주", "밀양", "양산", "청도", "영남알프스", "언양", "통도사", "표충사", "석남사")},
}
WD = "월화수목금토일"
THUNDER = (95, 96, 99)


def open_meteo(rows):
    """여러 산을 한 번에 조회 → {name: {date: {"thunder":h, "pop":%, "mm":x}}} (산행시간 08~15시)."""
    out = {r["name"]: {} for r in rows}
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=30, params={
            "latitude": ",".join(x["lat"] for x in rows), "longitude": ",".join(x["lon"] for x in rows),
            "hourly": "weather_code,precipitation,precipitation_probability",
            "timezone": "Asia/Seoul", "forecast_days": 10})
        data = r.json()
        if isinstance(data, dict):
            data = [data]
        for row, d in zip(rows, data):
            h = d.get("hourly") or {}
            for t, wc, mm, pop in zip(h.get("time", []), h.get("weather_code", []),
                                      h.get("precipitation", []), h.get("precipitation_probability", [])):
                dt = datetime.fromisoformat(t)
                if not 8 <= dt.hour <= 15:
                    continue
                day = out[row["name"]].setdefault(dt.date(), {"thunder": 0, "pop": 0, "mm": 0.0})
                day["thunder"] += 1 if (wc or 0) in THUNDER else 0
                day["pop"] = max(day["pop"], pop or 0)
                day["mm"] += mm or 0
    except Exception as e:
        S.log(f"Open-Meteo 조회 실패: {e}")
    return out


def kma_days(ser):
    """기상청 예보 → {date: {"rain_h", "snow_h", "pop", "mm"}} (산행시간 08~15시, 시간 가중)."""
    pcp, sno, pop = ser.get("PCP", {}), ser.get("SNO", {}), ser.get("POP", {})
    times = sorted(set(pcp) | set(sno) | set(pop))
    out = {}
    for i, t in enumerate(times):
        if not 8 <= t.hour <= 15:
            continue
        w = min(3, max(1, int((t - times[i - 1]).total_seconds() // 3600))) if i else 1
        d = out.setdefault(t.date(), {"rain_h": 0, "snow_h": 0, "pop": 0, "mm": 0.0})
        mm = S.pcp_to_mm(pcp.get(t)) if pcp.get(t) is not None else 0
        sn = S.sno_to_cm(sno.get(t), w) if sno.get(t) is not None else 0
        if mm and mm > 0:
            d["rain_h"] += w
            d["mm"] += mm
        if sn and sn > 0:
            d["snow_h"] += w
        d["pop"] = max(d["pop"], S.num(pop.get(t)) or 0)
    return out


def judge(k, o, ksrc=""):
    """하루 판정 → (아이콘, 등급, 설명). 등급: 0 좋음, 1 주의, 2 경고."""
    msgs, level = [], 0
    if not k and not o:
        return "❔", -1, "예보 전", ""
    if o and o["thunder"]:
        msgs.append(f"⚡ 낙뢰 가능 {o['thunder']}시간")
        level = 2
    if k:
        if k["rain_h"]:
            msgs.append(f"비 {k['rain_h']}시간·{k['mm']:.0f}mm")
            level = 2
        if k["snow_h"]:
            msgs.append(f"눈 {k['snow_h']}시간")
            level = max(level, 1)
        if not k["rain_h"] and k["pop"] >= 40:
            msgs.append(f"강수확률 {k['pop']:.0f}%")
            level = max(level, 1)
    elif o:                                    # 기상청 예보 범위 밖 → Open-Meteo 참고
        if o["pop"] >= 60 or o["mm"] >= 1:
            msgs.append(f"비 가능(참고) {o['pop']:.0f}%")
            level = max(level, 2 if o["mm"] >= 3 else 1)
        elif o["pop"] >= 40:
            msgs.append(f"강수확률(참고) {o['pop']:.0f}%")
            level = max(level, 1)
    icon = "⚡" if (o and o["thunder"]) else ("🌧" if level == 2 else ("🌥" if level == 1 else "☀"))
    base = "비 소식 없음" if k else "비 소식 없음(참고 예보)"
    # 출처: 비·눈은 기상청(없으면 Open-Meteo 참고), 낙뢰는 항상 Open-Meteo
    rain = ksrc if k else ("Open-Meteo(참고)" if o else "")
    return icon, level, " · ".join(msgs) or base, src_text([rain], bool(o))


def target_days(now):
    """이번 주 토요일(토 15시 이후면 다음 주) + 일요일."""
    d = now.date()
    sat = d + timedelta(days=(5 - d.weekday()) % 7)
    if sat == d and now.hour >= 15:
        sat += timedelta(days=7)
    return sat, sat + timedelta(days=1)


def mountain_days(r, key, now, om):
    """산 하나의 날짜별 판정 {date: (icon, level, msg)}."""
    mnum = r["mountainNum"].strip()
    if mnum:
        ser = S.mountain_fcst(mnum, now, key)
        label = H.escape(f'기상청 산악예보({(r.get("fc_point") or "").strip() or r["name"].split("(")[0]} 지점)')
        mt = (r.get("mtId") or "").strip()
        ksrc = (f'<a href="https://www.weather.go.kr/w/forecast/life/mountain.do?mtId={H.escape(mt)}" '
                f'target="_blank" rel="noopener">{label} ›</a>') if mt else label
    else:
        nx, ny = S.latlon_to_grid(float(r["lat"]), float(r["lon"]))
        ser = S.vilage_fcst(nx, ny, now, key)
        ksrc = "기상청 단기예보(5km 격자)"
    kd = kma_days(ser) if ser else {}
    days = [now.date() + timedelta(days=i) for i in range(10)]
    return {d: judge(kd.get(d), om.get(d), ksrc) for d in days}


def src_text(rains, thunder=True):
    """출처 문구: '비·눈: … · 낙뢰: Open-Meteo'."""
    rains = [x for x in rains if x]
    out = []
    if rains:
        out.append("비·눈: " + ", ".join(rains))
    if thunder:
        out.append("낙뢰: Open-Meteo")
    return " · ".join(out)


def merge(parts):
    """같은 날 여러 산 판정 → 코스 판정 (가장 나쁜 쪽 기준)."""
    lvl = max(p[1] for _, p in parts)
    if lvl < 0:
        return parts[0][1]
    if len({p[3] for _, p in parts}) <= 1:
        src = parts[0][1][3]
    else:                                            # 산마다 비·눈 출처가 다르면 산 이름과 함께
        rains = [f"{H.escape(n)} {p[3].split('비·눈: ')[1].split(' · 낙뢰')[0]}" for n, p in parts if "비·눈: " in p[3]]
        src = src_text(rains, any("낙뢰" in p[3] for _, p in parts))
    icon = "⚡" if any(p[0] == "⚡" for _, p in parts) else ("🌧" if lvl == 2 else ("🌥" if lvl == 1 else "☀"))
    bad = [f"{n} {p[2]}" for n, p in parts if p[1] > 0]
    msg = " / ".join(bad) if bad else parts[0][1][2]
    return icon, lvl, msg, src



LABEL = {-1: ("예보 전", "wn"), 0: ("산행 OK", "w0"), 1: ("주의", "w1"), 2: ("비", "w2")}


def tag(j):
    """판정 → (큰 글자 이름, css 클래스). 낙뢰는 따로 보라색."""
    icon, lvl = j[0], j[1]
    if icon == "⚡":
        return "낙뢰", "wt"
    return LABEL[lvl]


def pill(j):
    name, cls = tag(j)
    return f'<span class="pill {cls}">{j[0]} {name}</span>'


def snow_now(r, key, now):
    """지금 쌓인 눈(cm)과 최근 7일 신적설 합(cm) — 500m 격자 분석."""
    lat, lon = float(r["lat"]), float(r["lon"])
    obs_tm = (now - timedelta(minutes=40)).replace(second=0, microsecond=0)
    obs_tm = obs_tm.replace(minute=0 if obs_tm.minute < 30 else 30)
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cur = S.grid_obs(lat, lon, obs_tm, ["sd_tot"], key)["sd_tot"]
    week = [S.grid_obs(lat, lon, mid - timedelta(days=i), ["sd_24h"], key, cache=True)["sd_24h"] for i in range(7)]
    wk = sum(v for v in week if v is not None) if any(v is not None for v in week) else None
    return cur, wk


def plan_date(p):
    try:
        return datetime.strptime(p, "%Y-%m-%d").date()
    except ValueError:
        return None


def schedule_html(plan_rows, key, now):
    """영남알프스처럼 날짜 계획이 있는 경우: 1월(적설 많은 코스) · 지정일 · 4월(남은 코스)."""
    lines, flex = [], [p for p in plan_rows if p[0] == "jan_apr"]
    # 1월/4월 후보: 아직 안 간 코스 중 지금 눈이 더 많은 쪽을 1월로
    cand = []
    for _, label, todo, _ in flex:
        if not todo:
            lines.append(f"<div>✅ {H.escape(label)} 완등</div>")
            continue
        st = [(r["name"],) + snow_now(r, key, now) for r in todo]
        cur = [c for _, c, _ in st if c is not None]
        wk = [w for _, _, w in st if w is not None]
        score = (sum(cur) / len(cur) if cur else 0) + 0.3 * (sum(wk) / len(wk) if wk else 0)
        detail = ", ".join(f"{n} 지금 {c if c is not None else '-'}cm·7일 {w if w is not None else '-'}cm"
                           for n, c, w in st)
        cand.append((score, label, detail))
    cand.sort(reverse=True)
    jan_over = now.date() > datetime(2027, 1, 31).date() or any(not t for _, _, t, _ in flex)
    for i, (score, label, detail) in enumerate(cand):
        if not jan_over and i == 0:
            when = "❄ <b>1월 추천</b>" + (" (지금 적설 기준 미리보기)" if now.date().year < 2027 else "")
        else:
            when = "🗓 4월 (마지막)"
        lines.append(f'<div class="sl">{when}: <b>{H.escape(label)}</b><div class="s">{H.escape(detail)}</div></div>')
    if len(cand) == 2 and not jan_over and abs(cand[0][0] - cand[1][0]) < 1:
        lines.append('<div class="s">두 코스 적설 차이가 거의 없어요. 1월에 눈 온 뒤 다시 확인하세요.</div>')
    # 날짜가 정해진 코스
    for plan, label, todo, day_j in sorted(p for p in plan_rows if p[0] != "jan_apr"):
        d = plan_date(plan)
        if not d:
            continue
        head = f'{d.month}/{d.day}({WD[d.weekday()]}) <b>{H.escape(label)}</b>'
        if not todo:
            lines.append(f"<div>✅ {head} 완등</div>")
        elif d in day_j and day_j[d][1] >= 0:
            icon, lvl, msg, src = day_j[d]
            lines.append(f'<div class="sl">📅 {head} {pill(day_j[d])} <span class="s">{H.escape(msg)}</span>{f'<span class="sr"> (출처: {src})</span>' if src else ""}'
                         f'{"<b class=w> — 일정 조정 검토</b>" if lvl == 2 else ""}</div>')
        else:
            left = (d - now.date()).days
            lines.append(f'<div class="sl">📅 {head} <span class="s">예보 전 (D-{left}, 7일 전부터 표시)</span></div>')
    return ('<div class="info"><div><b>🗓 영남알프스 일정</b> <span class="s">4월까지 완등(메달 기준)</span></div>'
            + "".join(lines) + "</div>")


def course_photo(mts, region, key):
    """코스 사진 1장: 직접 고른 사진(photos.json) → 없으면 자동 검색(고른 사진이 하나도 없을 때만)."""
    if S.CHOSEN:
        return S.chosen_photo(key)
    for r in mts:
        ph = S.photo_for(r["name"], r.get("photo_q", "").strip() or None, must=region)
        if ph:
            return ph
    return None


def photo_html(ph):
    return S.photo_html(ph, "ph top", "⛰")


def _load_ramblr():
    """램블러 기록에서 뽑아 둔 경로·고도·요약 (ramblr_data.json, 트립 번호별)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ramblr_data.json")
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


RAMBLR = _load_ramblr()


def _hm(t):
    """'6h 9m 44s' → '6시간 10분'."""
    m = re.match(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", (t or "").strip())
    if not m or not any(m.groups()):
        return t
    h, mi, se = (int(x or 0) for x in m.groups())
    mi += 1 if se >= 30 else 0
    h, mi = h + mi // 60, mi % 60
    return (f"{h}시간 " if h else "") + f"{mi}분"


def route_svg(S, W, loop):
    """경로 모양(위에서 본 지도) SVG."""
    import math
    lat0 = sum(p[0] for p in S) / len(S)
    xs = [p[1] * math.cos(math.radians(lat0)) for p in S]
    ys = [-p[0] for p in S]
    w = h = 120
    pad = 10
    sx, sy = max(xs) - min(xs) or 1e-6, max(ys) - min(ys) or 1e-6
    k = (w - 2 * pad) / max(sx, sy)
    ox, oy = (w - sx * k) / 2 - min(xs) * k, (h - sy * k) / 2 - min(ys) * k
    pt = lambda i: (xs[i] * k + ox, ys[i] * k + oy)
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in map(pt, range(len(S))))
    out = [f'<svg class="map" viewBox="0 0 {w} {h}"><rect width="{w}" height="{h}" rx="8" fill="#eef3ea"/>',
           f'<polyline points="{line}" fill="none" stroke="#fff" stroke-width="5" stroke-linejoin="round"/>',
           f'<polyline points="{line}" fill="none" stroke="#e8590c" stroke-width="2.5" stroke-linejoin="round"/>']
    for name, km, ele in W:                          # 봉우리 위치: 기록 거리로 가장 가까운 점
        i = min(range(len(S)), key=lambda j: abs(S[j][3] - km))
        x, y = pt(i)
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#1c7ed6" stroke="#fff"/>')
    x, y = pt(0)
    out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#2f9e44" stroke="#fff" stroke-width="1.5"/>')
    if not loop:
        x, y = pt(len(S) - 1)
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#c92a2a" stroke="#fff" stroke-width="1.5"/>')
    out.append("</svg>")
    return "".join(out)


def profile_svg(S, W):
    """고도 그래프 SVG: 가로 거리(km), 세로 고도(m), 봉우리 표시."""
    w, h, l, r, t, b = 300, 120, 30, 6, 18, 16
    km = S[-1][3] or 1
    lo, hi = min(p[2] for p in S), max(p[2] for p in S)
    lo, hi = (lo // 100) * 100, -(-hi // 100) * 100
    X = lambda d: l + (w - l - r) * d / km
    Y = lambda e: t + (h - t - b) * (1 - (e - lo) / ((hi - lo) or 1))
    pts = " ".join(f"{X(p[3]):.1f},{Y(p[2]):.1f}" for p in S)
    out = [f'<svg class="prof" viewBox="0 0 {w} {h}">',
           f'<polygon points="{X(0):.1f},{Y(lo):.1f} {pts} {X(km):.1f},{Y(lo):.1f}" fill="#ffd8a8"/>',
           f'<polyline points="{pts}" fill="none" stroke="#e8590c" stroke-width="1.8"/>',
           f'<text x="{l - 3}" y="{Y(hi) + 4:.0f}" text-anchor="end">{int(hi)}</text>',
           f'<text x="{l - 3}" y="{Y(lo):.0f}" text-anchor="end">{int(lo)}</text>',
           f'<text x="{l}" y="{h - 3}">0</text><text x="{w - r}" y="{h - 3}" text-anchor="end">{km:.1f}km</text>']
    shown = []                                       # 이름이 겹치지 않게: 높은 봉우리부터, 가까우면 생략
    for name, d, ele in sorted(W, key=lambda v: -v[2]):
        if all(abs(d - s2) > km * 0.13 for s2 in shown):
            shown.append(d)
    for name, d, ele in W:
        x, y = X(min(d, km)), Y(ele)
        out.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{Y(lo):.1f}" stroke="#1c7ed6" stroke-dasharray="2 2"/>'
                   f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="#1c7ed6"/>')
        if d in shown:
            anc = "start" if x < l + 25 else ("end" if x > w - 25 else "middle")
            out.append(f'<text class="pk" x="{x:.1f}" y="{max(y - 5, 9):.1f}" text-anchor="{anc}">{H.escape(name)}</text>')
    out.append("</svg>")
    return "".join(out)


def ramblr_html(mts):
    """코스의 램블러 기록: 경로 모양 + 고도 그래프 + 거리·시간 + 기록 요약 + 링크."""
    blocks = []
    for r in mts:
        url = r.get("ramblr", "").strip()
        if not url:
            continue
        label = H.escape(r.get("ramblr_label", "").strip() or r["name"])
        link = f'<a class="rb" href="{H.escape(url)}" target="_blank" rel="noopener">🥾 {label} <b>램블러 ›</b></a>'
        d = RAMBLR.get(url.rstrip("/").split("/")[-1])
        if not d or not d.get("S"):
            blocks.append(f'<div class="rt">{link}</div>')
            continue
        kind = "원점회귀" if d.get("loop") else "편도(차량 회수 필요)"
        stat = (f'<b>{H.escape(d["dist"])}</b> · {_hm(d["dur"])} (걷는 시간 {_hm(d["move"])}) · '
                f'최고 {d["top"]:,}m · 누적 상승 {d["gain"]:,}m · 난이도 {H.escape(d["diff"])} · {kind}')
        notes = "".join(f"<li>{H.escape(x)}</li>" for x in d.get("sum", []))
        blocks.append(f'<div class="rt">{link}<div class="rs">{stat}</div>'
                      f'<div class="rv">{route_svg(d["S"], d["W"], d.get("loop"))}{profile_svg(d["S"], d["W"])}</div>'
                      f'<ul class="rn">{notes}</ul></div>')
    if not blocks:
        return ""
    return ('<div class="rts">' + "".join(blocks) +
            '<div class="s"><b style="color:#2f9e44">●</b> 출발 · <b style="color:#c92a2a">●</b> 도착(편도일 때) · <b style="color:#1c7ed6">●</b> 봉우리 — 경로와 기록은 위 램블러 공개 기록을 요약한 거예요.</div></div>')


def build(group, rows, key, now, next_up, outdir):
    g = GROUPS[group]
    om = open_meteo(rows)
    sat, sun = target_days(now)
    fri = sat - timedelta(days=1)
    show = (fri, sat, sun)                              # 화면에는 금·토·일만
    days = [now.date() + timedelta(days=i) for i in range(10)]
    # 하루 코스(1일 2산)로 묶기. course 칸이 비어 있으면 산 하나가 한 코스
    courses = {}
    for r in sorted(rows, key=lambda x: (int(x.get("course") or 0) or 999, int(x["order"]))):
        courses.setdefault(r.get("course") or r["name"], []).append(r)
    # 예보·사진은 여러 산을 동시에 미리 받아 둠
    from concurrent.futures import ThreadPoolExecutor
    todo_all = [r for r in rows if not r["done"].strip()]
    with ThreadPoolExecutor(max_workers=S.WORKERS) as ex:
        fc = dict(zip([r["name"] for r in todo_all],
                      ex.map(lambda r: mountain_days(r, key, now, om[r["name"]]), todo_all)))
        cps = dict(zip(list(courses), ex.map(lambda kv: course_photo(kv[1], g["region"], f"{group}:{kv[1][0].get('course') or kv[1][0]['name']}"), courses.items())))
    counted = [r for r in rows if r.get("counted", "").strip().upper() != "N"]
    done_n = sum(1 for r in counted if r["done"].strip())
    plan_rows = []                                   # (계획, 코스이름, 미완등 산들, 날짜별 판정)
    items, good_sat = [], []
    for ck, mts in courses.items():
        todo = [r for r in mts if not r["done"].strip()]
        names = " + ".join(H.escape(r["name"]) + (" ✅" if r["done"].strip() else "") for r in mts)
        badge = '<span class="bg">1일 2산</span>' if len(mts) > 1 else ""
        km, gain = mts[0].get("course_km", ""), mts[0].get("course_gain", "")
        if not (km and gain):                        # CSV에 없으면 램블러 기록 합계로
            recs = [RAMBLR.get(r["ramblr"].rstrip("/").split("/")[-1]) for r in mts if r.get("ramblr", "").strip()]
            recs = [x for x in recs if x]
            if recs:
                km = f'{sum(float(x["dist"].split()[0]) for x in recs):.1f}'
                gain = str(sum(x["gain"] for x in recs))
        stat = (f'{km}km · 누적 상승 {int(float(gain)):,}m' if km and gain else
                ("거리·상승고도 미정" if len(mts) > 1 else f'{H.escape(mts[0]["height_m"])}m'))
        notes = " · ".join(f'{H.escape(r["name"])}: {H.escape(r["note"])}' for r in mts if r["note"].strip())
        if not todo:
            when = ", ".join(r["done"].strip() for r in mts)
            if mts[0].get("plan", "").strip():
                plan_rows.append((mts[0]["plan"].strip(), " + ".join(r["name"] for r in mts), [], {}))
            items.append((9, f'<div class="card col done">{photo_html(cps[ck])}<div class="tx"><div class="hd"><b class="cn">✅ {" + ".join(H.escape(r["name"]) for r in mts)}</b>'
                             f'<span class="s">{stat}</span></div><div class="bd">완등 {H.escape(when)}</div></div></div>'))
            continue
        per = [(r["name"], fc[r["name"]]) for r in todo]
        day_j = {d: merge([(n, dj[d]) for n, dj in per]) for d in days}
        if mts[0].get("plan", "").strip():
            plan_rows.append((mts[0]["plan"].strip(), " + ".join(r["name"] for r in mts), todo, day_j))
        tiles, sat_lvl = "", 9
        for d in show:
            head = f'{WD[d.weekday()]}<small>{d.month}/{d.day}</small>'
            if d < now.date():
                tiles += f'<div class="tile wn"><div class="dh">{head}</div><div class="ic">·</div><div class="lb">지난 날</div></div>'
                continue
            j = day_j.get(d, ("❔", -1, "예보 전", ""))
            name, cls = tag(j)
            warn = '<div class="wr">산행 자제 권고</div>' if j[1] == 2 and d != fri else ""
            tiles += (f'<div class="tile {cls}{" we" if d != fri else ""}"><div class="dh">{head}</div>'
                      f'<div class="ic">{j[0]}</div><div class="lb">{name}</div>'
                      f'<div class="ms">{H.escape(j[2])}</div>{warn}'
                      + (f'<div class="sr">출처: {j[3]}</div>' if j[3] else "") + '</div>')
            if d == sat:
                sat_lvl = j[1] if j[1] >= 0 else 8
                if j[1] == 0:
                    good_sat.append(" + ".join(r["name"] for r in todo))
        note = f'<div class="s">{notes}</div>' if notes else ""
        note = ramblr_html(mts) + note
        items.append((sat_lvl, f'<div class="card col">{photo_html(cps[ck])}<div class="tx"><div class="hd"><b class="cn">{names}</b>{badge}'
                               f'<span class="s">{stat}</span></div>'
                               f'<div class="bd"><div class="tiles">{tiles}</div>{note}</div></div></div>'))
    items.sort(key=lambda x: x[0])                       # 토요일 날씨 좋은 코스부터, 완등 코스는 맨 아래
    cards = [c for _, c in items]
    total = len(counted)
    pct = round(done_n * 100 / total) if total else 0
    summary = (f"이번 토요일({sat.month}/{sat.day}) 비·낙뢰 없는 코스: <b>{' / '.join(good_sat)}</b>"
               if good_sat else f"이번 토요일({sat.month}/{sat.day})에 비·낙뢰 걱정 없는 코스가 아직 없어요 (또는 예보 전)")
    sched = schedule_html(plan_rows, key, now) if plan_rows else ""
    link = f' · <a href="{g["link"][1]}" target="_blank" rel="noopener">{g["link"][0]} ›</a>' if g["link"] else ""
    doc = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{g['title']}</title>
<style>{S.BASE_CSS}
.prog{{background:#e3e8ee;border-radius:6px;height:10px;margin:6px 0}}.prog i{{display:block;height:10px;border-radius:6px;background:#2a7de1}}
.card.col{{flex-direction:column}}.ph.top{{flex:none;height:150px}}.ph.top.none{{font-size:28px;height:56px}}@media(min-width:600px){{.ph.top{{height:200px}}}}
.rb{{display:inline-block;background:#eef5ee;color:#1b5e20;border:1px solid #b9dbbb;border-radius:16px;padding:4px 10px;font-size:13px;text-decoration:none}}
.rts{{margin-top:10px;border-top:1px dashed #d0d7de;padding-top:8px}}.rt{{margin-bottom:10px}}.rs{{font-size:12.5px;color:#333;margin:5px 0}}
.rv{{display:flex;gap:6px;align-items:stretch}}.rv .map{{flex:0 0 96px;height:96px}}.rv .prof{{flex:1;min-width:0;height:auto;background:#fffaf3;border-radius:8px}}
.prof text{{font-size:9px;fill:#666}}.prof text.pk{{font-size:9.5px;fill:#1c4f8a;font-weight:700}}
.rn{{margin:6px 0 0;padding-left:18px;font-size:12.5px;color:#444}}.rn li{{margin:2px 0}}
.cn{{font-size:17px}}.bg{{display:inline-block;margin-left:6px;font-size:11px;font-weight:700;color:#fff;background:#2a7de1;border-radius:10px;padding:1px 7px;vertical-align:middle}}
.tiles{{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:8px}}
.tile{{border-radius:10px;padding:8px 4px;text-align:center;border:2px solid transparent}}
.tile .dh{{font-weight:700;font-size:14px}}.tile .dh small{{font-weight:400;color:#666;margin-left:3px;font-size:12px}}
.tile .ic{{font-size:34px;line-height:1.2}}.tile .lb{{font-weight:800;font-size:16px}}
.tile .sr{{font-size:10px;color:#777;margin-top:4px;line-height:1.3;word-break:keep-all}}.sl .sr{{font-size:11px;color:#888}}.sr a{{color:#1c64c4;text-decoration:underline}}
.tile .ms{{font-size:11px;color:#555;margin-top:2px;word-break:keep-all}}.tile .wr{{font-size:11px;font-weight:700;color:#c62828;margin-top:2px}}
.w0{{background:#e3f4e5;color:#1b5e20}}.w1{{background:#fff3cd;color:#8a5a00}}.w2{{background:#fde2e0;color:#b71c1c}}
.wt{{background:#ede3fb;color:#5b2a9b}}.wn{{background:#f1f3f5;color:#888}}.tile.we{{border-color:#2a7de1}}
.pill{{display:inline-block;font-weight:800;font-size:14px;border-radius:12px;padding:2px 9px}}
.sl{{margin:5px 0}}.card.done{{opacity:.6}}.ok{{color:#2e7d32}}.c{{color:#b7791f}}</style></head><body>
{S.nav_html(g['tab'])}
<h1>⛰ {g['title']}</h1>
<div class="info">
<div>🏁 완등 <b>{done_n}/{total}</b> ({pct}%)</div><div class="prog"><i style="width:{pct}%"></i></div>
<div>{summary}</div>
<div class="s">{H.escape(g['info'])}{link}</div>
<div class="s">🕒 생성 {now:%m월 %d일 %H:%M} · 다음 업데이트 {next_up + timedelta(minutes=20):%m월 %d일 %H:%M}~{next_up + timedelta(minutes=30):%H:%M}</div>
</div>
{sched}
<div class="info"><div class="tiles">
<div class="tile w0"><div class="ic">☀</div><div class="lb">산행 OK</div><div class="ms">비·눈 소식 없음</div></div>
<div class="tile w1"><div class="ic">🌥</div><div class="lb">주의</div><div class="ms">눈 또는 강수확률 40%↑</div></div>
<div class="tile w2"><div class="ic">🌧</div><div class="lb">비</div><div class="ms">산행 자제 권고</div></div>
</div><div class="tiles" style="grid-template-columns:1fr 2fr"><div class="tile wt"><div class="ic">⚡</div><div class="lb">낙뢰</div><div class="ms">산행 자제 권고</div></div>
<div class="tile wn" style="text-align:left"><div class="ms">이번 주 <b>금·토·일</b> 산행시간(08~15시) 기준이에요. 파란 테두리는 토·일.<br>비·눈은 기상청 예보(약 4일), 그 이후와 낙뢰는 Open-Meteo 예보(참고)예요.</div></div></div></div>
{''.join(cards)}
<div class="meta">완등 기록: GitHub의 challenges.csv에서 해당 산 done 칸에 날짜(예: 2027-03-13)를 적으면 ✅로 바뀌어요.</div>
</body></html>"""
    with open(os.path.join(outdir, g["file"]), "w", encoding="utf-8") as f:
        f.write(doc)
    S.log(f"{g['title']}: {g['file']} 저장 (완등 {done_n}/{total})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="challenges.csv")
    ap.add_argument("--report-dir", default="site")
    ap.add_argument("--schedule", default="05:17,08:17,11:17,14:17,17:17,20:17,23:17")
    a = ap.parse_args()
    key = os.environ.get("KMA_AUTH_KEY", "")
    now = datetime.now(KST)
    rows = list(csv.DictReader(open(a.csv, encoding="utf-8-sig")))
    os.makedirs(a.report_dir, exist_ok=True)
    next_up = S.next_slot(now, a.schedule)
    S.IMG_DIR = os.path.join(a.report_dir, "img")
    cache = os.path.join(a.report_dir, "cache.json")
    S.load_cache(cache if os.path.exists(cache) else None)
    for grp in GROUPS:
        sub = [r for r in rows if r["group"] == grp]
        if sub:
            build(grp, sub, key, now, next_up, a.report_dir)
    S.save_cache(cache)


if __name__ == "__main__":
    main()
