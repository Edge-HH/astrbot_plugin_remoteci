"""快捷回复：收到换课提醒后，私聊直接回“同意 / 拒绝 理由 / 撤回”即可处理，不必输入 /rci 指令。

所有平台通用（钉钉卡片上的快捷按钮也是发出同样的文字）。为避免误伤普通聊天，只在同时满足以下条件时拦截：
- 私聊，消息不超过 40 字、不是问句，以“同意|通过|批准”“拒绝”“撤回”开头，后面紧跟空格、标点或直接结束；
- 该账号 24 小时内推送过、且服务端确认仍可处理的换课申请（swap_requested）或强制换课（swap_forced）。
推送时把待处理项记在 binding["pending_actions"]，处理成功或服务端已不可处理时移除。
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from .client import RemoteCiError
from .swaps import run_swap_command, short_id

if TYPE_CHECKING:  # pragma: no cover
    from .service import ChatUser, RemoteCiService

MAX_LENGTH = 40
PENDING_TTL = 24 * 3600
PENDING_LIMIT = 20

# 快捷回复的动词 → 换课操作；这些动词同时是 run_swap_command 认识的子命令。
VERBS = {"同意": "approve", "通过": "approve", "批准": "approve", "拒绝": "reject", "撤回": "revoke"}
# 通知类型 → 可以对它执行的快捷操作。
KIND_ACTIONS = {"swap_requested": ("approve", "reject"), "swap_forced": ("revoke",)}

_PATTERN = re.compile(r"^(同意|通过|批准|拒绝|撤回)(?:[\s,，:：、]+(.*))?$", re.S)
_REQUEST_ID = re.compile(r"^(?:[0-9a-f]{8}|[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$", re.I)
_TRAILING = "。.!！~～ "
_ID_BRACKETS = "[]【】()（）#＃"


def parse_quick_reply(text: str) -> tuple[str, str, str | None, str] | None:
    """“同意 a1b2c3d4 好的” → ("同意", "approve", "a1b2c3d4", "好的")；不像快捷回复时返回 None。"""
    text = (text or "").strip().rstrip(_TRAILING)
    if not text or len(text) > MAX_LENGTH or "?" in text or "？" in text:
        return None
    match = _PATTERN.match(text)
    if not match:
        return None
    verb, rest = match.group(1), (match.group(2) or "").strip()
    request_id = None
    if rest:
        parts = rest.split(None, 1)
        token = parts[0].strip(_ID_BRACKETS)
        if _REQUEST_ID.match(token):
            request_id, rest = token, (parts[1] if len(parts) > 1 else "")
    return verb, VERBS[verb], request_id, rest.strip()


def _norm(value: str | None) -> str:
    return (value or "").replace("-", "").lower()


def active_pending(binding: dict, now: float | None = None) -> list[dict]:
    """24 小时内推送过的待处理项；顺带清掉过期的。"""
    now = time.time() if now is None else now
    items = [p for p in binding.get("pending_actions") or [] if now - (p.get("at") or 0) < PENDING_TTL]
    if items != (binding.get("pending_actions") or []):
        binding["pending_actions"] = items
    return items


def remember_pending(binding: dict, note: dict, now: float | None = None) -> bool:
    """推送换课申请或强制换课后记下待处理项，返回是否记录。"""
    kind, request_id = note.get("kind"), note.get("swapRequestId")
    if kind not in KIND_ACTIONS or not request_id:
        return False
    now = time.time() if now is None else now
    items = [p for p in active_pending(binding, now) if _norm(p.get("id")) != _norm(request_id)]
    items.append({"id": request_id, "short": short_id(request_id), "kind": kind,
                  "title": (note.get("title") or "")[:60], "at": now})
    binding["pending_actions"] = items[-PENDING_LIMIT:]
    return True


def forget_pending(binding: dict, request_id: str) -> None:
    key = _norm(request_id)
    binding["pending_actions"] = [p for p in binding.get("pending_actions") or []
                                  if _norm(p.get("id")) != key and p.get("short") != key]


async def _still_pending(service: "RemoteCiService", binding: dict, items: list[dict]) -> list[dict]:
    """向服务端确认哪些仍可处理；已在 WebUI、手机端处理或已过期的从记录中移除。网络失败时不做过滤。"""
    try:
        incoming = await service._call(binding, "GET", "/api/swap-requests", params={"box": "incoming"}) or []
    except RemoteCiError:
        return items
    live = {_norm(x.get("id")) for x in incoming if x.get("canDecide") or x.get("canRevoke")}
    binding["pending_actions"] = [p for p in binding.get("pending_actions") or [] if _norm(p.get("id")) in live]
    return [p for p in items if _norm(p.get("id")) in live]


def _choose_prompt(verb: str, action: str, items: list[dict]) -> str:
    what = "强制换课" if action == "revoke" else "换课申请"
    example = f"{verb} 编号" + (" 理由" if action == "reject" else "")
    return (f"你有 {len(items)} 条待处理的{what}，请回复“{example}”：\n"
            + "\n".join(f"[{p.get('short')}] {p.get('title') or ''}" for p in items))


async def handle_quick_reply(service: "RemoteCiService", user: "ChatUser", text: str) -> str | None:
    """处理一条可能是快捷回复的私聊消息；返回回复文字，不是快捷回复时返回 None（交给后续的 LLM 等处理）。"""
    if not user.is_private:
        return None
    parsed = parse_quick_reply(text)
    if parsed is None:
        return None
    binding = service.store.binding_for(user.user_key)
    if not binding or binding.get("status") == "auth_failed" or not binding.get("pending_actions"):
        return None
    verb, action, request_id, note = parsed
    before = list(binding.get("pending_actions") or [])
    candidates = [p for p in active_pending(binding) if action in KIND_ACTIONS.get(p.get("kind"), ())]
    if candidates:
        candidates = await _still_pending(service, binding, candidates)
    if not candidates:
        if binding.get("pending_actions") != before:
            await service.store.save()
        return None

    target = candidates[-1]["short"] if len(candidates) == 1 else None
    if request_id:
        key = _norm(request_id)
        matched = next((p for p in candidates if _norm(p.get("id")) == key or p.get("short") == key), None)
        target = matched["short"] if matched else request_id  # 明确写了编号就照办，由服务端校验
    if target is None:
        await service.store.save()
        return _choose_prompt(verb, action, candidates)

    reply = await run_swap_command(service, user, [verb, target] + ([note] if note else []))
    if not reply.startswith("操作失败"):
        forget_pending(binding, target)
    await service.store.save()
    return reply
