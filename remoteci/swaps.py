"""老师换课申请：把 RemoteCI 个人通知主动推送到绑定账号的私聊会话，并提供审批指令。

服务端接口（Bearer 认证，API Key 或登录令牌都可）：
- GET  /api/me/notifications?after=<ISO>   个人通知（换课申请、审批结果、强制换课、设备与自动化告警等）
- GET  /api/swap-requests?box=incoming|outgoing&status=
- POST /api/swap-requests/{id 或 8 位短编号}/approve|reject|cancel|revoke|revert
通过时遇到需要确认的冲突（409 SWAP_CONFLICTS）先把冲突告诉用户，用户确认后带 acknowledgeConflicts 再通过。
发起申请需要逐节选课，留在 WebUI 和手机 App 完成；聊天里只做查看与处理。
运维群（会话角色 ops_group）用独立游标转发所绑定管理员账号的 device_* 与 automation_* 通知。
"""

from __future__ import annotations

import time
from datetime import date
from typing import TYPE_CHECKING

from .client import RemoteCiError
from .store import ROLE_OPS_GROUP

if TYPE_CHECKING:  # pragma: no cover
    from .service import ChatUser, RemoteCiService


PERMISSION_REQUEST_SWAP = 4096
POLL_SECONDS = 60

STATUS_TEXT = {1: "待审批", 2: "已通过", 3: "已拒绝", 4: "已撤销", 5: "已过期", 6: "已强制换课", 7: "已撤回", 8: "已失效"}
ERROR_CONFLICTS = "SWAP_CONFLICTS"
WEEKDAYS = "一二三四五六日"

SWAP_SUBCOMMANDS = {
    "待办": "incoming", "待处理": "incoming", "待我处理": "incoming", "收到": "incoming", "todo": "incoming",
    "我的": "outgoing", "我发起的": "outgoing", "mine": "outgoing",
    "通过": "approve", "同意": "approve", "批准": "approve", "approve": "approve",
    "确认通过": "approve!", "仍然通过": "approve!", "仍然同意": "approve!",
    "拒绝": "reject", "驳回": "reject", "reject": "reject",
    "撤回": "revoke", "revoke": "revoke",
    "撤回换课": "revert", "revert": "revert",
    "撤销": "cancel", "取消": "cancel", "cancel": "cancel",
}

SWAP_HELP = """换课申请（发起请在 RemoteCI WebUI 或手机 App 的“换课”页）
/rci 换课 待办   发给我的换课申请
/rci 换课 我的   我发起的换课申请
/rci 换课 通过 <编号> [备注]
/rci 换课 确认通过 <编号> [备注]   已看过冲突提示，仍要通过
/rci 换课 拒绝 <编号> [备注]
/rci 换课 撤回 <编号>   撤回别人对我的强制换课
/rci 换课 撤回换课 <编号>   班主任或管理员撤回已生效的换课
/rci 换课 撤销 <编号>   撤销我发起的待审批申请
收到换课提醒后，也可以在私聊直接回复“同意”“拒绝 理由”或“撤回”。"""

# 通知类型 → 追加在主动消息末尾、可直接回复的指令提示；{short} 为换课申请短编号。
NOTIFY_HINTS = {
    "swap_requested": "直接回复“同意”或“拒绝 理由”即可处理（编号 {short}），也可发送“/rci 换课 通过 {short}”。",
    "swap_forced": "如未事先沟通，直接回复“撤回”即可恢复课表（编号 {short}），也可发送“/rci 换课 撤回 {short}”。",
    "swap_invalidated": "如仍需换课，请在 RemoteCI WebUI 或手机 App 重新申请。",
    "device_offline": "发送“/rci 设备”查看全部教室设备状态。",
    "device_online": "发送“/rci 设备”查看全部教室设备状态。",
    "automation_failed": "发送“/rci 自动化”查看规则，或“/rci 自动化 运行 <规则名>”重新执行。",
    "app_guard": "发送“/rci 守护”查看应用守护策略与最近事件。",
}

# 通知类型 → 消息标题里的分类；未列出的按前缀归类，其余都算换课。
NOTIFY_CATEGORIES = {"device_offline": "设备离线", "device_online": "设备恢复", "automation_failed": "自动化告警",
                     "app_guard": "应用守护"}
CATEGORY_PREFIXES = (("personal_course_", "课程提醒"), ("device_", "设备"), ("automation_", "自动化"))
# 运维群只转发这些前缀的通知。
OPS_PREFIXES = ("device_", "automation_", "app_guard")


def is_ops_kind(kind: str | None) -> bool:
    return str(kind or "").startswith(OPS_PREFIXES)


def log_kind(kind: str | None) -> str:
    """推送记录里的分类：设备、自动化告警单独标出，其余沿用“swap”。"""
    kind = str(kind or "")
    if kind.startswith(("device_", "app_guard")):
        return "device"
    if kind.startswith("automation_"):
        return "automation"
    return "swap"


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
    if item.get("delivery") == "pending":
        lines.append("  教室电脑暂未确认，上线后自动生效")
    if item.get("status") == 8 and item.get("invalidReason"):
        lines.append(f"  失效原因：{item['invalidReason']}")
    return "\n".join(lines)


def conflict_text(ex: RemoteCiError) -> str | None:
    """换课需要确认的冲突（409 SWAP_CONFLICTS）：返回列出冲突的说明；其他错误返回 None。"""
    if ex.code != ERROR_CONFLICTS:
        return None
    lines = [f"· {x.get('message')}" for x in ex.data.get("conflicts") or []
             if isinstance(x, dict) and x.get("requiresAcknowledgement") and x.get("message")]
    return "\n".join(lines) or ex.message


def notification_category(kind: str | None) -> str:
    kind = str(kind or "")
    if kind in NOTIFY_CATEGORIES:
        return NOTIFY_CATEGORIES[kind]
    return next((name for prefix, name in CATEGORY_PREFIXES if kind.startswith(prefix)), "换课")


def format_notification(note: dict) -> str:
    kind = note.get("kind") or ""
    text = f"【RemoteCI {notification_category(kind)}】{note.get('title') or ''}\n{note.get('body') or ''}".rstrip()
    hint = NOTIFY_HINTS.get(kind)
    if hint and ("{short}" not in hint or note.get("swapRequestId")):
        text += "\n" + hint.format(short=short_id(note.get("swapRequestId")))
    return text


def can_request_swap(binding: dict) -> bool:
    permissions = (binding.get("profile") or {}).get("permissions")
    return isinstance(permissions, int) and permissions & PERMISSION_REQUEST_SWAP == PERMISSION_REQUEST_SWAP


class SwapInbox:
    """按绑定轮询个人通知并推送到绑定时的私聊会话；游标保存在 binding["swap_cursor"]。

    运维群（会话角色 ops_group）另按会话轮询所绑定管理员账号的通知，只转发设备与自动化告警，
    游标保存在 session["ops_cursor"]，与私聊的 swap_cursor 互不影响。
    """

    def __init__(self, service: "RemoteCiService"):
        self._service = service
        self._last_poll: dict[str, float] = {}

    async def poll(self, force: bool = False) -> bool:
        """轮询所有已绑定账号和运维群，返回是否需要保存状态。"""
        dirty = False
        for key, binding in list(self._service.store.bindings.items()):
            if binding.get("status") == "auth_failed" or not binding.get("umo"):
                continue
            if not self._due(key, force):
                continue
            try:
                dirty |= await self._poll_binding(binding)
            except RemoteCiError as ex:
                if ex.status not in (401, 403, 404):
                    self._service.log.warning(f"RemoteCI 换课通知轮询失败 {key}：{ex}")
        for umo, session in list(self._service.store.sessions.items()):
            if session.get("kind") != "group" or session.get("role") != ROLE_OPS_GROUP:
                continue
            binding = self._service.store.binding_for(session.get("binding_key") or "")
            if not binding or binding.get("status") == "auth_failed" or not self._due(f"ops:{umo}", force):
                continue
            try:
                dirty |= await self._poll_ops(umo, session, binding)
            except RemoteCiError as ex:
                if ex.status not in (401, 403, 404):
                    self._service.log.warning(f"RemoteCI 运维群通知轮询失败 {umo}：{ex}")
        return dirty

    def _due(self, key: str, force: bool) -> bool:
        if not force and time.time() - self._last_poll.get(key, 0) < POLL_SECONDS:
            return False
        self._last_poll[key] = time.time()
        return True

    async def _baseline(self, binding: dict) -> str:
        """第一次轮询只建立游标，避免把绑定前的历史通知一次性刷屏；未读的待办可用“/rci 换课 待办”查看。"""
        items = await self._service._call(binding, "GET", "/api/me/notifications", params={"limit": 1})
        return (items[0].get("createdAt") if items else None) or _utc_now_iso()

    async def _poll_binding(self, binding: dict) -> bool:
        from .quick_reply import remember_pending

        cursor = binding.get("swap_cursor")
        if not cursor:
            binding["swap_cursor"] = await self._baseline(binding)
            return True
        items = await self._service._call(binding, "GET", "/api/me/notifications", params={"after": cursor})
        if not items:
            return False
        for note in sorted(items, key=lambda x: x.get("createdAt") or ""):
            if not note.get("readAt"):
                if not await self._service.push_note(binding["umo"], binding, note, format_notification(note)):
                    return True  # 保留游标，下次轮询重试，避免丢失课程提醒。
                remember_pending(binding, note)  # 换课申请、强制换课可直接回复“同意 / 拒绝 / 撤回”
            binding["swap_cursor"] = max(binding["swap_cursor"], note.get("createdAt") or "")
        return True

    async def _poll_ops(self, umo: str, session: dict, binding: dict) -> bool:
        cursor = session.get("ops_cursor")
        if not cursor:
            session["ops_cursor"] = await self._baseline(binding)
            return True
        items = await self._service._call(binding, "GET", "/api/me/notifications", params={"after": cursor})
        if not items:
            return False
        for note in sorted(items, key=lambda x: x.get("createdAt") or ""):
            # 群里的其他人也需要知道设备离线，所以不看管理员本人是否已读。
            if is_ops_kind(note.get("kind")):
                if not await self._service.push_note(umo, binding, note, format_notification(note)):
                    return True
            session["ops_cursor"] = max(session["ops_cursor"], note.get("createdAt") or "")
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
            return f"用法：/rci 换课 {args[0]} <编号>" + (" [备注]" if action in ("approve", "approve!", "reject") else "")
        acknowledge = action == "approve!"
        return await decide(service, binding, "approve" if acknowledge else action, args[1], " ".join(args[2:]), acknowledge)
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
    if box == "incoming":
        await _mark_forced_seen(service, binding, items)
    title = "发给你的换课申请：" if box == "incoming" else "你发起的换课申请："
    return title + "\n" + "\n".join(format_request(x) for x in items)


async def _mark_forced_seen(service: "RemoteCiService", binding: dict, items: list) -> None:
    """强制换课看过即处理：在聊天里列出后上报服务端，不再计入“待我处理”（仍可撤回）。"""
    ids = [x["id"] for x in items if x.get("canRevoke") and x.get("needsAction") and x.get("id")]
    if not ids:
        return
    try:
        await service._call(binding, "POST", "/api/swap-requests/seen", body={"ids": ids})
    except RemoteCiError:
        pass  # 旧版服务端没有该接口，不影响列表展示


async def decide(service: "RemoteCiService", binding: dict, action: str, request_id: str, note: str = "",
                 acknowledge_conflicts: bool = False) -> str:
    body: dict = {}
    if action in ("approve", "reject") and note:
        body["note"] = note
    if action == "approve" and acknowledge_conflicts:
        body["acknowledgeConflicts"] = True
    try:
        item = await service._call(binding, "POST", f"/api/swap-requests/{request_id.strip()}/{action}", body=body)
    except RemoteCiError as ex:
        conflicts = conflict_text(ex) if action == "approve" else None
        if conflicts is None:
            raise
        rid = request_id.strip()
        return f"这次换课存在需要确认的冲突：\n{conflicts}\n确认仍要通过，请发送“/rci 换课 确认通过 {rid}”。"
    done = {"approve": "已同意换课，课表已临时调整。", "reject": "已拒绝换课申请。",
            "revoke": "已撤回强制换课，课表已恢复。", "revert": "已撤回换课，课表已恢复。",
            "cancel": "已撤销换课申请。"}[action]
    return done + ("\n" + format_request(item) if isinstance(item, dict) else "")


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
