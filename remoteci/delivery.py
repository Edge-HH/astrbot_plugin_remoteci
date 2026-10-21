"""按会话所在平台推送个人通知：飞书、钉钉、企业微信发原生卡片，其他平台由调用方发文本。

由 main.py 注入 AstrBot 的 Context；本模块只在用到时才导入 AstrBot / lark_oapi，单元测试可直接用假对象。
适配器按特性探测（AstrBot ≥ 4.26 的 context.get_platform_inst，或 context.platform_manager.platform_insts），
任何一步失败——找不到适配器、签名链接接口未实现（404）、平台返回错误——都返回 False，由调用方回退为文本推送。

企业微信的文本主动消息也在这里直接调平台接口（send_text）：AstrBot 的企业微信应用适配器重启后要等成员再发一条消息
才知道 agentid，智能机器人长连接模式没配消息推送 Webhook 时根本不能主动发送，两种情况下 context.send_message 都会失败。
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
WECOM_AI_SESSION_PREFIX = "wecom_ai_bot_"
WECOM_TEXT_LIMIT = 2000  # 企业微信文本消息上限 2048 字节，留点余量


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


def wecom_ai_chat_id(session_id: str) -> str:
    """智能机器人的会话 ID 形如 wecom_ai_bot_wecomai_<userid / chatid>，主动推送要用原始 ID。"""
    if not session_id.startswith(WECOM_AI_SESSION_PREFIX):
        return session_id
    rest = session_id[len(WECOM_AI_SESSION_PREFIX):]
    return rest.split("_", 1)[1] if "_" in rest else rest


def split_utf8(text: str, limit: int = WECOM_TEXT_LIMIT) -> list[str]:
    """按 UTF-8 字节数切分长文本，尽量在换行处断开。"""
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line.encode("utf-8")) > limit:
            cut = len(line)
            while len(line[:cut].encode("utf-8")) > limit:
                cut -= 1
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:cut])
            line = line[cut:]
        if len((current + line).encode("utf-8")) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current or not chunks:
        chunks.append(current)
    return [c.rstrip("\n") for c in chunks if c.strip()] or [text]


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

    # ---------- 企业微信文本主动消息 ----------

    def remember_wecom_agent(self, platform_id: str, agent: Any) -> bool:
        """记下企业微信应用的 agentid（收到成员消息时调用），重启后主动推送还能用；有变化时返回 True。"""
        agent = str(agent or "").strip()
        if not platform_id or not agent.isdigit():
            return False
        agents = self._service.store.state.setdefault("wecom_agents", {})
        if agents.get(platform_id) == agent:
            return False
        agents[platform_id] = agent
        return True

    def _wecom_agent(self, adapter: Any, platform_id: str) -> Any:
        config = getattr(adapter, "config", None) or {}
        agent = (config.get("agent_id") or config.get("agentid") or getattr(adapter, "agent_id", None)
                 or (self._service.store.state.get("wecom_agents") or {}).get(platform_id))
        if not agent:
            return None
        if not getattr(adapter, "agent_id", None):
            adapter.agent_id = str(agent)  # 顺手补上，AstrBot 自己的主动发送也能用了
        return int(agent) if str(agent).isdigit() else agent

    async def send_text(self, umo: str, text: str) -> bool:
        """企业微信会话直接调平台接口发文本；不是企业微信、或平台不支持时返回 False，由调用方走 AstrBot 通用发送。"""
        target = parse_umo(umo)
        if target is None:
            return False
        adapter = self.find_adapter(target.platform_id)
        platform = platform_name(adapter)
        sender = {"wecom": self._send_wecom_text, "wecom_ai_bot": self._send_wecom_ai_text}.get(platform)
        if sender is None:
            return False
        try:
            return bool(await sender(adapter, target, text))
        except Exception as ex:  # noqa: BLE001 - 失败后还会再试 AstrBot 通用发送
            self._service.log.warning(f"RemoteCI 企业微信主动消息发送失败（{platform}）：{ex}")
            return False

    async def _send_wecom_text(self, adapter: Any, target: Target, text: str) -> bool:
        client = getattr(adapter, "client", None)
        if client is None or not callable(getattr(client, "post", None)) or hasattr(client, "kf_message"):
            return False  # 微信客服不支持主动发送
        if target.is_group:
            return False
        agent = self._wecom_agent(adapter, target.platform_id)
        if not agent:
            self._service.log.warning("RemoteCI 企业微信主动消息：还不知道应用的 agentid，"
                                      "请让成员先给应用发一条消息，或在平台配置里填写 agent_id")
            return False
        loop = asyncio.get_running_loop()
        for chunk in split_utf8(text):
            payload = {"touser": target.session_id, "msgtype": "text", "agentid": agent, "text": {"content": chunk}}
            result = await loop.run_in_executor(None, lambda p=payload: client.post("message/send", data=p))
            if isinstance(result, dict) and (result.get("errcode") or result.get("invaliduser")):
                raise RuntimeError(f"企业微信返回：{result}")
        return True

    async def _send_wecom_ai_text(self, adapter: Any, target: Target, text: str) -> bool:
        """智能机器人长连接模式用 aibot_send_msg 主动推送（单聊填 userid，群聊填 chatid）。"""
        client = getattr(adapter, "long_connection_client", None)
        if client is None or not callable(getattr(client, "send_command", None)):
            return False  # HTTP 回调模式只能靠 AstrBot 的消息推送 Webhook
        chat_id = wecom_ai_chat_id(target.session_id)
        for chunk in split_utf8(text):
            body = {"chatid": chat_id, "msgtype": "markdown", "markdown": {"content": chunk}}
            if not await client.send_command(cmd="aibot_send_msg", req_id=uuid.uuid4().hex, body=body):
                raise RuntimeError(f"aibot_send_msg 发送失败（chatid={chat_id}），详见 AstrBot 日志")
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
        agent = self._wecom_agent(adapter, target.platform_id)
        if not agent:
            raise RuntimeError("还不知道应用的 agentid（成员先给应用发一条消息即可）")
        payload = cards.wecom_card(card, target.session_id, agent)
        # wechatpy 是同步客户端，放到线程池里调用；errcode 非 0 时它会抛出异常。
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: client.post("message/send", data=payload))
        if isinstance(result, dict) and (result.get("errcode") or result.get("invaliduser")):
            raise RuntimeError(f"企业微信返回：{result}")
