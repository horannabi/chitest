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

import os
import re
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
        self.idle_sec = env_int("IDLE_SEC", 5)

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
        })
        if cfg.site_url:
            self.sess.headers["Referer"] = cfg.site_url
        self.use_proxy = False
        self.direct_fail = 0

    def fetch(self, target):
        url = self.cfg.url_for(target)

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


def check_seats(cfg, fetcher, items):
    """
    목록상 매진이거나 잔여가 적은 회차만 좌석까지 들여다본다.
    각 회차에 seat_note 를 붙이고, {회차키: 일반 빈자리수} 를 돌려준다.
    """
    result = {}
    origin = "/".join(cfg.target_url.split("/")[:3])
    for it in items:
        for g in it["groups"]:
            for s in g["slots"]:
                if not seats.needs_closer_look(s.get("note")):
                    continue
                attrs = s.get("attrs") or {}
                key = attrs.get("screenplanid")
                if not key:
                    continue
                html = seats.fetch(fetcher.sess, origin, attrs)
                if html is None:
                    s["seat_note"] = " (좌석 확인 실패)"
                    continue
                c = seats.count(html)
                result[key] = c["free"]
                if c["free"]:
                    s["seat_note"] = " -> 앉을 수 있는 자리 %d (%s)" % (
                        c["free"], ", ".join(seats.free_labels(html, 8)))
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

    if show_all:
        head = "날짜 %s / 전체 %d개" % (ymd, len(items))
        if not items:
            return head + "\n아직 목록이 올라오지 않았습니다."
        return "\n".join([head, ""] + format_items(items))

    hits = match_keywords(items, cfg.keywords)
    if not hits:
        return ("날짜 %s\n아직 없습니다. (그날 전체 %d개)\n전체를 보려면 /nowall"
                % (ymd, len(items)))
    check_seats(cfg, fetcher, hits)          # 매진/소수 잔여 회차는 좌석까지 확인
    return "\n".join(["날짜 %s / %d건" % (ymd, len(hits)), ""] + format_items(hits))


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
    backoff = 0
    checks = 0
    last_count = None
    last_reason = None
    announced = False        # 편성 발견을 이미 알렸는지
    free_before = {}         # 회차별 직전 빈자리 수

    def status_text():
        _ok, why = should_watch(cfg, state.target, datetime.now(KST))
        out = "%s\n조회 %d회 / 이 세션 종료까지 %d분" % (
            why, checks, max(0, int((deadline - datetime.now(KST)).total_seconds() // 60)))
        return out + ("\n" + cfg.site_url if cfg.site_url else "")

    if cfg.notify_start:
        tg.send("감시 프로세스가 올라왔습니다.\n" + status_text())

    while datetime.now(KST) < deadline:
        # ---- 텔레그램 명령 먼저 처리
        for update_id, text in tg.poll(state.tg_offset):
            state.tg_offset = update_id
            if text is None:                 # 내 대화방이 아닌 메시지
                state.save("offset")
                continue
            log("명령 수신")
            changed = handle_command(
                text, state, tg, status_text,
                lambda d, allitems=False: snapshot(cfg, fetcher, d, allitems))
            if changed:
                last_count = None
                last_reason = None
                checks = 0
            state.save("offset")

        target = state.target
        watching, reason = should_watch(cfg, target, datetime.now(KST))

        if not watching:
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
        body, fmt = fetcher.fetch(target)

        if body is None:
            backoff = min(120, (backoff * 2) if backoff else 30)
            log("조회 실패. %d초 후 재시도합니다." % backoff)
            time.sleep(backoff)
            continue
        backoff = 0

        items = parse(body, fmt)

        if contains_keyword(body, cfg.keywords):
            hits = match_keywords(items, cfg.keywords)

            if not announced:
                # 편성이 뜬 것 자체를 처음 알리는 경우
                log("일치 항목 %d건 발견. 알림을 보냅니다." % len(hits))
                check_seats(cfg, fetcher, hits)
                text = build_message(cfg, target, hits)
                if cfg.verbose:
                    print(text, flush=True)
                tg.send(text)
                announced = True
                if not state.seatwatch:
                    state.target = None      # 빈자리 감시를 안 하면 여기서 끝
                    state.save("done")
                    return 0
                tg.send("이어서 빈자리를 지켜봅니다. 끄려면 /seatwatch off")
                free_before = check_seats(cfg, fetcher, hits)
            elif state.seatwatch:
                # 매진이던 회차에 자리가 나면 알린다
                free_now = check_seats(cfg, fetcher, hits)
                opened = [k for k, v in free_now.items()
                          if v > 0 and free_before.get(k, 0) == 0]
                if opened:
                    log("빈자리 발생 %d건" % len(opened))
                    tg.send("자리가 났습니다!\n날짜 %s (%s)\n\n%s\n\n%s"
                            % (target.strftime("%Y%m%d"), WDAY[target.weekday()],
                               "\n".join(format_items(hits)), cfg.site_url))
                free_before = free_now

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

        time.sleep(interval_for(cfg, datetime.now(KST)))

    log("이 세션의 실행 시간(%d분)이 끝났습니다. 다음 세션이 이어서 감시합니다." % cfg.max_run_min)
    return 0


if __name__ == "__main__":
    sys.exit(main())
