"""插件业务层：绑定账号、查询、控制、提醒设置、主动推送调度，以及 WebUI 数据。

与 AstrBot 的耦合只有两个注入点：`send(umo, text)` 主动发消息、`now()` 当前时间（测试可替换）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from . import schedule as sch
from .client import RemoteCiClient, RemoteCiError, normalize_base_url
from .holidays import HolidayCalendar
from .reminders import digest_due, personal_due, tomorrow_trigger
from .swaps import SwapInbox
from .store import (DEFAULT_SESSION_PUSH, REMINDER_KEYS, ROLE_AUTO, ROLE_CLASS_GROUP, ROLE_HEAD, ROLE_NONE,
                    ROLE_TEACHER, SESSION_ROLES, Store)

log = logging.getLogger("astrbot_plugin_remoteci")

ROLE_KIND_NAMES = {2: "管理员", 4: "班主任", 5: "老师", 1: "学生"}
ROLE_NAMES = {ROLE_AUTO: "自动", ROLE_NONE: "不推送", ROLE_TEACHER: "老师", ROLE_HEAD: "班主任",
              ROLE_CLASS_GROUP: "班级群"}

# 需要用户在对话中明确确认后才执行的命令编号（与 skill 的“先确认再执行”一致）。
DANGEROUS_COMMANDS = {5, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 22, 23}
# 与 RemoteCI 服务端 NotificationRequest.RollingSuggestionThreshold 一致。
ROLLING_SUGGESTION_THRESHOLD = 30


@dataclass
class ChatUser:
    user_key: str
    platform: str
    sender_id: str
    sender_name: str
    umo: str
    is_private: bool
    group_name: str = ""


class ServiceError(Exception):
    """直接展示给用户的业务错误。"""


class RemoteCiService:
    def __init__(self, data_dir: Path, *, send: Callable[[str, str], Awaitable[bool]],
                 config: dict | None = None, now: Callable[[], datetime] | None = None):
        self.store = Store(data_dir)
        self.config = config or {}
        self._send = send
        self._tz = _load_tz(self.config.get("timezone") or "Asia/Shanghai")
        self._now = now or (lambda: datetime.now(self._tz))
        self._http: aiohttp.ClientSession | None = None
        self.client = RemoteCiClient(self._session)
        self.holidays = HolidayCalendar(data_dir)
        self.holidays.load_cache()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._task: asyncio.Task | None = None
        self._last_poll: dict[str, float] = {}
        self.last_tick_error = ""
        # 换课申请个人通知：独立于节假日与推送角色，有“老师主动换课”权限的绑定都会收到。
        self.swaps = SwapInbox(self)

    # ---------- 生命周期 ----------

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.store.save()
        await self.store.save_snapshots()
        if self._http and not self._http.closed:
            await self._http.close()

    def now(self) -> datetime:
        return self._now()

    @property
    def default_server(self) -> str:
        return normalize_base_url(self.config.get("default_server_url") or "")

    @property
    def poll_seconds(self) -> int:
        return max(60, int(self.config.get("poll_minutes") or 5) * 60)

    # ---------- 绑定 ----------

    async def bind_api_key(self, user: ChatUser, api_key: str, server_url: str = "") -> str:
        api_key = api_key.strip()
        if not api_key.startswith("rci_"):
            raise ServiceError("API Key 应以 rci_ 开头，请在 RemoteCI WebUI“个人账号 → API Key”中创建。")
        server = self._server_or_raise(server_url)
        binding = self._new_binding(user, server, {"type": "api_key", "api_key": api_key})
        return await self._finish_bind(user, binding)

    async def bind_password(self, user: ChatUser, username: str, password: str, server_url: str = "") -> str:
        server = self._server_or_raise(server_url)
        try:
            auth = await self.client.login(server, username.strip(), password)
        except RemoteCiError as ex:
            raise ServiceError(f"登录失败：{ex}") from ex
        binding = self._new_binding(user, server, auth)
        return await self._finish_bind(user, binding)

    def _server_or_raise(self, server_url: str) -> str:
        server = normalize_base_url(server_url) or self.default_server
        if not server:
            raise ServiceError("请提供 RemoteCI 服务器地址，例如 https://remoteci.example.com"
                               "（管理员也可以在插件配置中填写默认服务器地址）。")
        return server

    def _new_binding(self, user: ChatUser, server: str, auth: dict) -> dict:
        old = self.store.binding_for(user.user_key) or {}
        return {
            "key": user.user_key, "platform": user.platform, "sender_id": user.sender_id,
            "sender_name": user.sender_name or old.get("sender_name", ""),
            "umo": user.umo if user.is_private else old.get("umo"),
            "server_url": server, "auth": auth, "profile": {}, "prefs": old.get("prefs", {}),
            "status": "ok", "status_message": "", "bound_at": time.time(), "updated_at": time.time(),
        }

    async def _finish_bind(self, user: ChatUser, binding: dict) -> str:
        old = self.store.binding_for(user.user_key)
        try:
            await self._refresh_profile(binding)
        except RemoteCiError as ex:
            raise ServiceError(f"连接失败：{ex}") from ex
        if old and old.get("auth") != binding["auth"]:
            await self.client.logout(old)
        self.store.bindings[user.user_key] = binding
        if user.is_private:
            self.store.touch_session(user.umo, kind="private", name=user.sender_name,
                                     platform=user.platform, user_key=user.user_key)
        self._drop_cache(user.user_key)
        await self.store.save()
        profile = binding["profile"]
        role = self.effective_role(self.store.sessions.get(binding.get("umo") or "", {}), binding)
        lines = [f"已连接 RemoteCI：{profile.get('displayName') or profile.get('username')}"
                 f"（{ROLE_KIND_NAMES.get(profile.get('roleKind'), '自定义角色')}）",
                 f"服务器：{binding['server_url']}"]
        classes = profile.get("classes") or []
        if classes:
            lines.append("可访问班级：" + "、".join(c.get("name", "") for c in classes[:12])
                         + ("…" if len(classes) > 12 else ""))
        if role in (ROLE_TEACHER, ROLE_HEAD):
            lines.append("已开启老师主动提醒，发送“/rci 提醒”查看或修改。")
        if not user.is_private:
            lines.append("⚠ 请勿在群聊中发送密码或 API Key，建议撤回刚才的消息。")
        return "\n".join(lines)

    async def unbind(self, user: ChatUser) -> str:
        binding = self.store.bindings.pop(user.user_key, None)
        if not binding:
            return "你还没有绑定 RemoteCI 账号。"
        await self.client.logout(binding)
        self._drop_cache(user.user_key)
        await self.store.save()
        return "已解除 RemoteCI 绑定，并移除了本插件登录产生的设备会话。"

    async def _refresh_profile(self, binding: dict) -> dict:
        me = await self._call(binding, "GET", "/api/me")
        binding["profile"] = {
            "id": me.get("id"), "username": me.get("username"), "displayName": me.get("displayName"),
            "role": me.get("role"), "roleKind": me.get("roleKind"), "permissions": me.get("permissions"),
            "classes": [{"id": c.get("id"), "name": c.get("name"), "roleKind": c.get("roleKind"),
                         "permissions": c.get("permissions")} for c in me.get("classes") or []],
        }
        binding["status"], binding["status_message"] = "ok", ""
        binding["updated_at"] = time.time()
        return binding["profile"]

    def require_binding(self, user: ChatUser) -> dict:
        binding = self.store.binding_for(user.user_key)
        if not binding:
            raise ServiceError("你还没有连接 RemoteCI。私聊发送以下任一指令完成绑定：\n"
                               "/rci 绑定 <API Key> [服务器地址]\n/rci 登录 <用户名> <密码> [服务器地址]")
        if user.is_private and binding.get("umo") != user.umo:
            binding["umo"] = user.umo
        return binding

    async def _save_auth(self, binding: dict) -> None:
        if binding.get("key") in self.store.bindings:
            await self.store.save()

    async def _call(self, binding: dict, method: str, path: str, *, params=None, body=None) -> Any:
        try:
            return await self.client.request(binding, method, path, params=params, body=body,
                                             on_auth_changed=self._save_auth)
        except RemoteCiError as ex:
            if ex.status == 401 and binding.get("key") in self.store.bindings:
                await self._mark_auth_failed(binding, str(ex))
            raise

    async def _mark_auth_failed(self, binding: dict, message: str) -> None:
        first = binding.get("status") != "auth_failed"
        binding["status"], binding["status_message"] = "auth_failed", message
        await self.store.save()
        if first and binding.get("umo"):
            await self._push(binding["umo"], "auth",
                             "【RemoteCI】账号凭据已失效，主动提醒已暂停。请私聊重新绑定：/rci 登录 <用户名> <密码> 或 /rci 绑定 <API Key>")

    def _drop_cache(self, user_key: str) -> None:
        for key in [k for k in self._cache if k.startswith(user_key + "|")]:
            del self._cache[key]

    async def _cached(self, binding: dict, key: str, loader: Callable[[], Awaitable[Any]], ttl: float) -> Any:
        full = f"{binding['key']}|{key}"
        hit = self._cache.get(full)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = await loader()
        self._cache[full] = (time.time(), value)
        return value

    # ---------- 班级 ----------

    def resolve_class(self, binding: dict, ref: str | None) -> dict | None:
        """按 ID、完整名称、名称片段匹配可访问的班级；ref 为空且只有一个班时返回它。"""
        classes = (binding.get("profile") or {}).get("classes") or []
        ref = (ref or "").strip()
        if not ref:
            return classes[0] if len(classes) == 1 else None
        for c in classes:
            if c.get("id") == ref or c.get("name") == ref:
                return c
        partial = [c for c in classes if ref in (c.get("name") or "")]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise ServiceError("匹配到多个班级：" + "、".join(c["name"] for c in partial) + "，请写完整班级名。")
        raise ServiceError(f"找不到班级“{ref}”。你可访问的班级：" + ("、".join(c.get("name", "") for c in classes) or "无"))

    def head_classes(self, session: dict, binding: dict) -> list[dict]:
        classes = (binding.get("profile") or {}).get("classes") or []
        ids = session.get("watch_class_ids")
        if ids:
            return [c for c in classes if c.get("id") in ids]
        return [c for c in classes if c.get("roleKind") == 4]

    # ---------- 查询 ----------

    def today(self) -> date:
        return self.now().date()

    def has_personal_schedule(self, binding: dict) -> bool:
        return (binding.get("profile") or {}).get("roleKind") in (4, 5)

    async def my_days(self, binding: dict, ttl: float = 120) -> dict[str, list[sch.Lesson]]:
        data = await self._cached(binding, "my", lambda: self._call(binding, "GET", "/api/me/schedule"), ttl)
        return sch.lessons_from_my_schedule(data or {})

    async def class_days(self, binding: dict, cls: dict | None, ttl: float = 120) -> dict[str, list[sch.Lesson]]:
        class_id = (cls or {}).get("id")
        data = await self._cached(binding, f"class:{class_id}",
                                  lambda: self._call(binding, "GET", "/api/schedule", params={"classId": class_id}), ttl)
        return sch.lessons_from_class_schedule(data or {}, class_id or "", (cls or {}).get("name", ""))

    def parse_day(self, text: str | None) -> list[date] | str:
        """“今天/明天/后天/本周/周三/2026-10-05/10-05” → 日期列表；返回字符串表示 week。"""
        today = self.today()
        text = (text or "今天").strip().lower()
        mapping = {"today": 0, "今天": 0, "今日": 0, "当日": 0, "tomorrow": 1, "明天": 1, "明日": 1, "次日": 1,
                   "后天": 2}
        if text in mapping:
            return [today + timedelta(days=mapping[text])]
        if text in ("week", "本周", "这周", "一周", "七天", "7天"):
            return "week"
        for i, ch in enumerate(sch.WEEKDAYS):
            if text in (f"周{ch}", f"星期{ch}", f"礼拜{ch}") or (ch == "日" and text in ("周天", "星期天")):
                delta = (i - today.weekday()) % 7
                return [today + timedelta(days=delta)]
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d", "%m月%d日", "%m月%d号"):
            try:
                parsed = datetime.strptime(text, fmt).date()
                if "%Y" not in fmt:
                    parsed = parsed.replace(year=today.year)
                    if parsed < today - timedelta(days=180):
                        parsed = parsed.replace(year=today.year + 1)
                return [parsed]
            except ValueError:
                continue
        raise ServiceError(f"无法识别日期“{text}”，可以用：今天、明天、后天、本周、周三、2026-10-05。")

    async def query_my_schedule(self, user: ChatUser, day: str | None = None) -> str:
        binding = self.require_binding(user)
        if not self.has_personal_schedule(binding):
            return "“我的日程”仅对老师和班主任账号开放。学生或其他账号可以用“/rci 班级 [班级名] [日期]”查看班级课表。"
        days = await self.my_days(binding)
        return self._render_days(days, day, title="我的日程", show_class=True, show_teacher=False,
                                 empty_hint="没有你的课程。若一直为空，请确认 RemoteCI 显示名与课表中的任课教师名完全一致。")

    async def query_class_schedule(self, user: ChatUser, class_ref: str | None = None, day: str | None = None) -> str:
        binding = self.require_binding(user)
        cls = self.resolve_class(binding, class_ref)
        days = await self.class_days(binding, cls)
        name = (cls or {}).get("name") or "默认班级"
        return self._render_days(days, day, title=f"{name}课表", show_class=False, show_teacher=True)

    def _render_days(self, days, day, *, title, show_class, show_teacher, empty_hint="没有课程") -> str:
        today = self.today()
        target = self.parse_day(day)
        dates = [today + timedelta(days=i) for i in range(7)] if target == "week" else target
        parts = []
        for d in dates:
            iso = d.isoformat()
            if iso not in days:
                parts.append(f"{title} · {sch.describe_date(iso, today)}\n超出服务端提供的七天范围。")
                continue
            holiday, reason = self.holidays.check(d, self.store.state["holidays"])
            text = sch.format_day(title, iso, days[iso], today, show_class=show_class, show_teacher=show_teacher,
                                  empty_text=empty_hint)
            if holiday:
                text += f"\n（{reason}，主动推送暂停）"
            if target != "week" or days[iso]:
                parts.append(text)
        return "\n\n".join(parts) if parts else f"{title}：未来七天没有课程。"

    async def query_next(self, user: ChatUser) -> str:
        binding = self.require_binding(user)
        if not self.has_personal_schedule(binding):
            return "“下一节课”基于老师的个人日程，仅对老师和班主任账号开放。"
        data = await self._call(binding, "GET", "/api/me/schedule/next")
        lines = []
        for key, title in (("current", "正在上"), ("next", "下一节")):
            slot = (data or {}).get(key)
            if not slot:
                continue
            course = slot.get("course") or {}
            when = ""
            starts = _parse_iso(slot.get("startsAt"))
            if key == "next" and starts:
                minutes = int((starts - datetime.now(starts.tzinfo)).total_seconds() // 60)
                when = f"，{_humanize_minutes(minutes)}后" if minutes > 0 else ""
            lines.append(f"{title}：{sch.describe_date(slot.get('date', ''), self.today())} {slot.get('className', '')} "
                         f"{course.get('label') or ''} {course.get('subject', '')}"
                         f"（{sch.short_clock(course.get('startTime'))}-{sch.short_clock(course.get('endTime'))}）{when}")
        return "\n".join(lines) if lines else "七天内没有你的后续课程。"

    async def query_state(self, user: ChatUser, class_ref: str | None = None) -> str:
        binding = self.require_binding(user)
        cls = self.resolve_class(binding, class_ref)
        data = await self._call(binding, "GET", "/api/state", params={"classId": (cls or {}).get("id")}) or {}
        states = {1: "上课中", 2: "课间", 3: "放学", 4: "预备", 0: "无课"}
        name = (cls or {}).get("name") or "默认班级"
        lines = [f"{name}：{states.get(data.get('currentState'), '未知')}"]
        if data.get("currentSubject"):
            lines.append(f"当前：{_subject_name(data.get('currentSubject'))}")
        if data.get("nextClassSubject"):
            lines.append(f"下一节：{_subject_name(data.get('nextClassSubject'))}")
        return "\n".join(lines)

    def describe_me(self, user: ChatUser) -> str:
        binding = self.require_binding(user)
        p = binding.get("profile") or {}
        session = self.store.sessions.get(binding.get("umo") or "", {})
        role = self.effective_role(session, binding)
        lines = [f"RemoteCI 账号：{p.get('displayName')}（{p.get('username')}）",
                 f"身份：{ROLE_KIND_NAMES.get(p.get('roleKind'), '自定义角色')}；推送角色：{ROLE_NAMES.get(role)}",
                 f"服务器：{binding.get('server_url')}",
                 "凭据：" + ("API Key rci_…" if binding["auth"].get("type") == "api_key" else "账号密码登录（仅保存续期凭据）")]
        if binding.get("status") == "auth_failed":
            lines.append("⚠ 凭据已失效，请重新绑定。")
        classes = p.get("classes") or []
        if classes:
            lines.append("班级：" + "、".join(f"{c.get('name')}" + ("（班主任）" if c.get("roleKind") == 4 else "")
                                           for c in classes))
        return "\n".join(lines)

    # ---------- 控制与通用 API ----------

    async def send_command(self, user: ChatUser, class_ref: str | None, command: int, payload: dict | None,
                           confirmed: bool) -> str:
        binding = self.require_binding(user)
        if command == 8:
            raise ServiceError("该控制命令当前不可用。")
        if command in DANGEROUS_COMMANDS and not confirmed:
            raise ServiceError("这是高风险操作，请先向用户复述目标班级和具体内容，得到明确同意后再以 confirmed=true 重试。")
        cls = self.resolve_class(binding, class_ref)
        body = dict(payload or {})
        body["command"] = command
        data = await self._call(binding, "POST", "/api/commands", params={"classId": (cls or {}).get("id")}, body=body)
        name = (cls or {}).get("name") or "默认班级"
        if isinstance(data, dict):
            ok = data.get("success", True)
            msg = data.get("message") or ("已完成" if ok else "执行失败")
            extra = f"\n{data['data']}" if data.get("data") else ""
            return f"{name}：{'✅' if ok else '❌'} {msg}{extra}"
        return f"{name}：已发送"

    async def notify(self, user: ChatUser, class_ref: str | None, message: str, title: str = "") -> str:
        sender = user.sender_name or "老师"
        # 聊天里无法像 WebUI/手机端那样提示用户；正文超过 30 字时直接开启滚动，避免静态正文显示不全。
        return await self.send_command(user, class_ref, 2, {"notification": {
            "title": title or f"{sender}的通知", "message": message, "isNotificationEffectEnabled": True,
            "isNotificationSoundEnabled": True,
            "isRollingEnabled": len(message.strip()) > ROLLING_SUGGESTION_THRESHOLD}}, confirmed=True)

    async def call_api(self, user: ChatUser, method: str, path: str, body: Any = None, query: dict | None = None,
                       confirmed: bool = False) -> str:
        binding = self.require_binding(user)
        method = (method or "GET").upper()
        if not path.startswith("/api/"):
            raise ServiceError("path 必须以 /api/ 开头。")
        if path.startswith("/api/auth/"):
            raise ServiceError("认证接口由插件自动处理，不能直接调用。")
        if path.startswith("/api/commands") and isinstance(body, dict) and body.get("command") == 8:
            raise ServiceError("该控制命令当前不可用。")
        risky = method == "DELETE" or "/restore" in path or path.startswith("/api/commands/broadcast") or (
            path.startswith("/api/commands") and isinstance(body, dict) and body.get("command") in DANGEROUS_COMMANDS)
        if risky and not confirmed:
            raise ServiceError("这是删除、恢复、广播或高风险控制操作，请先向用户复述对象和内容，得到明确同意后以 confirmed=true 重试。")
        data = await self._call(binding, method, path, params=query, body=body)
        if method != "GET" and path.startswith(("/api/me", "/api/classes", "/api/users")):
            self._drop_cache(binding["key"])
            try:
                await self._refresh_profile(binding)
                await self.store.save()
            except RemoteCiError:
                pass
        text = json.dumps(data, ensure_ascii=False) if data is not None else "OK（无返回内容）"
        return text if len(text) <= 12000 else text[:12000] + "…（已截断）"

    # ---------- 提醒设置 ----------

    def reminder_summary(self, user: ChatUser) -> str:
        binding = self.require_binding(user)
        prefs = self.store.effective_reminders(binding)
        session = self.store.sessions.get(binding.get("umo") or "", {})
        role = self.effective_role(session, binding)
        own = binding.get("prefs") or {}

        def mark(key):
            return "（个人）" if key in own else ""

        onoff = lambda v: "开" if v else "关"  # noqa: E731
        tomorrow = ("最后一节课下课时（无课时 " + prefs["tomorrow_fallback"] + "）"
                    if prefs["tomorrow_mode"] == "last_class" else prefs["tomorrow_time"])
        lines = [f"推送角色：{ROLE_NAMES.get(role)}" + ("" if role in (ROLE_TEACHER, ROLE_HEAD) else "（不会主动提醒）"),
                 f"当日日程：{onoff(prefs['today_enabled'])}{mark('today_enabled')}，{prefs['today_time']}{mark('today_time')}",
                 f"次日日程：{onoff(prefs['tomorrow_enabled'])}{mark('tomorrow_enabled')}，{tomorrow}"
                 f"{mark('tomorrow_mode') or mark('tomorrow_time')}",
                 f"课前提醒：{onoff(prefs['before_enabled'])}{mark('before_enabled')}，提前 {prefs['before_minutes']} 分钟"
                 f"{mark('before_minutes')}",
                 f"我的课被换：{onoff(prefs['change_enabled'])}{mark('change_enabled')}"]
        if role == ROLE_HEAD:
            names = "、".join(c.get("name", "") for c in self.head_classes(session, binding)) or "无（可在 WebUI 指定）"
            lines.append(f"班里的课被换：{onoff(prefs['class_change_enabled'])}{mark('class_change_enabled')}（{names}）")
        holiday, reason = self.holidays.check(self.today(), self.store.state["holidays"])
        if self.store.state.get("paused"):
            lines.append("⏸ 管理员已全局暂停主动推送。")
        elif holiday:
            lines.append(f"⏸ 今天是{reason}，主动推送暂停。")
        lines.append("修改示例：/rci 提醒 关 课前 · /rci 提醒 当日 7:30 · /rci 提醒 次日 下课 · /rci 提醒 课前 15")
        return "\n".join(lines)

    async def update_reminders(self, user: ChatUser, changes: dict) -> str:
        binding = self.require_binding(user)
        prefs = binding.setdefault("prefs", {})
        for key, value in changes.items():
            if key == "reset":
                prefs.clear()
                continue
            prefs[key] = validate_reminder(key, value)
        await self.store.save()
        return "已更新。\n" + self.reminder_summary(user)

    # ---------- 角色 ----------

    def effective_role(self, session: dict, binding: dict | None) -> str:
        role = (session or {}).get("role") or ROLE_AUTO
        if role != ROLE_AUTO:
            return role
        if (session or {}).get("kind") == "group" or not binding:
            return ROLE_NONE
        kind = (binding.get("profile") or {}).get("roleKind")
        return {4: ROLE_HEAD, 5: ROLE_TEACHER}.get(kind, ROLE_NONE)

    # ---------- 调度 ----------

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.tick()
                self.last_tick_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 - 调度循环不能因单次错误退出
                self.last_tick_error = f"{type(ex).__name__}: {ex}"
                log.exception("RemoteCI 推送调度出错")
            await asyncio.sleep(30)

    async def refresh_holidays(self, force: bool = False) -> list[str]:
        if not self.store.state["holidays"].get("use_official", True):
            return []
        year = self.today().year
        return await self.holidays.refresh(self._session(), [year, year + 1] if self.today().month >= 11 else [year],
                                           force=force)

    def holiday_flags(self) -> tuple[bool, bool, str]:
        today = self.today()
        settings = self.store.state["holidays"]
        h_today, reason = self.holidays.check(today, settings)
        h_tomorrow, _ = self.holidays.check(today + timedelta(days=1), settings)
        return h_today, h_tomorrow, reason

    async def tick(self) -> None:
        if self.store.state.get("paused"):
            return
        # 换课申请需要及时处理，节假日也照常提醒。
        swap_dirty = await self.swaps.poll()
        await self.refresh_holidays()
        now = self.now()
        h_today, h_tomorrow, _ = self.holiday_flags()
        dirty = False
        for umo, session in list(self.store.sessions.items()):
            binding = self.store.binding_for(session.get("binding_key") or "")
            if not binding or binding.get("status") == "auth_failed":
                continue
            role = self.effective_role(session, binding)
            try:
                if session.get("kind") == "private" and role in (ROLE_TEACHER, ROLE_HEAD):
                    dirty |= await self._tick_teacher(now, umo, session, binding, role, h_today, h_tomorrow)
                elif session.get("kind") == "group" and role == ROLE_CLASS_GROUP:
                    dirty |= await self._tick_group(now, umo, session, binding, h_today, h_tomorrow)
            except RemoteCiError as ex:
                if ex.status not in (401, 404):
                    log.warning("RemoteCI 推送 %s 失败：%s", umo, ex)
        if dirty or swap_dirty:
            self.store.prune_sent()
            await self.store.save()

    async def _tick_teacher(self, now, umo, session, binding, role, h_today, h_tomorrow) -> bool:
        prefs = self.store.effective_reminders(binding)
        poll_due = time.time() - self._last_poll.get(umo, 0) >= self.poll_seconds
        days = await self.my_days(binding, ttl=self.poll_seconds)
        dirty = False

        push = session.get("push") or DEFAULT_SESSION_PUSH
        digest_days = days
        title_scope = f"t:{binding['key']}"
        if push.get("content") == "class" and session.get("class_id"):
            cls = next((c for c in binding["profile"].get("classes") or [] if c.get("id") == session["class_id"]), None)
            if cls:
                digest_days = await self.class_days(binding, cls, ttl=self.poll_seconds)
        dues = personal_due(now, prefs, days, title_scope, h_today, h_tomorrow)
        if digest_days is not days:
            # 当日/次日改推班级课表，课前提醒仍按个人日程。
            dues = [d for d in dues if d.kind == "before"] + digest_due(
                now=now, scope=title_scope, title="班级课表", days=digest_days,
                today_enabled=prefs["today_enabled"], today_time=prefs["today_time"],
                tomorrow_enabled=prefs["tomorrow_enabled"],
                tomorrow_at=_tomorrow_at(now, prefs, days), holiday_today=h_today, holiday_tomorrow=h_tomorrow,
                skip_empty=prefs["skip_empty_days"], show_class=False, show_teacher=True)
        for due in dues:
            if not self.store.was_sent(due.key):
                self.store.mark_sent(due.key)
                await self._push(umo, due.kind, due.text)
                dirty = True

        if poll_due:
            self._last_poll[umo] = time.time()
            if not h_today:
                dirty |= await self._detect_changes(
                    umo, f"my:{binding['key']}", days, enabled=prefs["change_enabled"],
                    title="【课程变动】你的课有调整", show_class=True, track_teacher=False)
                if role == ROLE_HEAD:
                    for cls in self.head_classes(session, binding):
                        cdays = await self.class_days(binding, cls, ttl=0)
                        dirty |= await self._detect_changes(
                            umo, f"class:{binding['key']}:{cls['id']}", cdays,
                            enabled=prefs["class_change_enabled"],
                            title=f"【班级课表变动】{cls.get('name')}", show_class=False, track_teacher=True)
        return dirty

    async def _detect_changes(self, umo, snap_key, days, *, enabled, title, show_class, track_teacher) -> bool:
        new = sch.to_snapshot(days)
        old = self.store.snapshots.get(snap_key)
        self.store.snapshots[snap_key] = new
        await self.store.save_snapshots()
        if old is None or not enabled:
            return False
        changes = sch.diff_snapshots(old, new, self.today().isoformat(), track_teacher=track_teacher)
        if not changes:
            return False
        await self._push(umo, "change", sch.format_changes(title, changes, self.today(), show_class=show_class))
        return True

    async def _tick_group(self, now, umo, session, binding, h_today, h_tomorrow) -> bool:
        push = session.get("push") or {}
        if not push.get("enabled") or not session.get("class_id"):
            return False
        cls = next((c for c in binding["profile"].get("classes") or [] if c.get("id") == session["class_id"]), None)
        if not cls:
            return False
        days = await self.class_days(binding, cls, ttl=self.poll_seconds)
        tomorrow_clock = sch.parse_clock(push.get("tomorrow_time"))
        dues = digest_due(
            now=now, scope=f"g:{umo}", title=f"{cls.get('name')}课表", days=days,
            today_enabled=push.get("today_enabled", True), today_time=push.get("today_time"),
            tomorrow_enabled=push.get("tomorrow_enabled", True),
            tomorrow_at=sch.combine(now.date(), tomorrow_clock, now.tzinfo) if tomorrow_clock else None,
            holiday_today=h_today, holiday_tomorrow=h_tomorrow, skip_empty=True, show_class=False, show_teacher=True)
        dirty = False
        for due in dues:
            if not self.store.was_sent(due.key):
                self.store.mark_sent(due.key)
                await self._push(umo, due.kind, due.text)
                dirty = True
        return dirty

    async def _push(self, umo: str, kind: str, text: str) -> bool:
        try:
            ok = await self._send(umo, text)
        except Exception as ex:  # noqa: BLE001
            log.warning("RemoteCI 主动消息发送失败 %s：%s", umo, ex)
            ok = False
        session = self.store.sessions.get(umo) or {}
        self.store.add_log(kind, session.get("name") or umo, text, ok)
        return ok

    # ---------- WebUI ----------

    def overview(self) -> dict:
        h_today, h_tomorrow, reason = self.holiday_flags()
        bindings = self.store.bindings.values()
        roles = [self.effective_role(s, self.store.binding_for(s.get("binding_key") or ""))
                 for s in self.store.sessions.values()]
        today_key = self.today().isoformat()
        sent_today = sum(1 for x in self.store.state["log"]
                         if datetime.fromtimestamp(x["at"], self._tz).date().isoformat() == today_key)
        return {
            "now": self.now().isoformat(timespec="minutes"),
            "paused": self.store.state.get("paused", False),
            "holiday_today": h_today, "holiday_tomorrow": h_tomorrow, "holiday_reason": reason,
            "bindings": len(self.store.bindings),
            "auth_failed": sum(1 for b in bindings if b.get("status") == "auth_failed"),
            "teachers": sum(1 for r in roles if r in (ROLE_TEACHER, ROLE_HEAD)),
            "class_groups": sum(1 for r in roles if r == ROLE_CLASS_GROUP),
            "sent_today": sent_today,
            "default_server": self.default_server,
            "last_error": self.last_tick_error,
            "official_data": self.holidays.has_year(self.today().year),
        }

    def binding_rows(self) -> list[dict]:
        rows = []
        for b in self.store.bindings.values():
            p = b.get("profile") or {}
            session = self.store.sessions.get(b.get("umo") or "", {})
            rows.append({
                "key": b["key"], "platform": b.get("platform"), "sender_id": b.get("sender_id"),
                "sender_name": b.get("sender_name"), "umo": b.get("umo"),
                "display_name": p.get("displayName"), "username": p.get("username"),
                "role_kind": ROLE_KIND_NAMES.get(p.get("roleKind"), "自定义角色"),
                "push_role": self.effective_role(session, b), "classes": [c.get("name") for c in p.get("classes") or []],
                "auth_type": (b.get("auth") or {}).get("type"), "server_url": b.get("server_url"),
                "status": b.get("status"), "status_message": b.get("status_message"),
                "has_prefs": bool(b.get("prefs")), "bound_at": b.get("bound_at"),
            })
        rows.sort(key=lambda r: r.get("bound_at") or 0, reverse=True)
        return rows

    def session_rows(self) -> list[dict]:
        rows = []
        for umo, s in self.store.sessions.items():
            b = self.store.binding_for(s.get("binding_key") or "")
            p = (b or {}).get("profile") or {}
            rows.append({
                "umo": umo, "kind": s.get("kind"), "name": s.get("name"), "platform": s.get("platform"),
                "role": s.get("role", ROLE_AUTO), "effective_role": self.effective_role(s, b),
                "binding_key": s.get("binding_key"),
                "binding_name": p.get("displayName") or p.get("username") or "",
                "class_id": s.get("class_id"), "watch_class_ids": s.get("watch_class_ids"),
                "classes": [{"id": c.get("id"), "name": c.get("name"), "roleKind": c.get("roleKind")}
                            for c in p.get("classes") or []],
                "push": s.get("push") or DEFAULT_SESSION_PUSH, "last_seen": s.get("last_seen"),
            })
        rows.sort(key=lambda r: r.get("last_seen") or 0, reverse=True)
        return rows

    async def update_session(self, umo: str, patch: dict) -> dict:
        session = self.store.sessions.get(umo)
        if session is None:
            raise ServiceError("会话不存在")
        if "role" in patch:
            if patch["role"] not in SESSION_ROLES:
                raise ServiceError("未知角色")
            if patch["role"] == ROLE_CLASS_GROUP and session.get("kind") != "group":
                raise ServiceError("只有群聊可以设为班级群")
            if patch["role"] in (ROLE_TEACHER, ROLE_HEAD) and session.get("kind") != "private":
                raise ServiceError("老师/班主任角色只能设置在个人会话上；群聊不做主动提醒")
            session["role"] = patch["role"]
        if "binding_key" in patch and session.get("kind") == "group":
            key = patch["binding_key"] or None
            if key and key not in self.store.bindings:
                raise ServiceError("绑定账号不存在")
            session["binding_key"] = key
        if "class_id" in patch:
            session["class_id"] = patch["class_id"] or None
        if "watch_class_ids" in patch:
            ids = patch["watch_class_ids"]
            session["watch_class_ids"] = list(ids) if ids else None
        if "push" in patch and isinstance(patch["push"], dict):
            push = session.setdefault("push", dict(DEFAULT_SESSION_PUSH))
            for key, value in patch["push"].items():
                if key not in DEFAULT_SESSION_PUSH:
                    continue
                if key.endswith("_time"):
                    value = sch.normalize_clock(str(value))
                    if not value:
                        raise ServiceError("时间格式不正确")
                elif key == "content":
                    if value not in ("personal", "class"):
                        raise ServiceError("推送内容只能是 personal 或 class")
                else:
                    value = bool(value)
                push[key] = value
        if session.get("kind") == "group" and session.get("role") == ROLE_CLASS_GROUP and session["push"].get("enabled"):
            if not session.get("binding_key") or not session.get("class_id"):
                raise ServiceError("开启班级群课表推送前，请先选择读取课表的绑定账号和班级")
        await self.store.save()
        return session

    async def delete_session(self, umo: str) -> None:
        self.store.sessions.pop(umo, None)
        await self.store.save()

    async def admin_unbind(self, key: str) -> None:
        binding = self.store.bindings.pop(key, None)
        if binding:
            await self.client.logout(binding)
            self._drop_cache(key)
            await self.store.save()

    async def admin_refresh(self, key: str) -> dict:
        binding = self.store.binding_for(key)
        if not binding:
            raise ServiceError("绑定不存在")
        try:
            await self._refresh_profile(binding)
        except RemoteCiError as ex:
            raise ServiceError(str(ex)) from ex
        self._drop_cache(key)
        await self.store.save()
        return binding["profile"]

    async def admin_reset_prefs(self, key: str) -> None:
        binding = self.store.binding_for(key)
        if binding:
            binding["prefs"] = {}
            await self.store.save()

    async def update_defaults(self, patch: dict) -> dict:
        validated = {k: validate_reminder(k, v) for k, v in patch.items() if k in REMINDER_KEYS}
        self.store.state["reminders"].update(validated)
        await self.store.save()
        return self.store.state["reminders"]

    async def update_holiday_settings(self, body: dict) -> dict:
        settings = self.store.state["holidays"]
        if "use_official" in body:
            settings["use_official"] = bool(body["use_official"])
        if "weekend_as_holiday" in body:
            settings["weekend_as_holiday"] = bool(body["weekend_as_holiday"])
        if "ranges" in body:
            ranges = []
            for item in body["ranges"] or []:
                start, end = str(item.get("start") or ""), str(item.get("end") or item.get("start") or "")
                try:
                    date.fromisoformat(start)
                    date.fromisoformat(end)
                except ValueError as ex:
                    raise ServiceError(f"假期日期格式不正确：{start} ~ {end}") from ex
                if end < start:
                    raise ServiceError(f"假期结束日期早于开始日期：{start} ~ {end}")
                ranges.append({"name": str(item.get("name") or "假期")[:40], "start": start, "end": end})
            settings["ranges"] = sorted(ranges, key=lambda x: x["start"])
        await self.store.save()
        return settings

    async def set_paused(self, paused: bool) -> bool:
        self.store.state["paused"] = paused
        await self.store.save()
        return paused

    async def preview(self, umo: str) -> str:
        """WebUI“立即推送测试”：把当前会话今天/明天的内容推送一次。"""
        session = self.store.sessions.get(umo)
        if not session:
            raise ServiceError("会话不存在")
        binding = self.store.binding_for(session.get("binding_key") or "")
        if not binding:
            raise ServiceError("该会话没有可用的绑定账号")
        today = self.today()
        if session.get("kind") == "group" or (session.get("push") or {}).get("content") == "class":
            cls = next((c for c in binding["profile"].get("classes") or [] if c.get("id") == session.get("class_id")), None)
            if not cls:
                raise ServiceError("请先为该会话选择班级")
            days = await self.class_days(binding, cls, ttl=0)
            title, show_class, show_teacher = f"{cls.get('name')}课表", False, True
        else:
            days = await self.my_days(binding, ttl=0)
            title, show_class, show_teacher = "我的日程", True, False
        parts = [sch.format_day(f"【测试推送 · {title}】", d.isoformat(), days.get(d.isoformat(), []), today,
                                show_class=show_class, show_teacher=show_teacher)
                 for d in (today, today + timedelta(days=1))]
        text = "\n\n".join(parts)
        ok = await self._push(umo, "test", text)
        if not ok:
            raise ServiceError("发送失败：平台未找到该会话，或机器人已不在该会话中")
        await self.store.save()
        return text


def validate_reminder(key: str, value: Any) -> Any:
    if key not in REMINDER_KEYS:
        raise ServiceError(f"未知的提醒设置：{key}")
    if key.endswith("_time") or key == "tomorrow_fallback":
        normalized = sch.normalize_clock(str(value))
        if not normalized:
            raise ServiceError(f"时间格式不正确：{value}，请写成 07:30 这样的格式。")
        return normalized
    if key == "before_minutes":
        try:
            minutes = int(value)
        except (TypeError, ValueError) as ex:
            raise ServiceError("课前提醒的提前分钟数需要是整数。") from ex
        if not 1 <= minutes <= 120:
            raise ServiceError("课前提醒的提前分钟数需要在 1~120 之间。")
        return minutes
    if key == "tomorrow_mode":
        if value not in ("last_class", "fixed"):
            raise ServiceError("tomorrow_mode 只能是 last_class 或 fixed。")
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes", "开", "开启", "打开")
    return bool(value)


def _tomorrow_at(now, prefs, days):
    return tomorrow_trigger(now, prefs, days.get(now.date().isoformat(), []))


def _load_tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - Windows 缺 tzdata 时退回固定东八区
        from datetime import timezone
        return timezone(timedelta(hours=8), name="UTC+8")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _humanize_minutes(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {mins} 分钟" if mins else "")
    return f"{hours // 24} 天"


def _subject_name(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("name") or value.get("subject") or ""
    return str(value)
