#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
지정한 날짜의 목록을 주기적으로 조회해서, 키워드와 일치하는 항목이
나타나면 텔레그램으로 알려준다.

대상 주소와 키워드는 코드에 두지 않고 전부 환경변수(Secrets)로 받는다.

  TARGET_URL   조회할 주소. {date} 자리에 YYYYMMDD 가 들어간다.
  SITE_URL     알림에 같이 보낼 사람이 볼 주소
  KEYWORDS     쉼표로 구분. 항목 이름에 이 중 하나라도 들어가면 알림

주의 2가지
  1) 대상 서버가 구형 TLS 재협상을 요구하는 경우가 있다. OpenSSL 3.x 기본
     설정으로는 UNSAFE_LEGACY_RENEGOTIATION_DISABLED 로 접속 자체가 실패하므로
     LegacyTLSAdapter 로 OP_LEGACY_SERVER_CONNECT 를 켠다.
  2) 인증서 체인이 불완전해서 검증을 끄고 접속한다. 보내는 데이터가 없는
     공개 목록 조회라 문제되지 않는다.
"""

import faulthandler
import os
import re
import signal
import ssl
import sys
import time
import html as html_mod
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

import seats
from bot import State, Telegram, handle_command, parse_date
from health import Health, Watchdog

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 프로세스가 통째로 먹통이 됐을 때를 대비해 스택 덤프를 켜 둔다.
# (Watchdog 이 자리를 잡아 주지만, 그쪽도 못 도는 상황을 위한 보조다)
faulthandler.enable()

KST = timezone(timedelta(hours=9))
WDAY = ("월", "화", "수", "목", "금", "토", "일")

READER_PREFIX = "https://r.jina.ai/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

OP_LEGACY_SERVER_CONNECT = 0x4


# ---------------------------------------------------------------- 유틸

def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


def die(msg, code=2):
    log("오류: " + msg)
    sys.exit(code)


def env(name, default=None):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def env_int(name, default):
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def env_bool(name, default="0"):
    return (env(name, default) or "").lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 설정

class Config:
    """
    환경변수에서 읽어오는 실행 설정.

    감시 날짜는 state.json(텔레그램으로 지정) 이 우선이고,
    없으면 TARGET_DATE 환경변수를 쓴다. 둘 다 없으면 대기 모드로 들어간다.
    """

    def __init__(self):
        self.target_url = env("TARGET_URL")
        if not self.target_url:
            die("TARGET_URL 이 비어 있습니다. {date} 자리를 포함한 주소를 지정하세요.")
        if "{date}" not in self.target_url:
            die("TARGET_URL 에 {date} 자리가 없습니다.")
        self.site_url = env("SITE_URL", "")

        self.keywords = [k.strip() for k in (env("KEYWORDS", "") or "").split(",") if k.strip()]
        if not self.keywords:
            die("KEYWORDS 가 비어 있습니다.")

        raw_date = env("TARGET_DATE")
        self.target = None
        if raw_date:
            self.target = parse_date(raw_date)
            if self.target is None:
                die("TARGET_DATE 형식이 잘못되었습니다: %r (예: 20261115)" % raw_date)

        self.label = env("LABEL", "")            # 알림에 붙일 짧은 이름. 없어도 됨

        # until = 오늘부터 지정일까지 매일 감시 / onday = 지정일 당일에만 감시
        self.mode = (env("MODE", "until") or "until").lower()

        self.poll_sec = env_int("POLL_SEC", 10)
        self.burst_sec = env_int("BURST_SEC", 2)
        self.burst_windows = parse_windows(env("BURST_WINDOWS", ""))
        self.max_run_min = env_int("MAX_RUN_MIN", 340)
        # 세션 시작 후 이 시간이 지나면 다음 실행을 스스로 요청한다(이어받기).
        # 요청이 실패하면 max_run_min 까지 버티면서 계속 다시 요청한다.
        self.relay_after_min = env_int("RELAY_AFTER_MIN", max(1, self.max_run_min - 15))
        self.relay_workflow = env("RELAY_WORKFLOW", "watch.yml")
        # 생존 기록 간격(초)과 외부 ping 주소
        # HEALTH=0 이면 생존 기록/외부 ping 을 아예 끈다 (텔레그램 /health on 도 무시)
        self.health_enabled = env_bool("HEALTH", "1")
        self.health_beat_sec = env_int("HEALTH_BEAT_SEC", 60)
        self.healthcheck_url = env("HEALTHCHECK_URL")
        # 세션 시작 시 잰 감시 공백이 이 초를 넘으면 텔레그램으로 알린다
        self.gap_alert_sec = env_int("GAP_ALERT_SEC", 120)
        # 메인 루프가 한 단계에서 이 초만큼 진행이 없으면 멈춘 것으로 보고
        # 스택을 떠서 알린다. 조회 쪽 타임아웃이 가장 긴 것이 60초(우회로)라
        # 그 세 배로 잡았다. 0 이면 멈춤 감시를 끈다.
        self.stall_sec = env_int("STALL_SEC", 180)
        # 이만큼 멈춰 있으면 다음 세션을 띄우고 스스로 끝낸다(새 세션이 이어받는다).
        # 알림 임계보다 뒤여야 스택을 먼저 남기고 나간다. 0 이면 끝내지 않는다.
        self.stall_exit_sec = env_int("STALL_EXIT_SEC", 300)
        # 기동하고 이 시간이 지나기 전에 멈춘 경우에는 스스로 끝내지 않는다.
        # 올라오자마자 또 멈추면 재기동이 끝없이 돌기 때문이다.
        self.stall_min_uptime_sec = env_int("STALL_MIN_UPTIME_SEC", 600)
        self.gh_run_id = env("GITHUB_RUN_ID")
        self.gh_event = env("GITHUB_EVENT_NAME")
        self.idle_sec = env_int("IDLE_SEC", 5)
        # 읽음 확인(/ok) 전까지 알림을 다시 보내는 간격(초)
        self.repeat_sec = env_int("ALERT_REPEAT_SEC", 5)

        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = env("TELEGRAM_CHAT_ID")
        self.gh_token = env("GITHUB_TOKEN")
        self.gh_repo = env("GITHUB_REPOSITORY")
        self.gh_branch = env("GITHUB_REF_NAME", "main")

        self.force = env_bool("FORCE")
        self.notify_start = env_bool("NOTIFY_START")
        # 조회 결과 항목 이름을 로그에 남길지. 공개 저장소에서는 꺼두는 편이 낫다.
        self.verbose = env_bool("VERBOSE")

    def url_for(self, target):
        return self.target_url.replace("{date}", target.strftime("%Y%m%d"))


def parse_windows(spec):
    """'09:50-10:20,13:00-13:10' -> [(분, 분), ...] (자정 기준 분 단위)."""
    out = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", chunk)
        if not m:
            log("BURST_WINDOWS 항목을 해석하지 못해 무시합니다: %r" % chunk)
            continue
        out.append((int(m.group(1)) * 60 + int(m.group(2)),
                    int(m.group(3)) * 60 + int(m.group(4))))
    return out


# ---------------------------------------------------------------- 조회

class LegacyTLSAdapter(HTTPAdapter):
    """구형 TLS 재협상을 허용하는 어댑터."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().proxy_manager_for(*args, **kwargs)

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


class Fetcher:
    """
    1순위 : 직접 접속 (레거시 TLS 어댑터)
    2순위 : r.jina.ai 리더 경유 (방화벽에 막힌 망에서 쓰기 위한 우회로)

    반환값은 (본문, 형식) 이고 형식은 'html' 또는 'markdown'.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.sess = requests.Session()
        self.sess.mount("https://", LegacyTLSAdapter())
        self.sess.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9",
            # 중간 캐시가 낡은 목록을 돌려주지 않게 한다.
            # 11-06 실측: 좌석을 취소해 웹에서는 26석이 됐는데, 봇은 한참 뒤에도
            # 22석으로 고정이었다. 캐시 지정이 아예 없었다.
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        })
        if cfg.site_url:
            self.sess.headers["Referer"] = cfg.site_url
        self.use_proxy = False
        self.direct_fail = 0

    _seq = 0            # 같은 밀리초에 두 번 불려도 주소가 겹치지 않게 하는 순번

    @classmethod
    def _bust(cls, url):
        """
        조회할 때마다 주소를 다르게 만든다. 캐시 방지 헤더만으로는 중간 캐시가
        낡은 응답을 돌려주는 것을 못 막는 경우가 있다.
        사이트는 모르는 파라미터를 무시하므로 결과는 달라지지 않는다.
        """
        cls._seq = (cls._seq + 1) % 1000
        sep = "&" if "?" in url else "?"
        return "%s%s_=%d%03d" % (url, sep, int(time.time() * 1000), cls._seq)

    def fetch(self, target):
        url = self._bust(self.cfg.url_for(target))

        if not self.use_proxy:
            text = self._direct(url)
            if text is not None:
                self.direct_fail = 0
                return text, "html"
            self.direct_fail += 1
            if self.direct_fail >= 3:
                log("직접 접속이 3회 연속 실패했습니다. 우회로로 전환합니다.")
                self.use_proxy = True

        text = self._via_proxy(url)
        return (text, "markdown") if text is not None else (None, None)

    def _direct(self, url):
        try:
            r = self.sess.get(url, timeout=15, verify=False)
        except requests.RequestException as e:
            log("직접 접속 예외: %s" % str(e)[:160])
            return None

        if r.status_code == 200 and r.text:
            return r.text

        if "FortiGuard" in r.text or "Web Page Blocked" in r.text:
            log("네트워크 방화벽이 접속을 차단하고 있습니다. 우회로로 전환합니다.")
            self.direct_fail = 99
            return None

        log("직접 접속 응답 %s" % r.status_code)
        return None

    def _via_proxy(self, url):
        # 조각 HTML 을 리더의 html 모드로 받으면 렌더링 과정에서 내용이 날아간다.
        # markdown 모드는 본문이 그대로 남으므로 이쪽을 쓴다.
        try:
            r = requests.get(READER_PREFIX + url,
                             headers={"User-Agent": UA, "x-no-cache": "true"},
                             timeout=60)
        except requests.RequestException as e:
            log("우회로 예외: %s" % str(e)[:160])
            return None
        if r.status_code == 200 and r.text:
            return r.text
        log("우회로 응답 %s" % r.status_code)
        return None


# ---------------------------------------------------------------- 파싱

TAG_RE = re.compile(r"<[^>]+>")
ITEM_BLOCK_RE = re.compile(
    r'<div class="each-movie-time">(.*?)(?=<div class="each-movie-time">|\Z)', re.S)
NAME_RE = re.compile(r'<div class="title">(.*?)</div>', re.S)
GROUP_RE = re.compile(
    r'<div class="screen-name"[^>]*>(.*?)</div>(.*?)(?=<div class="screen-name"|\Z)', re.S)
SLOT_RE = re.compile(
    r'<div class="time"((?:\s+data-[a-z]+="[^"]*")+)\s*>(.*?)</div>', re.S)
ATTR_RE = re.compile(r'data-([a-z]+)="([^"]*)"')

# markdown 모드용
MD_SLOT_RE = re.compile(r"^\[(\d{1,2}:\d{2})\s*~\s*(\d{1,2}:\d{2})\s*(.*?)\]\(")
MD_GROUP_RE = re.compile(r"^(?:\([^)]*\))?\s*\d+\s*관$")
MD_NAME_RE = re.compile(r"^(?:전체|청불|0|12|15|18|19)\s+(\S.*)$")
MD_SKIP = ("title:", "url source:", "markdown content:", "warning:")


def strip_tags(s):
    return re.sub(r"\s+", " ", html_mod.unescape(TAG_RE.sub(" ", s))).strip()


def parse_html(body):
    """본문 -> [{name, groups:[{name, slots:[{time, note}]}]}]"""
    items = []
    for block in ITEM_BLOCK_RE.findall(body):
        nm = NAME_RE.search(block)
        if not nm:
            continue
        # 이름 앞에 붙는 등급 배지는 떼어낸다
        raw_name = re.sub(r'<span class="rate[^"]*">.*?</span>', " ", nm.group(1), flags=re.S)
        name = strip_tags(raw_name)
        if not name:
            continue

        groups = []
        for gname, gbody in GROUP_RE.findall(block):
            slots = []
            for raw_attrs, sbody in SLOT_RE.findall(gbody):
                # 좌석 조회에 필요한 식별자를 그대로 들고 간다
                attrs = dict(ATTR_RE.findall(raw_attrs))
                txt = strip_tags(sbody)
                m = re.match(r"^(\d{1,2}:\d{2})\s*~\s*(\d{1,2}:\d{2})\s*(.*)$", txt)
                if m:
                    slots.append({"time": "%s~%s" % (m.group(1), m.group(2)),
                                  "note": m.group(3).strip(), "attrs": attrs})
                elif txt:
                    slots.append({"time": txt, "note": "", "attrs": attrs})
            if slots:
                groups.append({"name": strip_tags(gname), "slots": slots})

        items.append({"name": name, "groups": groups})
    return items


def parse_markdown(body):
    """리더의 markdown 응답 -> parse_html 과 같은 구조."""
    items = []
    cur_item = None
    cur_group = None

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith(MD_SKIP) or line.startswith("*"):
            continue
        if line.startswith("[") and "](" in line and not MD_SLOT_RE.match(line):
            continue

        m = MD_SLOT_RE.match(line)
        if m:
            if cur_item is None:
                continue
            if cur_group is None:
                cur_group = {"name": "", "slots": []}
                cur_item["groups"].append(cur_group)
            cur_group["slots"].append({"time": "%s~%s" % (m.group(1), m.group(2)),
                                       "note": m.group(3).strip()})
            continue

        if MD_GROUP_RE.match(line):
            cur_group = {"name": line, "slots": []}
            if cur_item is None:
                cur_item = {"name": "(이름 불명)", "groups": []}
                items.append(cur_item)
            cur_item["groups"].append(cur_group)
            continue

        m = MD_NAME_RE.match(line)
        if m:
            cur_item = {"name": m.group(1).strip(), "groups": []}
            cur_group = None
            items.append(cur_item)

        # 그 외 부가 표시는 무시

    return [it for it in items if it["groups"]]


def parse(body, fmt):
    try:
        return parse_html(body) if fmt == "html" else parse_markdown(body)
    except Exception as e:                                   # 파싱이 깨져도 감시는 계속
        log("파싱 중 예외(무시하고 계속): %s" % str(e)[:160])
        return []


def contains_keyword(body, keywords):
    """
    파싱과 무관하게 응답 원문에 키워드가 있는지 본다.
    대상 구조가 바뀌어 파싱이 깨져도 알림을 놓치지 않기 위한 안전망.
    응답에는 해당 날짜 목록만 들어있어 오탐 위험이 낮다.
    """
    low = body.lower()
    return any(k.lower() in low for k in keywords)


def match_keywords(items, keywords):
    hits = []
    for it in items:
        low = it["name"].lower()
        if any(k.lower() in low for k in keywords):
            hits.append(it)
    return hits


# ---------------------------------------------------------------- 알림

def format_items(items):
    lines = []
    for it in items:
        lines.append("▪ %s" % it["name"])
        for g in it["groups"]:
            lines.append("   %s" % g["name"])
            for s in g["slots"]:
                note = (" [%s]" % s["note"]) if s["note"] else ""
                extra = s.get("seat_note", "")
                lines.append("     %s%s%s" % (s["time"], note, extra))
    return lines


def slot_label(item, group, slot):
    """사람이 보는 회차 표기 (영화명|관|시간). 알림 문구와 ID 검증 기록에 쓴다."""
    return "%s|%s|%s" % (item["name"], group["name"], slot["time"])


def slot_key(item, group, slot):
    """
    회차를 구분하는 키. 사이트 ID(screenplanid)가 있으면 그것으로, 없으면
    (우회로 markdown 으로 받은 경우) 영화명|관|시간 으로 만든다.

    2026-11-05 20:32 극장이 여러 회차 시간을 5분씩 당겼을 때 ID 는 그대로였다.
    시간으로 구분하면 그때마다 같은 회차를 새 회차로 보고, 직전 0석 기록도 끊긴다.
    ID 가 계속 고정인지는 observe_slots 에서 계속 검증한다.
    """
    sid = str((slot.get("attrs") or {}).get("screenplanid") or "")
    return ("id:" + sid) if sid else slot_label(item, group, slot)


def observe_slots(state, items):
    """
    이번 조회의 회차 전체를 state.slot_ids ({표기: ID}) 와 비교한다.

    반환: (새 회차 키 목록, 이상 설명 목록, 회차 정보 변경 목록, state 가 바뀌었는지)
      - 처음 보는 날짜(기록이 비어 있음)면 전부 기준으로 삼고 새 회차로 치지 않는다.
      - 같은 ID 인데 시간 등 표기가 바뀐 것은 이상이 아니라 "변경"으로 돌려준다.
        예전 표기는 기록에서 지워서 같은 변경을 다시 보고하지 않는다.
      - 이상: 같은 표기인데 ID 가 바뀜 / 한 번의 조회 안에서 ID 가 겹침.
    """
    seen = {}
    anomalies = []
    for it in items:
        for g in it["groups"]:
            for s in g["slots"]:
                k = slot_label(it, g, s)
                sid = str((s.get("attrs") or {}).get("screenplanid") or "")
                if k in seen and sid and seen[k] and seen[k] != sid:
                    anomalies.append("같은 회차에 ID 두 개: %s (%s, %s)" % (k, seen[k], sid))
                seen[k] = sid or seen.get(k, "")

    by_id = {}
    for k, sid in seen.items():
        if not sid:
            continue
        if sid in by_id and by_id[sid] != k:
            anomalies.append("한 ID(%s)에 회차 두 개: %s / %s" % (sid, by_id[sid], k))
        by_id[sid] = k

    old = state.slot_ids
    old_labels = {}                          # ID -> 그 ID 로 기록된 표기들
    for k, v in old.items():
        if v:
            old_labels.setdefault(v, []).append(k)

    changes = []
    merged = dict(old)
    for k, sid in seen.items():
        if not sid:
            continue
        if old.get(k) and old[k] != sid:
            anomalies.append("같은 회차의 ID 변경: %s  %s -> %s" % (k, old[k], sid))
        prev = [p for p in old_labels.get(sid, []) if p != k]
        if prev and k not in old_labels.get(sid, []):
            changes.append("%s -> %s" % (prev[0], k))
        for p in prev:
            if merged.get(p) == sid:
                del merged[p]                # 같은 ID 의 예전 표기는 지운다

    baseline = not old
    new_keys = []
    if not baseline:
        for it in items:
            for g in it["groups"]:
                for s in g["slots"]:
                    k = slot_label(it, g, s)
                    sid = seen.get(k, "")
                    known = (k in old) or (sid and sid in old_labels)
                    if not known:
                        new_keys.append(slot_key(it, g, s))

    for k, sid in seen.items():
        if sid or k not in merged:
            merged[k] = sid
    changed = merged != old
    state.slot_ids = merged
    if state.id_since is None and any(seen.values()):
        state.id_since = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        changed = True
    return new_keys, anomalies, changes, changed


def only_slots(items, keys):
    """items 에서 keys 에 해당하는 회차만 남긴 사본."""
    keys = set(keys)
    out = []
    for it in items:
        groups = []
        for g in it["groups"]:
            slots = [s for s in g["slots"] if slot_key(it, g, s) in keys]
            if slots:
                groups.append(dict(g, slots=slots))
        if groups:
            out.append(dict(it, groups=groups))
    return out


def check_seats(cfg, fetcher, items, pump=None):
    """
    목록상 매진이거나 잔여가 적은 회차만 좌석까지 들여다본다.
    각 회차에 seat_note 를 붙이고, {회차키(slot_key): 일반 빈자리수} 를 돌려준다.

    좌석까지 보지 않은 회차(잔여가 넉넉한 회차)는 목록의 잔여수를 그대로 넣는다.
    그래야 매진이던 회차가 한꺼번에 많이 풀린 경우도 비교할 수 있다.
    조회에 실패한 회차는 결과에 넣지 않는다.

    좌석 조회는 회차마다 왕복이 한 번씩 필요해서 느리다. 그동안 텔레그램
    명령이 밀리지 않도록, 좌석을 조회하기 직전마다 pump() 를 불러 먼저 처리한다.
    """
    result = {}
    origin = "/".join(cfg.target_url.split("/")[:3])
    for it in items:
        for g in it["groups"]:
            for s in g["slots"]:
                attrs = s.get("attrs") or {}
                key = slot_key(it, g, s)
                note = s.get("note")
                if not seats.needs_closer_look(note):
                    n = seats.list_count(note)
                    if n is not None:
                        result[key] = n
                    continue
                if not attrs.get("screenplanid"):
                    continue                 # 좌석 조회에는 사이트 ID 가 필요하다
                if pump:
                    pump()
                html = seats.fetch(fetcher.sess, origin, attrs)
                if html is None:
                    s["seat_note"] = " (좌석 확인 실패)"
                    continue
                c = seats.count(html)
                result[key] = c["free"]
                if c["free"]:
                    s["seat_note"] = " -> 앉을 수 있는 자리 %d (%s)" % (
                        c["free"], ", ".join(seats.free_labels(html, 8)))
                    if "매진" in (note or ""):
                        # 목록은 매진인데 배치도엔 빈자리가 보인다. 판정이 틀렸을
                        # 수 있으니 표시해두고, 원인을 볼 수 있게 태그 원문을 남긴다.
                        s["seat_note"] += " ※목록은 매진, 직접 확인 필요"
                        log("매진 회차 %s 에 빈자리 판정 %d석: %s" % (
                            key, c["free"], " || ".join(seats.free_tags(html))[:600]))
                else:
                    s["seat_note"] = " -> 없음%s" % (
                        " (장애인석 %d 비어있음)" % c["free_special"]
                        if c["free_special"] else "")
    return result


def build_message(cfg, target, hits):
    head = "찾았습니다!"
    if cfg.label:
        head = "%s - 찾았습니다!" % cfg.label
    lines = [head,
             "날짜 %s (%s)" % (target.strftime("%Y%m%d"), WDAY[target.weekday()]),
             ""]
    if hits:
        lines.extend(format_items(hits))
        lines.append("")
    else:
        lines.append("※ 원문에는 키워드가 있는데 상세 파싱에 실패했습니다.")
        lines.append("   직접 확인하세요.")
        lines.append("")
    if cfg.site_url:
        lines.append(cfg.site_url)
    return "\n".join(lines)


# ---------------------------------------------------------------- 실행 게이트

def should_watch(cfg, target, now):
    """
    지금 이 시점에 target 날짜를 감시해야 하는지 판단.
    감시할 필요가 없으면 대기 모드로 들어간다. (감시여부, 사유)
    """
    if target is None:
        return False, "지정된 날짜가 없습니다. 텔레그램에서 /watch 20261107 로 지정하세요."
    if cfg.force:
        return True, "FORCE=1 이라 날짜 조건을 무시합니다."

    today = now.date()
    if today > target:
        return False, "지정일(%s)이 이미 지났습니다." % target.strftime("%Y%m%d")
    if cfg.mode == "onday" and today != target:
        return False, ("오늘은 지정일(%s)이 아닙니다. MODE=onday 라 대기합니다."
                       % target.strftime("%Y%m%d"))
    return True, "날짜 %s 감시 중 (D-%d)" % (target.strftime("%Y%m%d"), (target - today).days)


def interval_for(cfg, now):
    minute = now.hour * 60 + now.minute
    for a, b in cfg.burst_windows:
        if a <= minute <= b:
            return cfg.burst_sec
    return cfg.poll_sec


# ---------------------------------------------------------------- main

def snapshot(cfg, fetcher, target, show_all=False):
    """
    지금 목록을 한 번 조회해서 사람이 읽을 문자열로 돌려준다.
    show_all=False 면 키워드와 일치하는 것만 보여준다. (/now)
    show_all=True  면 그날 전체를 보여준다.              (/nowall)
    """
    if target is None:
        return "지정된 날짜가 없습니다. /watch 20261107 처럼 지정하세요."
    body, fmt = fetcher.fetch(target)
    if body is None:
        return "조회에 실패했습니다. 잠시 후 다시 시도하세요."

    items = parse(body, fmt)
    ymd = target.strftime("%Y%m%d")
    wday = WDAY[target.weekday()]

    if show_all:
        head = "날짜 %s (%s) / 전체 %d개" % (ymd, wday, len(items))
        if not items:
            return head + "\n아직 목록이 올라오지 않았습니다."
        return "\n".join([head, ""] + format_items(items))

    hits = match_keywords(items, cfg.keywords)
    if not hits:
        return ("날짜 %s (%s)\n아직 없습니다. (그날 전체 %d개)\n전체를 보려면 /nowall"
                % (ymd, wday, len(items)))
    check_seats(cfg, fetcher, hits)          # 매진/소수 잔여 회차는 좌석까지 확인
    return "\n".join(["날짜 %s (%s) / %d건" % (ymd, wday, len(hits)), ""] + format_items(hits))


def raise_alert(cfg, state, tg, text):
    """
    알림을 걸어둔다. /ok 를 받을 때까지 계속 다시 보낸다.

    이미 확인 안 된 알림이 있으면 덮어쓰지 않고 뒤에 이어 붙인다.
    새 소식이 와도 앞의 내용이 사라지지 않게 하려는 것이다.
    """
    entry = "[%s] %s" % (datetime.now(KST).strftime("%H:%M"), text)
    if state.ack_date is not None and state.ack_text:
        state.ack_text = state.ack_text + "\n\n" + ("-" * 20) + "\n" + entry
    else:
        state.ack_text = entry

    state.ack_date = state.target or state.ack_date
    state.ack_count = 1                   # 새 소식이니 횟수는 다시 센다
    state.ack_last = datetime.now(KST)
    state.save("alert")
    tg.send(state.ack_text + "\n\n확인하셨으면 /ok 를 보내주세요.")


def resend_alert(cfg, state, tg, fetcher):
    """미확인 알림이 있으면 간격에 맞춰 다시 보낸다."""
    if state.ack_date is None:
        return
    now = datetime.now(KST)
    if state.ack_last and (now - state.ack_last).total_seconds() < cfg.repeat_sec:
        return

    if not state.ack_text:
        # 세션이 바뀌어 본문이 없어졌다. 이때만 새로 조회한다.
        state.ack_text = snapshot(cfg, fetcher, state.ack_date)

    state.ack_count += 1
    state.ack_last = now
    tg.send("%s\n\n아직 확인하지 않으셨습니다. (%d번째)\n확인하셨으면 /ok 를 보내주세요."
            % (state.ack_text, state.ack_count))
    if state.ack_count % 12 == 0:          # 횟수는 참고값이라 저장소에는 안 올린다
        state.save("ack %d" % state.ack_count, commit=False)


def main():
    cfg = Config()
    start = datetime.now(KST)

    tg = Telegram(cfg.tg_token, cfg.tg_chat)
    state = State(cfg.gh_token, cfg.gh_repo, cfg.gh_branch)

    # 날짜 우선순위: 텔레그램으로 지정한 state.json > TARGET_DATE 환경변수
    if state.target is None and cfg.target is not None:
        state.target = cfg.target
        state.save("init from env")

    log("기동 완료 / 키워드 %d개" % len(cfg.keywords))
    log("간격 평상시 %d초, 집중 %d초, 집중구간 %s"
        % (cfg.poll_sec, cfg.burst_sec, cfg.burst_windows or "없음"))
    if not tg.enabled:
        log("경고: 텔레그램이 설정되지 않아 명령 수신이 불가합니다.")

    fetcher = Fetcher(cfg)
    deadline = start + timedelta(minutes=cfg.max_run_min)
    relay_at = start + timedelta(minutes=cfg.relay_after_min)
    relay_last = None        # 마지막으로 다음 실행을 요청한 시각

    # ---- 생존 기록. 이전 세션과의 감시 공백을 잰다
    health = Health(cfg.gh_token, cfg.gh_repo, cfg.gh_branch, cfg.gh_run_id, cfg.gh_event,
                    ping_url=cfg.healthcheck_url, beat_sec=cfg.health_beat_sec)
    health.active = cfg.health_enabled and not state.health_off
    gap = health.start()
    if gap is not None and gap > cfg.gap_alert_sec:
        tg.send("감시 공백이 있었습니다: %d초 (%d분)\n이전 세션 run=%s\n/health 로 자세히 볼 수 있습니다."
                % (gap, gap // 60, health.prev_run))
    log("다음 workflow run 요청 예정 %s / 강제 종료 %s"
        % (relay_at.strftime("%H:%M:%S"), deadline.strftime("%H:%M:%S")))

    # ---- 멈춤 감시. 생존 기록도 이 스레드가 맡는다.
    # 메인 루프 안에서 기록하면, 루프가 멈출 때 기록까지 같이 끊겨서 어느
    # 단계였는지 단서가 남지 않는다 (11-06 16:01 실측).
    # 알림만으로는 감시가 돌아오지 않으므로, 더 오래 멈춰 있으면 다음 세션을
    # 띄우고 스스로 끝낸다. 요청이 실패하면 끝내지 않는다(아무도 안 받으면 통째로 빈다).
    wd = Watchdog(health, tg, cfg.stall_sec,
                  exit_sec=cfg.stall_exit_sec,
                  relay_fn=lambda: health.request_next(
                      cfg.relay_workflow, cfg.gh_branch,
                      {"force": "true"} if cfg.force else {}),
                  min_uptime_sec=cfg.stall_min_uptime_sec)
    wd.start()

    # 이어받기로 끊길 때(GitHub 취소) 마지막 생존 시각을 남기고 나간다.
    # 그래야 다음 세션이 공백을 정확히 잴 수 있다.
    def on_signal(signum, _frame):
        wd.stop()
        health.stop("cancelled(signal %d)" % signum, checks)
        sys.exit(0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    backoff = 0
    checks = 0
    last_count = None
    last_reason = None
    def ymd(d):
        return d.strftime("%Y%m%d") if d else None

    # 편성 발견을 이미 알렸는지, 회차별 직전 빈자리 수.
    # 이전 세션에서 이어받는다. 안 그러면 이어받을 때마다 "찾았습니다" 를 다시 보낸다.
    # 빈자리 수는 0 으로 확인된 회차만 저장해 둔다 (자리가 나는지 볼 대상은 그것뿐이고,
    # 다른 회차 숫자까지 저장하면 표가 팔릴 때마다 커밋이 생긴다).
    announced = state.target is not None and state.announced == ymd(state.target)
    # 예전 버전은 매진 회차를 "영화명|관|시간" 으로 저장했다. ID 를 알면 ID 키로 옮긴다
    migrated = {("id:" + state.slot_ids[k]) if state.slot_ids.get(k) else k
                for k in state.zero_slots}
    if migrated != state.zero_slots:
        state.zero_slots = migrated
        state.save("zero slots by id")
    free_before = {k: 0 for k in state.zero_slots} if announced else {}
    if announced:
        log("이전 세션에서 이미 알린 날짜입니다. 매진 회차 %d개를 이어서 지켜봅니다."
            % len(state.zero_slots))

    id_compares = 0          # 이번 세션에서 screenplanid 를 비교한 조회 횟수

    def id_check_lines():
        """/health 에 붙일 screenplanid 검증 현황."""
        if not state.id_since:
            return ["회차 ID 검증: 아직 시작 전 (ID 가 있는 조회가 없었음)"]
        return ["회차 ID 검증: %s 부터 / 이상 %d건 / 이번 세션 비교 %d회 / 기록된 회차 %d개"
                % (state.id_since, state.id_anomalies, id_compares, len(state.slot_ids))]

    def extra_lines():
        """/health 에 붙일 줄. 지금 어느 단계인지와 회차 ID 검증 현황."""
        return [wd.summary()] + id_check_lines()
    health.extra_fn = extra_lines

    def remember_zero():
        """0 으로 확인된 회차 목록을 state 에 반영. 바뀌었으면 True."""
        z = {k for k, v in free_before.items() if v == 0}
        if z == state.zero_slots:
            return False
        state.zero_slots = z
        return True

    def status_text():
        _ok, why = should_watch(cfg, state.target, datetime.now(KST))
        out = "%s\n조회 %d회 / 이 세션 종료까지 %d분" % (
            why, checks, max(0, int((deadline - datetime.now(KST)).total_seconds() // 60)))
        if state.ack_date is not None:
            out += "\n미확인 알림 있음 (%d번 보냄). /ok 로 멈춥니다." % state.ack_count
        if state.seatwatch:
            out += "\n빈자리 지켜보기: 켜짐"
        if not cfg.health_enabled:
            out += "\nhealth 체크: 꺼짐 (저장소 변수 HEALTH=0)"
        else:
            out += "\nhealth 체크: %s%s" % (
                "켜짐" if health.active else "꺼짐 (/health on 으로 켜기)",
                " / 외부 ping 사용" if (health.active and cfg.healthcheck_url) else "")
        r = health.relay
        if r["requested_at"]:
            out += "\n다음 workflow run 요청: %s %s" % (r["requested_at"][11:],
                                                 "성공" if r["ok"] else "실패")
        else:
            left = max(0, int((relay_at - datetime.now(KST)).total_seconds() // 60))
            out += "\n다음 workflow run 요청 예정: %s (%d분 뒤)" % (relay_at.strftime("%H:%M"), left)
        return out + ("\n" + cfg.site_url if cfg.site_url else "")

    if cfg.notify_start:
        tg.send("감시 프로세스가 올라왔습니다.\n" + status_text())

    target_changed = [False]   # pump() 안에서 감시 대상이 바뀌었는지

    def pump():
        """쌓인 텔레그램 명령을 처리한다. 루프 맨 앞과 좌석 조회 사이사이에 부른다."""
        # 좌석 조회는 회차마다 왕복이라 오래 걸린다. 그 사이에도 이 함수가 불리므로
        # 여기서 진행 표시를 갱신해 두면 멈춘 것으로 오해받지 않는다.
        wd.mark("텔레그램 명령")
        for update_id, text in tg.poll(state.tg_offset):
            state.tg_offset = update_id
            if text is None:                 # 내 대화방이 아닌 메시지
                state.save("offset", commit=False)
                continue
            log("명령 수신")
            if handle_command(
                    text, state, tg, status_text,
                    lambda d, allitems=False: snapshot(cfg, fetcher, d, allitems),
                    health_fn=health.summary):
                target_changed[0] = True
            state.save("offset", commit=False)

    while datetime.now(KST) < deadline:
        # 생존 기록은 Watchdog 스레드가 맡는다. 여기서는 지금 어느 단계인지만 알린다.
        wd.mark("루프 시작", checks)

        # ---- 다음 workflow run 요청. 성공 후에도 안 끊기면 5분 뒤, 실패면 1분 뒤 다시 요청
        now = datetime.now(KST)
        if now >= relay_at:
            wait = 300 if health.relay["ok"] else 60
            if relay_last is None or (now - relay_last).total_seconds() >= wait:
                relay_last = now
                wd.mark("다음 workflow run 요청")
                health.request_next(cfg.relay_workflow, cfg.gh_branch,
                                    {"force": "true"} if cfg.force else {})

        # ---- 텔레그램 명령 먼저 처리
        wd.mark("텔레그램 명령")
        pump()
        health.active = cfg.health_enabled and not state.health_off   # /health on|off 반영
        if target_changed[0]:
            # 감시 대상이 바뀌었으니 직전 날짜에 대한 판단은 전부 버린다
            target_changed[0] = False
            last_count = None
            last_reason = None
            checks = 0
            announced = False
            free_before = {}
            if state.announced or state.zero_slots or state.slot_ids:
                state.announced = None
                state.zero_slots = set()
                state.slot_ids = {}          # 회차 기록은 날짜별이다. ID 검증 누적치는 유지
                state.save("reset progress")

        # ---- 미확인 알림 반복 전송
        wd.mark("알림 재전송")
        resend_alert(cfg, state, tg, fetcher)

        target = state.target
        watching, reason = should_watch(cfg, target, datetime.now(KST))

        if not watching:
            if state.ack_date is not None:
                # 감시는 끝났지만 아직 확인을 못 받았다. 재전송만 하며 대기한다.
                time.sleep(min(cfg.repeat_sec, cfg.idle_sec))
                continue
            if reason != last_reason:
                log(reason + " 대기 모드로 텔레그램 명령만 기다립니다.")
                last_reason = reason
            time.sleep(cfg.idle_sec)
            continue

        if reason != last_reason:
            log(reason)
            last_reason = reason

        # ---- 조회
        checks += 1
        wd.mark("목록 조회", checks)
        body, fmt = fetcher.fetch(target)

        if body is None:
            backoff = min(120, (backoff * 2) if backoff else 30)
            log("조회 실패. %d초 후 재시도합니다." % backoff)
            health.error("조회 실패 (%s)" % ("우회" if fetcher.use_proxy else "직접"))
            wd.mark("조회 실패 후 대기 %d초" % backoff)
            time.sleep(backoff)
            continue
        backoff = 0

        wd.mark("목록 해석")
        items = parse(body, fmt)

        # ---- 회차 기록 갱신 + screenplanid 검증
        new_keys, anomalies, slot_changes, slots_changed = observe_slots(state, items)
        if slot_changes:
            # 같은 ID 인 회차의 시간 등이 바뀌었다. 이상은 아니고 한 번만 알린다
            for c in slot_changes:
                health.event("[회차 변경] " + c)
            tg.send("[회차 정보 변경] %d건 (같은 회차 ID, 시간 등 변경)\n\n%s"
                    % (len(slot_changes), "\n".join(slot_changes[:30])))
        if any((s.get("attrs") or {}).get("screenplanid")
               for it in items for g in it["groups"] for s in g["slots"]):
            id_compares += 1
        if anomalies:
            state.id_anomalies += len(anomalies)
            for a in anomalies:
                health.event("[ID 검증] " + a)
            tg.send("[회차 ID 검증] 이상 %d건 (누적 %d건)\n\n%s"
                    % (len(anomalies), state.id_anomalies, "\n".join(anomalies[:20])))
            slots_changed = True
        if slots_changed:
            state.save("slots %d" % len(state.slot_ids))

        if contains_keyword(body, cfg.keywords):
            hits = match_keywords(items, cfg.keywords)

            if not announced:
                # 편성이 뜬 것 자체를 처음 알리는 경우
                log("일치 항목 %d건 발견. 알림을 보냅니다." % len(hits))
                wd.mark("좌석 조회")
                free_before = check_seats(cfg, fetcher, hits, pump)
                if target_changed[0]:
                    continue                 # 조회 도중 날짜가 바뀌었다. 결과는 버린다
                text = build_message(cfg, target, hits)
                if cfg.verbose:
                    print(text, flush=True)
                announced = True
                state.announced = ymd(target)
                remember_zero()
                raise_alert(cfg, state, tg, text)   # 여기서 state 가 같이 저장된다
                if not state.seatwatch:
                    state.target = None      # 감시는 끝. 확인 받을 때까지 재전송만 한다
                    state.save("done")
                    continue
                tg.send("이어서 빈자리를 지켜봅니다. 끄려면 /seatwatch off")
            elif state.seatwatch:
                # 매진이던 회차에 자리가 나면 알린다.
                # 직전에 0 으로 "확인된" 회차만 본다. 기록이 없는 회차(조회 실패 등)를
                # 0 으로 치면, 원래 자리가 있던 회차도 새로 난 것처럼 알림이 간다.
                wd.mark("좌석 조회")
                free_now = check_seats(cfg, fetcher, hits, pump)
                if target_changed[0]:
                    continue                 # 조회 도중 날짜가 바뀌었다. 결과는 버린다
                opened = [k for k, v in free_now.items()
                          if v > 0 and free_before.get(k) == 0]
                # 이번에 조회 못 한 회차는 직전 값을 유지한다
                free_before = dict(free_before, **free_now)
                zero_changed = remember_zero()

                # 찾는 항목에 회차가 새로 생겼으면 알린다
                hit_keys = {slot_key(it, g, s) for it in hits
                            for g in it["groups"] for s in g["slots"]}
                added = [k for k in new_keys if k in hit_keys]
                if added:
                    log("회차 추가 %d건" % len(added))
                    raise_alert(cfg, state, tg,     # 여기서 state 가 같이 저장된다
                                "회차가 추가됐습니다!\n날짜 %s (%s)\n\n%s\n\n%s"
                                % (target.strftime("%Y%m%d"), WDAY[target.weekday()],
                                   "\n".join(format_items(only_slots(hits, added))),
                                   cfg.site_url))

                if opened:
                    log("빈자리 발생 %d건" % len(opened))
                    raise_alert(cfg, state, tg,     # 여기서 state 가 같이 저장된다
                                "자리가 났습니다!\n날짜 %s (%s)\n\n%s\n\n%s"
                                % (target.strftime("%Y%m%d"), WDAY[target.weekday()],
                                   "\n".join(format_items(hits)), cfg.site_url))
                elif zero_changed and not added:
                    state.save("zero slots %d" % len(state.zero_slots))

        # trace 가 켜져 있으면 못 찾았을 때도 매 조회마다 한 줄씩 보고한다
        if state.trace_on():
            tg.send("[trace] %s · %d회 · 없음 (전체 %d개) · %s"
                    % (datetime.now(KST).strftime("%H:%M:%S"), checks, len(items),
                       "우회" if fetcher.use_proxy else "직접"))
        elif state.trace_until is not None:
            state.trace_until = None
            state.save("trace expired")
            tg.send("trace 시간이 끝나 종료했습니다. 감시는 계속됩니다.")

        # 목록 개수가 바뀌면 로그에 남겨 추적할 수 있게 한다
        if len(items) != last_count:
            if cfg.verbose:
                log("현재 %d개: %s" % (len(items), " | ".join(it["name"] for it in items)))
            else:
                log("현재 %d개" % len(items))
            last_count = len(items)
        elif checks % 30 == 0:
            log("감시 중... %d회 조회, 변동 없음 (%d개)" % (checks, len(items)))

        wd.mark("다음 조회 대기")
        time.sleep(interval_for(cfg, datetime.now(KST)))

    wd.stop()
    log("이 세션의 실행 시간(%d분)이 끝났습니다. 다음 세션이 이어서 감시합니다." % cfg.max_run_min)
    # 이어받기 없이 시간이 다 됐다는 건 다음 workflow run 요청이 계속 실패했다는 뜻이다
    health.stop("ended(timeout)", checks)
    tg.send("다음 workflow run 없이 이 세션이 끝났습니다. 다음 예약 실행까지 감시가 비어 있을 수 있습니다.\n"
            "다음 workflow run 요청: %d회 시도, 마지막 %s"
            % (health.relay["tries"], "성공" if health.relay["ok"] else "실패"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
