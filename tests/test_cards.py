import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

from remoteci.cards import build_card, dingtalk_card, lark_card, site_path, text_message, wecom_card
from remoteci.client import RemoteCiError
from remoteci.delivery import NoteDelivery, parse_umo
from remoteci.service import RemoteCiService

TZ = timezone(timedelta(hours=8))
SERVER = "https://rci.example.com"
REQ = "a1b2c3d4-0000-0000-0000-000000000000"
LINKS = {"swap.approve": "https://rci.example.com/a/approve-token", "swap.reject": "/a/reject-token"}
SWAP = {"kind": "swap_requested", "title": "王老师 申请与你换课", "body": "高一1班 第3节 数学 ⇄ 第1节 英语\n理由：外出教研",
        "swapRequestId": REQ}
OFFLINE = {"kind": "device_offline", "title": "高一1班 教室电脑离线", "body": "已离线 10 分钟", "link": "/Devices?classId=c1"}


def test_swap_card_has_signed_links_quick_reply_and_view_button():
    card = build_card(SWAP, SERVER, {**LINKS, "swap.reject": "https://rci.example.com/a/reject-token"})
    assert card.title == "RemoteCI 换课 · 王老师 申请与你换课" and card.short == "a1b2c3d4"
    assert [(b.title, bool(b.url), b.reply) for b in card.buttons] == [
        ("同意", True, ""), ("拒绝", True, ""), ("直接回复“同意”", False, "同意 a1b2c3d4"), ("在 RemoteCI 中查看", True, "")]
    assert card.link == SERVER + "/SwapRequests" and "直接回复“同意”" in card.hint
    forced = build_card({"kind": "swap_forced", "title": "强制换课", "swapRequestId": REQ}, SERVER,
                        {"swap.revoke": "https://rci.example.com/a/revoke"})
    assert [b.title for b in forced.buttons] == ["撤回", "直接回复“撤回”", "在 RemoteCI 中查看"]


def test_device_and_automation_cards_link_to_site_pages_only():
    card = build_card(OFFLINE, SERVER + "/")
    assert [(b.title, b.url) for b in card.buttons] == [("查看设备状态", SERVER + "/Devices?classId=c1")]
    assert card.tone == "red" and "/rci 设备" in card.hint
    evil = build_card({**OFFLINE, "link": "https://evil.example.com/x"}, SERVER)
    assert evil.link == SERVER + "/Devices"
    assert site_path("//evil.example.com") == "" and site_path("/Automation") == "/Automation"
    auto = build_card({"kind": "automation_failed", "title": "放学关机 执行失败"}, SERVER)
    assert auto.buttons[0].title == "查看规则" and auto.link == SERVER + "/Automation"
    assert build_card({"kind": "swap_approved", "title": "已通过"}, SERVER) is None


def test_text_message_adds_view_link_but_never_signed_links():
    text = text_message(SWAP, SERVER)
    assert text.startswith("【RemoteCI 换课】王老师 申请与你换课")
    assert "直接回复“同意”" in text and text.endswith("在 RemoteCI 中查看：https://rci.example.com/SwapRequests")
    assert "token" not in text
    assert text_message(OFFLINE, SERVER).endswith("查看设备状态：https://rci.example.com/Devices?classId=c1")
    assert "【RemoteCI 设备离线】" in text_message(OFFLINE, SERVER)
    course = {"kind": "personal_course_start", "title": "上课提醒", "body": ""}
    assert text_message(course, SERVER) == "【RemoteCI 课程提醒】上课提醒"


def test_lark_card_json_v2():
    data = lark_card(build_card(SWAP, SERVER, {**LINKS, "swap.reject": SERVER + "/a/reject-token"}))
    assert data["schema"] == "2.0" and data["header"]["template"] == "blue"
    assert data["header"]["title"] == {"tag": "plain_text", "content": "RemoteCI 换课 · 王老师 申请与你换课"}
    body, buttons = data["body"]["elements"]
    assert "理由：外出教研" in body["text"]["content"]
    assert buttons["tag"] == "column_set"
    rendered = [c["elements"][0] for c in buttons["columns"]]
    assert [b["text"]["content"] for b in rendered] == ["同意", "拒绝", "在 RemoteCI 中查看"]  # 飞书没有快捷回复按钮
    assert rendered[0]["type"] == "primary_filled" and rendered[1]["type"] == "danger"
    assert rendered[0]["behaviors"] == [{"type": "open_url", "default_url": LINKS["swap.approve"]}]
    json.dumps(data, ensure_ascii=False)


def test_dingtalk_card_wraps_links_and_quick_reply():
    key, param = dingtalk_card(build_card(SWAP, SERVER, {**LINKS, "swap.reject": SERVER + "/a/reject-token"}))
    assert key == "sampleActionCard4"
    assert param["title"] == "RemoteCI 换课 · 王老师 申请与你换课" and param["text"].startswith("#### ")
    assert "理由：外出教研" in param["text"]
    assert param["actionTitle1"] == "同意"
    assert param["actionURL1"].startswith("dingtalk://dingtalkclient/page/link?url=https%3A%2F%2F")
    assert param["actionURL1"].endswith("&pc_slide=false")
    assert unquote(param["actionURL1"].split("url=")[1].split("&")[0]) == LINKS["swap.approve"]
    assert param["actionURL3"] == "dtmd://dingtalkclient/sendMessage?content=%E5%90%8C%E6%84%8F%20a1b2c3d4"
    key, param = dingtalk_card(build_card(OFFLINE, SERVER))
    assert key == "sampleActionCard" and param["singleTitle"] == "查看设备状态" and "singleURL" in param


def test_wecom_text_notice_card():
    data = wecom_card(build_card(SWAP, SERVER, {**LINKS, "swap.reject": SERVER + "/a/reject-token"}), "lisi", 1000002)
    assert data["msgtype"] == "template_card" and data["agentid"] == 1000002 and data["touser"] == "lisi"
    card = data["template_card"]
    assert card["card_type"] == "text_notice" and len(card["main_title"]["title"]) <= 36
    assert len(card["sub_title_text"]) <= 160
    assert [j["title"] for j in card["jump_list"]] == ["同意", "拒绝", "在 RemoteCI 中查看"]
    assert all(j["type"] == 1 and j["url"].startswith("https://") for j in card["jump_list"])
    assert card["card_action"] == {"type": 1, "url": SERVER + "/SwapRequests"}
    assert card["horizontal_content_list"] == [{"keyname": "编号", "value": "a1b2c3d4"}]


def test_parse_umo():
    target = parse_umo("my_lark:GroupMessage:oc_1:extra")
    assert (target.platform_id, target.is_group, target.session_id) == ("my_lark", True, "oc_1:extra")
    assert parse_umo("bad") is None


# ---------- 发送与回退（假的 AstrBot 上下文与企业微信客户端） ----------

class Meta:
    def __init__(self, name, id):
        self.name, self.id = name, id


class FakeWecomClient:
    def __init__(self):
        self.posts = []

    def post(self, url, data=None):
        self.posts.append((url, data))
        return {"errcode": 0, "errmsg": "ok"}


class FakeAdapter:
    def __init__(self, name, id):
        self._meta = Meta(name, id)
        self.client = FakeWecomClient()
        self.config = {"id": id}
        self.agent_id = "1000002"

    def meta(self):
        return self._meta


class FakeManager:
    def __init__(self, adapters):
        self.platform_insts = adapters


class FakeContext:
    def __init__(self, adapters):
        self.platform_manager = FakeManager(adapters)


def make(tmp_path, adapter, link_status=200):
    sent = []

    async def send(umo, text):
        sent.append((umo, text))
        return True

    service = RemoteCiService(tmp_path, send=send, config={}, now=lambda: datetime(2026, 10, 5, 8, 0, tzinfo=TZ))
    calls = []

    async def fake_call(binding, method, path, *, params=None, body=None):
        calls.append((method, path, body))
        if path == "/api/me/action-links":
            if link_status != 200:
                raise RemoteCiError(link_status, "Not Found")
            return {"url": f"/a/{body['action']}-token", "expiresAt": "2026-10-06T00:00:00Z"}
        raise AssertionError(path)

    service._call = fake_call  # noqa: SLF001 - 替换网络层
    service.note_sender = NoteDelivery(service, FakeContext([adapter]))
    binding = {"key": "wecom:lisi", "umo": "corp:FriendMessage:lisi", "server_url": SERVER}
    return service, sent, calls, binding


def test_wecom_card_delivery_and_text_fallbacks(tmp_path):
    async def run():
        adapter = FakeAdapter("wecom", "corp")
        service, sent, calls, binding = make(tmp_path, adapter)
        assert await service.push_note(binding["umo"], binding, SWAP, "原文")
        assert sent == []
        url, payload = adapter.client.posts[0]
        assert url == "message/send" and payload["agentid"] == 1000002 and payload["touser"] == "lisi"
        assert payload["template_card"]["jump_list"][0]["url"] == SERVER + "/a/swap.approve-token"
        assert [c[2]["action"] for c in calls] == ["swap.approve", "swap.reject"]
        assert "已发送卡片" in service.store.state["log"][-1]["text"]

        # 关闭企业微信卡片后改发文本（附查看链接，不含签名链接）。
        await service.update_card_settings({"wecom": False})
        assert await service.push_note(binding["umo"], binding, SWAP, "原文")
        assert len(adapter.client.posts) == 1 and sent[-1][1].endswith("/SwapRequests")
    asyncio.run(run())


def test_action_link_404_and_unknown_platform_fall_back_to_text(tmp_path):
    async def run():
        adapter = FakeAdapter("wecom", "corp")
        service, sent, _, binding = make(tmp_path, adapter, link_status=404)
        assert await service.push_note(binding["umo"], binding, SWAP, "原文")
        assert adapter.client.posts == [] and "直接回复“同意”" in sent[-1][1]
        # 设备告警不需要签名链接，接口 404 也照样发卡片。
        assert await service.push_note(binding["umo"], binding, OFFLINE, "原文")
        assert adapter.client.posts[-1][1]["template_card"]["jump_list"][0]["title"] == "查看设备状态"

        qq = {**binding, "umo": "aiocqhttp:FriendMessage:42"}
        assert await service.push_note(qq["umo"], qq, OFFLINE, "原文")
        assert sent[-1] == ("aiocqhttp:FriendMessage:42", text_message(OFFLINE, SERVER))
    asyncio.run(run())
