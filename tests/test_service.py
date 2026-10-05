import asyncio
from datetime import datetime, timedelta, timezone

from remoteci.commands import HELP, run_command
from remoteci.service import ChatUser, RemoteCiService

TZ = timezone(timedelta(hours=8))
CLASS_A = "11111111-0000-0000-0000-00000000000a"


class FakeServer:
    """按 (method, path) 返回预设响应的假 RemoteCI。"""

    def __init__(self, role_kind=5):
        self.role_kind = role_kind
        self.my = {"days": []}
        self.class_schedule = {"days": []}
        self.calls = []

    async def __call__(self, binding, method, path, *, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path == "/api/me":
            return {"id": "u1", "username": "zhang", "displayName": "张老师", "role": 1, "roleKind": self.role_kind,
                    "permissions": 2569, "classes": [{"id": CLASS_A, "name": "高一(1)班", "roleKind": self.role_kind,
                                                      "permissions": 2779}]}
        if path == "/api/me/schedule":
            return self.my
        if path == "/api/schedule":
            return self.class_schedule
        if path == "/api/commands":
            return {"success": True, "code": "OK", "message": "已显示"}
        raise AssertionError(path)


def day_entry(iso, courses):
    return {"date": iso, "items": [{"classId": CLASS_A, "className": "高一(1)班", "courses": [
        {"index": i, "label": f"第{i + 1}节", "subject": s, "startTime": st, "endTime": en, "enabled": True,
         "teacher": "张老师"} for i, s, st, en in courses]}]}


def make(tmp_path, clock, role_kind=5):
    sent = []

    async def send(umo, text):
        sent.append((umo, text))
        return True

    service = RemoteCiService(tmp_path, send=send, config={"default_server_url": "rci.example.com", "poll_minutes": 1},
                              now=lambda: clock[0])
    fake = FakeServer(role_kind)
    service._call = fake  # noqa: SLF001 - 替换网络层

    async def no_holiday_refresh(force=False):
        return []
    service.refresh_holidays = no_holiday_refresh
    return service, fake, sent


PRIVATE = ChatUser("aiocqhttp:42", "aiocqhttp", "42", "小张", "aiocqhttp:FriendMessage:42", True)
GROUP = ChatUser("aiocqhttp:42", "aiocqhttp", "42", "小张", "aiocqhttp:GroupMessage:900", False)


def test_bind_with_api_key_then_daily_and_change_reminders(tmp_path):
    async def run():
        clock = [datetime(2026, 10, 9, 6, 50, tzinfo=TZ)]
        service, fake, sent = make(tmp_path, clock)
        fake.my = {"days": [day_entry("2026-10-09", [(0, "数学", "08:00", "08:40")]),
                            day_entry("2026-10-10", [(1, "数学", "09:00", "09:40")])]}
        reply = await run_command(service, PRIVATE, "rci 绑定 rci_secret")
        assert "张老师" in reply and "rci_secret" not in reply
        assert service.store.bindings["aiocqhttp:42"]["server_url"] == "https://rci.example.com"

        await service.tick()  # 06:50：只建立换课基线
        assert sent == []
        clock[0] = clock[0].replace(hour=7, minute=1)
        await service.tick()
        assert len(sent) == 1 and "【今日日程】" in sent[0][1]
        await service.tick()
        assert len(sent) == 1  # 不重复

        # 换课：数学 → 物理
        fake.my = {"days": [day_entry("2026-10-09", [(0, "物理", "08:00", "08:40")]),
                            day_entry("2026-10-10", [(1, "数学", "09:00", "09:40")])]}
        service._cache.clear()
        service._last_poll.clear()
        clock[0] = clock[0].replace(hour=7, minute=20)
        await service.tick()
        assert any("数学 → 物理" in text for _, text in sent)
    asyncio.run(run())


def test_holiday_pauses_all_pushes(tmp_path):
    async def run():
        clock = [datetime(2026, 10, 1, 7, 1, tzinfo=TZ)]
        service, fake, sent = make(tmp_path, clock)
        service.holidays.load_year_data(2026, {"days": [{"name": "国庆节", "date": "2026-10-01", "isOffDay": True}]})
        fake.my = {"days": [day_entry("2026-10-01", [(0, "数学", "08:00", "08:40")])]}
        await run_command(service, PRIVATE, "rci 绑定 rci_x")
        await service.tick()
        assert sent == []
    asyncio.run(run())


def test_group_rules_bind_refused_and_no_personal_reminders(tmp_path):
    async def run():
        clock = [datetime(2026, 10, 9, 7, 1, tzinfo=TZ)]
        service, fake, sent = make(tmp_path, clock)
        assert "私聊" in await run_command(service, GROUP, "rci 登录 zhang pwd")
        assert not service.store.bindings
        await run_command(service, PRIVATE, "rci 绑定 rci_x")
        service.store.touch_session(GROUP.umo, kind="group", name="群 900", platform="aiocqhttp", user_key=None)
        fake.class_schedule = {"days": [{"date": "2026-10-09", "courses": [
            {"index": 0, "label": "第1节", "subject": "语文", "startTime": "08:00", "endTime": "08:40", "enabled": True,
             "teacher": "李老师"}]}]}
        fake.my = {"days": []}
        await service.tick()
        assert all(umo != GROUP.umo for umo, _ in sent)  # 群默认不推送
        await service.update_session(GROUP.umo, {"role": "class_group", "binding_key": PRIVATE.user_key,
                                                 "class_id": CLASS_A, "push": {"enabled": True}})
        await service.tick()
        group_msgs = [t for umo, t in sent if umo == GROUP.umo]
        assert len(group_msgs) == 1 and "语文" in group_msgs[0] and "李老师" in group_msgs[0]
    asyncio.run(run())


def test_reminder_commands(tmp_path):
    async def run():
        clock = [datetime(2026, 10, 9, 12, 0, tzinfo=TZ)]
        service, _, _ = make(tmp_path, clock)
        await run_command(service, PRIVATE, "rci 绑定 rci_x")
        await run_command(service, PRIVATE, "/rci 提醒 当日 7点半")
        await run_command(service, PRIVATE, "/rci 提醒 次日 21:00")
        await run_command(service, PRIVATE, "/rci 提醒 关 课前")
        prefs = service.store.effective_reminders(service.store.bindings[PRIVATE.user_key])
        assert prefs["today_time"] == "07:30"
        assert prefs["tomorrow_mode"] == "fixed" and prefs["tomorrow_time"] == "21:00"
        assert prefs["before_enabled"] is False
        await run_command(service, PRIVATE, "/rci 提醒 次日 下课")
        assert service.store.effective_reminders(service.store.bindings[PRIVATE.user_key])["tomorrow_mode"] == "last_class"
        assert "无法识别" not in await run_command(service, PRIVATE, "rci 提醒")
        await run_command(service, PRIVATE, "/rci 提醒 重置")
        assert service.store.bindings[PRIVATE.user_key]["prefs"] == {}
        assert await run_command(service, PRIVATE, "rci help") == HELP
    asyncio.run(run())


def test_dangerous_command_requires_confirmation(tmp_path):
    async def run():
        clock = [datetime(2026, 10, 9, 12, 0, tzinfo=TZ)]
        service, fake, _ = make(tmp_path, clock)
        await run_command(service, PRIVATE, "rci 绑定 rci_x")
        try:
            await service.send_command(PRIVATE, "高一", 5, {"powerAction": 1}, confirmed=False)
            raise AssertionError("expected refusal")
        except Exception as ex:  # noqa: BLE001
            assert "confirmed=true" in str(ex)
        assert not any(c[1] == "/api/commands" for c in fake.calls)
        reply = await run_command(service, PRIVATE, "rci 通知 高一 下午班会")
        assert "已显示" in reply
        body = [c for c in fake.calls if c[1] == "/api/commands"][0][3]
        assert body["command"] == 2 and body["notification"]["message"] == "下午班会"
    asyncio.run(run())
