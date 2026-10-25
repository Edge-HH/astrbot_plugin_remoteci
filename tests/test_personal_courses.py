import asyncio
from datetime import datetime, timedelta, timezone

from remoteci.personal_courses import save_course, list_courses, delete_course
from remoteci.reminders import before_class_due
from remoteci.schedule import lessons_from_my_schedule, to_snapshot
from remoteci.service import ChatUser

ID = "afde8374-3324-473a-9807-7f89d57f14e9"
PRIVATE = ChatUser("p:1", "p", "1", "用户", "p:private:1", True)
GROUP = ChatUser("p:1", "p", "1", "用户", "p:group:1", False)


class FakeService:
    def __init__(self):
        self.calls = []
        self._cache = {"old": 1}

    def require_binding(self, user):
        return {"key": user.user_key}

    async def _call(self, binding, method, path, **kwargs):
        self.calls.append((binding, method, path, kwargs))
        return {"id": ID, **kwargs.get("body", {})}


def test_create_update_delete_use_current_account_and_preserve_reminder_off():
    async def run():
        service = FakeService()
        assert "已添加" in await save_course(service, PRIVATE, "自习", "2030-01-01T08:00+08:00", "2030-01-01T09:00+08:00", reminder_minutes=-1)
        assert service.calls[-1][1] == "POST"
        assert service.calls[-1][3]["body"]["reminderMinutes"] is None
        assert service._cache == {}
        await save_course(service, PRIVATE, "改名", "2030-01-01T08:00+08:00", "2030-01-01T09:00+08:00", course_id=ID)
        assert service.calls[-1][1:3] == ("PUT", f"/api/me/schedule/courses/{ID}")
        await delete_course(service, PRIVATE, ID)
        assert service.calls[-1][1] == "DELETE"
    asyncio.run(run())


def test_group_never_reads_or_mutates_private_courses():
    async def run():
        service = FakeService()
        assert "私聊" in await list_courses(service, GROUP)
        assert "私聊" in await save_course(service, GROUP, "私事", "2030-01-01T08:00+08:00", "2030-01-01T09:00+08:00")
        assert "私聊" in await delete_course(service, GROUP, ID)
        assert service.calls == []
        assert "时区" in await save_course(service, PRIVATE, "自习", "2030-01-01T08:00", "2030-01-01T09:00")
        assert service.calls == []
    asyncio.run(run())


def test_personal_courses_have_stable_snapshot_ids_and_no_duplicate_bot_reminder():
    day = "2030-01-01"
    courses = [{"personalCourseId": identity, "subject": "自习", "startTime": "08:00", "endTime": "09:00"}
               for identity in (ID, "second")]
    days = lessons_from_my_schedule({"days": [{"date": day, "items": [{"source": "personal", "courses": courses}]}]})
    assert len(to_snapshot(days)[day]) == 2
    assert before_class_due(now=datetime(2030, 1, 1, 7, 55, tzinfo=timezone(timedelta(hours=8))),
                            scope="self", lessons=days[day], minutes=10, holiday_today=False) == []


def test_weekly_courses_send_weekday_time_and_rotation():
    async def run():
        service = FakeService()
        service._tz_params = lambda: {"timeZoneOffsetMinutes": 480}
        reply = await save_course(service, PRIVATE, "社团课", week_day="周三", start_time="14:00", end_time="14:45", weeks="双周")
        assert "已添加" in reply
        body = service.calls[-1][3]["body"]
        assert (body["weekDay"], body["startTime"], body["endTime"]) == (3, "14:00", "14:45")
        assert (body["weekCountDiv"], body["weekCountDivTotal"], body["timeZoneOffsetMinutes"]) == (2, 2, 480)
        assert "startsAt" not in body
        await save_course(service, PRIVATE, "晚自习", week_day="星期日", start_time="9:05", end_time="10:00", weeks="三周轮换第2周")
        body = service.calls[-1][3]["body"]
        assert (body["weekDay"], body["startTime"], body["weekCountDiv"], body["weekCountDivTotal"]) == (0, "09:05", 2, 3)
        calls = len(service.calls)
        assert "星期" in await save_course(service, PRIVATE, "x", week_day="周八", start_time="14:00", end_time="14:45")
        assert "星期" in await save_course(service, PRIVATE, "x", week_day="周一", start_time="15:00", end_time="14:45")
        assert "周次" in await save_course(service, PRIVATE, "x", week_day="周一", start_time="14:00", end_time="14:45", weeks="三周轮换第4周")
        assert len(service.calls) == calls
    asyncio.run(run())


def test_week_parsers():
    from remoteci.personal_courses import parse_week_day, parse_week_rule
    assert [parse_week_day(x) for x in ("周一", "每周五", "礼拜天", "7", "1", "周八")] == [1, 5, 0, 0, 1, None]
    assert parse_week_rule("每周") == (0, 2)
    assert parse_week_rule("单周") == (1, 2)
    assert parse_week_rule("四周轮换第4周") == (4, 4)
    assert parse_week_rule("隔周") is None
