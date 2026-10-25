"""屏幕截图：让班级电脑截取当前屏幕并上传到 RemoteCI 服务端，再把图片发回聊天。

服务端接口（需要该班的“屏幕截图”权限，班主任默认拥有）：
- POST /api/classes/{classId}/screenshot   等插件截屏并上传完成（最长约 45 秒），返回截图信息与 url
- GET  /api/screenshots/{id}               PNG 图片
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from .client import RemoteCiError
from .service import ServiceError

if TYPE_CHECKING:  # pragma: no cover
    from .service import ChatUser, RemoteCiService

NO_PERMISSION = "没有这个班级的屏幕截图权限：请系统管理员在 RemoteCI 的角色配置或账号权限中为你开启“屏幕截图”。"
CAPTURE_TIMEOUT = 60
FAILURES = {
    503: "班级电脑不在线，无法截图。",
    409: "班级电脑上的 RemoteCI 插件版本过旧，请升级后再截图。",
    504: "等待班级电脑截图超时，请稍后再试。",
}


def caption(cls_name: str, shot: dict) -> str:
    try:
        when = datetime.fromisoformat(str(shot.get("capturedAt"))).astimezone().strftime("%H:%M:%S")
    except ValueError:
        when = ""
    size = f"{shot.get('width')}×{shot.get('height')}" if shot.get("width") else ""
    parts = [p for p in (when, shot.get("deviceName") or "", size) if p]
    return f"【{cls_name}】屏幕截图" + (f"（{' · '.join(parts)}）" if parts else "")


async def capture(service: RemoteCiService, user: ChatUser, class_ref: str | None) -> tuple[str, bytes]:
    """返回 (说明文字, PNG 字节)；失败时抛出 ServiceError，消息可直接回复给用户。"""
    binding = service.require_binding(user)
    cls = service.resolve_class(binding, class_ref)
    if cls is None:
        classes = (binding.get("profile") or {}).get("classes") or []
        raise ServiceError("请指定要截图的班级。你可访问的班级：" + ("、".join(c.get("name", "") for c in classes) or "无"))
    try:
        shot = await service._call(binding, "POST", f"/api/classes/{cls['id']}/screenshot", body={},
                                   timeout=CAPTURE_TIMEOUT)
        png = await service._call(binding, "GET", shot["url"], raw=True)
    except RemoteCiError as ex:
        if ex.status == 403:
            raise ServiceError(NO_PERMISSION) from ex
        if ex.status in FAILURES:
            raise ServiceError(FAILURES[ex.status]) from ex
        if ex.status == 422:
            raise ServiceError(f"截图失败：{ex.message}") from ex
        raise
    if not isinstance(png, (bytes, bytearray)) or not bytes(png).startswith(b"\x89PNG"):
        raise ServiceError("截图下载失败，请稍后再试。")
    return caption(cls.get("name") or "班级", shot), bytes(png)
