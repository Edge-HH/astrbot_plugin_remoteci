import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from remoteci.client import RemoteCiError
from remoteci.commands import HELP, run_command
from remoteci.operations import format_audit, format_devices, format_lesson_stats, parse_period
from remoteci.service import ChatUser, RemoteCiService, ServiceError

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 18, 15, 0, tzinfo=TZ)
PRIVATE = ChatUser("aiocqhttp:1", "aiocqhttp", "1", "管理员", "aiocqhttp:FriendMessage:1", True)
CLASS_A = "11111111-0000-0000-0000-00000000000a"
RULE = {"id": "r1", "name": "放学关机", "enabled": True, "lastRunAt": "2026-10-18T09:30:00Z", "lastResult": "成功 3 个班级",
        "trigger": {"kind": "time"}, "actions": [{"command": 5}], "target": {"classIds": [CLASS_A]}}
DEVICES = [
    {"credentialId": "d1", "classId": CLASS_A, "className": "高一1班", "deviceName": "教室电脑", "online": False,
     "lastSeenAt": "2026-10-18T05:00:00Z", "offlineSince": "2026-10-18T05:00:00Z", "pluginVersion": "2.1.0",
     "classIslandVersion": "2.0.1", "outdated": True},
    {"credentialId": "d2", "classId": "c2", "className": "高一2班", "deviceName": "一体机", "online": True,
     "lastSeenAt": "2026-10-18T07:00:00Z", "pluginVersion": "2.2.0", "classIslandVersion": "2.0.1", "outdated": False,
     "profileDriftCount": 2},
]


class FakeServer:
    def __init__(self):
        self.calls = []
        self.forbidden = set()

    async def __call__(self, binding, method, path, *, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path in self.forbidden:
            raise RemoteCiError(403, "权限不足")
        if path == "/api/devices/health":
            return DEVICES
        if path in ("/api/me/lesson-stats", "/api/lesson-stats"):
            return {"from": params["from"], "to": params["to"], "teachers": [
                {"teacher": params.get("teacher") or "张老师", "total": 32, "substitute": 2, "substituted": 0,
                 "classes": [{"className": "高一1班", "total": 20}, {"className": "高一2班", "total": 12}]}]}
        if path == "/api/audit":
            return {"items": [{"id": "1", "createdAt": "2026-10-18T06:02:00Z", "actorName": "张老师", "source": "WebUi",
                               "sourceLabel": "网页", "className": "高一1班", "deviceName": "教室电脑", "command": 2,
                               "summary": "通知：下午班会", "success": False, "resultMessage": "教室端离线"}]}
        if path == "/api/automation/rules":
            return [RULE, {"id": "r2", "name": "早读开机", "enabled": False}]
        if path == "/api/automation/rules/r1/run":
            return [{"className": "高一1班", "status": "Succeeded", "statusLabel": "成功"},
                    {"className": "高一2班", "status": "Failed", "statusLabel": "失败", "message": "设备离线"}]
        if path == "/api/automation/rules/r1":
            return RULE if method == "GET" else {**body}
        raise AssertionError(path)


def make(tmp_path):
    async def send(umo, text):
        return True

    service = RemoteCiService(tmp_path, send=send, config={}, now=lambda: NOW)
    fake = FakeServer()
    service._call = fake  # noqa: SLF001 - 替换网络层
    service.store.bindings[PRIVATE.user_key] = {
        "key": PRIVATE.user_key, "umo": PRIVATE.umo, "server_url": "https://rci.example.com",
        "auth": {"type": "api_key", "api_key": "rci_x"}, "status": "ok",
        "profile": {"displayName": "管理员", "roleKind": 2, "classes": [{"id": CLASS_A, "name": "高一1班"}]},
    }
    return service, fake


def test_format_devices_lists_offline_outdated_and_drift():
    text = format_devices(DEVICES, NOW)
    assert text.splitlines()[0] == "设备状态：共 2 台，在线 1 台，离线 1 台，待升级 1 台"
    assert "· 高一1班 教室电脑  已离线 2 小时（最后在线 10-18 13:00）" in text
    assert "插件 2.1.0 · ClassIsland 2.0.1" in text and "高一2班 一体机  2 处" in text
    assert "全部在线" in format_devices([{**DEVICES[1], "profileDriftCount": 0}], NOW)


def test_parse_period():
    today = date(2026, 10, 18)
    assert parse_period([], today) == (date(2026, 10, 1), today, [])
    assert parse_period(["上月", "李老师"], today) == (date(2026, 9, 1), date(2026, 9, 30), ["李老师"])
    assert parse_period(["2026-09-01", "2026-09-15", "王"], today) == (date(2026, 9, 1), date(2026, 9, 15), ["王"])
    assert parse_period(["李老师"], today) == (date(2026, 10, 1), today, ["李老师"])
    assert parse_period(["上月"], date(2026, 1, 5))[:2] == (date(2025, 12, 1), date(2025, 12, 31))
    for bad in (["2026-09-15", "2026-09-01"], ["2026-09-01"]):
        with pytest.raises(ServiceError):
            parse_period(bad, today)


def test_formatters_for_lessons_and_audit():
    text = format_lesson_stats({"from": "2026-10-01", "to": "2026-10-18", "teachers": [
        {"teacher": "张老师", "total": 3, "substitute": 0, "substituted": 1, "classes": [{"className": "高一1班", "total": 3}]}]})
    assert "张老师：共 3 节（被代 1 节）" in text and "高一1班 3 节" in text
    assert "没有课时记录" in format_lesson_stats({"teachers": []})
    audit = format_audit({"items": [{"createdAt": "2026-10-18T06:02:00Z", "actorName": "张老师", "sourceLabel": "网页",
                                     "command": 2, "commandLabel": "通知", "success": True}]}, TZ)
    assert "· 10-18 14:02 张老师（网页） 通知 ✅" in audit


def test_operation_commands(tmp_path):
    async def run():
        service, fake = make(tmp_path)
        assert "离线 1 台" in await run_command(service, PRIVATE, "rci 设备")

        text = await run_command(service, PRIVATE, "rci 课时 上月")
        assert fake.calls[-1][1:3] == ("/api/me/lesson-stats", {"from": "2026-09-01", "to": "2026-09-30"})
        assert "张老师：共 32 节（代课 2 节）" in text and "高一1班 20 节、高一2班 12 节" in text
        await run_command(service, PRIVATE, "rci 课时 李老师")
        assert fake.calls[-1][1:3] == ("/api/lesson-stats", {"from": "2026-10-01", "to": "2026-10-18", "teacher": "李老师"})
        fake.forbidden.add("/api/lesson-stats")
        assert "只有系统管理员" in await run_command(service, PRIVATE, "rci 课时 李老师")

        text = await run_command(service, PRIVATE, "rci 审计 高一1班")
        assert fake.calls[-1][2] == {"limit": 10, "classId": CLASS_A}
        assert "（高一1班）" in text and "张老师（网页·" not in text and "❌ 教室端离线" in text
        assert "找不到班级" in await run_command(service, PRIVATE, "rci 审计 三年级")

        listing = await run_command(service, PRIVATE, "rci 自动化")
        assert "[启用] 放学关机，上次 10-18 17:30：成功 3 个班级" in listing and "[停用] 早读开机" in listing
        ran = await run_command(service, PRIVATE, "rci 自动化 运行 放学")
        assert "2 个班级，成功 1 个，失败 1 个" in ran and "高一2班 失败：设备离线" in ran
        assert "已经是启用状态" in await run_command(service, PRIVATE, "rci 自动化 开 放学关机")
        assert "已停用" in await run_command(service, PRIVATE, "rci 自动化 关 放学关机")
        method, path, _, body = fake.calls[-1]
        assert (method, path) == ("PUT", "/api/automation/rules/r1")
        assert body["enabled"] is False and body["trigger"] == RULE["trigger"] and body["actions"] == RULE["actions"]
        assert "找不到规则" in await run_command(service, PRIVATE, "rci 自动化 运行 午休")
        fake.forbidden.add("/api/automation/rules")
        assert "只有系统管理员" in await run_command(service, PRIVATE, "rci 自动化")
        assert "/rci 自动化" in HELP and "/rci 设备" in HELP
    asyncio.run(run())
