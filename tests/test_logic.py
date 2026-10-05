from datetime import date, datetime, timedelta, timezone

from remoteci import schedule as sch
from remoteci.holidays import HolidayCalendar
from remoteci.reminders import personal_due
from remoteci.store import DEFAULT_REMINDERS

TZ = timezone(timedelta(hours=8))
TODAY = date(2026, 10, 9)  # 周五


def my_schedule(days):
    """days: {iso: [(class, index, subject, start, end)]} → /api/me/schedule 响应。"""
    out = []
    for iso, courses in days.items():
        items = {}
        for cname, idx, subject, start, end in courses:
            items.setdefault(cname, []).append({"index": idx, "label": f"第{idx + 1}节", "subject": subject,
                                                "startTime": start, "endTime": end, "enabled": True})
        out.append({"date": iso, "items": [{"classId": f"id-{c}", "className": c, "courses": v} for c, v in items.items()]})
    return {"days": out}


def at(hour, minute, day=TODAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def test_normalize_clock_accepts_chinese_forms():
    assert sch.normalize_clock("7:30") == "07:30"
    assert sch.normalize_clock("7点半") == "07:30"
    assert sch.normalize_clock("18点") == "18:00"
    assert sch.normalize_clock("07：05") == "07:05"
    assert sch.normalize_clock("25:00") is None
    assert sch.parse_clock("08:00:00").hour == 8


def test_diff_reports_subject_change_cancel_and_add_but_ignores_rolled_and_past_days():
    old = sch.to_snapshot(sch.lessons_from_my_schedule(my_schedule({
        "2026-10-08": [("高一1班", 0, "数学", "08:00", "08:40")],
        "2026-10-09": [("高一1班", 0, "数学", "08:00", "08:40"), ("高一2班", 2, "数学", "10:00", "10:40")],
    })))
    new = sch.to_snapshot(sch.lessons_from_my_schedule(my_schedule({
        "2026-10-08": [],
        "2026-10-09": [("高一1班", 0, "物理", "08:00", "08:40"), ("高一2班", 4, "数学", "14:00", "14:40")],
        "2026-10-15": [("高一1班", 0, "数学", "08:00", "08:40")],
    })))
    changes = sch.diff_snapshots(old, new, "2026-10-09")
    kinds = sorted(c.kind for c in changes)
    assert kinds == ["added", "removed", "subject"]
    text = sch.format_changes("【课程变动】", changes, TODAY, show_class=True)
    assert "数学 → 物理" in text and "已取消" in text and "新增 数学" in text


def test_today_digest_fires_in_window_once_per_key():
    days = sch.lessons_from_my_schedule(my_schedule({
        TODAY.isoformat(): [("高一1班", 0, "数学", "08:00", "08:40")],
        (TODAY + timedelta(days=1)).isoformat(): [],
    }))
    prefs = dict(DEFAULT_REMINDERS)
    assert not [d for d in personal_due(at(6, 59), prefs, days, "t", False, False) if d.kind == "today"]
    due = [d for d in personal_due(at(7, 5), prefs, days, "t", False, False) if d.kind == "today"]
    assert len(due) == 1 and "高一1班" in due[0].text and due[0].key.endswith(TODAY.isoformat())
    assert not [d for d in personal_due(at(7, 5), prefs, days, "t", True, False) if d.kind == "today"]


def test_tomorrow_digest_after_last_class_and_fallback():
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    days = sch.lessons_from_my_schedule(my_schedule({
        TODAY.isoformat(): [("A", 0, "数学", "08:00", "08:40"), ("B", 5, "数学", "15:00", "15:40")],
        tomorrow: [("A", 1, "数学", "09:00", "09:40")],
    }))
    prefs = dict(DEFAULT_REMINDERS)
    assert not [d for d in personal_due(at(15, 39), prefs, days, "t", False, False) if d.kind == "tomorrow"]
    assert [d for d in personal_due(at(15, 41), prefs, days, "t", False, False) if d.kind == "tomorrow"]
    # 明天放假：不推送
    assert not [d for d in personal_due(at(15, 41), prefs, days, "t", False, True) if d.kind == "tomorrow"]
    # 今天无课：用备用时刻
    no_class_today = {TODAY.isoformat(): [], tomorrow: days[tomorrow]}
    assert not [d for d in personal_due(at(15, 41), prefs, no_class_today, "t", False, False) if d.kind == "tomorrow"]
    assert [d for d in personal_due(at(17, 31), prefs, no_class_today, "t", False, False) if d.kind == "tomorrow"]
    # 固定时间
    fixed = {**prefs, "tomorrow_mode": "fixed", "tomorrow_time": "20:00"}
    assert not [d for d in personal_due(at(15, 41), fixed, days, "t", False, False) if d.kind == "tomorrow"]
    assert [d for d in personal_due(at(20, 0), fixed, days, "t", False, False) if d.kind == "tomorrow"]


def test_before_class_reminder_window():
    days = sch.lessons_from_my_schedule(my_schedule({TODAY.isoformat(): [("高一1班", 2, "数学", "10:00", "10:40")]}))
    prefs = dict(DEFAULT_REMINDERS)
    assert not [d for d in personal_due(at(9, 49), prefs, days, "t", False, False) if d.kind == "before"]
    due = [d for d in personal_due(at(9, 52), prefs, days, "t", False, False) if d.kind == "before"]
    assert len(due) == 1 and "8 分钟后" in due[0].text
    assert not [d for d in personal_due(at(10, 0), prefs, days, "t", False, False) if d.kind == "before"]
    assert not [d for d in personal_due(at(9, 52), {**prefs, "before_enabled": False}, days, "t", False, False)
                if d.kind == "before"]


def test_holiday_calendar_official_makeup_custom_and_weekend(tmp_path):
    cal = HolidayCalendar(tmp_path)
    cal.load_year_data(2026, {"days": [
        {"name": "国庆节", "date": "2026-10-01", "isOffDay": True},
        {"name": "国庆节", "date": "2026-10-02", "isOffDay": True},
        {"name": "国庆节", "date": "2026-10-10", "isOffDay": False},
    ]})
    settings = {"use_official": True, "weekend_as_holiday": True,
                "ranges": [{"name": "寒假", "start": "2027-01-20", "end": "2027-02-20"}]}
    assert cal.check(date(2026, 10, 1), settings) == (True, "国庆节")
    assert cal.check(date(2026, 10, 10), settings)[0] is False  # 周六调休上课
    assert cal.check(date(2026, 10, 11), settings) == (True, "周末")
    assert cal.check(date(2027, 2, 1), settings) == (True, "寒假")
    assert cal.check(date(2026, 10, 9), settings)[0] is False
    upcoming = cal.upcoming(date(2026, 9, 30), settings)
    assert upcoming[0] == {"name": "国庆节", "start": "2026-10-01", "end": "2026-10-02", "off": True, "source": "official"}
