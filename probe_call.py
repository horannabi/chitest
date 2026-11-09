#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
엔드포인트 비교기.

같은 날짜·같은 극장으로 여러 주소를 호출해서 나란히 비교한다.
알고 싶은 것은 하나다 - 예매 팝업이 쓰는 주소가 지금 봇이 긁는 목록보다
실제로 더 정확하고 빠른가.

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


def main():
    target = (os.environ.get("TARGET_URL") or "").strip()
    theater = (os.environ.get("PROBE_THEATER") or "1001").strip()
    date = (os.environ.get("PROBE_DATE") or "").strip()
    if not date:
        date = (datetime.now(KST) + timedelta(days=1)).strftime("%Y%m%d")

    origin = "/".join(target.split("/")[:3]) if target else "https://www.cineq.co.kr"
    sess = make_session(origin)

    out = ["probe_call %s" % datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
           "극장 %s / 날짜 %s" % (theater, date),
           "같은 날짜를 여러 주소로 불러 숫자를 비교합니다."]

    cases = [
        ("예매 팝업", "POST", origin + "/Popup/Reserve",
         {"theaterCode": theater, "playDate": date}),
        ("하단 목록 - 시간대별", "POST", origin + "/Theater/TimeTable",
         {"TheaterCode": theater, "PlayDate": date}),
        ("하단 목록 - 영화별", "POST", origin + "/Theater/MovieTable2",
         {"TheaterCode": theater, "PlayDate": date}),
    ]
    if target and "{date}" in target:
        cases.append(("지금 봇이 긁는 주소", "GET", target.replace("{date}", date), None))

    for name, method, url, data in cases:
        r, why = call(sess, method, url, data)
        out += summarize(name, method, url, data, r, why)
        print("%s -> %s" % (name, "실패(%s)" % why if r is None else r.status_code),
              flush=True)

    out += ["", "=" * 64,
            "회차 ID 개수와 잔여 숫자가 주소마다 다르면, 많은 쪽이 더 최신입니다."]

    text = "\n".join(out) + "\n"
    with open("probe_call.txt", "w", encoding="utf-8") as f:
        f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
