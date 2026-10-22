import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from remoteci import files
from remoteci.client import RemoteCiError
from remoteci.commands import run_command
from remoteci.service import ChatUser, RemoteCiService, ServiceError

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=TZ)
PRIVATE = ChatUser("aiocqhttp:1", "aiocqhttp", "1", "张老师", "aiocqhttp:FriendMessage:1", True)


def view(*states, size=2_000_000):
    deliveries = []
    for name, state, received in states:
        deliveries.append({"targetName": name, "state": state, "receivedBytes": received, "totalBytes": size,
                           "percent": received * 100 // size,
                           "savedPath": rf"C:\Users\T\Desktop\课件.pptx" if state == files.COMPLETED else None,
                           "message": "插件未在线，操作未执行" if state == files.FAILED else None})
    return {"id": "f" * 32, "fileName": "课件.pptx", "size": size, "deliveries": deliveries}


class FakeClient:
    def __init__(self, polls, forbidden=False):
        self.calls = []
        self.polls = list(polls)
        self.forbidden = forbidden
        self.uploaded = b""

    async def request(self, binding, method, path, *, params=None, body=None, content=None, headers=None,
                      timeout=None, on_auth_changed=None, **_):
        self.calls.append((method, path, params, body))
        if path == "/api/files":
            if self.forbidden:
                raise RemoteCiError(403, "没有文件分发权限")
            self.uploaded = content.read()
            return {"id": "f" * 32, "fileName": params["name"], "size": len(self.uploaded)}
        if path.endswith("/send"):
            return self.polls.pop(0)
        if path.startswith("/api/files/"):
            return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        raise AssertionError(path)


def make(tmp_path, client, classes=None):
    pushed = []

    async def send(umo, text):
        pushed.append((umo, text))
        return True

    service = RemoteCiService(tmp_path, send=send, config={}, now=lambda: NOW)
    service.client = client
    binding = {"key": PRIVATE.user_key, "server_url": "https://rci.example", "auth": {"type": "api_key"},
               "umo": PRIVATE.umo,
               "profile": {"displayName": "张老师", "classes": classes if classes is not None else
                           [{"id": "c1", "name": "高一3班"}, {"id": "c2", "name": "高一5班"}]}}
    service.store.bindings[PRIVATE.user_key] = binding
    return service, pushed


def write(tmp_path, data=b"x" * 2048, name="课件.pptx"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_split_targets_and_args():
    assert files.split_targets("高一3班、高一5班 下节课") == (["高一3班", "高一5班"], True)
    assert files.parse_command_args(["三班", "下载", "覆盖"]) == (["三班"], "下载", True)
    with pytest.raises(ServiceError):
        files.parse_folder("回收站")


def test_send_to_next_class_reports_progress(tmp_path):
    client = FakeClient([view(("高一3班", files.DOWNLOADING, 500_000)),
                         view(("高一3班", files.COMPLETED, 2_000_000))])
    service, _ = make(tmp_path, client)
    text = asyncio.run(files.send_file(service, PRIVATE, write(tmp_path), "课件.pptx", "", next_class=True,
                                       folder="桌面", poll_interval=0))
    assert client.uploaded == b"x" * 2048
    send = next(c for c in client.calls if c[1].endswith("/send"))
    assert send[3]["nextClass"] is True and send[3]["classNames"] == [] and send[3]["targetFolder"] == 1
    assert "timeZoneOffsetMinutes" in send[3]
    assert "📁 课件.pptx（1.9 MB）→ 桌面" in text
    assert "✅ 高一3班：已保存到" in text


def test_unfinished_transfer_follows_up(tmp_path, monkeypatch):
    pending = view(("高一3班", files.DOWNLOADING, 1_000_000), ("高一5班", files.FAILED, 0))
    done = view(("高一3班", files.COMPLETED, 2_000_000), ("高一5班", files.FAILED, 0))
    client = FakeClient([pending, pending, done])
    service, pushed = make(tmp_path, client)

    async def run():
        orig = files._follow_up

        async def fast(*args, **kwargs):
            await orig(*args, poll_interval=0, **kwargs)
        monkeypatch.setattr(files, "_follow_up", fast)
        text = await files.send_file(service, PRIVATE, write(tmp_path), "课件.pptx", "高一3班 高一5班",
                                     wait_seconds=0, poll_interval=0)
        await asyncio.gather(*files._follow_ups)
        return text

    text = asyncio.run(run())
    assert "⏳ 高一3班：接收中 50%" in text
    assert "❌ 高一5班：插件未在线" in text
    assert "完成后会再通知你" in text
    assert pushed and "文件分发完成" in pushed[-1][1] and "✅ 高一3班" in pushed[-1][1]


def test_requires_target_when_multiple_classes(tmp_path):
    service, _ = make(tmp_path, FakeClient([]))
    with pytest.raises(ServiceError, match="哪个班"):
        asyncio.run(files.send_file(service, PRIVATE, write(tmp_path), "a.txt", ""))


def test_forbidden_upload_explains_permission(tmp_path):
    service, _ = make(tmp_path, FakeClient([], forbidden=True))
    with pytest.raises(ServiceError, match="文件分发权限"):
        asyncio.run(files.send_file(service, PRIVATE, write(tmp_path), "a.txt", "高一3班"))


def test_command_uses_file_sender(tmp_path):
    service, _ = make(tmp_path, FakeClient([]))
    seen = []

    async def sender(targets, folder, overwrite):
        seen.append((targets, folder, overwrite))
        return "ok"

    assert asyncio.run(run_command(service, PRIVATE, "/rci 发文件 高一3班 文档", file_sender=sender)) == "ok"
    assert seen == [(["高一3班"], "文档", False)]
