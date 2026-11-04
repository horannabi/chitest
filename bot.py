#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
텔레그램 명령 수신 + 감시 상태 보관.

명령
    /watch 20261115   감시할 날짜 지정 (1115, 11-15, 11/15 도 허용)
    /status           현재 상태
    /now              지금 바로 한 번 조회해서 알려주기
    /stop             감시 중지
    /help             도움말

상태는 state.json 에 보관하고, GitHub API 로 커밋해서
실행 세션이 바뀌어도 유지되게 한다. (로컬 실행 시에는 파일에만 저장)
"""

import os
import re
import json
import base64
from datetime import datetime, date, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
STATE_FILE = "state.json"

WDAY = ("월", "화", "수", "목", "금", "토", "일")

HELP = """사용법

/watch 20261115   이 날짜에 뜨면 알림
/status           현재 감시 상태
/now              지금 바로 확인
/stop             감시 중지
/help             이 도움말

날짜는 20261115 형식입니다. (1115 처럼 월일만 보내도 됩니다)"""


def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 날짜 파싱

def parse_date(text):
    """사용자가 보낸 문자열에서 날짜를 뽑아낸다. 실패하면 None."""
    s = (text or "").strip().replace(".", "-").replace("/", "-")
    today = datetime.now(KST).date()
    year_omitted = False

    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)              # 20261115 (기본)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)    # 2026-11-15
        if m:
            y, mo, d = (int(x) for x in m.groups())
        else:
            m = re.match(r"^(\d{2})(\d{2})$", s)             # 1115
            if not m:
                m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)    # 11-15
            if not m:
                return None
            mo, d = int(m.group(1)), int(m.group(2))
            y = today.year
            year_omitted = True

    try:
        result = date(y, mo, d)
    except ValueError:
        return None

    # 연도를 생략했고 이미 지난 날짜면 내년으로 본다
    if year_omitted and result < today:
        try:
            result = date(y + 1, mo, d)
        except ValueError:
            return None
    return result


# ---------------------------------------------------------------- 상태

class State:
    """감시 상태. state.json 에 저장하고 가능하면 저장소에도 커밋한다."""

    def __init__(self, gh_token=None, gh_repo=None, branch=None):
        self.gh_token = gh_token
        self.gh_repo = gh_repo
        self.branch = branch or "main"
        self.target = None          # date | None
        self.tg_offset = 0          # 처리한 텔레그램 update_id
        self._sha = None            # 저장소 파일 sha (커밋에 필요)
        self.load()

    # ---- 읽기

    def load(self):
        data = {}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                log("state.json 읽기 실패(무시): %s" % e)

        raw = data.get("target_date")
        if raw:
            self.target = parse_date(str(raw))
        self.tg_offset = int(data.get("tg_offset") or 0)
        self._sha = self._remote_sha()

    def _remote_sha(self):
        if not (self.gh_token and self.gh_repo):
            return None
        try:
            r = requests.get(
                "https://api.github.com/repos/%s/contents/%s" % (self.gh_repo, STATE_FILE),
                headers=self._headers(), params={"ref": self.branch}, timeout=20)
        except requests.RequestException:
            return None
        if r.status_code == 200:
            return r.json().get("sha")
        return None

    # ---- 쓰기

    def save(self, reason=""):
        payload = {
            "target_date": self.target.strftime("%Y%m%d") if self.target else None,
            "tg_offset": self.tg_offset,
            "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                f.write(body)
        except OSError as e:
            log("state.json 쓰기 실패: %s" % e)

        self._commit(body, reason)

    def _commit(self, body, reason):
        if not (self.gh_token and self.gh_repo):
            return
        payload = {
            "message": "state: %s" % (reason or "update"),
            "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if self._sha:
            payload["sha"] = self._sha
        try:
            r = requests.put(
                "https://api.github.com/repos/%s/contents/%s" % (self.gh_repo, STATE_FILE),
                headers=self._headers(), data=json.dumps(payload).encode("utf-8"), timeout=20)
        except requests.RequestException as e:
            log("state 커밋 예외: %s" % str(e)[:160])
            return

        if r.status_code in (200, 201):
            self._sha = r.json().get("content", {}).get("sha")
        elif r.status_code == 409:
            # 다른 job 이 먼저 썼다. sha 를 다시 읽고 다음 저장 때 반영한다.
            log("state 커밋 충돌. sha 를 다시 읽습니다.")
            self._sha = self._remote_sha()
        else:
            log("state 커밋 실패 %s: %s" % (r.status_code, r.text[:200]))

    def _headers(self):
        return {"Authorization": "Bearer %s" % self.gh_token,
                "Accept": "application/vnd.github+json"}


# ---------------------------------------------------------------- 텔레그램

class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.enabled = bool(token and chat_id)

    def _url(self, method):
        return "https://api.telegram.org/bot%s/%s" % (self.token, method)

    def send(self, text):
        if not self.enabled:
            log("(텔레그램 미설정) " + text.replace("\n", " / ")[:200])
            return False
        try:
            r = requests.post(self._url("sendMessage"),
                              data={"chat_id": self.chat_id, "text": text}, timeout=20)
        except requests.RequestException as e:
            log("텔레그램 전송 예외: %s" % str(e)[:160])
            return False
        if r.status_code != 200:
            log("텔레그램 전송 실패 %s: %s" % (r.status_code, r.text[:200]))
            return False
        return True

    def poll(self, offset):
        """새 명령을 [(update_id, 본문), ...] 로 반환."""
        if not self.enabled:
            return []
        try:
            r = requests.get(self._url("getUpdates"),
                             params={"offset": offset + 1, "timeout": 0, "limit": 20},
                             timeout=20)
        except requests.RequestException as e:
            log("텔레그램 수신 예외: %s" % str(e)[:160])
            return []
        if r.status_code != 200:
            log("텔레그램 수신 실패 %s" % r.status_code)
            return []

        out = []
        for upd in r.json().get("result", []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            # 내 대화방 메시지만 처리한다
            if text and chat == self.chat_id:
                out.append((upd["update_id"], text))
            elif text:
                log("다른 대화방(%s) 메시지는 무시합니다." % chat)
                out.append((upd["update_id"], None))
        return out


# ---------------------------------------------------------------- 명령 처리

def handle_command(text, state, tg, status_fn, now_fn):
    """
    명령 하나를 처리한다.
    status_fn() -> 상태 문자열, now_fn() -> 즉시 조회 결과 문자열
    반환값: 감시 대상 날짜가 바뀌었으면 True
    """
    cmd = text.strip()
    low = cmd.lower()
    head = low.split()[0] if low.split() else ""
    arg = cmd[len(head):].strip()

    if head in ("/help", "help", "/start"):
        tg.send(HELP)
        return False

    if head in ("/status", "status"):
        tg.send(status_fn())
        return False

    if head in ("/now", "now"):
        tg.send(now_fn())
        return False

    if head in ("/stop", "stop"):
        state.target = None
        state.save("stop")
        tg.send("감시를 중지했습니다. 다시 시작하려면 /watch 날짜 를 보내세요.")
        return True

    if head in ("/watch", "watch"):
        d = parse_date(arg)
        if not d:
            tg.send("날짜를 알아듣지 못했습니다: %r\n예) /watch 20261115" % arg)
            return False
        return _set_date(d, state, tg)

    # 명령어 없이 날짜만 보낸 경우도 받아준다
    d = parse_date(cmd)
    if d:
        return _set_date(d, state, tg)

    tg.send("모르는 명령입니다.\n\n" + HELP)
    return False


def _set_date(d, state, tg):
    today = datetime.now(KST).date()
    if d < today:
        tg.send("%s 는 이미 지난 날짜입니다." % d.strftime("%Y%m%d"))
        return False
    state.target = d
    state.save("watch %s" % d.strftime("%Y%m%d"))
    tg.send("감시 시작합니다.\n날짜 %s (%s) D-%d\n뜨는 즉시 알려드릴게요."
            % (d.strftime("%Y%m%d"), WDAY[d.weekday()], (d - today).days))
    return True
