#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
엔드포인트 비교기.

여러 주소를 같은 간격으로 계속 찔러보면서, 값이 바뀌는 시점을 기록한다.
알고 싶은 것은 하나다 - 예매 팝업이 쓰는 주소가 지금 봇이 긁는 목록보다
실제로 더 빨리 갱신되는가.

한 번만 불러서는 아무것도 알 수 없다. 변화가 없는 시점에는 어느 주소든
같은 값을 주기 때문이다. 차이는 변화가 일어난 직후 몇 분에만 드러난다.
그래서 돌려놓고 그 사이에 사람이 좌석을 예매하거나 취소해야 한다.

11-06 실측: 좌석을 취소해 사이트는 26석이 됐는데 봇은 한참 뒤에도 22석이었고,
새 회차는 매진되고 나서야 알림이 왔다. 목록 쪽 수치가 늦는 것으로 보인다.

호출 대상 (probe_endpoints 로 찾은 것)
    POST /Theater/TimeTable    {TheaterCode, PlayDate}     페이지 하단 시간대별
    POST /Theater/MovieTable2  {TheaterCode, PlayDate}     페이지 하단 영화별
    POST /Popup/Reserve        {theaterCode, playDate}     예매 팝업
    GET  TARGET_URL                                        지금 봇이 긁는 주소
"""

import os
import re
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

KST = timezone(timedelta(hours=9))
OP_LEGACY_SERVER_CONNECT = 0x4

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PLAN_RE = re.compile(r"""screenplanid['"]?\s*[:=]\s*['"]?(\d+)""", re.I)
MOVIE_RE = re.compile(r"""data-moviecode=['"](\d+)['"]""", re.I)
TITLE_RE = re.compile(r"""class=["']title["']>(.*?)</div>""", re.S | re.I)
LEFT_RE = re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})")
SOLDOUT_RE = re.compile("매진")
DUMP = 1600                     # 응답에서 떠볼 길이


class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().init_poolmanager(*args, **kwargs)

    @staticmethod
    def _ctx():
        ctx = create_urllib3_context()
        ctx.options |= OP_LEGACY_SERVER_CONNECT
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def make_session(origin):
    s = requests.Session()
    s.mount("https://", LegacyTLSAdapter())
    s.headers.update({
        "User-Agent": CHROME_UA,
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": origin + "/",
        "X-Requested-With": "XMLHttpRequest",     # ajax 호출임을 알린다
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return s


def call(sess, method, url, data=None):
    try:
        if method == "POST":
            r = sess.post(url, data=data, timeout=25, verify=False)
        else:
            r = sess.get(url, timeout=25, verify=False)
    except requests.RequestException as e:
        return None, type(e).__name__
    return r, None


def summarize(name, method, url, data, r, why):
    """응답 하나를 사람이 읽을 수 있게 요약한다."""
    head = ["", "=" * 64, "%s" % name, "  %s %s" % (method, url)]
    if data:
        head.append("  보낸 값: %s" % data)
    if r is None:
        return head + ["  실패: %s" % why]

    body = r.text or ""
    plans = PLAN_RE.findall(body)
    lefts = LEFT_RE.findall(body)
    out = head + [
        "  HTTP %s / len=%d / %s" % (r.status_code, len(body),
                                     r.headers.get("Content-Type", "?")[:40]),
        "  회차 ID(screenplanid) %d개 / 고유 %d개" % (len(plans), len(set(plans))),
        "  매진 표시 %d곳" % len(SOLDOUT_RE.findall(body)),
        "  잔여 표시(숫자/숫자) %d곳" % len(lefts),
    ]
    if lefts:
        out.append("    예: " + ", ".join("%s/%s" % p for p in lefts[:12]))
    if plans:
        out.append("    회차 ID 예: " + ", ".join(sorted(set(plans))[:12]))

    # 캐시 관련 응답 헤더. 목록이 늦게 갱신되는 이유를 여기서 볼 수도 있다.
    for h in ("Cache-Control", "Age", "Expires", "Last-Modified", "X-Cache", "Via"):
        if h in r.headers:
            out.append("  %s: %s" % (h, r.headers[h][:70]))

    out += ["  --- 응답 앞부분 ---", "  " + re.sub(r"\s+", " ", body[:DUMP]).strip()]
    return out


def movie_codes(body):
    """
    목록에서 (영화 제목, 영화 코드) 를 뽑는다. 더빙판/자막판처럼 같은 영화도
    코드가 서로 다른 블록으로 따로 나오므로, 제목이 같아도 전부 모은다.
    """
    out = []
    for blk in body.split("each-movie-time")[1:]:
        t = TITLE_RE.search(blk)
        title = re.sub(r"<[^>]+>", "", t.group(1)).strip() if t else "?"
        m = MOVIE_RE.search(blk)
        if m and (title, m.group(1)) not in out:
            out.append((title, m.group(1)))
    return out


def pick_movies(codes, keywords):
    """
    감시 중인 키워드와 맞는 (제목, 코드) 들을 전부 돌려준다 (더빙판/자막판 등
    여러 블록이 있을 수 있어 전부 모은다). 하나도 안 맞으면 전체를 돌려준다 -
    뭐라도 시도는 해봐야 한다.
    """
    hit = [(title, code) for title, code in codes
           if any(kw and kw in title for kw in keywords)]
    return hit or codes


def fingerprint(body):
    """이 응답이 말하는 내용을 한 덩어리로 줄인다. 달라지면 갱신된 것이다."""
    plans = tuple(sorted(set(PLAN_RE.findall(body))))
    lefts = tuple(LEFT_RE.findall(body))
    return plans, lefts


def diff_text(old, new):
    """무엇이 어떻게 바뀌었는지 한 줄로."""
    oplans, olefts = old
    nplans, nlefts = new
    msgs = []
    added = [p for p in nplans if p not in oplans]
    gone = [p for p in oplans if p not in nplans]
    if added:
        msgs.append("회차 추가 %s" % ",".join(added[:6]))
    if gone:
        msgs.append("회차 사라짐 %s" % ",".join(gone[:6]))
    if olefts != nlefts:
        if len(olefts) == len(nlefts):
            ch = ["%s/%s -> %s/%s" % (o[0], o[1], n[0], n[1])
                  for o, n in zip(olefts, nlefts) if o != n]
            if ch:
                msgs.append("잔여 " + ", ".join(ch[:8]))
        else:
            msgs.append("잔여 항목 수 %d -> %d" % (len(olefts), len(nlefts)))
    return "; ".join(msgs) or "내용 변경"


def env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip())
    except ValueError:
        return default


def main():
    target = (os.environ.get("TARGET_URL") or "").strip()
    theater = (os.environ.get("PROBE_THEATER") or "1001").strip()
    date = (os.environ.get("PROBE_DATE") or "").strip()
    if not date:
        date = (datetime.now(KST) + timedelta(days=1)).strftime("%Y%m%d")
    watch_sec = env_int("PROBE_WATCH_SEC", 300)
    step_sec = max(5, env_int("PROBE_STEP_SEC", 10))

    origin = "/".join(target.split("/")[:3]) if target else "https://www.cineq.co.kr"
    sess = make_session(origin)

    # 팝업은 /Popup/Reserve 자체가 아니라, 그 안의 자바스크립트가 영화를 고른
    # 뒤에 /popup/ReserveScreenPlan 을 한 번 더 불러서 시간표를 채운다.
    # probe_popup_movie.html 에서 확인: loadScreenPlan() -> POST ReserveScreenPlan
    #
    # 같은 영화도 더빙판/자막판처럼 코드가 다른 블록으로 따로 나온다. 어느 코드가
    # 맞는지 미리 알 수 없으므로, 후보를 전부 실제로 호출해 보고 시간표가 실제로
    # 들어온(회차 ID 가 1개라도 있는) 첫 번째 것을 쓴다.
    keywords = [k.strip() for k in (os.environ.get("KEYWORDS") or "").split(",") if k.strip()]
    out = ["probe_call %s" % datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
           "극장 %s / 날짜 %s" % (theater, date)]
    _write(out)                 # 이 시점부터는 중단돼도 뭐라도 남는다

    r0, why0 = call(sess, "POST", origin + "/Theater/MovieTable2",
                    {"TheaterCode": theater, "PlayDate": date})
    codes = movie_codes(r0.text or "") if r0 is not None else []
    candidates = pick_movies(codes, keywords)
    print("목록에서 영화 %d편 확인. 후보 %d개: %s" %
          (len(codes), len(candidates), ", ".join(t for t, _c in candidates[:5])), flush=True)

    title, movie = None, None
    for t, c in candidates:
        rp, _why = call(sess, "POST", origin + "/popup/ReserveScreenPlan",
                        {"theaterCode": theater, "playDate": date, "movieCode": c})
        ok = rp is not None and PLAN_RE.search(rp.text or "")
        print("  시도: %-30s code=%s -> %s" % (t, c, "성공" if ok else "빈 응답"), flush=True)
        if ok:
            title, movie = t, c
            break
    if not movie and candidates:
        title, movie = candidates[0]        # 전부 실패해도 하나는 비교 대상으로 남긴다

    cases = [
        ("목록-시간대별", "POST", origin + "/Theater/TimeTable",
         {"TheaterCode": theater, "PlayDate": date}),
        ("목록-영화별", "POST", origin + "/Theater/MovieTable2",
         {"TheaterCode": theater, "PlayDate": date}),
    ]
    if movie:
        cases.insert(0, ("팝업ScreenPlan", "POST", origin + "/popup/ReserveScreenPlan",
                         {"theaterCode": theater, "playDate": date, "movieCode": movie}))
    if target and "{date}" in target:
        cases.append(("봇이 긁는 주소", "GET", target.replace("{date}", date), None))

    start = datetime.now(KST)
    out += ["%d초 동안 %d초 간격으로 지켜봅니다." % (watch_sec, step_sec),
            "지금부터 좌석을 예매하거나 취소하면, 어느 주소가 먼저 반영하는지 보입니다.",
            "고른 영화: %s (code=%s)" % (title, movie)]

    # 먼저 한 번씩 불러 응답 생김새를 남긴다. 구조를 봐야 다음 판단을 할 수 있다.
    base = {}
    for name, method, url, data in cases:
        r, why = call(sess, method, url, data)
        out += summarize(name, method, url, data, r, why)
        if r is not None:
            base[name] = fingerprint(r.text or "")
        print("%s -> %s" % (name, "실패(%s)" % why if r is None else r.status_code),
              flush=True)
        _write(out)              # 매 호출마다 즉시 반영 - 중단돼도 여기까진 남는다

    if watch_sec <= 0:
        # 응답만 받아보려는 경우. 좌석을 움직일 필요가 없다.
        out += ["", "=" * 64, "지켜보기 없이 응답만 받았습니다.", "=" * 64]
        _write(out)
        return 0

    out += ["", "=" * 64, "변화 기록", "=" * 64]
    _write(out)
    print("기준값을 잡았습니다. 지금 좌석을 예매하거나 취소하세요.", flush=True)

    first_change = {}
    rounds = 0
    while (datetime.now(KST) - start).total_seconds() < watch_sec:
        time.sleep(step_sec)
        rounds += 1
        changed_this_round = False
        for name, method, url, data in cases:
            r, _why = call(sess, method, url, data)
            if r is None:
                continue
            fp = fingerprint(r.text or "")
            if name not in base:
                base[name] = fp
                continue
            if fp != base[name]:
                now = datetime.now(KST)
                line = "[%s] %-14s %s" % (now.strftime("%H:%M:%S"), name,
                                          diff_text(base[name], fp))
                out.append(line)
                print(line, flush=True)
                base[name] = fp
                first_change.setdefault(name, now)
                changed_this_round = True
        if changed_this_round:
            _write(out)          # 변화가 잡힌 순간 바로 반영 - 중단해도 결과가 남는다
        elif rounds % 6 == 0:
            print("  (%d회째, 아직 변화 없음)" % rounds, flush=True)

    out += ["", "=" * 64, "요약 (%d회 확인)" % rounds, "=" * 64]
    if not first_change:
        out += ["아무 주소에서도 변화가 없었습니다.",
                "지켜보는 동안 예매/취소가 없었거나, 모두 같은 시점에 갱신됩니다.",
                "다시 돌리면서 중간에 좌석을 움직여 보세요."]
    else:
        order = sorted(first_change.items(), key=lambda kv: kv[1])
        t0 = order[0][1]
        for name, t in order:
            gap = int((t - t0).total_seconds())
            out.append("  %-14s 첫 변화 %s  (+%d초)" % (name, t.strftime("%H:%M:%S"), gap))
        missing = [n for n, _m, _u, _d in cases if n not in first_change]
        for n in missing:
            out.append("  %-14s 끝까지 변화 없음" % n)
        out += ["", "가장 먼저 바뀐 주소가 가장 빠른 소스입니다.",
                "봇이 긁는 주소가 뒤쪽이라면 앞쪽으로 바꾸면 됩니다."]

    _write(out)
    return 0


def _write(lines):
    with open("probe_call.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
