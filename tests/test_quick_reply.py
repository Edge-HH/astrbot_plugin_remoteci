import asyncio
import time
from datetime import datetime, timedelta, timezone

from remoteci.quick_reply import active_pending, handle_quick_reply, parse_quick_reply, remember_pending
from remoteci.service import ChatUser, RemoteCiService

TZ = timezone(timedelta(hours=8))
PRIVATE = ChatUser("aiocqhttp:7", "aiocqhttp", "7", "李老师", "aiocqhttp:FriendMessage:7", True)
GROUP = ChatUser("aiocqhttp:7", "aiocqhttp", "7", "李老师", "aiocqhttp:GroupMessage:900", False)
REQ_A = "a1b2c3d4-0000-0000-0000-000000000000"
REQ_B = "b2c3d4e5-0000-0000-0000-000000000000"
FORCED = "c3d4e5f6-0000-0000-0000-000000000000"


def note(request_id, kind="swap_requested", title="王老师 申请与你换课"):
    return {"kind": kind, "title": title, "swapRequestId": request_id}


class FakeServer:
    def __init__(self):
        self.incoming = []
        self.calls = []

    async def __call__(self, binding, method, path, *, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path == "/api/swap-requests":
            return self.incoming
        if path.startswith("/api/swap-requests/") and method == "POST":
            short = path.split("/")[3]
            item = next(x for x in self.incoming if x["id"].replace("-", "").startswith(short))
            return {**item, "status": 2, "canDecide": False, "canRevoke": False}
        raise AssertionError(path)


def make(tmp_path):
    async def send(umo, text):
        return True

    service = RemoteCiService(tmp_path, send=send, config={}, now=lambda: datetime(2026, 10, 5, 8, 0, tzinfo=TZ))
    fake = FakeServer()
    service._call = fake  # noqa: SLF001 - 替换网络层
    binding = {"key": PRIVATE.user_key, "umo": PRIVATE.umo, "server_url": "https://rci.example.com",
               "auth": {"type": "api_key", "api_key": "rci_x"}, "status": "ok",
               "profile": {"displayName": "李老师", "classes": []}}
    service.store.bindings[PRIVATE.user_key] = binding
    return service, fake, binding


def request(request_id, *, can_decide=True, can_revoke=False):
    return {"id": request_id, "shortId": request_id[:8], "status": 1, "requesterName": "王老师", "reason": "教研",
            "canDecide": can_decide, "canRevoke": can_revoke, "mode": 1,
            "source": {"className": "高一1班", "date": "2026-10-06", "label": "第3节", "subject": "数学"},
            "target": {"className": "高一1班", "date": "2026-10-07", "label": "第1节", "subject": "英语"}}


def test_parse_quick_reply_accepts_short_replies_only():
    assert parse_quick_reply("同意") == ("同意", "approve", None, "")
    assert parse_quick_reply("通过 a1b2c3d4 好的") == ("通过", "approve", "a1b2c3d4", "好的")
    assert parse_quick_reply("批准，辛苦了！") == ("批准", "approve", None, "辛苦了")
    assert parse_quick_reply("拒绝 那天我要监考") == ("拒绝", "reject", None, "那天我要监考")
    assert parse_quick_reply("拒绝 [A1B2C3D4] 有课") == ("拒绝", "reject", "A1B2C3D4", "有课")
    assert parse_quick_reply("撤回 " + FORCED) == ("撤回", "revoke", FORCED, "")
    for text in ("同意你的看法", "我同意", "同意吗？", "通过 什么方式?", "/rci 换课 通过 a1b2c3d4", "",
                 "同意 " + "很" * 40):
        assert parse_quick_reply(text) is None, text


def test_pending_actions_are_recorded_for_swap_notifications_and_expire():
    binding = {}
    assert remember_pending(binding, note(REQ_A), now=1000)
    assert remember_pending(binding, note(FORCED, "swap_forced"), now=1000)
    assert not remember_pending(binding, {"kind": "swap_approved", "swapRequestId": REQ_B}, now=1000)
    assert not remember_pending(binding, {"kind": "device_offline"}, now=1000)
    assert remember_pending(binding, note(REQ_A, title="再次提醒"), now=2000)  # 同一申请只保留最新一条
    items = active_pending(binding, now=2000)
    assert [p["short"] for p in items] == ["c3d4e5f6", "a1b2c3d4"] and items[-1]["title"] == "再次提醒"
    assert active_pending(binding, now=1000 + 24 * 3600 + 1) == [items[-1]]


def test_quick_reply_approves_the_only_pending_request(tmp_path):
    async def run():
        service, fake, binding = make(tmp_path)
        fake.incoming = [request(REQ_A)]
        remember_pending(binding, note(REQ_A))
        reply = await handle_quick_reply(service, PRIVATE, "同意，好的")
        assert reply.startswith("已同意换课")
        assert fake.calls[-1][1] == "/api/swap-requests/a1b2c3d4/approve" and fake.calls[-1][3] == {"note": "好的"}
        assert binding["pending_actions"] == []
        # 处理完后不再拦截，普通的“同意”交给后续处理。
        assert await handle_quick_reply(service, PRIVATE, "同意") is None
    asyncio.run(run())


def test_quick_reply_lists_ids_when_several_are_pending(tmp_path):
    async def run():
        service, fake, binding = make(tmp_path)
        fake.incoming = [request(REQ_A), request(REQ_B)]
        remember_pending(binding, note(REQ_A))
        remember_pending(binding, note(REQ_B, title="张老师 申请与你换课"))
        reply = await handle_quick_reply(service, PRIVATE, "拒绝 没空")
        assert "2 条待处理的换课申请" in reply and "[a1b2c3d4]" in reply and "[b2c3d4e5] 张老师" in reply
        assert "拒绝 编号 理由" in reply
        assert not any(c[0] == "POST" for c in fake.calls)
        reply = await handle_quick_reply(service, PRIVATE, "拒绝 b2c3d4e5 没空")
        assert reply.startswith("已")
        assert fake.calls[-1][1] == "/api/swap-requests/b2c3d4e5/reject" and fake.calls[-1][3] == {"note": "没空"}
        assert [p["short"] for p in binding["pending_actions"]] == ["a1b2c3d4"]
    asyncio.run(run())


def test_quick_reply_skips_handled_requests_groups_and_other_kinds(tmp_path):
    async def run():
        service, fake, binding = make(tmp_path)
        remember_pending(binding, note(REQ_A))
        assert await handle_quick_reply(service, GROUP, "同意") is None  # 只在私聊
        assert await handle_quick_reply(service, PRIVATE, "撤回") is None  # 没有强制换课
        assert fake.calls == []
        # 已在 WebUI 处理：服务端不再可审批，不拦截并清掉记录。
        fake.incoming = [request(REQ_A, can_decide=False)]
        assert await handle_quick_reply(service, PRIVATE, "同意") is None
        assert binding["pending_actions"] == []
        # 超过 24 小时的推送不再触发。
        fake.incoming = [request(REQ_A)]
        binding["pending_actions"] = [{"id": REQ_A, "short": "a1b2c3d4", "kind": "swap_requested", "title": "",
                                       "at": time.time() - 25 * 3600}]
        assert await handle_quick_reply(service, PRIVATE, "同意") is None
    asyncio.run(run())


def test_quick_reply_revokes_forced_swap(tmp_path):
    async def run():
        service, fake, binding = make(tmp_path)
        fake.incoming = [request(FORCED, can_decide=False, can_revoke=True)]
        remember_pending(binding, note(FORCED, "swap_forced", "王老师 强制换走了你的课"))
        reply = await handle_quick_reply(service, PRIVATE, "撤回")
        assert reply.startswith("已撤回强制换课")
        assert fake.calls[-1][1] == "/api/swap-requests/c3d4e5f6/revoke"
    asyncio.run(run())
