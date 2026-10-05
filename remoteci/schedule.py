"""课表数据的归一化、格式化与变更比对。

RemoteCI 有两种课表：
- 班级课表 `GET /api/schedule`：days[].courses[]，每节课带 teacher。
- 我的日程 `GET /api/me/schedule`：days[].items[]（按班级分组）.courses[]。
这里把两者都整理成按日期分组的 `Lesson` 列表，供格式化、提醒和比对共用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class Lesson:
    date: str
    index: int
    label: str
    subject: str
    start: str
    end: str
    class_id: str = ""
    class_name: str = ""
    teacher: str = ""

    def start_time(self) -> time | None:
        return parse_clock(self.start)

    def end_time(self) -> time | None:
        return parse_clock(self.end)

    @property
    def period(self) -> str:
        return self.label or f"第{self.index + 1}节"


_CLOCK = re.compile(r"^\s*(\d{1,2})[:：](\d{2})(?::(\d{2}))?\s*$")


def parse_clock(value: str | None) -> time | None:
    """解析 `HH:mm` 或 `HH:mm:ss`；非法值返回 None。"""
    if not value:
        return None
    match = _CLOCK.match(value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def normalize_clock(value: str) -> str | None:
    """把用户输入的 `7:30`、`7点30`、`07：30` 统一成 `07:30`。"""
    text = value.strip().replace("：", ":")
    text = re.sub(r"^(\d{1,2})点半$", r"\1:30", text)
    text = re.sub(r"^(\d{1,2})点(\d{1,2})分?$", r"\1:\2", text)
    text = re.sub(r"^(\d{1,2})点$", r"\1:00", text)
    match = re.match(r"^(\d{1,2}):(\d{1,2})$", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def short_clock(value: str) -> str:
    parsed = parse_clock(value)
    return parsed.strftime("%H:%M") if parsed else (value or "?")


def lessons_from_class_schedule(bundle: dict, class_id: str = "", class_name: str = "") -> dict[str, list[Lesson]]:
    result: dict[str, list[Lesson]] = {}
    for day in bundle.get("days") or []:
        day_date = day.get("date") or ""
        lessons = [
            Lesson(
                date=day_date,
                index=int(c.get("index", 0)),
                label=c.get("label") or "",
                subject=c.get("subject") or "",
                start=c.get("startTime") or "",
                end=c.get("endTime") or "",
                class_id=class_id or str(bundle.get("classId") or ""),
                class_name=class_name,
                teacher=c.get("teacher") or "",
            )
            for c in day.get("courses") or []
            if c.get("enabled", True)
        ]
        result[day_date] = sorted(lessons, key=lambda x: x.index)
    return result


def lessons_from_my_schedule(response: dict) -> dict[str, list[Lesson]]:
    result: dict[str, list[Lesson]] = {}
    for day in response.get("days") or []:
        day_date = day.get("date") or ""
        lessons: list[Lesson] = []
        for item in day.get("items") or []:
            for c in item.get("courses") or []:
                if not c.get("enabled", True):
                    continue
                lessons.append(Lesson(
                    date=day_date,
                    index=int(c.get("index", 0)),
                    label=c.get("label") or "",
                    subject=c.get("subject") or "",
                    start=c.get("startTime") or "",
                    end=c.get("endTime") or "",
                    class_id=str(item.get("classId") or ""),
                    class_name=item.get("className") or "",
                    teacher=c.get("teacher") or "",
                ))
        result[day_date] = sorted(lessons, key=_lesson_order)
    return result


def _lesson_order(lesson: Lesson):
    start = lesson.start_time()
    return (start or time(23, 59), lesson.index, lesson.class_name)


def describe_date(day: str, today: date | None = None) -> str:
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return day
    text = f"{parsed.month}月{parsed.day}日（周{WEEKDAYS[parsed.weekday()]}）"
    if today is not None:
        delta = (parsed - today).days
        prefix = {0: "今天", 1: "明天", 2: "后天"}.get(delta)
        if prefix:
            text = f"{prefix} {text}"
    return text


def format_lessons(lessons: list[Lesson], *, show_class: bool, show_teacher: bool = False) -> str:
    lines = []
    for lesson in lessons:
        parts = [f"{lesson.period}", f"{short_clock(lesson.start)}-{short_clock(lesson.end)}"]
        if show_class and lesson.class_name:
            parts.append(lesson.class_name)
        parts.append(lesson.subject or "（未命名）")
        if show_teacher and lesson.teacher:
            parts.append(f"（{lesson.teacher}）")
        lines.append("· " + " ".join(parts))
    return "\n".join(lines)


def format_day(title: str, day: str, lessons: list[Lesson], today: date, *, show_class: bool,
               show_teacher: bool = False, empty_text: str = "没有课程") -> str:
    head = f"{title} · {describe_date(day, today)}"
    if not lessons:
        return f"{head}\n{empty_text}"
    return f"{head}（共 {len(lessons)} 节）\n{format_lessons(lessons, show_class=show_class, show_teacher=show_teacher)}"


def last_lesson_end(lessons: list[Lesson]) -> time | None:
    ends = [x.end_time() for x in lessons if x.end_time()]
    return max(ends) if ends else None


def combine(day: date, clock: time, tz) -> datetime:
    return datetime.combine(day, clock, tzinfo=tz)


# ---------- 变更比对 ----------

def snapshot_key(lesson: Lesson) -> str:
    return f"{lesson.date}|{lesson.class_id}|{lesson.index}"


def to_snapshot(days: dict[str, list[Lesson]]) -> dict[str, dict[str, dict]]:
    """序列化成可持久化的 {date: {key: lesson 字段}}，用于下次比对。"""
    return {
        day: {snapshot_key(x): {
            "index": x.index, "label": x.label, "subject": x.subject, "start": x.start, "end": x.end,
            "class_id": x.class_id, "class_name": x.class_name, "teacher": x.teacher,
        } for x in lessons}
        for day, lessons in days.items()
    }


@dataclass(frozen=True)
class Change:
    date: str
    kind: str  # added / removed / subject / time / teacher
    class_name: str
    period: str
    before: str = ""
    after: str = ""


def diff_snapshots(old: dict, new: dict, from_date: str, *, track_teacher: bool = False) -> list[Change]:
    """比较两次快照中同时存在、且不早于 from_date 的日期。

    服务端课表是滚动七天窗口，新滚入或滚出的日期不算变动。
    """
    changes: list[Change] = []
    for day in sorted(set(old) & set(new)):
        if day < from_date:
            continue
        before, after = old[day], new[day]
        for key in sorted(set(before) | set(after), key=lambda k: _index_of(before.get(k) or after.get(k))):
            a, b = before.get(key), after.get(key)
            ref = b or a
            period = ref.get("label") or f"第{ref.get('index', 0) + 1}节"
            cname = ref.get("class_name") or ""
            if a and not b:
                changes.append(Change(day, "removed", cname, period, before=a.get("subject", "")))
            elif b and not a:
                changes.append(Change(day, "added", cname, period, after=_lesson_text(b)))
            elif a.get("subject") != b.get("subject"):
                changes.append(Change(day, "subject", cname, period, a.get("subject", ""), b.get("subject", "")))
            elif (a.get("start"), a.get("end")) != (b.get("start"), b.get("end")):
                changes.append(Change(day, "time", cname, period,
                                      f"{short_clock(a.get('start'))}-{short_clock(a.get('end'))}",
                                      f"{short_clock(b.get('start'))}-{short_clock(b.get('end'))}"))
            elif track_teacher and (a.get("teacher") or "") != (b.get("teacher") or ""):
                changes.append(Change(day, "teacher", cname, period,
                                      a.get("teacher") or "无", b.get("teacher") or "无"))
    return changes


def _index_of(item: dict | None) -> int:
    return int((item or {}).get("index", 0))


def _lesson_text(item: dict) -> str:
    return f"{item.get('subject', '')}（{short_clock(item.get('start'))}-{short_clock(item.get('end'))}）"


def format_changes(title: str, changes: list[Change], today: date, *, show_class: bool) -> str:
    lines = [title]
    current = None
    for change in changes:
        if change.date != current:
            current = change.date
            lines.append(describe_date(change.date, today))
        where = f"{change.class_name} {change.period}" if show_class and change.class_name else change.period
        text = {
            "removed": f"{change.before} 已取消",
            "added": f"新增 {change.after}",
            "subject": f"{change.before} → {change.after}",
            "time": f"时间 {change.before} → {change.after}",
            "teacher": f"任课教师 {change.before} → {change.after}",
        }[change.kind]
        lines.append(f"· {where}：{text}")
    return "\n".join(lines)


def add_days(day: date, n: int) -> date:
    return day + timedelta(days=n)
