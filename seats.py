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

SEAT_RE = re.compile(r"""<input[^>]*class=['"]([^'"]*)['"][^>]*>""")
ID_RE = re.compile(r"""\bid=['"]([^'"]*)['"]""")
SEATMAP_PATH = "/Popup/SeatChoiceSeatMap"

# 잔여석이 이 수 이하이거나 매진인 회차만 좌석까지 들여다본다.
# 여유가 많은 회차는 굳이 확인할 이유가 없다.
LOOK_CLOSER_BELOW = 5


def count(html):
    """
    배치도 HTML -> 집계 결과.

    total        전체 좌석
    taken        이미 나간 좌석
    blocked      고를 수 없는 좌석
    free_special 비어 있는 특정 좌석 (따로 세는 것)
    free         비어 있는 일반 좌석  <- 우리가 보고 싶은 숫자
    """
    total = taken = blocked = free_special = free = 0
    for cls in SEAT_RE.findall(html):
        names = cls.split()
        if "choose-seat" not in names and "choose-seat_large" not in names:
            continue
        total += 1
        if "checked" in names:
            taken += 1
            continue
        if "disabled" in names:
            blocked += 1
            continue
        if "accessible" in names:
            free_special += 1
            continue
        free += 1
    return {"total": total, "taken": taken, "blocked": blocked,
            "free_special": free_special, "free": free}


def free_labels(html, limit=12):
    """비어 있는 일반 좌석의 이름(A3 같은)을 몇 개만 뽑는다."""
    out = []
    for m in re.finditer(r"<input[^>]*>", html):
        tag = m.group(0)
        cm = re.search(r"""class=['"]([^'"]*)['"]""", tag)
        if not cm:
            continue
        names = cm.group(1).split()
        if "choose-seat" not in names and "choose-seat_large" not in names:
            continue
        if {"checked", "disabled", "accessible"} & set(names):
            continue
        im = ID_RE.search(tag)
        if im:
            out.append(im.group(1))
        if len(out) >= limit:
            break
    return out


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
