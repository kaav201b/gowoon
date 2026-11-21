"""고운고 학교생활 페이지 주간 갱신 스크립트.

GitHub Actions가 매주 실행합니다. 나이스 Open API에서 시간표·급식·학사일정을 받아
index.html 안의 DATA 블록만 바꿉니다. 수행평가(pa)와 평가계획 일정은 그대로 둡니다.
인증키는 저장소 Secrets의 NEIS_KEY에서 읽습니다.
"""
import json
import time
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ATPT = "I10"          # 세종특별자치시교육청
SCHUL = "9300178"     # 고운고등학교
GRADE = "1"
CLASS = "1"
API = "https://open.neis.go.kr/hub/"
HTML = "index.html"
START = "/* ===== DATA START ===== */"
END = "/* ===== DATA END ===== */"


def ymd(d):
    return d.strftime("%Y%m%d")


def call(service, **params):
    key = os.environ.get("NEIS_KEY", "")
    q = {"Type": "json", "pIndex": 1, "pSize": 1000,
         "ATPT_OFCDC_SC_CODE": ATPT, "SD_SCHUL_CODE": SCHUL, **params}
    if key:
        q["KEY"] = key
    url = API + service + "?" + urllib.parse.urlencode(q)
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:  # 네트워크 오류는 잠깐 쉬었다가 재시도
            last = e
            time.sleep(5)
    else:
        raise RuntimeError(f"{service} 요청 실패: {last}")
    if "RESULT" in data:  # 데이터 없음(INFO-200) 또는 오류
        code = data["RESULT"].get("CODE")
        if code == "INFO-200":
            return []
        raise RuntimeError(f"{service}: {code} {data['RESULT'].get('MESSAGE')}")
    return data[service][1]["row"]


def target_week(now):
    # 토·일에 실행하면 다음 주, 평일에 실행하면 이번 주
    wd = now.weekday()  # 월=0
    monday = now + timedelta(days=7 - wd) if wd >= 5 else now - timedelta(days=wd)
    return [monday + timedelta(days=i) for i in range(5)]


def build_days(days):
    mon, fri = ymd(days[0]), ymd(days[-1])
    tt_rows = call("hisTimetable", GRADE=GRADE, CLASS_NM=CLASS, TI_FROM_YMD=mon, TI_TO_YMD=fri)
    meal_rows = call("mealServiceDietInfo", MLSV_FROM_YMD=mon, MLSV_TO_YMD=fri)

    out = {ymd(d): {"tt": [], "meal": []} for d in days}
    by_day = {}
    for r in tt_rows:
        by_day.setdefault(r["ALL_TI_YMD"], []).append((int(r["PERIO"]), r["ITRT_CNTNT"]))
    for k, rows in by_day.items():
        if k not in out:
            continue
        subjects = [s for _, s in sorted(rows)]
        if len(subjects) > 1 and len(set(subjects)) == 1:  # 수능 같은 하루 행사
            subjects = subjects[:1]
        out[k]["tt"] = subjects

    order = {"조식": 0, "중식": 1, "석식": 2}
    for r in sorted(meal_rows, key=lambda r: (r["MLSV_YMD"], order.get(r["MMEAL_SC_NM"], 9))):
        k = r["MLSV_YMD"]
        if k not in out:
            continue
        cal = re.sub(r"[^\d.]", "", r.get("CAL_INFO", "")) or "0"
        out[k]["meal"].append({"t": r["MMEAL_SC_NM"], "cal": str(round(float(cal))), "m": r["DDISH_NM"]})
    return out


def build_events(old_events, today, first_day):
    keep = [e for e in old_events if e.get("src") != "나이스" and e.get("to", e["from"]) >= ymd(today)]
    rows = call("SchoolSchedule", AA_FROM_YMD=ymd(first_day), AA_TO_YMD=ymd(first_day + timedelta(days=90)))
    rows = [r for r in rows if r["EVENT_NM"] != "토요휴업일" and r.get("SBTR_DD_SC_NM") != "공휴일"]
    rows.sort(key=lambda r: r["AA_YMD"])

    merged = []
    for r in rows:
        d, n = r["AA_YMD"], r["EVENT_NM"].strip()
        if merged and merged[-1]["n"] == n:
            prev = datetime.strptime(merged[-1]["to"], "%Y%m%d")
            if (datetime.strptime(d, "%Y%m%d") - prev).days <= 3:  # 주말 건너 이어지는 일정
                merged[-1]["to"] = d
                continue
        merged.append({"from": d, "to": d, "n": n, "sub": "", "src": "나이스"})

    keep_dates = {e["from"] for e in keep}
    merged = [e for e in merged if e["from"] not in keep_dates]
    return sorted(keep + merged, key=lambda e: e["from"])



# ---------------- 등교 버스 (국토교통부 TAGO, 공공데이터포털) ----------------
TAGO = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/"


def tago(op, **params):
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        raise RuntimeError("DATA_GO_KR_KEY가 GitHub Secrets에 없음")
    key = urllib.parse.unquote(key)  # 인코딩 키를 넣어도 동작하도록
    q = {"serviceKey": key, "_type": "json", "numOfRows": 500, "pageNo": 1, **params}
    url = TAGO + op + "?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except ValueError:  # 키 오류 등은 XML로 옴
        msg = re.findall(r"<(?:returnAuthMsg|errMsg|resultMsg)>([^<]+)<", raw)
        raise RuntimeError(f"{op}: JSON 아님 ({', '.join(msg) or raw[:150]})")
    header = data.get("response", {}).get("header", {})
    if str(header.get("resultCode", "00")) not in ("00", "0"):
        raise RuntimeError(f"{op}: {header.get('resultCode')} {header.get('resultMsg')}")
    body = data.get("response", {}).get("body", {})
    items = body.get("items") or {}
    item = items.get("item", []) if isinstance(items, dict) else []
    return item if isinstance(item, list) else [item]


def hhmm(v):
    v = str(v or "").strip()
    if len(v) == 3:
        v = "0" + v
    return f"{v[:2]}:{v[2:4]}" if len(v) >= 4 and v[:4].isdigit() else ""


def _pos(stops, no, name):
    # 정류장 번호(nodeno)로 먼저 찾고, 없으면 이름으로 찾음
    for i, st in enumerate(stops):
        if str(st.get("nodeno", "")) == no:
            return i
    for i, st in enumerate(stops):
        if name and name in str(st.get("nodenm", "")):
            return i
    return -1


def build_bus(bus):
    cities = tago("getCtyCodeList")
    city = next((c["citycode"] for c in cities if "세종" in str(c.get("cityname", ""))), None)
    if city is None:
        raise RuntimeError("TAGO 도시코드에서 세종을 찾지 못함")
    print("버스: 세종 도시코드", city)
    routes = []
    for no in bus["nos"]:
        cands = [r for r in tago("getRouteNoList", cityCode=city, routeNo=no) if str(r.get("routeno")) == str(no)]
        print(f"버스: {no}번 노선 후보 {len(cands)}개")
        picked, order = None, 0
        for r in cands:  # 타는 정류장을 지난 뒤 내리는 정류장을 지나는 방향만
            stops = tago("getRouteAcctoThrghSttnList", cityCode=city, routeId=r["routeid"])
            i = _pos(stops, bus["fromNo"], bus["from"])
            j = _pos(stops, bus["toNo"], bus["to"])
            print(f"  - {r['routeid']}: 정류장 {len(stops)}개, 타는 곳 {i}, 내리는 곳 {j}")
            if i >= 0 and j > i:
                picked, order = r, i
                break
        if not picked:
            routes.append({"no": str(no), "ok": False})
            continue
        info = (tago("getRouteInfoIem", cityCode=city, routeId=picked["routeid"]) or [{}])[0]
        print(f"  선택: {picked['routeid']} 첫차 {info.get('startvehicletime')} 막차 {info.get('endvehicletime')} 간격 {info.get('intervaltime')}")
        routes.append({
            "no": str(no), "ok": True,
            "first": hhmm(info.get("startvehicletime")), "last": hhmm(info.get("endvehicletime")),
            "gap": str(info.get("intervaltime") or ""), "gapSat": str(info.get("intervalsattime") or ""),
            "gapSun": str(info.get("intervalsuntime") or ""),
            "order": order,
        })
    return routes

# ---------------- 날씨 (기상청 단기예보, 공공데이터포털) ----------------
KMA = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
NX, NY = 65, 104  # 세종 고운동 격자


def build_weather(now):
    key = urllib.parse.unquote(os.environ.get("DATA_GO_KR_KEY", "").strip())
    if not key:
        raise RuntimeError("DATA_GO_KR_KEY 없음")
    # 05시 발표분(05:10 이후 제공). 그 전에 돌면 전날 23시 발표분
    if now.hour * 60 + now.minute >= 5 * 60 + 15:
        base_d, base_t = now, "0500"
    else:
        base_d, base_t = now - timedelta(days=1), "2300"
    q = {"serviceKey": key, "dataType": "JSON", "numOfRows": 2000, "pageNo": 1,
         "base_date": ymd(base_d), "base_time": base_t, "nx": NX, "ny": NY}
    with urllib.request.urlopen(KMA + "?" + urllib.parse.urlencode(q), timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except ValueError:
        msg = re.findall(r"<(?:returnAuthMsg|errMsg|resultMsg)>([^<]+)<", raw)
        raise RuntimeError(f"날씨: JSON 아님 ({', '.join(msg) or raw[:150]})")
    head = data["response"]["header"]
    if head.get("resultCode") != "00":
        raise RuntimeError(f"날씨: {head.get('resultCode')} {head.get('resultMsg')}")
    items = data["response"]["body"]["items"]["item"]

    days = {}
    for it in items:
        d = days.setdefault(it["fcstDate"], {"hours": {}})
        if it["category"] in ("TMN", "TMX"):
            d[it["category"]] = it["fcstValue"]
        else:
            d["hours"].setdefault(it["fcstTime"], {})[it["category"]] = it["fcstValue"]

    out = {}
    for k, d in days.items():
        h = d["hours"]
        am = h.get("0700") or h.get("0800") or {}
        pops = [int(v.get("POP", 0)) for t, v in h.items() if "0600" <= t <= "1800" and str(v.get("POP", "")).isdigit()]
        ptys = [v.get("PTY", "0") for t, v in h.items() if "0600" <= t <= "1800"]
        rain = next((p for p in ptys if p not in ("0", "")), "0")
        tmps = [float(v["TMP"]) for v in h.values() if "TMP" in v]
        out[k] = {
            "min": round(float(d["TMN"])) if "TMN" in d else (round(min(tmps)) if tmps else None),
            "max": round(float(d["TMX"])) if "TMX" in d else (round(max(tmps)) if tmps else None),
            "am": round(float(am["TMP"])) if "TMP" in am else None,
            "sky": am.get("SKY", ""), "pty": rain, "pop": max(pops) if pops else None,
        }
    return out


def main():
    now = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = target_week(today)

    html = open(HTML, encoding="utf-8").read()
    a, b = html.index(START) + len(START), html.index(END)
    body = html[a:b].strip()
    data = json.loads(body[body.index("{"):body.rindex("}") + 1])

    data["days"] = build_days(days)
    data["events"] = build_events(data.get("events", []), today, days[0])
    data["updated"] = today.strftime("%Y-%m-%d")
    data["ttPartial"] = False

    try:
        data["weather"] = build_weather(now)
    except Exception as e:  # 날씨 실패해도 나머지 갱신은 계속
        print("날씨 갱신 건너뜀:", e)

    if data.get("bus"):
        try:
            data["bus"]["routes"] = build_bus(data["bus"])
            data["bus"]["updated"] = today.strftime("%Y-%m-%d")
        except Exception as e:  # 버스 정보 실패해도 시간표·급식 갱신은 계속
            print("버스 정보 갱신 건너뜀:", e)

    block = "\nconst DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n"
    open(HTML, "w", encoding="utf-8").write(html[:a] + block + html[b:])
    print(f"갱신 완료: {ymd(days[0])}~{ymd(days[-1])}, 일정 {len(data['events'])}건")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("갱신 실패:", e)
        sys.exit(1)
