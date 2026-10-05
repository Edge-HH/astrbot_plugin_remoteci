import asyncio
from datetime import datetime, timedelta, timezone

from remoteci.service import ChatUser, RemoteCiService
from remoteci.swaps import SwapInbox, format_notification, format_request, run_swap_command

TZ = timezone(timedelta(hours=8))
PRIVATE = ChatUser("aiocqhttp:7", "aiocqhttp", "7", "李老师", "aiocqhttp:FriendMessage:7", True)
REQUEST = {
    "id": "a1b2c3d4-0000-0000-0000-000000000000", "shortId": "a1b2c3d4", "mode": 1, "status": 1, "forced": False,
    "requesterName": "王老师", "reason": "外出教研", "canDecide": True,
    "source": {"classId": "c1", "date": "2026-10-06", "index": 2, "className": "高一1班", "label": "第3节",
               "subject": "数学", "teacher": "王老师"},
    "target": {"classId": "c1", "date": "2026-10-07", "index": 0, "className": "高一1班", "label": "第1节",
               "subject": "英语", "teacher": "李老师"},
}


class FakeSwapServer:
    def __init__(self):
        self.notifications = []
        self.calls = []

    async def __call__(self, binding, method, path, *, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path == "/api/me/notifications":
            after = (params or {}).get("after")
            items = [n for n in self.notifications if not after or n["createdAt"] > after]
            items.sort(key=lambda n: n["createdAt"], reverse=True)
            return items[: int((params or {}).get("limit", 50))]
        if path == "/api/swap-requests":
            return [REQUEST]
        if path.startswith("/api/swap-requests/") and method == "POST":
            return {**REQUEST, "status": 2 if path.endswith("/approve") else 3, "canDecide": False,
                    "decisionNote": (body or {}).get("note")}
        raise AssertionError(path)


def make(tmp_path, permissions=6665):
    sent = []

    async def send(umo, text):
        sent.append((umo, text))
        return True

    service = RemoteCiService(tmp_path, send=send, config={}, now=lambda: datetime(2026, 10, 5, 8, 0, tzinfo=TZ))
    fake = FakeSwapServer()
    service._call = fake  # noqa: SLF001 - 替换网络层
    service.store.bindings[PRIVATE.user_key] = {
        "key": PRIVATE.user_key, "umo": PRIVATE.umo, "server_url": "https://rci.example.com",
        "auth": {"type": "api_key", "api_key": "rci_x"}, "status": "ok",
        "profile": {"displayName": "李老师", "permissions": permissions, "classes": []},
    }
    return service, fake, sent


def test_inbox_sets_baseline_then_pushes_new_notifications_with_reply_hint(tmp_path):
    async def run():
        service, fake, sent = make(tmp_path)
        fake.notifications.append({"id": "n0", "kind": "swap_requested", "title": "旧通知", "body": "",
                                   "createdAt": "2026-10-05T00:00:00+00:00"})
        inbox = SwapInbox(service)
        assert await inbox.poll(force=True)
        assert sent == []  # 第一次只建立游标
        fake.notifications.append({"id": "n1", "kind": "swap_requested", "title": "王老师 申请与你换课",
                                   "body": "高一1班 ⇄ 高一1班\n理由：外出教研", "swapRequestId": REQUEST["id"],
                                   "createdAt": "2026-10-05T01:00:00+00:00"})
        assert await inbox.poll(force=True)
        assert len(sent) == 1 and sent[0][0] == PRIVATE.umo
        assert "王老师 申请与你换课" in sent[0][1] and "/rci 换课 通过 a1b2c3d4" in sent[0][1]
        assert not await inbox.poll(force=True)  # 不重复推送
        assert len(sent) == 1
    asyncio.run(run())


def test_inbox_skips_accounts_without_swap_permission(tmp_path):
    async def run():
        service, fake, sent = make(tmp_path, permissions=2569)
        assert not await SwapInbox(service).poll(force=True)
        assert fake.calls == []
    asyncio.run(run())


def test_swap_commands_list_and_decide(tmp_path):
    async def run():
        service, fake, _ = make(tmp_path)
        listing = await run_swap_command(service, PRIVATE, ["待办"])
        assert "[a1b2c3d4] 王老师" in listing and "外出教研" in listing and "⇄" in listing
        reply = await run_swap_command(service, PRIVATE, ["通过", "a1b2c3d4", "好的"])
        assert reply.startswith("已同意换课")
        assert fake.calls[-1][1] == "/api/swap-requests/a1b2c3d4/approve" and fake.calls[-1][3] == {"note": "好的"}
        reply = await run_swap_command(service, PRIVATE, ["拒绝", "a1b2c3d4"])
        assert reply.startswith("已拒绝")
        assert "用法" in await run_swap_command(service, PRIVATE, ["撤回"])
        assert "换课申请" in await run_swap_command(service, PRIVATE, ["未知"])
    asyncio.run(run())


def test_formatters():
    assert "撤回" in format_notification({"kind": "swap_forced", "title": "王老师 强制换走了你的课", "body": "",
                                          "swapRequestId": REQUEST["id"]})
    replace = {**REQUEST, "mode": 2, "source": None, "subjectName": "数学"}
    assert "→ 数学（王老师）" in format_request(replace)
