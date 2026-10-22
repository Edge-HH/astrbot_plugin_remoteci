"""文件分发：把聊天里收到的文件经 RemoteCI 服务端中转，发到班级电脑的桌面、下载或文档文件夹。

服务端接口（需要“文件分发”权限，系统管理员默认拥有；按目标班级逐个复核）：
- POST /api/files?name=<文件名>        请求体就是文件内容，返回传输编号、大小与 SHA-256
- POST /api/files/{id}/send            {classNames, nextClass, targetFolder, overwrite}，返回逐台接收状态
- GET  /api/files/{id}                 逐台进度：state 0 等待、1 接收中、2 已送达、3 失败

设备在后台断点续传下载并校验完整性；聊天里先等一小会儿报告进度，没收完的在后台继续跟踪，完成后再通知。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .client import RemoteCiError
from .service import ServiceError

if TYPE_CHECKING:  # pragma: no cover
    from .service import ChatUser, RemoteCiService

FOLDERS = {"桌面": 1, "desktop": 1, "下载": 2, "downloads": 2, "download": 2, "文档": 3, "documents": 3, "我的文档": 3}
FOLDER_NAMES = {1: "桌面", 2: "下载", 3: "文档"}
PENDING, DOWNLOADING, COMPLETED, FAILED = 0, 1, 2, 3
NEXT_CLASS_WORDS = {"下节课", "下一节课", "下一节", "下节", "下个班", "下一个班", "next"}
NO_PERMISSION = "没有文件分发权限：请系统管理员在 RemoteCI 的角色配置或账号权限中为你开启“文件分发”。"
WAIT_SECONDS = 45
FOLLOW_UP_SECONDS = 30 * 60
_follow_ups: set[asyncio.Task] = set()


def format_size(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def parse_folder(text: str | None) -> int:
    key = (text or "桌面").strip().lower()
    if key not in FOLDERS:
        raise ServiceError("保存位置只能是桌面、下载或文档。")
    return FOLDERS[key]


def split_targets(classes: str | list[str] | None) -> tuple[list[str], bool]:
    """拆出班级名与“下节课”关键词；班级名可用空格、逗号或顿号分隔。"""
    if isinstance(classes, str):
        for sep in ("，", ",", "、", "；", ";"):
            classes = classes.replace(sep, " ")
        classes = classes.split()
    names, next_class = [], False
    for item in classes or []:
        item = item.strip()
        if item in NEXT_CLASS_WORDS:
            next_class = True
        elif item:
            names.append(item)
    return names, next_class


def parse_command_args(args: list[str]) -> tuple[list[str], str, bool]:
    """“/rci 发文件 三班 五班 下载 覆盖” → (["三班", "五班"], "下载", True)。"""
    targets, folder, overwrite = [], "桌面", False
    for arg in args:
        if arg.lower() in FOLDERS:
            folder = arg
        elif arg in ("覆盖", "覆盖同名", "overwrite"):
            overwrite = True
        else:
            targets.append(arg)
    return targets, folder, overwrite


def finished(view: dict) -> bool:
    deliveries = view.get("deliveries") or []
    return bool(deliveries) and all(d.get("state") in (COMPLETED, FAILED) for d in deliveries)


def format_view(view: dict) -> str:
    lines = []
    for d in view.get("deliveries") or []:
        state, name = d.get("state"), d.get("targetName") or "设备"
        if state == COMPLETED:
            lines.append(f"✅ {name}：已保存到 {d.get('savedPath') or FOLDER_NAMES.get(1)}")
        elif state == FAILED:
            lines.append(f"❌ {name}：{d.get('message') or '失败'}")
        elif state == DOWNLOADING:
            note = d.get("message") or ""
            note = "" if note.startswith("开始接收") else f"，{note}"
            lines.append(f"⏳ {name}：接收中 {d.get('percent', 0)}%"
                         f"（{format_size(int(d.get('receivedBytes') or 0))} / {format_size(int(d.get('totalBytes') or 0))}{note}）")
        else:
            lines.append(f"⏳ {name}：等待设备开始接收")
    return "\n".join(lines) or "没有可发送的设备。"


def summary(view: dict, folder: int) -> str:
    return f"📁 {view.get('fileName')}（{format_size(int(view.get('size') or 0))}）→ {FOLDER_NAMES.get(folder, '桌面')}\n" \
           + format_view(view)


async def _request(service: RemoteCiService, binding: dict, method: str, path: str, **kwargs) -> Any:
    try:
        return await service.client.request(binding, method, path, on_auth_changed=service._save_auth, **kwargs)
    except RemoteCiError as ex:
        if ex.status == 401 and binding.get("key") in service.store.bindings:
            await service._mark_auth_failed(binding, str(ex))
        if ex.status == 403:
            raise ServiceError(ex.message if "权限" in ex.message and ex.message != "权限不足" else NO_PERMISSION) from ex
        if ex.status in (404, 405) and path == "/api/files":
            raise ServiceError("RemoteCI 服务器暂不支持文件中转分发，请先升级服务端。") from ex
        if ex.status in (400, 404, 413):
            raise ServiceError(ex.message) from ex
        raise


async def upload(service: RemoteCiService, binding: dict, path: Path, name: str) -> dict:
    size = path.stat().st_size
    if size <= 0:
        raise ServiceError("文件是空的。")
    # 按最低约 256 KB/s 估算上传时间，至少 2 分钟。
    timeout = max(120.0, size / (256 * 1024))
    with path.open("rb") as stream:
        return await _request(service, binding, "POST", "/api/files", params={"name": name}, content=stream,
                              headers={"Content-Type": "application/octet-stream"}, timeout=timeout)


async def send_file(service: RemoteCiService, user: ChatUser, path: Path, name: str, classes: str | list[str] | None,
                    next_class: bool = False, folder: str = "桌面", overwrite: bool = False,
                    wait_seconds: float = WAIT_SECONDS, poll_interval: float = 2.0) -> str:
    binding = service.require_binding(user)
    names, keyword_next = split_targets(classes)
    next_class = next_class or keyword_next
    if not names and not next_class:
        accessible = (binding.get("profile") or {}).get("classes") or []
        if len(accessible) != 1:
            raise ServiceError("要发到哪个班？请说出班级名，或说“下节课的班”。")
        names = [accessible[0].get("name")]
    target_folder = parse_folder(folder)

    uploaded = await upload(service, binding, path, name)
    body = {"classNames": names, "nextClass": next_class, "targetFolder": target_folder, "overwrite": bool(overwrite),
            **service._tz_params()}
    view = await _request(service, binding, "POST", f"/api/files/{uploaded['id']}/send", body=body)

    deadline = time.monotonic() + wait_seconds
    while not finished(view) and time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        view = await _request(service, binding, "GET", f"/api/files/{uploaded['id']}")
    text = summary(view, target_folder)
    if not finished(view) and binding.get("umo"):
        task = asyncio.create_task(_follow_up(service, binding, binding["umo"], uploaded["id"], target_folder))
        _follow_ups.add(task)
        task.add_done_callback(_follow_ups.discard)
        text += "\n其余设备仍在后台接收，完成后会再通知你。"
    return text


async def _follow_up(service: RemoteCiService, binding: dict, umo: str, transfer_id: str, folder: int,
                     poll_interval: float = 5.0, limit: float = FOLLOW_UP_SECONDS) -> None:
    deadline = time.monotonic() + limit
    view: dict | None = None
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            view = await _request(service, binding, "GET", f"/api/files/{transfer_id}")
        except (RemoteCiError, ServiceError):
            continue
        if finished(view):
            break
    if view:
        prefix = "【RemoteCI】文件分发完成" if finished(view) else "【RemoteCI】文件分发 30 分钟内未全部完成"
        await service._push(umo, "file", f"{prefix}\n{summary(view, folder)}")
