#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
좌석 단위 조회.

목록에서는 회차별 잔여석 숫자만 알 수 있는데, 그 숫자에는 특정 좌석까지
섞여 있어서 "실제로 앉을 수 있는 자리"와 다르다. 좌석 배치도를 직접 받아
좌석 하나하나를 세어 그 차이를 걷어낸다.

배치도 응답 형태 (속성이 작은따옴표인 점에 주의)
    <input type='checkbox' class='choose-seat ' id='A3' value='A3'
           data-seatgroup='2' data-rowname='A' data-colnumber='3'
           data-seatmapid='8001' data-screenid='66' data-screenplanid='1109718' />

    class 에 checked     -> 이미 나간 자리
    class 에 accessible  -> 따로 빼서 세는 자리
    class 에 disabled    -> 고를 수 없는 자리
"""

import re

ID_RE = re.compile(r"""\bid=['"]([^'"]*)['"]""")
SEATMAP_PATH = "/Popup/SeatChoiceSeatMap"

# 잔여석이 이 수 이하이거나 매진인 회차만 좌석까지 들여다본다.
# 여유가 많은 회차는 굳이 확인할 이유가 없다.
LOOK_CLOSER_BELOW = 5


SEAT_BASE = {"choose-seat", "choose-seat_large"}
# 빈 일반 좌석에 붙어 있어도 되는 것으로 확인된 class. 이 밖의 class 가
# 붙어 있으면 무슨 뜻인지 모르므로 빈자리로 세지 않는다.
FREE_OK = SEAT_BASE | {"mini"}
# class 가 아니라 속성으로 붙어 나오는 경우도 막는다 (예: disabled='disabled')
ATTR_FLAG_RE = re.compile(r"""\s(checked|disabled)(?:\s*=|[\s/>])""")


def seat_state(tag):
    """
    좌석 input 태그 하나 -> 'taken' / 'blocked' / 'special' / 'unknown' / 'free'.
    좌석이 아니면 None.
    """
    cm = re.search(r"""class=['"]([^'"]*)['"]""", tag)
    if not cm:
        return None
    names = set(cm.group(1).split())
    if not names & SEAT_BASE:
        return None
    flags = set(ATTR_FLAG_RE.findall(tag))
    if "checked" in names or "checked" in flags:
        return "taken"
    if "disabled" in names or "disabled" in flags:
        return "blocked"
    if "accessible" in names:
        return "special"
    if names - FREE_OK:
        return "unknown"
    return "free"


def _seat_tags(html):
    for m in re.finditer(r"<input[^>]*>", html):
        tag = m.group(0)
        st = seat_state(tag)
        if st:
            yield tag, st


def count(html):
    """
    배치도 HTML -> 집계 결과.

    total        전체 좌석
    taken        이미 나간 좌석
    blocked      고를 수 없는 좌석
    free_special 비어 있는 특정 좌석 (따로 세는 것)
    unknown      모르는 표시가 붙은 좌석 (빈자리로 세지 않음)
    free         비어 있는 일반 좌석  <- 우리가 보고 싶은 숫자
    """
    c = {"total": 0, "taken": 0, "blocked": 0, "free_special": 0,
         "unknown": 0, "free": 0}
    key = {"taken": "taken", "blocked": "blocked", "special": "free_special",
           "unknown": "unknown", "free": "free"}
    for _tag, st in _seat_tags(html):
        c["total"] += 1
        c[key[st]] += 1
    return c


def free_labels(html, limit=12):
    """비어 있는 일반 좌석의 이름(A3 같은)을 몇 개만 뽑는다."""
    out = []
    for tag, st in _seat_tags(html):
        if st != "free":
            continue
        im = ID_RE.search(tag)
        if im:
            out.append(im.group(1))
        if len(out) >= limit:
            break
    return out


def free_tags(html, limit=3):
    """빈자리로 판정한 좌석 태그 원문. 판정이 의심스러울 때 로그로 남기기 위한 것."""
    return [tag for tag, st in _seat_tags(html) if st == "free"][:limit]


def list_count(note):
    """목록의 잔여 표시 -> 남은 수. '매진'이면 0, 해석 못 하면 None."""
    note = (note or "").strip()
    if "매진" in note:
        return 0
    m = re.match(r"^(\d+)\s*/\s*(\d+)$", note)
    return int(m.group(1)) if m else None


def needs_closer_look(note):
    """
    목록에 적힌 잔여 표시만 보고, 좌석까지 확인할 가치가 있는지 판단한다.
    '매진'이거나 남은 수가 적을 때만 들여다본다.
    """
    note = (note or "").strip()
    if not note:
        return False
    if "매진" in note:
        return True
    m = re.match(r"^(\d+)\s*/\s*(\d+)$", note)
    if m:
        return int(m.group(1)) <= LOOK_CLOSER_BELOW
    return False


def fetch(session, origin, attrs, timeout=20):
    """배치도를 받아온다. 실패하면 None."""
    payload = {"playDate": attrs.get("playdate", ""),
               "theaterCode": attrs.get("theatercode", ""),
               "movieCode": attrs.get("moviecode", ""),
               "screenPlanId": attrs.get("screenplanid", ""),
               "playNumber": attrs.get("playnumber", "")}
    if not payload["screenPlanId"]:
        return None
    try:
        r = session.post(origin + SEATMAP_PATH, data=payload, timeout=timeout, verify=False)
    except Exception:
        return None
    if r.status_code != 200 or len(r.text) < 200:
        return None
    return r.text
