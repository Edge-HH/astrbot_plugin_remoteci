"""自定义课程管理：使用当前私聊绑定的认证信息，不接受账号或班级参数。"""
from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

PRIVATE_ONLY = "自定义课程仅自己可见，请在私聊中查询和管理。"
# 与服务端 weekDay 一致：0=周日，1=周一 … 6=周六。
_WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 0, "天": 0, "七": 0}
_ROTATION_TOTALS = {"二": 2, "两": 2, "三": 3, "四": 4, "2": 2, "3": 3, "4": 4}


def parse_week_day(text: str) -> int | None:
    """周三、星期三、礼拜天、3（周三）、7（周日）→ 服务端 weekDay。"""
    value = re.sub(r"^(每)?(周|星期|礼拜)", "", (text or "").strip())
    if value.isdigit() and 1 <= int(value) <= 7:
        return int(value) % 7
    return _WEEKDAYS.get(value) if len(value) == 1 else None


def parse_week_rule(text: str) -> tuple[int, int] | None:
    """每周、单周、双周、三周轮换第2周 → (weekCountDiv, weekCountDivTotal)。"""
    value = (text or "每周").strip().replace(" ", "")
    if value in ("", "每周"):
        return 0, 2
    if value == "单周":
        return 1, 2
    if value == "双周":
        return 2, 2
    match = re.fullmatch(r"([二两三四234])周(?:一)?轮换?的?第([1-4一二三四])周", value)
    if not match:
        return None
    total = _ROTATION_TOTALS[match.group(1)]
    index = match.group(2)
    div = int(index) if index.isdigit() else "一二三四".index(index) + 1
    return (div, total) if div <= total else None


def _hhmm(text: str) -> str | None:
    match = re.fullmatch(r"(\d{1,2})[:：](\d{2})", (text or "").strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


async def list_courses(service, user) -> str:
    if not user.is_private:
        return PRIVATE_ONLY
    binding = service.require_binding(user)
    items = await service._call(binding, "GET", "/api/me/schedule/courses") or []
    if not items:
        return "你还没有自定义课程。"
    return "你的自定义课程：\n" + "\n".join(
        f"· {x['subject']} {x.get('summary') or str(x['startsAt']) + '–' + str(x['endsAt'])} {x.get('location', '')}\n"
        f"  编号：{x['id']}；提前提醒：{x.get('reminderMinutes')} 分钟；上课提醒：{x.get('notifyAtStart', True)}"
        for x in items)


async def save_course(service, user, subject: str, starts_at: str = "", ends_at: str = "", location: str = "",
                      reminder_minutes: int = 10, notify_at_start: bool = True, course_id: str = "",
                      week_day: str = "", start_time: str = "", end_time: str = "", weeks: str = "每周") -> str:
    """传 week_day 时创建每周重复的课程（可单双周/多周轮换），否则按 starts_at/ends_at 创建单次课程。"""
    if not user.is_private:
        return PRIVATE_ONLY
    binding = service.require_binding(user)
    try:
        path = "/api/me/schedule/courses"
        if course_id:
            path += "/" + str(UUID(course_id))
    except ValueError:
        return "修改时请使用完整课程编号。"
    body = {"subject": subject, "location": location,
            "reminderMinutes": None if reminder_minutes == -1 else reminder_minutes, "notifyAtStart": notify_at_start}
    if week_day:
        day, start, end, rule = parse_week_day(week_day), _hhmm(start_time), _hhmm(end_time), parse_week_rule(weeks)
        if day is None or start is None or end is None or end <= start:
            return "请提供星期（如 周三）和同一天内的起止时刻（如 14:00 和 14:45）。"
        if rule is None:
            return "周次请用 每周、单周、双周，或 三周轮换第2周 这样的写法（最多四周轮换）。"
        tz = getattr(service, "_tz_params", lambda: {})()
        body.update({"weekDay": day, "startTime": start, "endTime": end,
                     "weekCountDiv": rule[0], "weekCountDivTotal": rule[1], **tz})
    else:
        try:
            start, end = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (starts_at, ends_at))
            if start.utcoffset() is None or end.utcoffset() is None or end <= start:
                raise ValueError()
        except ValueError:
            return ("每周重复的课程请提供星期和起止时刻；单次课程请提供含时区的起止时间"
                    "（如 2026-10-17T08:00:00+08:00），且在当天内结束。")
        body.update({"startsAt": start.isoformat(), "endsAt": end.isoformat()})
    result = await service._call(binding, "PUT" if course_id else "POST", path, body=body)
    service._cache.clear()
    summary = result.get("summary")
    return (f"已{'修改' if course_id else '添加'}自定义课程：{result['subject']}"
            + (f"（{summary}）" if summary else "")
            + f"\n编号：{result['id']}\n已同步到 RemoteCI 个人日程。")


async def delete_course(service, user, course_id: str) -> str:
    if not user.is_private:
        return PRIVATE_ONLY
    binding = service.require_binding(user)
    try:
        course_id = str(UUID(course_id))
    except ValueError:
        return "请使用完整课程编号。"
    await service._call(binding, "DELETE", f"/api/me/schedule/courses/{course_id}")
    service._cache.clear()
    return "已删除自定义课程，所有设备将同步更新。"
