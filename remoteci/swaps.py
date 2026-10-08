"""老师换课申请：把 RemoteCI 个人通知主动推送到绑定账号的私聊会话，并提供审批指令。

服务端接口（Bearer 认证，API Key 或登录令牌都可）：
- GET  /api/me/notifications?after=<ISO>   个人通知（换课申请、审批结果、强制换课等）
- GET  /api/swap-requests?box=incoming|outgoing&status=
- POST /api/swap-requests/{id 或 8 位短编号}/approve|reject|cancel|revoke
发起申请需要逐节选课，留在 WebUI 和手机 App 完成；聊天里只做查看与处理。
"""

from __future__ import annotations

import time
from datetime import date
from typing import TYPE_CHECKING

from .client import RemoteCiError

if TYPE_CHECKING:  # pragma: no cover
    from .service import ChatUser, RemoteCiService


PERMISSION_REQUEST_SWAP = 4096
POLL_SECONDS = 60

STATUS_TEXT = {1: "待审批", 2: "已通过", 3: "已拒绝", 4: "已撤销", 5: "已过期", 6: "已强制换课", 7: "已撤回"}
WEEKDAYS = "一二三四五六日"

SWAP_SUBCOMMANDS = {
    "待办": "incoming", "待处理": "incoming", "待我处理": "incoming", "收到": "incoming", "todo": "incoming",
    "我的": "outgoing", "我发起的": "outgoing", "mine": "outgoing",
    "通过": "approve", "同意": "approve", "批准": "approve", "approve": "approve",
    "拒绝": "reject", "驳回": "reject", "reject": "reject",
    "撤回": "revoke", "revoke": "revoke",
    "撤销": "cancel", "取消": "cancel", "cancel": "cancel",
}

SWAP_HELP = """换课申请（发起请在 RemoteCI WebUI 或手机 App 的“换课”页）
/rci 换课 待办   发给我的换课申请
/rci 换课 我的   我发起的换课申请
/rci 换课 通过 <编号> [备注]
/rci 换课 拒绝 <编号> [备注]
/rci 换课 撤回 <编号>   撤回别人对我的强制换课
/rci 换课 撤销 <编号>   撤销我发起的待审批申请"""

# 通知类型 → 追加在主动消息末尾、可直接回复的指令提示。
NOTIFY_HINTS = {
    "swap_requested": "回复“/rci 换课 通过 {short}”同意，或“/rci 换课 拒绝 {short} 理由”拒绝。",
    "swap_forced": "如未事先沟通，可回复“/rci 换课 撤回 {short}”撤回。",
}


def short_id(value: str | None) -> str:
    return (value or "").replace("-", "")[:8]


def date_label(iso: str | None) -> str:
    try:
        value = date.fromisoformat(iso or "")
    except ValueError:
        return iso or ""
    return f"{value.month}月{value.day}日 周{WEEKDAYS[value.weekday()]}"


def slot_text(slot: dict | None) -> str:
    if not slot:
        return ""
    teacher = f"（{slot['teacher']}）" if slot.get("teacher") else ""
    return " ".join(x for x in (slot.get("className"), date_label(slot.get("date")), slot.get("label"),
                                f"{slot.get('subject') or ''}{teacher}") if x)


def format_request(item: dict) -> str:
    """一条申请的多行描述，编号用 8 位短编号便于回复。"""
    if item.get("mode") == 2 or not item.get("source"):
        what = f"{slot_text(item.get('target'))} → {item.get('subjectName') or ''}（{item.get('requesterName') or ''}）"
    else:
        what = f"{slot_text(item.get('source'))}\n  ⇄ {slot_text(item.get('target'))}"
    head = f"[{item.get('shortId') or short_id(item.get('id'))}] {item.get('requesterName') or ''}" \
           f"{' 强制换课' if item.get('forced') else ''} · {STATUS_TEXT.get(item.get('status'), '未知')}"
    lines = [head, f"  {what}", f"  理由：{item.get('reason') or ''}"]
    if item.get("decisionNote"):
        lines.append(f"  备注：{item['decisionNote']}")
    return "\n".join(lines)


def format_notification(note: dict) -> str:
    text = f"【RemoteCI 换课】{note.get('title') or ''}\n{note.get('body') or ''}".rstrip()
    hint = NOTIFY_HINTS.get(note.get("kind") or "")
    if hint and note.get("swapRequestId"):
        text += "\n" + hint.format(short=short_id(note["swapRequestId"]))
    return text


def can_request_swap(binding: dict) -> bool:
    permissions = (binding.get("profile") or {}).get("permissions")
    return isinstance(permissions, int) and permissions & PERMISSION_REQUEST_SWAP == PERMISSION_REQUEST_SWAP


class SwapInbox:
    """按绑定轮询个人通知并推送到绑定时的私聊会话；游标保存在 binding["swap_cursor"]。"""

    def __init__(self, service: "RemoteCiService"):
        self._service = service
        self._last_poll: dict[str, float] = {}

    async def poll(self, force: bool = False) -> bool:
        """轮询所有已绑定账号，返回是否需要保存状态。"""
        dirty = False
        for key, binding in list(self._service.store.bindings.items()):
            if binding.get("status") == "auth_failed" or not binding.get("umo") or not can_request_swap(binding):
                continue
            if not force and time.time() - self._last_poll.get(key, 0) < POLL_SECONDS:
                continue
            self._last_poll[key] = time.time()
            try:
                dirty |= await self._poll_binding(binding)
            except RemoteCiError as ex:
                if ex.status not in (401, 403, 404):
                    self._service.log.warning(f"RemoteCI 换课通知轮询失败 {key}：{ex}")
        return dirty

    async def _poll_binding(self, binding: dict) -> bool:
        cursor = binding.get("swap_cursor")
        if not cursor:
            # 第一次轮询只建立游标，避免把绑定前的历史通知一次性刷屏；未读的待办可用“/rci 换课 待办”查看。
            items = await self._service._call(binding, "GET", "/api/me/notifications", params={"limit": 1})
            binding["swap_cursor"] = (items[0].get("createdAt") if items else None) or _utc_now_iso()
            return True
        items = await self._service._call(binding, "GET", "/api/me/notifications", params={"after": cursor})
        if not items:
            return False
        for note in sorted(items, key=lambda x: x.get("createdAt") or ""):
            if not note.get("readAt"):
                await self._service._push(binding["umo"], "swap", format_notification(note))
            binding["swap_cursor"] = max(binding["swap_cursor"], note.get("createdAt") or "")
        return True


async def run_swap_command(service: "RemoteCiService", user: "ChatUser", args: list[str]) -> str:
    if not args:
        args = ["待办"]
    action = SWAP_SUBCOMMANDS.get(args[0].lower())
    if action is None:
        return SWAP_HELP
    binding = service.require_binding(user)
    try:
        if action in ("incoming", "outgoing"):
            return await list_requests(service, binding, action)
        if len(args) < 2:
            return f"用法：/rci 换课 {args[0]} <编号>" + (" [备注]" if action in ("approve", "reject") else "")
        return await decide(service, binding, action, args[1], " ".join(args[2:]))
    except RemoteCiError as ex:
        return f"操作失败：{ex}"


async def list_requests(service: "RemoteCiService", binding: dict, box: str, pending_only: bool = False) -> str:
    params = {"box": box}
    if pending_only:
        params["status"] = 1
    items = await service._call(binding, "GET", "/api/swap-requests", params=params) or []
    if box == "incoming":
        items = [x for x in items if x.get("canDecide") or x.get("canRevoke")] or items[:5]
    else:
        items = items[:10]
    if not items:
        return "没有待你处理的换课申请。" if box == "incoming" else "你还没有发起过换课申请。"
    title = "发给你的换课申请：" if box == "incoming" else "你发起的换课申请："
    return title + "\n" + "\n".join(format_request(x) for x in items)


async def decide(service: "RemoteCiService", binding: dict, action: str, request_id: str, note: str = "") -> str:
    body = {"note": note} if action in ("approve", "reject") and note else ({} if action in ("approve", "reject") else None)
    item = await service._call(binding, "POST", f"/api/swap-requests/{request_id.strip()}/{action}",
                               body=body if body is not None else {})
    done = {"approve": "已同意换课，课表已临时调整。", "reject": "已拒绝换课申请。",
            "revoke": "已撤回强制换课，课表已恢复。", "cancel": "已撤销换课申请。"}[action]
    return done + ("\n" + format_request(item) if isinstance(item, dict) else "")


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
