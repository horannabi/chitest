#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
엔드포인트 탐색기.

PROBE_PAGE_URL 페이지와 그 페이지가 불러오는 스크립트를 받아서,
서버로 보내는 호출 주소를 뽑아낸다. 결과는 probe_endpoints.txt 에 남긴다.

알고 싶은 것은 하나다 - 예매 팝업이 상영시간표를 어디서 받아오는가.
지금 봇이 긁는 목록 페이지는 갱신이 늦어서, 새 회차가 떠도 한참 뒤에야
알아챈다(11-06 실측). 팝업이 쓰는 주소를 찾으면 그쪽으로 바꿀 수 있다.

이 러너에서는 브라우저를 띄울 수 없으므로 스크립트 원문에서 찾는다.
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

# ajax/fetch 호출에서 주소를 꺼낸다
CALL_RE = re.compile(
    r"""(?:url\s*[:=]\s*|fetch\s*\(\s*|\$\.(?:get|post|getJSON|ajax)\s*\(\s*|"""
    r"""open\s*\(\s*['"][A-Z]+['"]\s*,\s*)['"]([^'"\s]{3,200})['"]""")
# 따옴표 안의 경로 문자열 (위에서 못 잡는 조립형 호출용)
PATH_RE = re.compile(r"""['"](/[A-Za-z0-9_\-./]{3,120})['"]""")
# 페이지가 불러오는 스크립트
SRC_RE = re.compile(r"""<script[^>]+src=['"]([^'"]+)['"]""", re.I)

# 시간표·예매와 관련 있어 보이는 경로만 따로 추린다
INTEREST = ("schedule", "time", "play", "movie", "theater", "popup", "rsv",
            "reserv", "booking", "seat", "ticket", "list", "ajax", "json")
# 눈에 띄면 바로 쓸모 있는 확장자
DATA_EXT = (".json", ".ashx", ".asmx", ".svc")

SKIP_SRC = ("google", "facebook", "kakao", "naver", "jquery", "bootstrap",
            "analytics", "gtag", "gtm", "swiper", "slick", "polyfill")


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


def make_session():
    s = requests.Session()
    s.mount("https://", LegacyTLSAdapter())
    s.headers.update({"User-Agent": CHROME_UA,
                      "Accept-Language": "ko-KR,ko;q=0.9"})
    return s


def get(sess, url, referer=None):
    """본문을 돌려준다. 실패하면 (None, 사유)."""
    headers = {"Referer": referer} if referer else {}
    try:
        r = sess.get(url, headers=headers, timeout=25, verify=False)
    except requests.RequestException as e:
        return None, type(e).__name__
    if r.status_code != 200:
        return None, "HTTP %s" % r.status_code
    return r.text or "", None


def absolute(src, origin, page_url):
    """스크립트 src 를 완전한 주소로 만든다."""
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http"):
        return src
    if src.startswith("/"):
        return origin + src
    return page_url.rsplit("/", 1)[0] + "/" + src


def interesting(path):
    low = path.lower()
    if any(low.endswith(e) for e in DATA_EXT):
        return True
    return any(k in low for k in INTEREST)


def snippet(text, at, before=180, after=320):
    """호출부 주변 코드. 어떤 파라미터를 함께 보내는지 보려는 것이다."""
    s = max(0, at - before)
    e = min(len(text), at + after)
    cut = text[s:e]
    cut = re.sub(r"\s+", " ", cut).strip()
    return ("..." if s else "") + cut + ("..." if e < len(text) else "")


def scan(text, where, found, ctx):
    """본문에서 호출 주소를 뽑아 found 에 모은다. {경로: {출처}}"""
    for m in CALL_RE.finditer(text):
        p = m.group(1)
        if p.startswith(("/", "http")) or "/" in p:
            found.setdefault(p, set()).add(where)
            ctx.setdefault(p, []).append((where, snippet(text, m.start())))
    for m in PATH_RE.finditer(text):
        p = m.group(1)
        if interesting(p):
            found.setdefault(p, set()).add(where)
            ctx.setdefault(p, []).append((where, snippet(text, m.start())))


def main():
    page_url = (os.environ.get("PROBE_PAGE_URL") or "").strip()
    if not page_url:
        print("PROBE_PAGE_URL 이 비어 있습니다.")
        return 2

    origin = "/".join(page_url.split("/")[:3])
    sess = make_session()
    out = ["probe_endpoints %s" % datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
           "page  %s" % page_url, ""]

    body, why = get(sess, page_url)
    if body is None:
        out.append("페이지를 받지 못했습니다: %s" % why)
        _write(out)
        return 1
    out.append("페이지 len=%d" % len(body))

    found, ctx = {}, {}
    scan(body, "page", found, ctx)

    # 페이지가 불러오는 스크립트도 같이 본다. 호출 주소는 보통 여기 들어 있다.
    srcs = []
    for s in SRC_RE.findall(body):
        if any(k in s.lower() for k in SKIP_SRC):
            continue
        srcs.append(s)
    srcs = srcs[:25]                      # 너무 많으면 앞쪽만
    out.append("스크립트 %d개 확인" % len(srcs))
    out.append("")

    for s in srcs:
        url = absolute(s, origin, page_url)
        js, why = get(sess, url, referer=page_url)
        if js is None:
            out.append("  [건너뜀] %s (%s)" % (s[:90], why))
            continue
        scan(js, s.rsplit("/", 1)[-1][:40], found, ctx)

    # 관심 경로를 먼저, 그 외는 뒤에
    hot = sorted(p for p in found if interesting(p))
    rest = sorted(p for p in found if not interesting(p))

    out += ["", "=" * 60,
            "시간표·예매로 보이는 주소 (%d개)" % len(hot), "=" * 60]
    for p in hot:
        out.append("  %-60s  <- %s" % (p[:60], ", ".join(sorted(found[p]))[:60]))

    out += ["", "=" * 60, "그 밖의 호출 (%d개)" % len(rest), "=" * 60]
    for p in rest[:80]:
        out.append("  %-60s  <- %s" % (p[:60], ", ".join(sorted(found[p]))[:60]))

    # 시간표로 보이는 주소는 호출부 코드까지 보여준다. 어떤 파라미터를
    # 함께 보내는지, GET 인지 POST 인지 여기서 드러난다.
    key = ("timetable", "movietable", "schedule", "seatchoice", "play", "reserve")
    out += ["", "=" * 60, "호출부 코드 (파라미터 확인용)", "=" * 60]
    for p in hot:
        if not any(k in p.lower() for k in key):
            continue
        for where, sn in ctx.get(p, [])[:2]:
            out += ["", "[%s]  (%s)" % (p, where), "  " + sn]

    out += ["", "위 주소 중 상영시간표를 돌려주는 것을 찾으면,",
            "봇이 긁는 목록 대신 그쪽을 쓰도록 바꿀 수 있습니다."]
    _write(out)
    return 0


def _write(lines):
    text = "\n".join(lines) + "\n"
    with open("probe_endpoints.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print(text, flush=True)


if __name__ == "__main__":
    sys.exit(main())
