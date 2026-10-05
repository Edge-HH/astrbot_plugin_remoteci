"""主动提醒的判定逻辑（纯函数）：给定当前时刻、提醒设置和课表，算出此刻应发送的提醒。

调度循环每隔约 30 秒调用一次；每条提醒带唯一 key，由调用方去重，所以同一时间窗内重复调用是安全的。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .schedule import (Lesson, combine, format_day, last_lesson_end, parse_clock, short_clock)

DIGEST_GRACE = timedelta(minutes=90)  # 插件重启或短暂离线后，最多补发 90 分钟内错过的日程提醒


@dataclass(frozen=True)
class Due:
    key: str
    kind: str  # today / tomorrow / before
    text: str


def tomorrow_trigger(now: datetime, prefs: dict, today_lessons: list[Lesson]):
    """次日日程的触发时刻；last_class 模式取自己最后一节课的下课时间，当天无课时用备用时刻。"""
    today = now.date()
    if prefs.get("tomorrow_mode") == "fixed":
        clock = parse_clock(prefs.get("tomorrow_time"))
    else:
        clock = last_lesson_end(today_lessons) or parse_clock(prefs.get("tomorrow_fallback"))
    return combine(today, clock, now.tzinfo) if clock else None


def digest_due(*, now: datetime, scope: str, title: str, days: dict[str, list[Lesson]],
               today_enabled: bool, today_time: str | None,
               tomorrow_enabled: bool, tomorrow_at: datetime | None,
               holiday_today: bool, holiday_tomorrow: bool, skip_empty: bool,
               show_class: bool, show_teacher: bool) -> list[Due]:
    today = now.date()
    tomorrow = today + timedelta(days=1)
    result: list[Due] = []

    clock = parse_clock(today_time)
    if today_enabled and clock and not holiday_today and today.isoformat() in days:
        at = combine(today, clock, now.tzinfo)
        lessons = days[today.isoformat()]
        if at <= now <= at + DIGEST_GRACE and (lessons or not skip_empty):
            result.append(Due(f"{scope}:today:{today.isoformat()}", "today",
                              format_day(f"【今日{title}】", today.isoformat(), lessons, today,
                                         show_class=show_class, show_teacher=show_teacher)))

    if tomorrow_enabled and tomorrow_at and not holiday_tomorrow and tomorrow.isoformat() in days:
        lessons = days[tomorrow.isoformat()]
        if tomorrow_at <= now <= tomorrow_at + DIGEST_GRACE and (lessons or not skip_empty):
            result.append(Due(f"{scope}:tomorrow:{tomorrow.isoformat()}", "tomorrow",
                              format_day(f"【明日{title}】", tomorrow.isoformat(), lessons, today,
                                         show_class=show_class, show_teacher=show_teacher)))
    return result


def before_class_due(*, now: datetime, scope: str, lessons: list[Lesson], minutes: int,
                     holiday_today: bool) -> list[Due]:
    if holiday_today or minutes <= 0:
        return []
    result = []
    lead = timedelta(minutes=minutes)
    for lesson in lessons:
        start = lesson.start_time()
        if not start or lesson.date != now.date().isoformat():
            continue
        at = combine(now.date(), start, now.tzinfo)
        if at - lead <= now < at:
            left = max(1, round((at - now).total_seconds() / 60))
            where = f"{lesson.class_name} " if lesson.class_name else ""
            text = (f"【课前提醒】{left} 分钟后上课\n"
                    f"{where}{lesson.period} {lesson.subject}（{short_clock(lesson.start)}-{short_clock(lesson.end)}）")
            result.append(Due(f"{scope}:before:{lesson.date}:{lesson.class_id}:{lesson.index}", "before", text))
    return result


def personal_due(now: datetime, prefs: dict, days: dict[str, list[Lesson]], scope: str,
                 holiday_today: bool, holiday_tomorrow: bool) -> list[Due]:
    """老师/班主任个人会话：当日日程、次日日程、课前提醒。"""
    today_lessons = days.get(now.date().isoformat(), [])
    result = digest_due(
        now=now, scope=scope, title="日程", days=days,
        today_enabled=bool(prefs.get("today_enabled")), today_time=prefs.get("today_time"),
        tomorrow_enabled=bool(prefs.get("tomorrow_enabled")),
        tomorrow_at=tomorrow_trigger(now, prefs, today_lessons),
        holiday_today=holiday_today, holiday_tomorrow=holiday_tomorrow,
        skip_empty=bool(prefs.get("skip_empty_days", True)), show_class=True, show_teacher=False)
    if prefs.get("before_enabled"):
        result += before_class_due(now=now, scope=scope, lessons=today_lessons,
                                   minutes=int(prefs.get("before_minutes") or 0), holiday_today=holiday_today)
    return result
