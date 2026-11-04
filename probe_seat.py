#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
좌석 정보 구조 진단기.

1) TARGET_URL 로 지정일 목록을 받아 회차 하나를 고른다
2) 그 회차로 상세(POST) 엔드포인트들을 호출한다
3) 응답의 "구조"만 요약해서 probe_seat.txt 에 남긴다
   (태그/클래스/데이터속성 이름과 개수. 본문 내용은 최소한만)

목적은 하나다 - 좌석 단위 정보를 받을 수 있는지, 그 안에
특정 좌석 종류를 구분할 표시가 있는지 확인한다.
"""

import os
import re
import ssl
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

KST = timezone(timedelta(hours=9))
OP_LEGACY_SERVER_CONNECT = 0x4
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 좌석 종류를 구분할 만한 표시들
MARKERS = ("장애", "휠체어", "우대", "커플", "노약", "임산",
           "disabled", "wheel", "handicap", "special", "sold", "매진",
           "선택", "예매", "available", "occupied")

PATHS = ["/Popup/SeatChoiceSeatMap", "/Popup/Seatchoice"]

# 좌석 배치도에서 좌석 상태/종류를 나타내는 클래스들
SEAT_CLASSES = ("normal", "reserved", "selected", "disabled", "accessible", "couple")


class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = self._ctx()
        return super().init_poolmanager(*a, **kw)

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


def session_for(origin):
    s = requests.Session()
    s.mount("https://", LegacyTLSAdapter())
    s.headers.update({"User-Agent": UA,
                      "Accept-Language": "ko-KR,ko;q=0.9",
                      "Referer": origin + "/",
                      "X-Requested-With": "XMLHttpRequest"})
    return s


def summarize(body):
    """응답 구조만 요약한다."""
    out = []
    classes = Counter(c for grp in re.findall(r'class="([^"]+)"', body)
                      for c in grp.split())
    datas = Counter(re.findall(r'(data-[a-z0-9-]+)=', body))
    out.append("  class  : " + ", ".join("%s(%d)" % kv for kv in classes.most_common(25)))
    out.append("  data-* : " + ", ".join("%s(%d)" % kv for kv in datas.most_common(15)))
    found = [m for m in MARKERS if m.lower() in body.lower()]
    out.append("  표시    : " + (", ".join(found) if found else "(없음)"))

    # 이 응답 안에서 또 호출하는 주소가 있는지 (좌석 배치도는 보통 따로 받는다)
    calls = set(re.findall(r'\$\.(?:post|get|ajax)\(\s*["\'](/[^"\']+)["\']', body))
    calls |= set(re.findall(r'url\s*:\s*["\'](/[^"\']+)["\']', body))
    calls |= set(re.findall(r'["\'](/[A-Za-z]+/[A-Za-z][A-Za-z0-9_]*)["\']', body))
    out.append("  호출    : " + (", ".join(sorted(calls)[:20]) if calls else "(없음)"))

    # 좌석 상태/종류별 개수. 이 숫자로 빈자리 판정이 되는지 바로 보인다.
    tally = {k: sum(1 for grp in classes.elements() if grp == k) for k in SEAT_CLASSES}
    if any(tally.values()):
        out.append("  좌석수  : " + ", ".join("%s=%d" % (k, v) for k, v in tally.items()))

    # 좌석 요소 원본 샘플 2개. 상태가 어느 속성에 들어가는지 보려는 것.
    seats = re.findall(r'<[^>]*data-seatmapid="[^"]*"[^>]*>', body)
    if seats:
        out.append("  좌석요소: " + " || ".join(seats[:2])[:600])
        groups = Counter(re.findall(r'data-seatgroup="([^"]*)"', body))
        out.append("  seatgroup 값: " + ", ".join("%s(%d)" % kv for kv in groups.most_common(10)))

    # 인라인 스크립트에서 좌석 상태를 칠하는 부분
    hits = []
    for sm in re.findall(r'<script[^>]*>(.*?)</script>', body, re.S):
        for mm in re.finditer(r'.{90}(?:addClass|SeatMap|seatMap|reserved|soldSeat).{150}', sm, re.S):
            hits.append(re.sub(r'\s+', ' ', mm.group(0)))
            if len(hits) >= 4:
                break
        if len(hits) >= 4:
            break
    for h in hits:
        out.append("  script  : " + h[:260])
    return out


def main():
    table_url = (os.environ.get("TARGET_URL") or "").strip()
    if not table_url or "{date}" not in table_url:
        print("TARGET_URL 이 없거나 {date} 가 없습니다.")
        return 2

    date_str = (os.environ.get("PROBE_DATE") or "").strip() \
        or datetime.now(KST).strftime("%Y%m%d")
    origin = "/".join(table_url.split("/")[:3])
    sess = session_for(origin)

    lines = ["probe_seat %s" % datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
             "date %s" % date_str, ""]

    # 1) 목록에서 회차 하나 고르기
    try:
        r = sess.get(table_url.replace("{date}", date_str), timeout=20, verify=False)
    except requests.RequestException as e:
        lines.append("목록 조회 실패: %s" % type(e).__name__)
        write(lines)
        return 1

    lines.append("목록 %s len=%d" % (r.status_code, len(r.text)))
    m = re.search(r'<div class="time"((?:\s+data-[a-z]+="[^"]*")+)', r.text)
    if not m:
        lines.append("회차를 찾지 못했습니다. (그날 편성이 없을 수 있음)")
        write(lines)
        return 0

    attrs = dict(re.findall(r'data-([a-z]+)="([^"]*)"', m.group(1)))
    payload = {"playDate": attrs.get("playdate", date_str),
               "theaterCode": attrs.get("theatercode", ""),
               "movieCode": attrs.get("moviecode", ""),
               "screenPlanId": attrs.get("screenplanid", ""),
               "playNumber": attrs.get("playnumber", "")}
    lines.append("회차 screenPlanId=%s playNumber=%s"
                 % (payload["screenPlanId"], payload["playNumber"]))
    lines.append("")

    # 2) 상세 엔드포인트 호출
    for path in PATHS:
        url = origin + path
        try:
            rr = sess.post(url, data=payload, timeout=25, verify=False)
        except requests.RequestException as e:
            lines.append("%s -> EXC %s" % (path, type(e).__name__))
            continue
        lines.append("%s -> %s len=%d" % (path, rr.status_code, len(rr.text)))
        if rr.status_code == 200 and len(rr.text) > 300:
            lines.extend(summarize(rr.text))
        lines.append("")

    write(lines)
    return 0


def write(lines):
    text = "\n".join(lines) + "\n"
    print(text, flush=True)
    with open("probe_seat.txt", "w", encoding="utf-8") as f:
        f.write(text)


if __name__ == "__main__":
    sys.exit(main())
