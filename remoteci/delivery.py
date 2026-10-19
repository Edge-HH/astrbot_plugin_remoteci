"""按会话所在平台推送个人通知：飞书、钉钉、企业微信发原生卡片，其他平台由调用方发文本。

由 main.py 注入 AstrBot 的 Context；本模块只在用到时才导入 AstrBot / lark_oapi，单元测试可直接用假对象。
适配器按特性探测（AstrBot ≥ 4.26 的 context.get_platform_inst，或 context.platform_manager.platform_insts），
任何一步失败——找不到适配器、签名链接接口未实现（404）、平台返回错误——都返回 False，由调用方回退为文本推送。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from . import cards
from .client import RemoteCiError

if TYPE_CHECKING:  # pragma: no cover
    from .service import RemoteCiService

DINGTALK_OTO_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
DINGTALK_GROUP_URL = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
GROUP_MESSAGE = "GroupMessage"


@dataclass
class Target:
    """unified_msg_origin 拆开后的三段：平台实例 ID、消息类型、会话 ID。"""
    umo: str
    platform_id: str
    message_type: str
    session_id: str

    @property
    def is_group(self) -> bool:
        return self.message_type == GROUP_MESSAGE

    def __str__(self) -> str:  # 钉钉适配器按 str(session) 查 staff_id 映射
        return self.umo


def parse_umo(umo: str) -> Target | None:
    parts = (umo or "").split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return Target(umo, *parts)


def platform_name(adapter: Any) -> str:
    try:
        return str(getattr(adapter.meta(), "name", "") or "")
    except Exception:  # noqa: BLE001 - 第三方适配器的 meta() 可能抛错
        return ""


class NoteDelivery:
    """service.note_sender 的实现：`await delivery(umo, binding, note, text)` 返回是否已用卡片送达。"""

    def __init__(self, service: "RemoteCiService", context: Any):
        self._service = service
        self._context = context

    async def __call__(self, umo: str, binding: dict, note: dict, text: str) -> bool:
        kind = note.get("kind") or ""
        target = parse_umo(umo)
        if kind not in cards.CARD_KINDS or target is None:
            return False
        adapter = self.find_adapter(target.platform_id)
        platform = platform_name(adapter)
        sender = {"lark": self._send_lark, "dingtalk": self._send_dingtalk, "wecom": self._send_wecom}.get(platform)
        if sender is None or not self._service.cards_enabled(platform):
            return False
        links = await self._action_links(binding, note)
        if links is None:
            return False
        card = cards.build_card(note, binding.get("server_url") or "", links)
        if card is None:
            return False
        try:
            await sender(adapter, target, card)
        except Exception as ex:  # noqa: BLE001 - 平台 SDK 抛出的异常种类很多，一律回退文本
            self._service.log.warning(f"RemoteCI 卡片发送失败（{platform}），改发文本：{ex}")
            return False
        return True

    # ---------- 适配器与签名链接 ----------

    def find_adapter(self, platform_id: str) -> Any:
        getter = getattr(self._context, "get_platform_inst", None)
        if callable(getter):
            try:
                adapter = getter(platform_id)
                if adapter is not None:
                    return adapter
            except Exception:  # noqa: BLE001
                pass
        manager = getattr(self._context, "platform_manager", None)
        adapters = getattr(manager, "platform_insts", None)
        if adapters is None and callable(getattr(manager, "get_insts", None)):
            adapters = manager.get_insts()
        adapters = list(adapters or [])
        for adapter in adapters:
            try:
                if getattr(adapter.meta(), "id", None) == platform_id:
                    return adapter
            except Exception:  # noqa: BLE001
                continue
        # 旧版本的 umo 首段是平台类型而不是实例 ID。
        return next((a for a in adapters if platform_name(a) == platform_id), None)

    async def _action_links(self, binding: dict, note: dict) -> dict[str, str] | None:
        """换课类通知申请一次性签名链接；接口未实现（404）或任何失败时返回 None，整条通知改发文本。"""
        actions = cards.ACTION_LINK_KINDS.get(note.get("kind") or "", ())
        if not actions:
            return {}
        target_id = note.get("swapRequestId")
        if not target_id:
            return None
        links = {}
        for action in actions:
            try:
                data = await self._service._call(binding, "POST", "/api/me/action-links",
                                                 body={"action": action, "targetId": target_id})
            except RemoteCiError as ex:
                if ex.status != 404:
                    self._service.log.warning(f"RemoteCI 签名链接申请失败，改发文本：{ex}")
                return None
            url = data.get("url") if isinstance(data, dict) else None
            if not url:
                return None
            links[action] = cards.site_url(binding.get("server_url") or "", str(url))
        return links

    # ---------- 飞书 ----------

    async def _send_lark(self, adapter: Any, target: Target, card: cards.Card) -> None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        api = getattr(adapter, "lark_api", None)
        if api is None or getattr(api, "im", None) is None:
            raise RuntimeError("飞书适配器没有可用的 lark_api")
        if target.is_group:
            # 与 AstrBot 飞书适配器一致：群会话 ID 可能带 “用户%群” 前缀。
            id_type = "chat_id"
            receive_id = target.session_id.split("%")[1] if "%" in target.session_id else target.session_id
        else:
            id_type, receive_id = "open_id", target.session_id
        request = (CreateMessageRequest.builder().receive_id_type(id_type).request_body(
            CreateMessageRequestBody.builder().receive_id(receive_id).msg_type("interactive")
            .content(json.dumps(cards.lark_card(card), ensure_ascii=False)).uuid(str(uuid.uuid4())).build()).build())
        response = await api.im.v1.message.acreate(request)
        if not response.success():
            raise RuntimeError(f"飞书返回 {response.code}：{response.msg}")

    # ---------- 钉钉 ----------

    async def _send_dingtalk(self, adapter: Any, target: Target, card: cards.Card) -> None:
        token = await adapter.get_access_token()
        if not token:
            raise RuntimeError("钉钉 access_token 为空")
        robot_code = getattr(adapter, "client_id", None) or (getattr(adapter, "config", None) or {}).get("client_id")
        if not robot_code:
            raise RuntimeError("钉钉适配器缺少 client_id（robotCode）")
        msg_key, msg_param = cards.dingtalk_card(card)
        payload: dict = {"robotCode": robot_code, "msgKey": msg_key,
                         "msgParam": json.dumps(msg_param, ensure_ascii=False)}
        if target.is_group:
            url, payload["openConversationId"] = DINGTALK_GROUP_URL, target.session_id
            staff_id = ""
        else:
            staff_id = await self._dingtalk_staff_id(adapter, target)
            url, payload["userIds"] = DINGTALK_OTO_URL, [staff_id]
        headers = {"Content-Type": "application/json", "x-acs-dingtalk-access-token": token}
        async with self._service._session().post(url, json=payload, headers=headers,
                                                 timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"钉钉返回 HTTP {resp.status}：{text[:200]}")
        try:
            data = json.loads(text or "{}")
        except json.JSONDecodeError:
            data = {}
        rejected = (data.get("invalidStaffIdList") or []) + (data.get("flowControlledStaffIdList") or [])
        if staff_id and staff_id in rejected:
            raise RuntimeError(f"钉钉拒绝向该用户发送：{text[:200]}")

    @staticmethod
    async def _dingtalk_staff_id(adapter: Any, target: Target) -> str:
        """私聊需要 staff_id：AstrBot 收到消息时按 umo 记下了映射，读不到时与适配器一样退回会话 ID。"""
        getter = getattr(adapter, "_get_sender_staff_id", None)
        if callable(getter):
            try:
                staff_id = await getter(target)
                if staff_id:
                    return str(staff_id)
            except Exception:  # noqa: BLE001
                pass
        return target.session_id

    # ---------- 企业微信 ----------

    async def _send_wecom(self, adapter: Any, target: Target, card: cards.Card) -> None:
        client = getattr(adapter, "client", None)
        if client is None or not callable(getattr(client, "post", None)):
            raise RuntimeError("企业微信适配器没有可用的 client")
        if hasattr(client, "kf_message"):
            raise RuntimeError("微信客服模式不支持主动发送卡片")
        if target.is_group:
            raise RuntimeError("企业微信应用消息只能发给成员")
        config = getattr(adapter, "config", None) or {}
        agent = config.get("agent_id") or config.get("agentid") or getattr(adapter, "agent_id", None)
        if not agent:
            raise RuntimeError("还不知道应用的 agentid（成员先给应用发一条消息即可）")
        agent = int(agent) if str(agent).isdigit() else agent
        payload = cards.wecom_card(card, target.session_id, agent)
        # wechatpy 是同步客户端，放到线程池里调用；errcode 非 0 时它会抛出异常。
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: client.post("message/send", data=payload))
        if isinstance(result, dict) and (result.get("errcode") or result.get("invaliduser")):
            raise RuntimeError(f"企业微信返回：{result}")
