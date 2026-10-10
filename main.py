"""AstrBot × RemoteCI：在聊天里查课表、控制教室、管理 RemoteCI，并给老师主动推送日程。

AstrBot 相关的胶水代码都在这里：指令、LLM 工具（自然语言）、群聊 @ 判定、主动消息、插件页面 Web API。
业务逻辑见 remoteci/ 目录。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.agent.message import TextPart
except ImportError:  # 旧版 AstrBot
    TextPart = None

from .remoteci.client import RemoteCiError
from .remoteci.commands import HELP, run_command
from .remoteci.service import ROLE_NAMES, ChatUser, RemoteCiService, ServiceError

try:  # AstrBot 新版插件页面 Web API（FastAPI）
    from astrbot.api.web import error_response, json_response
    from astrbot.api.web import request as web_request
    _NEW_WEB = True
except ImportError:  # 旧版 Dashboard（Quart）
    from quart import jsonify
    from quart import request as web_request
    _NEW_WEB = False

PLUGIN = "astrbot_plugin_remoteci"
SKILL_DIR = Path(__file__).parent / "remoteci" / "skill"
QQ_OFFICIAL = {"qq_official", "qq_official_webhook"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _data_dir() -> Path:
    try:
        from astrbot.api.star import StarTools
        return Path(StarTools.get_data_dir(PLUGIN))
    except Exception:  # noqa: BLE001 - 兼容没有 StarTools 的旧版本
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN


@register(PLUGIN, "MEMZ_Edge", "RemoteCI 课表查询、教室控制与老师日程主动推送", "1.0.0")
class RemoteCiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.service = RemoteCiService(_data_dir(), send=self._send, config=dict(self.config), logger=logger)
        self._register_web()

    async def initialize(self):
        self.service.start()

    async def terminate(self):
        await self.service.stop()

    # ---------- 公共 ----------

    async def _send(self, umo: str, text: str) -> bool:
        return bool(await self.context.send_message(umo, MessageChain().message(text)))

    def _user(self, event: AstrMessageEvent) -> ChatUser:
        umo = event.unified_msg_origin
        platform = umo.split(":", 1)[0] if umo else event.get_platform_name()
        sender_id = str(event.get_sender_id())
        return ChatUser(
            user_key=f"{platform}:{sender_id}", platform=platform, sender_id=sender_id,
            sender_name=event.get_sender_name() or sender_id, umo=umo,
            is_private=event.is_private_chat(), group_name=str(event.get_group_id() or ""),
        )

    def _addressed(self, event: AstrMessageEvent) -> bool:
        """私聊总是处理；群聊只在 @ 机器人时处理。"""
        if event.is_private_chat() or not self.config.get("group_require_at", True):
            return True
        self_id = str(event.get_self_id())
        for comp in event.get_messages():
            if isinstance(comp, Comp.At) and str(getattr(comp, "qq", "")) == self_id:
                return True
        # QQ 官方机器人只会收到 @ 它的群消息，且消息链里不一定保留 At 段。
        return event.get_platform_name() in QQ_OFFICIAL and bool(getattr(event, "is_at_or_wake_command", False))

    async def _remember(self, event: AstrMessageEvent, user: ChatUser) -> None:
        sessions = self.service.store.sessions
        known = event.unified_msg_origin in sessions
        if user.is_private:
            name = user.sender_name
        else:
            group = event.get_group_id()
            old = sessions.get(event.unified_msg_origin, {}).get("name")
            name = old if old and old != f"群 {group}" else f"群 {group}"
        self.service.store.touch_session(event.unified_msg_origin, kind="private" if user.is_private else "group",
                                         name=name, platform=user.platform,
                                         user_key=user.user_key if user.is_private else None)
        binding = self.service.store.binding_for(user.user_key)
        if binding and user.is_private and binding.get("umo") != user.umo:
            binding["umo"] = user.umo
            known = False
        if not known:
            await self.service.store.save()

    # ---------- 指令 ----------

    @filter.command("rci", alias={"remoteci", "课表"})
    async def rci(self, event: AstrMessageEvent):
        """RemoteCI：查课表、教室控制、老师日程提醒。发送 /rci 帮助 查看用法"""
        if not self._addressed(event):
            return
        user = self._user(event)
        await self._remember(event, user)
        try:
            text = await run_command(self.service, user, event.message_str, lambda: self._first_image(event))
        except RemoteCiError as ex:
            text = f"RemoteCI 请求失败：{ex}"
        except Exception as ex:  # noqa: BLE001
            logger.exception("RemoteCI 指令出错")
            text = f"RemoteCI 插件出错：{ex}"
        yield event.plain_result(text)
        event.stop_event()

    # ---------- 图片读取（班级头像） ----------

    async def _first_image(self, event: AstrMessageEvent) -> bytes | None:
        """读取本条消息里的第一张图片；没有时再看被回复的消息。"""
        try:
            chain = list(event.get_messages())
        except Exception:  # noqa: BLE001
            return None
        images = [c for c in chain if isinstance(c, Comp.Image)]
        for comp in chain:
            if isinstance(comp, Comp.Reply) and getattr(comp, "chain", None):
                images.extend(c for c in comp.chain if isinstance(c, Comp.Image))
        for image in images:
            data = await self._image_bytes(image)
            if data:
                return data
        return None

    async def _image_bytes(self, image) -> bytes | None:
        convert = getattr(image, "convert_to_base64", None)
        if convert:
            try:
                return base64.b64decode(await convert())
            except Exception:  # noqa: BLE001 - 不同平台适配器的图片来源不同，失败时再按 url/file 读取
                pass
        for ref in (getattr(image, "url", None), getattr(image, "file", None)):
            if not ref or not isinstance(ref, str):
                continue
            if ref.startswith("base64://"):
                return base64.b64decode(ref[len("base64://"):])
            if ref.startswith(("http://", "https://")):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(ref, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                            if resp.status == 200 and (resp.content_length or 0) <= MAX_IMAGE_BYTES:
                                data = await resp.content.read(MAX_IMAGE_BYTES + 1)
                                return data if len(data) <= MAX_IMAGE_BYTES else None
                except (aiohttp.ClientError, TimeoutError):
                    continue
            path = Path(ref.removeprefix("file:///"))
            if path.is_file() and path.stat().st_size <= MAX_IMAGE_BYTES:
                return path.read_bytes()
        return None

    # ---------- 自然语言（LLM 工具） ----------

    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """告诉模型当前用户的 RemoteCI 连接状态，便于自然语言调用工具。"""
        try:
            user = self._user(event)
            binding = self.service.store.binding_for(user.user_key)
        except Exception:  # noqa: BLE001
            return
        if binding:
            p = binding.get("profile") or {}
            hint = (f"[RemoteCI] 当前用户已连接 RemoteCI 账号“{p.get('displayName')}”。"
                    "涉及课表、日程、下节课、班级、教室控制、提醒设置或 RemoteCI 管理时，使用 remoteci_ 开头的工具；"
                    "需要接口细节时先调用 remoteci_reference。")
        else:
            hint = ("[RemoteCI] 当前用户尚未连接 RemoteCI。若用户想查课表或使用 RemoteCI，"
                    "引导其私聊提供服务器地址和 API Key（或用户名密码），然后调用 remoteci_connect。")
        # 作为本轮用户消息的临时附加内容注入，不改系统提示词（保住提示词缓存），也不写入对话历史。
        if hasattr(TextPart, "mark_as_temp") and hasattr(req, "extra_user_content_parts"):
            req.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())
        else:  # 旧版 AstrBot 没有 extra_user_content_parts / mark_as_temp
            req.system_prompt = (req.system_prompt or "") + "\n" + hint

    async def _tool(self, event: AstrMessageEvent, fn) -> str:
        if not self._addressed(event):
            return "群聊中需要 @机器人 才能处理 RemoteCI 请求。"
        user = self._user(event)
        await self._remember(event, user)
        try:
            return await fn(user)
        except ServiceError as ex:
            return str(ex)
        except RemoteCiError as ex:
            return f"RemoteCI 请求失败：{ex}"

    @filter.llm_tool(name="remoteci_connect")
    async def tool_connect(self, event: AstrMessageEvent, server_url: str = "", api_key: str = "",
                           username: str = "", password: str = ""):
        """把当前聊天用户连接（绑定）到其 RemoteCI 账号。提供 api_key，或者 username+password 之一。只能在私聊中使用；回复中不要复述密码或密钥。

        Args:
            server_url(string): RemoteCI 服务器地址，如 https://remoteci.example.com；管理员配置了默认地址时可留空
            api_key(string): 以 rci_ 开头的 API Key
            username(string): RemoteCI 用户名
            password(string): RemoteCI 密码
        """
        async def run(user: ChatUser):
            if not user.is_private:
                return "为保护账号安全，请让用户私聊机器人完成绑定，并撤回群里的密码或密钥。"
            if api_key:
                return await self.service.bind_api_key(user, api_key, server_url)
            if username and password:
                return await self.service.bind_password(user, username, password, server_url)
            return "需要 API Key，或用户名和密码。"
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_disconnect")
    async def tool_disconnect(self, event: AstrMessageEvent):
        """解除当前用户与 RemoteCI 账号的绑定。

        Args:
        """
        return await self._tool(event, self.service.unbind)

    @filter.llm_tool(name="remoteci_whoami")
    async def tool_whoami(self, event: AstrMessageEvent):
        """查看当前用户绑定的 RemoteCI 账号、身份、可访问班级和推送角色。

        Args:
        """
        async def run(user):
            return self.service.describe_me(user)
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_my_schedule")
    async def tool_my_schedule(self, event: AstrMessageEvent, day: str = "今天"):
        """查询老师/班主任本人的“我的日程”（跨班级聚合的个人课表）。

        Args:
            day(string): 今天、明天、后天、本周、周三，或 2026-10-05 这样的日期
        """
        return await self._tool(event, lambda user: self.service.query_my_schedule(user, day))

    @filter.llm_tool(name="remoteci_next_course")
    async def tool_next(self, event: AstrMessageEvent):
        """查询老师正在上的课和下一节课（班级、科目、节次、时间）。

        Args:
        """
        return await self._tool(event, self.service.query_next)

    @filter.llm_tool(name="remoteci_class_schedule")
    async def tool_class_schedule(self, event: AstrMessageEvent, class_name: str = "", day: str = "今天"):
        """查询某个班级的课表（含任课教师）。

        Args:
            class_name(string): 班级名称或其片段，只有一个班时可留空
            day(string): 今天、明天、后天、本周、周三，或 2026-10-05 这样的日期
        """
        return await self._tool(event, lambda user: self.service.query_class_schedule(user, class_name or None, day))

    @filter.llm_tool(name="remoteci_class_state")
    async def tool_class_state(self, event: AstrMessageEvent, class_name: str = ""):
        """查询班级教室当前课堂状态（上课/课间/放学、当前与下一节科目）。

        Args:
            class_name(string): 班级名称，只有一个班时可留空
        """
        return await self._tool(event, lambda user: self.service.query_state(user, class_name or None))

    @filter.llm_tool(name="remoteci_swap_requests")
    async def tool_swap_requests(self, event: AstrMessageEvent, box: str = "incoming"):
        """查看换课申请：incoming 为发给我的（待我审批、或被强制换走可撤回的），outgoing 为我发起的。回答时给出申请人、两节课、理由和编号，再问用户是否通过。发起换课需要在 RemoteCI WebUI 或手机 App 的“换课”页完成。

        Args:
            box(string): incoming 或 outgoing
        """
        from .remoteci.swaps import list_requests

        async def run(user):
            binding = self.service.require_binding(user)
            return await list_requests(self.service, binding, "outgoing" if box == "outgoing" else "incoming")
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_swap_decide")
    async def tool_swap_decide(self, event: AstrMessageEvent, request_id: str, action: str, note: str = ""):
        """处理换课申请。通过或撤回前先向用户复述两节课并确认。

        Args:
            request_id(string): 换课申请编号（8 位短编号或完整 ID）
            action(string): approve 通过、reject 拒绝、revoke 撤回别人对我的强制换课、cancel 撤销我发起的待审批申请
            note(string): 通过或拒绝时的备注，可留空
        """
        from .remoteci.swaps import decide

        async def run(user):
            if action not in ("approve", "reject", "revoke", "cancel"):
                return "action 只能是 approve、reject、revoke 或 cancel。"
            binding = self.service.require_binding(user)
            return await decide(self.service, binding, action, request_id, note)
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_holidays")
    async def tool_holidays(self, event: AstrMessageEvent):
        """查看近期法定假期与调休安排：哪几天放假（教室课表自动关闭）、哪几天调休上学以及上周几的课。"""
        from .remoteci.makeup import list_makeup

        async def run(user):
            return await list_makeup(self.service, self.service.require_binding(user))
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_set_makeup")
    async def tool_set_makeup(self, event: AstrMessageEvent, date: str, follow: str):
        """修改某个调休上学日补哪天的课（需要系统管理员）。修改前先向用户复述日期和安排，得到确认后再调用。

        Args:
            date(string): 调休上学日，格式 YYYY-MM-DD
            follow(string): 周一、周二、周三、周四、周五，或“不补课”，或“自动”（恢复自动推算）
        """
        from .remoteci.makeup import set_makeup

        async def run(user):
            return await set_makeup(self.service, self.service.require_binding(user), date, follow)
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_profiles")
    async def tool_profiles(self, event: AstrMessageEvent):
        """查看服务端档案库（ClassIsland 时间表/课表/科目档案）：全局模板与各班档案、修订号和内容数量。需要系统管理员或班主任。"""
        from .remoteci.profiles import list_profiles

        async def run(user):
            return await list_profiles(self.service, self.service.require_binding(user))
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_collect_profile")
    async def tool_collect_profile(self, event: AstrMessageEvent, classes: str = "", save: bool = False):
        """从教室电脑读取 ClassIsland 当前正在使用的档案（含按日期安排的临时层），报告内容概要。只读；save=true 时会把结果保存为该班的服务端档案并覆盖原有版本，保存前必须得到用户同意。需要系统管理员或班主任。

        Args:
            classes(string): 班级名，多个用空格分隔；只管理一个班时可留空
            save(boolean): 用户已明确同意把收集结果保存为服务端班级档案时为 true
        """
        from .remoteci.profiles import collect_profiles

        async def run(user):
            return await collect_profiles(self.service, self.service.require_binding(user), classes.split() or None, save)
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_apply_profile")
    async def tool_apply_profile(self, event: AstrMessageEvent, profile: str, mode: str, classes: str = "",
                                 sections: str = "全部", import_name: str = "", confirm_replace: bool = False,
                                 replace_temp_layers: bool = False):
        """把服务端已保存的档案下发到教室电脑，会改变教室正在使用的课表。调用前必须向用户复述档案、班级、方式和类别（临时层则复述日期）并得到同意。

        Args:
            profile(string): 档案名称或 ID
            mode(string): 更新（合并到当前档案）、替换（整体替换所选类别，需 confirm_replace=true）、新建（创建并启用新档案，需 import_name）或 临时层（只下发档案中按日期安排的临时层，不改常规课表）
            classes(string): 目标班级名，多个用空格分隔；班级档案可留空，全局模板必填
            sections(string): 时间表、课表、科目 或 全部，多个用顿号分隔；临时层方式忽略
            import_name(string): 新建方式下设备上的新档案名
            confirm_replace(boolean): 用户已明确同意整体替换时为 true
            replace_temp_layers(boolean): 临时层方式下，用户已同意替换教室电脑同一天已有的临时层或预定课表时为 true
        """
        from .remoteci.profiles import apply_profile

        async def run(user):
            return await apply_profile(self.service, self.service.require_binding(user), profile, mode,
                                       classes.split() or None, sections, import_name, confirm_replace, replace_temp_layers)
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_get_reminders")
    async def tool_get_reminders(self, event: AstrMessageEvent):
        """查看当前用户的主动提醒设置（当日日程、次日日程、课前提醒、换课提醒、班主任班级换课提醒）。

        Args:
        """
        async def run(user):
            return self.service.reminder_summary(user)
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_set_reminders")
    async def tool_set_reminders(self, event: AstrMessageEvent, today_enabled: bool | None = None,
                                 today_time: str | None = None, tomorrow_enabled: bool | None = None,
                                 tomorrow_mode: str | None = None, tomorrow_time: str | None = None,
                                 before_enabled: bool | None = None, before_minutes: int | None = None,
                                 change_enabled: bool | None = None, class_change_enabled: bool | None = None,
                                 reset: bool | None = None):
        """修改当前用户自己的主动提醒设置，只传需要修改的项。

        Args:
            today_enabled(boolean): 是否在上学时推送当天个人日程
            today_time(string): 当天日程推送时间，HH:mm
            tomorrow_enabled(boolean): 是否推送次日个人日程
            tomorrow_mode(string): last_class 表示自己最后一节课下课时推送，fixed 表示固定时间推送
            tomorrow_time(string): fixed 模式下的次日日程推送时间，HH:mm；设置它时应同时把 tomorrow_mode 设为 fixed
            before_enabled(boolean): 是否开启课前提醒
            before_minutes(number): 课前提前多少分钟提醒，1-120
            change_enabled(boolean): 自己的课被换了是否提醒
            class_change_enabled(boolean): 班主任：班里的课被换了是否提醒
            reset(boolean): true 表示清除个人设置，恢复管理员统一设置
        """
        changes = {k: v for k, v in {
            "today_enabled": today_enabled, "today_time": today_time, "tomorrow_enabled": tomorrow_enabled,
            "tomorrow_mode": tomorrow_mode, "tomorrow_time": tomorrow_time, "before_enabled": before_enabled,
            "before_minutes": before_minutes, "change_enabled": change_enabled,
            "class_change_enabled": class_change_enabled}.items() if v is not None}
        if tomorrow_time and not tomorrow_mode:
            changes["tomorrow_mode"] = "fixed"
        if reset:
            changes = {"reset": True, **changes}
        return await self._tool(event, lambda user: self.service.update_reminders(user, changes))

    @filter.llm_tool(name="remoteci_send_notification")
    async def tool_notify(self, event: AstrMessageEvent, class_name: str, message: str, title: str = ""):
        """向班级教室的 ClassIsland 发送通知（显示在教室大屏上）。

        Args:
            class_name(string): 班级名称
            message(string): 通知正文
            title(string): 通知标题，可留空
        """
        return await self._tool(event, lambda user: self.service.notify(user, class_name, message, title))

    @filter.llm_tool(name="remoteci_rename_class")
    async def tool_rename_class(self, event: AstrMessageEvent, new_name: str, class_name: str = ""):
        """修改班级名称。需要系统管理员，或系统管理员允许改班名的本班班主任；没有权限时如实告诉用户。

        Args:
            new_name(string): 新的班级名称，1-40 个字符
            class_name(string): 要改名的班级（当前名称），只有一个班级时可留空
        """
        return await self._tool(event, lambda user: self.service.rename_class(user, class_name or None, new_name))

    @filter.llm_tool(name="remoteci_set_class_avatar")
    async def tool_set_class_avatar(self, event: AstrMessageEvent, class_name: str = "", clear: bool = False):
        """把用户在本条消息中发送（或回复）的图片设为班级头像；clear=true 时清除头像，改为显示默认班级图标。
        需要系统管理员，或系统管理员允许改头像的本班班主任。用户没有附带图片时，请对方连同图片一起发送。

        Args:
            class_name(string): 班级名称，只有一个班级时可留空
            clear(boolean): 是否清除头像
        """
        async def run(user):
            if clear:
                return await self.service.clear_class_avatar(user, class_name or None)
            image = await self._first_image(event)
            if not image:
                return "没有在这条消息里找到图片：请把图片和要求一起发送，或回复那张图片。"
            return await self.service.set_class_avatar(user, class_name or None, image)
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_class_command")
    async def tool_class_command(self, event: AstrMessageEvent, class_name: str, command: int,
                                 payload_json: str = "{}", confirmed: bool = False):
        """对单个班级执行 RemoteCI 控制命令（POST /api/commands）。命令编号与载荷格式先用 remoteci_reference(topic="control") 查询。
        电源、重启、插件卸载、终端、文件分发等高风险操作必须先向用户复述并得到明确同意，再设 confirmed=true。

        Args:
            class_name(string): 班级名称
            command(number): 命令编号，如 2 通知、1 换课、6 音量、5 电源
            payload_json(string): 命令载荷 JSON 对象（不含 command 字段），如 {"volume":{"level":30}}
            confirmed(boolean): 用户是否已明确确认高风险操作
        """
        async def run(user):
            if int(command) == 8:
                return "该控制命令当前不可用。"
            try:
                payload = json.loads(payload_json or "{}")
            except json.JSONDecodeError as ex:
                return f"payload_json 不是合法 JSON：{ex}"
            return await self.service.send_command(user, class_name, int(command), payload, bool(confirmed))
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_api")
    async def tool_api(self, event: AstrMessageEvent, method: str, path: str, query_json: str = "{}",
                       body_json: str = "", confirmed: bool = False):
        """以当前用户的 RemoteCI 账号调用任意 REST API（服务端按账号权限鉴权），用于换课、广播、班级/分组/账号/成员管理、配对码、备份等。
        调用前先用 remoteci_reference 查阅接口；删除、恢复备份、广播和高风险控制必须先得到用户明确同意并设 confirmed=true。
        整体替换类接口（如班级成员）要先 GET、合并、再 PUT。

        Args:
            method(string): HTTP 方法：GET、POST、PUT、DELETE
            path(string): 以 /api/ 开头的路径，如 /api/schedule
            query_json(string): 查询参数 JSON 对象，如 {"classId":"..."}
            body_json(string): 请求体 JSON，可留空
            confirmed(boolean): 用户是否已明确确认危险操作
        """
        async def run(user):
            try:
                query = json.loads(query_json or "{}")
                body = json.loads(body_json) if body_json else None
            except json.JSONDecodeError as ex:
                return f"JSON 参数不合法：{ex}"
            return await self.service.call_api(user, method, path, body, query, bool(confirmed))
        return await self._tool(event, run)

    @filter.llm_tool(name="remoteci_reference")
    async def tool_reference(self, event: AstrMessageEvent, topic: str = "overview"):
        """读取 RemoteCI API 使用手册：overview（连接、身份权限、查询、错误码）、control（教室控制命令、换课、广播、扩展插件设置）、admin（班级、分组、账号、成员、配对码、备份、班主任权限）、swaps（换课申请、审批、强制换课与个人通知）、plugin（本插件的指令与提醒）。

        Args:
            topic(string): overview、control、admin、swaps 或 plugin
        """
        files = {"overview": SKILL_DIR / "SKILL.md", "control": SKILL_DIR / "references" / "control.md",
                 "admin": SKILL_DIR / "references" / "admin.md",
                 "swaps": SKILL_DIR / "references" / "swap-requests.md"}
        if topic == "plugin":
            return HELP
        path = files.get(topic, files["overview"])
        text = path.read_text("utf-8")
        return text + ("\n\n（在本插件中，凭据由插件管理：用 remoteci_api 调用接口，无需自己处理 Authorization 头或登录续期。）"
                       if topic == "overview" else "")

    # ---------- 插件页面 Web API ----------

    def _register_web(self) -> None:
        routes = [
            ("overview", self.web_overview, ["GET"]),
            ("bindings", self.web_bindings, ["GET"]),
            ("sessions", self.web_sessions, ["GET"]),
            ("settings", self.web_settings, ["GET"]),
            ("log", self.web_log, ["GET"]),
            ("session/update", self.web_session_update, ["POST"]),
            ("session/delete", self.web_session_delete, ["POST"]),
            ("session/test", self.web_session_test, ["POST"]),
            ("binding/unbind", self.web_binding_unbind, ["POST"]),
            ("binding/refresh", self.web_binding_refresh, ["POST"]),
            ("binding/reset_prefs", self.web_binding_reset, ["POST"]),
            ("settings/reminders", self.web_settings_reminders, ["POST"]),
            ("settings/holidays", self.web_settings_holidays, ["POST"]),
            ("settings/pause", self.web_settings_pause, ["POST"]),
            ("holidays/refresh", self.web_holidays_refresh, ["POST"]),
        ]
        for route, handler, methods in routes:
            try:
                self.context.register_web_api(f"/{PLUGIN}/{route}", handler, methods, f"RemoteCI {route}")
            except Exception as ex:  # noqa: BLE001 - 旧版本没有插件 Web API 时不影响聊天功能
                logger.warning(f"RemoteCI 插件页面接口注册失败（AstrBot 版本过旧？）：{ex}")
                return

    @staticmethod
    def _ok(data):
        payload = {"ok": True, "data": data}
        return json_response(payload) if _NEW_WEB else jsonify(payload)

    @staticmethod
    def _fail(message: str, status: int = 400):
        if _NEW_WEB:
            return error_response(message, status_code=status)
        return jsonify({"ok": False, "status": "error", "message": message}), status

    @staticmethod
    async def _body() -> dict:
        if _NEW_WEB:
            data = await web_request.json(default={})
        else:
            data = await web_request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    async def _guard(self, coro):
        try:
            return self._ok(await coro)
        except ServiceError as ex:
            return self._fail(str(ex))
        except RemoteCiError as ex:
            return self._fail(str(ex), 502)
        except Exception as ex:  # noqa: BLE001
            logger.exception("RemoteCI 页面接口出错")
            return self._fail(f"{type(ex).__name__}: {ex}", 500)

    async def _value(self, value):
        return value

    async def web_overview(self):
        return await self._guard(self._value({**self.service.overview(), "roles": ROLE_NAMES}))

    async def web_bindings(self):
        return await self._guard(self._value(self.service.binding_rows()))

    async def web_sessions(self):
        return await self._guard(self._value({"sessions": self.service.session_rows(),
                                              "bindings": [{"key": b["key"], "name": b["display_name"] or b["username"]}
                                                           for b in self.service.binding_rows()]}))

    async def web_settings(self):
        state = self.service.store.state
        return await self._guard(self._value({
            "reminders": state["reminders"], "holidays": state["holidays"], "paused": state.get("paused", False),
            "upcoming": self.service.holidays.upcoming(self.service.today(), state["holidays"]),
            "official_data": self.service.holidays.has_year(self.service.today().year),
            "poll_minutes": self.service.poll_seconds // 60,
        }))

    async def web_log(self):
        return await self._guard(self._value(list(reversed(self.service.store.state["log"][-150:]))))

    async def web_session_update(self):
        body = await self._body()
        return await self._guard(self.service.update_session(str(body.get("umo") or ""), body.get("patch") or {}))

    async def web_session_delete(self):
        body = await self._body()
        return await self._guard(self.service.delete_session(str(body.get("umo") or "")))

    async def web_session_test(self):
        body = await self._body()
        return await self._guard(self.service.preview(str(body.get("umo") or "")))

    async def web_binding_unbind(self):
        body = await self._body()
        return await self._guard(self.service.admin_unbind(str(body.get("key") or "")))

    async def web_binding_refresh(self):
        body = await self._body()
        return await self._guard(self.service.admin_refresh(str(body.get("key") or "")))

    async def web_binding_reset(self):
        body = await self._body()
        return await self._guard(self.service.admin_reset_prefs(str(body.get("key") or "")))

    async def web_settings_reminders(self):
        body = await self._body()
        return await self._guard(self.service.update_defaults(body.get("patch") or {}))

    async def web_settings_holidays(self):
        body = await self._body()
        return await self._guard(self.service.update_holiday_settings(body))

    async def web_settings_pause(self):
        body = await self._body()
        return await self._guard(self.service.set_paused(bool(body.get("paused"))))

    async def web_holidays_refresh(self):
        return await self._guard(self.service.refresh_holidays(force=True))
