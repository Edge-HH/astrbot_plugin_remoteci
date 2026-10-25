import asyncio

import pytest

from remoteci import screenshots
from remoteci.client import RemoteCiError
from remoteci.commands import run_command
from remoteci.service import ChatUser, RemoteCiService, ServiceError

PRIVATE = ChatUser("aiocqhttp:1", "aiocqhttp", "1", "张老师", "aiocqhttp:FriendMessage:1", True)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SHOT = {"id": "a" * 32, "deviceName": "高一3班电脑", "width": 1920, "height": 1080, "size": 40,
        "capturedAt": "2026-10-25T02:03:04+00:00", "url": "/api/screenshots/" + "a" * 32}


class FakeClient:
    def __init__(self, error: RemoteCiError | None = None):
        self.calls = []
        self.error = error

    async def request(self, binding, method, path, *, params=None, body=None, raw=False, timeout=None,
                      on_auth_changed=None, **_):
        self.calls.append((method, path, raw, timeout))
        if self.error:
            raise self.error
        if path.endswith("/screenshot"):
            return SHOT
        if path == SHOT["url"]:
            assert raw
            return PNG
        raise AssertionError(path)


def make(tmp_path, client):
    async def send(umo, text):
        return True

    service = RemoteCiService(tmp_path, send=send, config={})
    service.client = client
    service.store.bindings[PRIVATE.user_key] = {
        "key": PRIVATE.user_key, "server_url": "https://rci.example", "auth": {"type": "api_key"}, "umo": PRIVATE.umo,
        "profile": {"displayName": "张老师", "classes": [{"id": "c1", "name": "高一3班"}, {"id": "c2", "name": "高一5班"}]},
    }
    return service


def test_capture_returns_caption_and_png(tmp_path):
    client = FakeClient()
    service = make(tmp_path, client)
    text, png = asyncio.run(screenshots.capture(service, PRIVATE, "3班"))
    assert png == PNG
    assert text.startswith("【高一3班】屏幕截图（")
    assert "高一3班电脑 · 1920×1080" in text
    assert client.calls[0] == ("POST", "/api/classes/c1/screenshot", False, screenshots.CAPTURE_TIMEOUT)
    assert client.calls[1][:3] == ("GET", SHOT["url"], True)


@pytest.mark.parametrize("status,expected", [
    (403, "屏幕截图权限"),
    (503, "不在线"),
    (409, "版本过旧"),
    (504, "超时"),
    (422, "截图失败：无法截取屏幕"),
])
def test_capture_maps_server_failures(tmp_path, status, expected):
    service = make(tmp_path, FakeClient(RemoteCiError(status, "无法截取屏幕")))
    with pytest.raises(ServiceError) as ex:
        asyncio.run(screenshots.capture(service, PRIVATE, "高一3班"))
    assert expected in str(ex.value)


def test_capture_requires_class_when_ambiguous(tmp_path):
    service = make(tmp_path, FakeClient())
    with pytest.raises(ServiceError) as ex:
        asyncio.run(screenshots.capture(service, PRIVATE, None))
    assert "高一3班、高一5班" in str(ex.value)


def test_rci_screenshot_command_uses_sender(tmp_path):
    service = make(tmp_path, FakeClient())
    seen = []

    async def sender(class_ref):
        seen.append(class_ref)
        return "截图已发送。"

    assert asyncio.run(run_command(service, PRIVATE, "/rci 截图 高一3班", screenshot_sender=sender)) == "截图已发送。"
    assert seen == ["高一3班"]
    assert asyncio.run(run_command(service, PRIVATE, "/rci 截图")) == "当前环境无法发送图片。"
