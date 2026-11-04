#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
접속 가능 여부 진단기.

PROBE_URL 에 지정한 주소를 여러 방식으로 호출해 보고, 결과 요약만
probe.txt 에 남긴다. 받아온 본문 자체는 저장하지 않는다.

여기서 알고 싶은 것은 하나다 - 이 러너의 IP 로 대상에 접근이 되는가.
"""

import os
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
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

# 차단 페이지에서 흔히 보이는 표현들
BLOCK_HINTS = ("비정상적으로", "이용이 제한", "Access Denied", "Forbidden",
               "Web Page Blocked", "FortiGuard", "Attention Required",
               "cf-browser-verification", "RAY_ID", "captcha")


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


def attempt(name, url, headers):
    """한 가지 방식으로 호출해 보고 결과를 한 줄로 요약한다."""
    sess = requests.Session()
    sess.mount("https://", LegacyTLSAdapter())
    try:
        r = sess.get(url, headers=headers, timeout=25, verify=False)
    except requests.RequestException as e:
        return "%-14s EXC   %s" % (name, type(e).__name__)

    body = r.text or ""
    hits = [h for h in BLOCK_HINTS if h.lower() in body.lower()]
    verdict = "BLOCKED(%s)" % ",".join(hits[:2]) if hits else (
        "OK" if r.status_code == 200 and len(body) > 500 else "EMPTY?")
    return "%-14s %-5s len=%-7d %s" % (name, r.status_code, len(body), verdict)


def main():
    url = (os.environ.get("PROBE_URL") or "").strip()
    if not url:
        print("PROBE_URL 이 비어 있습니다.")
        return 2

    origin = "/".join(url.split("/")[:3])
    lines = ["probe %s" % datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
             "host  %s" % origin.split("//")[-1],
             ""]

    cases = [
        ("plain", {}),
        ("chrome-ua", {"User-Agent": CHROME_UA,
                       "Accept-Language": "ko-KR,ko;q=0.9"}),
        ("chrome-full", {"User-Agent": CHROME_UA,
                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                         "Accept-Language": "ko-KR,ko;q=0.9",
                         "Referer": origin + "/",
                         "Upgrade-Insecure-Requests": "1",
                         "Sec-Fetch-Dest": "document",
                         "Sec-Fetch-Mode": "navigate",
                         "Sec-Fetch-Site": "same-origin"}),
        ("mobile-ua", {"User-Agent": MOBILE_UA,
                       "Accept-Language": "ko-KR,ko;q=0.9"}),
    ]

    for name, headers in cases:
        line = attempt(name, url, headers)
        print(line, flush=True)
        lines.append(line)

    lines.append("")
    lines.append("OK 가 하나라도 있으면 이 러너에서 접근 가능합니다.")

    with open("probe.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
