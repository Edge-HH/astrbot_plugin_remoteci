"""`/rci` 指令系统：解析参数并分派到 RemoteCiService。中英文子命令等价。"""

from __future__ import annotations

from .service import ChatUser, RemoteCiService, ServiceError
from .swaps import run_swap_command

COMMAND_NAMES = ("rci", "remoteci", "课表")

HELP = """RemoteCI 指令（/rci 也可写作 /课表）
━ 连接（请私聊）━
/rci 绑定 <API Key> [服务器]   用 API Key 连接
/rci 登录 <用户名> <密码> [服务器]   用账号密码连接
/rci 我   查看已连接的账号
/rci 解绑   断开连接
━ 查询 ━
/rci 今天 | 明天 | 本周 | 周三 | 10-05   我的日程（老师/班主任）
/rci 下节   正在上和下一节课
/rci 班级 [班级名] [日期]   班级课表
/rci 状态 [班级名]   教室当前课堂状态
/rci 班级列表   可访问的班级
/rci 假期   今天是否因节假日暂停推送
━ 主动提醒（老师/班主任，仅私聊推送）━
/rci 提醒   查看提醒设置
/rci 提醒 开|关 <当日|次日|课前|换课|班级换课|全部>
/rci 提醒 当日 07:30   修改当日日程推送时间
/rci 提醒 次日 20:00   次日日程改为固定时间
/rci 提醒 次日 下课   次日日程在自己最后一节课下课时推送
/rci 提醒 课前 15   课前提前 15 分钟提醒
/rci 提醒 重置   恢复管理员的统一设置
━ 换课申请（老师/班主任）━
/rci 换课 待办 | 我的   查看发给我的 / 我发起的换课申请
/rci 换课 通过|拒绝 <编号> [备注]
/rci 换课 撤回 <编号>   撤回别人对我的强制换课
发起换课请在 RemoteCI WebUI 或手机 App 的“换课”页；有新申请时会主动私聊提醒。
━ 控制 ━
/rci 通知 <班级名> <内容>   向教室发送通知
/rci 老师来了 <班级名>
更多操作（换课、音量、广播、系统管理等）直接用自然语言告诉我即可。
群聊中请 @我 后再发指令；群聊不做个人主动提醒。"""

SUBCOMMANDS = {
    "help": "help", "帮助": "help", "?": "help", "？": "help",
    "bind": "bind", "绑定": "bind", "key": "bind",
    "login": "login", "登录": "login",
    "unbind": "unbind", "解绑": "unbind", "logout": "unbind", "退出": "unbind",
    "me": "me", "我": "me", "账号": "me", "whoami": "me",
    "next": "next", "下节": "next", "下一节": "next", "下节课": "next",
    "class": "class", "班级": "class", "班级课表": "class",
    "state": "state", "状态": "state",
    "classes": "classes", "班级列表": "classes",
    "remind": "remind", "提醒": "remind", "reminder": "remind",
    "notify": "notify", "通知": "notify",
    "coming": "coming", "老师来了": "coming",
    "holiday": "holiday", "假期": "holiday", "节假日": "holiday",
    "swap": "swap", "换课": "swap", "换课申请": "swap", "调课": "swap",
}

REMIND_ITEMS = {
    "当日": ["today_enabled"], "今日": ["today_enabled"], "today": ["today_enabled"],
    "次日": ["tomorrow_enabled"], "明日": ["tomorrow_enabled"], "tomorrow": ["tomorrow_enabled"],
    "课前": ["before_enabled"], "before": ["before_enabled"],
    "换课": ["change_enabled"], "change": ["change_enabled"], "调课": ["change_enabled"],
    "班级换课": ["class_change_enabled"], "class": ["class_change_enabled"], "班级": ["class_change_enabled"],
    "全部": ["today_enabled", "tomorrow_enabled", "before_enabled", "change_enabled", "class_change_enabled"],
    "all": ["today_enabled", "tomorrow_enabled", "before_enabled", "change_enabled", "class_change_enabled"],
}
ON_WORDS = {"开", "开启", "打开", "on", "enable"}
OFF_WORDS = {"关", "关闭", "off", "disable"}
LAST_CLASS_WORDS = {"下课", "放学", "最后一节", "最后一节课", "last", "last_class", "课后"}


def strip_command(text: str) -> list[str]:
    tokens = (text or "").strip().split()
    if tokens:
        head = tokens[0].lstrip("/").lower()
        if head in COMMAND_NAMES:
            tokens = tokens[1:]
    return tokens


async def run_command(service: RemoteCiService, user: ChatUser, text: str) -> str:
    tokens = strip_command(text)
    if not tokens:
        return HELP
    head = tokens[0].lower()
    action = SUBCOMMANDS.get(head)
    args = tokens[1:]
    try:
        if action is None:
            # “/rci 明天”“/rci 周三”“/rci 10-05” 直接查我的日程
            return await service.query_my_schedule(user, " ".join(tokens))
        if action == "help":
            return HELP
        if action in ("bind", "login") and not user.is_private:
            return "为保护账号安全，请私聊我完成绑定；如果刚才在群里发送了密码或 API Key，请立即撤回。"
        if action == "bind":
            if not args:
                return "用法：/rci 绑定 <API Key> [服务器地址]"
            return await service.bind_api_key(user, args[0], args[1] if len(args) > 1 else "")
        if action == "login":
            if len(args) < 2:
                return "用法：/rci 登录 <用户名> <密码> [服务器地址]"
            return await service.bind_password(user, args[0], args[1], args[2] if len(args) > 2 else "")
        if action == "unbind":
            return await service.unbind(user)
        if action == "me":
            return service.describe_me(user)
        if action == "next":
            return await service.query_next(user)
        if action == "class":
            class_ref, day = _split_class_and_day(service, args)
            return await service.query_class_schedule(user, class_ref, day)
        if action == "state":
            return await service.query_state(user, " ".join(args) or None)
        if action == "classes":
            binding = service.require_binding(user)
            classes = binding.get("profile", {}).get("classes") or []
            return "你可访问的班级：\n" + "\n".join(
                f"· {c.get('name')}" + ("（班主任）" if c.get("roleKind") == 4 else "") for c in classes) if classes else "没有可访问的班级。"
        if action == "holiday":
            h_today, h_tomorrow, reason = service.holiday_flags()
            if service.store.state.get("paused"):
                return "管理员已全局暂停主动推送。"
            return (f"今天：{reason or '节假日'}，主动推送暂停。" if h_today else
                    "今天正常推送" + (f"（{reason}）" if reason else "") + "。") + ("明天放假，不推送次日日程。" if h_tomorrow else "")
        if action == "remind":
            return await _remind(service, user, args)
        if action == "notify":
            if len(args) < 2:
                return "用法：/rci 通知 <班级名> <内容>"
            return await service.notify(user, args[0], " ".join(args[1:]))
        if action == "swap":
            return await run_swap_command(service, user, args)
        if action == "coming":
            return await service.send_command(user, " ".join(args) or None, 8, None, confirmed=True)
    except ServiceError as ex:
        return str(ex)
    return HELP


def _split_class_and_day(service: RemoteCiService, args: list[str]) -> tuple[str | None, str | None]:
    """最后一个参数能识别为日期时当作日期，其余当作班级名。"""
    if not args:
        return None, None
    try:
        service.parse_day(args[-1])
        return (" ".join(args[:-1]) or None), args[-1]
    except ServiceError:
        return " ".join(args), None


async def _remind(service: RemoteCiService, user: ChatUser, args: list[str]) -> str:
    if not args:
        return service.reminder_summary(user)
    first = args[0].lower()
    if first in ("重置", "reset", "默认"):
        return await service.update_reminders(user, {"reset": True})
    if first in ON_WORDS | OFF_WORDS:
        item = args[1] if len(args) > 1 else "全部"
        keys = REMIND_ITEMS.get(item.lower()) or REMIND_ITEMS.get(item)
        if not keys:
            return "可开关的项目：当日、次日、课前、换课、班级换课、全部"
        return await service.update_reminders(user, {k: first in ON_WORDS for k in keys})
    if first in ("当日", "今日", "today") and len(args) > 1:
        if args[1] in ON_WORDS | OFF_WORDS:
            return await service.update_reminders(user, {"today_enabled": args[1] in ON_WORDS})
        return await service.update_reminders(user, {"today_time": args[1], "today_enabled": True})
    if first in ("次日", "明日", "tomorrow") and len(args) > 1:
        value = args[1]
        if value in ON_WORDS | OFF_WORDS:
            return await service.update_reminders(user, {"tomorrow_enabled": value in ON_WORDS})
        if value.lower() in LAST_CLASS_WORDS:
            return await service.update_reminders(user, {"tomorrow_mode": "last_class", "tomorrow_enabled": True})
        return await service.update_reminders(user, {"tomorrow_mode": "fixed", "tomorrow_time": value,
                                                     "tomorrow_enabled": True})
    if first in ("备用", "fallback") and len(args) > 1:
        return await service.update_reminders(user, {"tomorrow_fallback": args[1]})
    if first in ("课前", "before") and len(args) > 1:
        value = args[1].removesuffix("分钟").removesuffix("分")
        if value in ON_WORDS | OFF_WORDS:
            return await service.update_reminders(user, {"before_enabled": value in ON_WORDS})
        if not value.isdigit():
            return "用法：/rci 提醒 课前 <分钟数>"
        return await service.update_reminders(user, {"before_minutes": int(value), "before_enabled": True})
    return "没看懂这条提醒设置。\n" + HELP.split("━ 主动提醒")[1].split("━ 控制")[0].strip()
