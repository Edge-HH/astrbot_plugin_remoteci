import asyncio

from remoteci.cards import build_card
from remoteci.client import RemoteCiError
from remoteci.commands import HELP, run_command
from remoteci.operations import describe_app_guard
from remoteci.swaps import format_notification, is_ops_kind, log_kind
from tests.test_operations import NOW, PRIVATE, make

SERVER = "https://rci.example.com"
POLICY = {"enabled": True, "restartAfterExit": True, "restartAfterExitDelayMinutes": 40, "restartOnCrash": True,
          "preventDeletion": False, "backupIntervalHours": 24, "organizationName": "实验中学"}
EVENTS = [{"credentialId": "d1", "deviceName": "教室电脑", "kind": "restored", "kindName": "已自动恢复",
           "detail": "已恢复 12 个文件并重新启动", "atUnixMs": int(NOW.timestamp() * 1000)}]


def guard_server(fake, policy):
    plain = fake.__call__

    async def call(binding, method, path, *, params=None, body=None):
        if path == "/api/settings/app-guard":
            fake.calls.append((method, path, params, body))
            if path in fake.forbidden:
                raise RemoteCiError(403, "权限不足")
            if method == "PUT":
                policy.clear()
                policy.update(body)
            return dict(policy)
        if path == "/api/app-guard/events":
            fake.calls.append((method, path, params, body))
            return EVENTS
        return await plain(binding, method, path, params=params, body=body)

    return call


def test_app_guard_command_shows_toggles_and_sets_delay(tmp_path):
    async def run():
        service, fake = make(tmp_path)
        policy = dict(POLICY)
        service._call = guard_server(fake, policy)  # noqa: SLF001 - 替换网络层

        text = await run_command(service, PRIVATE, "rci 守护")
        assert "应用守护：退出 40 分钟后自启 / 意外退出时重启" in text
        assert "恢复提示的管理方：实验中学" in text
        assert "10-18 15:00 教室电脑 已自动恢复：已恢复 12 个文件并重新启动" in text
        assert fake.calls[-1][2] == {"limit": 5}

        assert "已经是开启状态" in await run_command(service, PRIVATE, "rci 守护 开")
        saved = await run_command(service, PRIVATE, "rci 守护 关")
        assert "未启用" in saved
        method, path, _, body = fake.calls[-1]
        assert (method, path) == ("PUT", "/api/settings/app-guard")
        assert body["enabled"] is False and body["restartAfterExitDelayMinutes"] == 40 and body["organizationName"] == "实验中学"

        assert "退出后立即自启" in await run_command(service, PRIVATE, "rci 守护 延时 0")
        assert policy["enabled"] is True and policy["restartAfterExit"] is True
        assert "范围 0–1440" in await run_command(service, PRIVATE, "rci 守护 延时 两小时")
        assert "范围 0–1440" in await run_command(service, PRIVATE, "rci 守护 延时 99999")
        assert "/rci 守护 延时" in await run_command(service, PRIVATE, "rci 守护 帮我看看")

        fake.forbidden.add("/api/settings/app-guard")
        assert "只有系统管理员" in await run_command(service, PRIVATE, "rci 守护")
        assert "/rci 守护" in HELP
    asyncio.run(run())


def test_describe_app_guard():
    assert describe_app_guard({"enabled": False}) == "未启用"
    assert describe_app_guard({"enabled": True}) == "已启用（未开启任何守护项）"
    assert describe_app_guard({"enabled": True, "preventDeletion": True, "backupIntervalHours": 6}) == "禁止删除（每 6 小时备份）"


def test_app_guard_notification_is_ops_alert_with_card_and_hint():
    note = {"kind": "app_guard", "title": "教室电脑：ClassIsland 已自动恢复", "body": "检测到 ClassIsland 被删除"}
    assert format_notification(note).startswith("【RemoteCI 应用守护】") and "/rci 守护" in format_notification(note)
    assert is_ops_kind("app_guard") and log_kind("app_guard") == "device"
    card = build_card({**note, "link": "/AppGuard"}, SERVER)
    assert [(b.title, b.url) for b in card.buttons] == [("查看应用守护", SERVER + "/AppGuard")]
    assert card.tone == "orange"
