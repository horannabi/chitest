#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
세션 생존 기록 + 다음 세션 띄우기(relay).

기록은 main 과 커밋이 부딪히지 않도록 별도 브랜치(health)에 남긴다.

    health.json   마지막 생존 시각, 세션 정보, 조회 횟수, 최근 오류 (자주 덮어씀)
    health.log    세션 시작/요청/종료/감시 공백 같은 사건 기록 (최근 500줄 유지)

새 세션은 시작할 때 이전 세션의 마지막 생존 시각을 읽어, 그 사이에
감시가 비어 있던 시간(공백)을 계산해 남긴다.

HEALTHCHECK_URL 이 있으면 생존 기록 때마다 그 주소로 GET 을 보낸다.
(healthchecks.io 같은 외부 감시 서비스에 연결하면, ping 이 끊겼을 때
 GitHub 와 무관하게 따로 알림을 받을 수 있다)
"""

import json
import base64
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
FMT = "%Y-%m-%d %H:%M:%S"
API = "https://api.github.com/repos/%s"
LOG_KEEP = 500


def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


def now_kst():
    return datetime.now(KST)


def fmt(dt):
    return dt.strftime(FMT) if dt else None


def parse(s):
    try:
        return datetime.strptime(str(s), FMT).replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


class Health:
    def __init__(self, gh_token, gh_repo, base_branch, run_id, run_event,
                 ping_url=None, beat_sec=60, branch="health"):
        self.token = gh_token
        self.repo = gh_repo
        self.base_branch = base_branch or "main"
        self.branch = branch
        self.run_id = run_id or "local"
        self.run_event = run_event or "local"
        self.ping_url = ping_url
        self.beat_sec = max(10, beat_sec)
        self.enabled = bool(gh_token and gh_repo)
        # False 면 기록(health 브랜치)과 외부 ping 을 하지 않는다. /health off 로 끈다.
        # 다음 실행 요청(request_next)은 이 값과 무관하게 동작한다.
        self.active = True
        # /health 응답 끝에 덧붙일 줄을 돌려주는 함수 (회차 ID 검증 현황 등)
        self.extra_fn = None

        self.started = now_kst()
        self.last_beat = None          # 마지막으로 health.json 을 쓴 시각
        self.last_ping = None
        self.last_error = None
        self.error_count = 0
        self.gap_sec = None            # 이번 세션 시작 시 잰 감시 공백
        self.prev_run = None
        self.relay = {"requested_at": None, "ok": None, "tries": 0}
        self._sha = {}                 # 파일별 sha

    # ------------------------------------------------------------ 시작/종료

    def start(self):
        """
        브랜치를 준비하고, 이전 세션의 마지막 생존 시각으로 공백을 잰다.
        반환: 공백(초) 또는 None (이전 기록이 없을 때)
        """
        if not self.active:
            log("health: 꺼져 있어 기록하지 않습니다.")
            return None
        if not self.enabled:
            log("health: GitHub 정보가 없어 기록하지 않습니다.")
            return None
        self._ensure_branch()

        prev = self._read_json("health.json") or {}
        prev_seen = parse(prev.get("last_seen"))
        self.prev_run = prev.get("run_id")
        if prev_seen:
            self.gap_sec = int((self.started - prev_seen).total_seconds())

        if prev_seen is None:
            msg = "세션 시작 run=%s (%s). 이전 기록 없음" % (self.run_id, self.run_event)
        else:
            how = prev.get("status") or "?"
            msg = ("세션 시작 run=%s (%s). 이전 run=%s 마지막 생존 %s (%s) -> 공백 %d초"
                   % (self.run_id, self.run_event, self.prev_run,
                      prev.get("last_seen"), how, self.gap_sec))
            if how == "running":
                # 이전 세션이 끝 인사 없이 끊겼다. 실제 공백은 이보다 최대 beat_sec 짧다.
                msg += " (이전 세션이 기록 없이 끊김. 오차 최대 %d초)" % self.beat_sec
        self.event(msg)
        self.beat(force=True)
        return self.gap_sec

    def stop(self, status, checks=0):
        """세션이 끝날 때. status: 'ended'(시간 다 됨) / 'cancelled'(이어받기로 끊김) 등."""
        self.beat(force=True, status=status, checks=checks)
        self.event("세션 종료 run=%s (%s)" % (self.run_id, status))

    # ------------------------------------------------------------ 생존 기록

    def beat(self, force=False, status="running", checks=0, extra=None):
        """beat_sec 마다 health.json 을 갱신하고 외부 ping 을 보낸다."""
        if not self.active:
            return
        now = now_kst()
        if not force and self.last_beat and (now - self.last_beat).total_seconds() < self.beat_sec:
            return
        self.last_beat = now
        self._ping()
        if not self.enabled:
            return
        data = {
            "status": status,
            "run_id": self.run_id,
            "run_event": self.run_event,
            "started": fmt(self.started),
            "last_seen": fmt(now),
            "checks": checks,
            "gap_at_start_sec": self.gap_sec,
            "prev_run": self.prev_run,
            "relay": self.relay,
            "error_count": self.error_count,
            "last_error": self.last_error,
        }
        if extra:
            data.update(extra)
        body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self._put("health.json", body, "beat")

    def error(self, msg):
        self.error_count += 1
        self.last_error = "%s %s" % (fmt(now_kst()), msg[:200])

    def _ping(self):
        if not self.ping_url:
            return
        try:
            requests.get(self.ping_url, timeout=10)
            self.last_ping = now_kst()
        except requests.RequestException as e:
            log("health ping 실패: %s" % str(e)[:120])

    # ------------------------------------------------------------ 사건 기록

    def event(self, msg):
        """health.log 에 한 줄 덧붙인다. (실행 로그에도 같이 남긴다)"""
        line = "[%s] %s" % (fmt(now_kst()), msg)
        log("health: " + msg)
        if not (self.enabled and self.active):
            return
        old = self._read_text("health.log") or ""
        lines = old.splitlines()[-(LOG_KEEP - 1):] + [line]
        self._put("health.log", "\n".join(lines) + "\n", "event")

    def summary(self):
        """/health 명령 응답."""
        now = now_kst()
        up = int((now - self.started).total_seconds() // 60)
        out = ["health 기록: %s (끄기 /health off, 켜기 /health on)"
               % ("켜짐" if self.active else "꺼짐"),
               "세션 run=%s (%s)" % (self.run_id, self.run_event),
               "시작 %s (%d분째)" % (self.started.strftime("%m-%d %H:%M:%S"), up)]
        if self.gap_sec is not None:
            out.append("시작 시 감시 공백 %d초 (이전 run=%s)" % (self.gap_sec, self.prev_run))
        r = self.relay
        if r["requested_at"]:
            out.append("다음 실행 요청: %s %s (%d회 시도)"
                       % (r["requested_at"], "성공" if r["ok"] else "실패", r["tries"]))
        else:
            out.append("다음 실행 요청: 아직")
        out.append("오류 %d건%s" % (self.error_count,
                                   (" / 최근: " + self.last_error) if self.last_error else ""))
        out.append("외부 ping: %s" % (
            "꺼짐" if not self.ping_url
            else (self.last_ping.strftime("%H:%M:%S") if self.last_ping else "아직")))
        if self.extra_fn:
            try:
                out.extend(self.extra_fn())
            except Exception as e:            # 부가 정보 때문에 응답이 막히면 안 된다
                out.append("(부가 정보 오류: %s)" % str(e)[:100])
        if self.enabled:
            out.append("기록: https://github.com/%s/tree/%s" % (self.repo, self.branch))
        return "\n".join(out)

    # ------------------------------------------------------------ 다음 세션 띄우기

    def request_next(self, workflow, ref, inputs=None):
        """
        workflow_dispatch 로 다음 실행을 요청한다. 성공하면 True.
        새 실행이 뜨면 concurrency 설정에 따라 지금 세션은 끊기고 이어받는다.
        """
        self.relay["tries"] += 1
        self.relay["requested_at"] = fmt(now_kst())
        if not self.enabled:
            self.relay["ok"] = False
            return False
        try:
            r = requests.post(
                (API % self.repo) + "/actions/workflows/%s/dispatches" % workflow,
                headers=self._headers(),
                data=json.dumps({"ref": ref, "inputs": inputs or {}}).encode("utf-8"),
                timeout=20)
        except requests.RequestException as e:
            self.relay["ok"] = False
            self.event("다음 실행 요청 예외 (%d번째): %s" % (self.relay["tries"], str(e)[:160]))
            return False
        ok = r.status_code == 204
        self.relay["ok"] = ok
        if ok:
            self.event("다음 실행 요청 성공 (%d번째)" % self.relay["tries"])
        else:
            self.event("다음 실행 요청 실패 %s (%d번째): %s"
                       % (r.status_code, self.relay["tries"], r.text[:200]))
        return ok

    # ------------------------------------------------------------ GitHub API

    def _headers(self):
        return {"Authorization": "Bearer %s" % self.token,
                "Accept": "application/vnd.github+json"}

    def _ensure_branch(self):
        """health 브랜치가 없으면 기준 브랜치에서 만든다."""
        try:
            r = requests.get((API % self.repo) + "/git/ref/heads/%s" % self.branch,
                             headers=self._headers(), timeout=20)
            if r.status_code == 200:
                return
            if r.status_code != 404:
                log("health 브랜치 확인 실패 %s" % r.status_code)
                return
            b = requests.get((API % self.repo) + "/git/ref/heads/%s" % self.base_branch,
                             headers=self._headers(), timeout=20)
            if b.status_code != 200:
                log("health: 기준 브랜치 조회 실패 %s" % b.status_code)
                return
            c = requests.post((API % self.repo) + "/git/refs", headers=self._headers(),
                              data=json.dumps({"ref": "refs/heads/%s" % self.branch,
                                               "sha": b.json()["object"]["sha"]}),
                              timeout=20)
            log("health 브랜치 생성 %s" % c.status_code)
        except requests.RequestException as e:
            log("health 브랜치 준비 예외: %s" % str(e)[:160])

    def _get(self, path):
        try:
            r = requests.get((API % self.repo) + "/contents/%s" % path,
                             headers=self._headers(), params={"ref": self.branch}, timeout=20)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            if r.status_code == 404:
                self._sha.pop(path, None)
            return None
        body = r.json()
        self._sha[path] = body.get("sha")
        try:
            return base64.b64decode(body.get("content", "")).decode("utf-8")
        except (ValueError, TypeError):
            return None

    def _read_text(self, path):
        return self._get(path)

    def _read_json(self, path):
        t = self._get(path)
        if not t:
            return None
        try:
            return json.loads(t)
        except ValueError:
            return None

    def _put(self, path, body, reason, retry=True):
        payload = {
            "message": "health: %s" % reason,
            "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if self._sha.get(path):
            payload["sha"] = self._sha[path]
        try:
            r = requests.put((API % self.repo) + "/contents/%s" % path,
                             headers=self._headers(),
                             data=json.dumps(payload).encode("utf-8"), timeout=20)
        except requests.RequestException as e:
            log("health 기록 예외: %s" % str(e)[:160])
            return
        if r.status_code in (200, 201):
            self._sha[path] = r.json().get("content", {}).get("sha")
        elif r.status_code in (409, 422) and retry:
            # sha 가 어긋났다 (이전 세션이 마지막에 썼거나 처음 쓰는 파일). 다시 읽고 한 번 더.
            self._get(path)
            self._put(path, body, reason, retry=False)
        else:
            log("health 기록 실패 %s: %s" % (r.status_code, r.text[:200]))
