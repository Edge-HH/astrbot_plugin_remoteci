"""集控运维指令：设备状态、课时统计、操作记录、自动化规则、应用守护。

服务端接口（权限由服务端判定，403 时给出可读说明）：
- GET  /api/devices/health                       设备在线、版本与档案偏离（系统管理员看全部，班主任看本班）
- GET  /api/me/lesson-stats?from&to             本人课时；GET /api/lesson-stats?teacher=&from&to 查询他人（系统管理员）
- GET  /api/audit?classId&limit=10              最近的控制命令记录（系统管理员、班主任）
- GET  /api/automation/rules                     自动化规则（系统管理员）；POST …/{id}/run 立即运行；
  GET/PUT …/{id} 读取、整体保存（开关规则时先读完整规则，只改 enabled）
- GET/PUT /api/settings/app-guard                应用守护全局策略（系统管理员，PUT 整体保存）
- GET  /api/app-guard/events?limit=              最近守护事件（系统管理员）
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING

from .client import RemoteCiError
from .service import ServiceError

if TYPE_CHECKING:  # pragma: no cover
    from .service import ChatUser, RemoteCiService

LESSON_HELP = "用法：/rci 课时 [本月|上月|<开始日期> <结束日期>] [教师名]，日期格式 2026-10-01；教师名仅系统管理员可用。"
AUTOMATION_HELP = """自动化规则（系统管理员）
/rci 自动化   列出规则
/rci 自动化 运行 <规则名>   立即对全部目标班级执行一次
/rci 自动化 开|关 <规则名>   启用或停用规则"""
AUTOMATION_SUBCOMMANDS = {
    "列表": "list", "list": "list", "查看": "list",
    "运行": "run", "执行": "run", "run": "run",
    "开": "enable", "开启": "enable", "启用": "enable", "on": "enable", "enable": "enable",
    "关": "disable", "关闭": "disable", "停用": "disable", "off": "disable", "disable": "disable",
}
APP_GUARD_HELP = """应用守护（系统管理员，全局策略；单台设备请在 WebUI“应用守护”页设置）
/rci 守护   查看策略与最近守护事件
/rci 守护 开|关   开启或关闭应用守护
/rci 守护 延时 <分钟>   开启退出自启并设置延时，0 为立即"""
APP_GUARD_SUBCOMMANDS = {
    "查看": "show", "状态": "show", "show": "show",
    "开": "enable", "开启": "enable", "启用": "enable", "on": "enable", "enable": "enable",
    "关": "disable", "关闭": "disable", "停用": "disable", "off": "disable", "disable": "disable",
    "延时": "delay", "延迟": "delay", "delay": "delay",
}
APP_GUARD_EVENT_LIMIT = 5
APP_GUARD_MAX_DELAY = 1440
THIS_MONTH, LAST_MONTH = {"本月", "这个月", "当月"}, {"上月", "上个月"}
AUDIT_LIMIT = 10
LESSON_TEACHER_LIMIT = 30


def _missing(feature: str) -> str:
    return f"RemoteCI 服务器暂不支持{feature}，请先升级服务端。"


def _parse_time(value: str | None, tz: tzinfo | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(tz) if tz and parsed.tzinfo else parsed


def _clock(value: str | None, tz: tzinfo | None) -> str:
    parsed = _parse_time(value, tz)
    return parsed.strftime("%m-%d %H:%M") if parsed else "—"


def _duration(start: str | None, now: datetime) -> str:
    parsed = _parse_time(start, now.tzinfo)
    if not parsed:
        return ""
    minutes = max(0, int((now - parsed).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    return f"{hours} 小时" if hours < 48 else f"{hours // 24} 天"


# ---------- 设备状态 ----------

def format_devices(items: list[dict], now: datetime) -> str:
    if not items:
        return "没有可查看的教室设备。"
    offline = [x for x in items if not x.get("online")]
    outdated = [x for x in items if x.get("outdated")]
    drifted = [x for x in items if (x.get("profileDriftCount") or 0) > 0]
    head = f"设备状态：共 {len(items)} 台，在线 {len(items) - len(offline)} 台"
    head += f"，离线 {len(offline)} 台" if offline else ""
    head += f"，待升级 {len(outdated)} 台" if outdated else ""
    lines = [head]

    def name(x):
        return " ".join(v for v in (x.get("className"), x.get("deviceName")) if v) or "未命名设备"

    if offline:
        lines.append("离线：")
        for x in offline:
            since = _duration(x.get("offlineSince") or x.get("lastSeenAt"), now)
            lines.append(f"· {name(x)}" + (f"  已离线 {since}" if since else "")
                         + (f"（最后在线 {_clock(x.get('lastSeenAt'), now.tzinfo)}）" if x.get("lastSeenAt") else "（从未上线）"))
    if outdated:
        lines.append("待升级：")
        lines.extend(f"· {name(x)}  插件 {x.get('pluginVersion') or '未知'} · ClassIsland {x.get('classIslandVersion') or '未知'}"
                     for x in outdated)
    if drifted:
        lines.append("档案与服务端不一致：")
        lines.extend(f"· {name(x)}  {x['profileDriftCount']} 处" for x in drifted)
    if not offline and not outdated:
        lines.append("全部在线，插件均为最新版本。")
    return "\n".join(lines)


async def run_device_command(service: "RemoteCiService", user: "ChatUser", args: list[str]) -> str:
    binding = service.require_binding(user)
    try:
        items = await service._call(binding, "GET", "/api/devices/health") or []
    except RemoteCiError as ex:
        if ex.status == 403:
            return "只有系统管理员和班主任可以查看设备状态。"
        if ex.status == 404:
            return _missing("设备状态")
        return f"RemoteCI 请求失败：{ex}"
    return format_devices(items if isinstance(items, list) else [], service.now())


# ---------- 课时统计 ----------

def _month_range(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def parse_period(args: list[str], today: date) -> tuple[date, date, list[str]]:
    """解析“本月”“上月”“2026-10-01 2026-10-31”，返回 (开始, 结束, 剩余参数)；默认本月 1 日到今天。"""
    if args and args[0] in LAST_MONTH:
        last = today.replace(day=1) - timedelta(days=1)
        return (*_month_range(last.year, last.month), args[1:])
    if args and args[0] in THIS_MONTH:
        return today.replace(day=1), today, args[1:]
    if len(args) >= 2:
        try:
            start, end = date.fromisoformat(args[0]), date.fromisoformat(args[1])
        except ValueError:
            start = end = None
        if start and end:
            if end < start:
                raise ServiceError("结束日期不能早于开始日期。")
            if (end - start).days > 366:
                raise ServiceError("统计区间最长一年。")
            return start, end, args[2:]
    if args:
        try:
            date.fromisoformat(args[0])
        except ValueError:
            pass
        else:
            raise ServiceError(LESSON_HELP)
    return today.replace(day=1), today, args


def format_lesson_stats(data: dict, teacher: str = "") -> str:
    title = f"课时统计（{data.get('from') or ''} 至 {data.get('to') or ''}）"
    teachers = data.get("teachers") or []
    if not teachers:
        return title + "\n没有课时记录。" + ("请确认教师名与课表中的任课教师一致。" if teacher else "")
    lines = [title]
    for t in teachers[:LESSON_TEACHER_LIMIT]:
        extra = "，".join(x for x in (f"代课 {t['substitute']} 节" if t.get("substitute") else "",
                                      f"被代 {t['substituted']} 节" if t.get("substituted") else "") if x)
        lines.append(f"{t.get('teacher') or '未知教师'}：共 {t.get('total', 0)} 节" + (f"（{extra}）" if extra else ""))
        classes = [f"{c.get('className')} {c.get('total', 0)} 节" for c in t.get("classes") or []]
        if classes:
            lines.append("  " + "、".join(classes))
    if len(teachers) > LESSON_TEACHER_LIMIT:
        lines.append(f"…另有 {len(teachers) - LESSON_TEACHER_LIMIT} 位老师，请在 RemoteCI WebUI 查看完整统计。")
    return "\n".join(lines)


async def run_lesson_command(service: "RemoteCiService", user: "ChatUser", args: list[str]) -> str:
    binding = service.require_binding(user)
    start, end, rest = parse_period(args, service.today())
    teacher = " ".join(rest).strip()
    params = {"from": start.isoformat(), "to": end.isoformat()}
    if teacher:
        path, params["teacher"] = "/api/lesson-stats", teacher
    else:
        path = "/api/me/lesson-stats"
    try:
        data = await service._call(binding, "GET", path, params=params) or {}
    except RemoteCiError as ex:
        if ex.status == 403:
            return "只有系统管理员可以查询其他老师的课时；查询自己的课时请去掉教师名。" if teacher else f"RemoteCI 请求失败：{ex}"
        if ex.status == 404:
            return _missing("课时统计")
        return f"RemoteCI 请求失败：{ex}"
    return format_lesson_stats(data if isinstance(data, dict) else {}, teacher)


# ---------- 操作记录 ----------

def format_audit(data: dict, tz: tzinfo | None, class_name: str = "") -> str:
    items = (data or {}).get("items") or []
    if not items:
        return "没有操作记录。" if not class_name else f"{class_name}没有操作记录。"
    lines = [f"最近 {min(len(items), AUDIT_LIMIT)} 条操作记录" + (f"（{class_name}）" if class_name else "") + "："]
    for x in items[:AUDIT_LIMIT]:
        source = x.get("sourceLabel") or x.get("source") or ""
        if x.get("sourceDetail"):
            source += f"·{x['sourceDetail']}"
        where = " ".join(v for v in (x.get("className"), x.get("deviceName")) if v)
        what = x.get("summary") or x.get("commandLabel") or f"命令 {x.get('command')}"
        result = {True: "✅", False: "❌"}.get(x.get("success"), "⏳")
        if x.get("success") is False and x.get("resultMessage"):
            result += f" {x['resultMessage']}"
        lines.append(f"· {_clock(x.get('createdAt'), tz)} {x.get('actorName') or '系统'}"
                     + (f"（{source}）" if source else "") + (f" {where}" if where else "") + f" {what} {result}")
    return "\n".join(lines)


async def run_audit_command(service: "RemoteCiService", user: "ChatUser", args: list[str]) -> str:
    binding = service.require_binding(user)
    cls = service.resolve_class(binding, " ".join(args)) if args else None
    params = {"limit": AUDIT_LIMIT}
    if cls:
        params["classId"] = cls.get("id")
    try:
        data = await service._call(binding, "GET", "/api/audit", params=params) or {}
    except RemoteCiError as ex:
        if ex.status == 403:
            return "只有系统管理员和班主任可以查看操作记录。"
        if ex.status == 404:
            return _missing("操作记录")
        return f"RemoteCI 请求失败：{ex}"
    return format_audit(data if isinstance(data, dict) else {}, service.now().tzinfo, (cls or {}).get("name") or "")


# ---------- 自动化规则 ----------

def format_rules(rules: list[dict], tz: tzinfo | None) -> str:
    if not rules:
        return "还没有自动化规则，可在 RemoteCI WebUI 的“自动化”页创建。"
    lines = [f"自动化规则（{len(rules)} 条）："]
    for r in rules:
        state = "启用" if r.get("enabled") else "停用"
        last = ""
        if r.get("lastRunAt"):
            last = f"，上次 {_clock(r.get('lastRunAt'), tz)}"
            if r.get("lastResult"):
                last += f"：{r['lastResult']}"
        lines.append(f"· [{state}] {r.get('name') or '未命名'}{last}")
    lines.append("立即运行：/rci 自动化 运行 <规则名>；开关：/rci 自动化 开|关 <规则名>")
    return "\n".join(lines)


def find_rule(rules: list[dict], name: str) -> dict:
    """按完整名称、再按唯一的名称片段查找规则。"""
    exact = [r for r in rules if (r.get("name") or "") == name]
    if exact:
        return exact[0]
    partial = [r for r in rules if name in (r.get("name") or "")]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise ServiceError("匹配到多条规则：" + "、".join(r.get("name") or "" for r in partial) + "，请写完整名称。")
    raise ServiceError(f"找不到规则“{name}”。现有规则：" + ("、".join(r.get("name") or "" for r in rules) or "无"))


def format_run_result(rule: dict, runs) -> str:
    name = rule.get("name") or ""
    if not isinstance(runs, list):
        return f"已运行自动化规则“{name}”。"
    if not runs:
        return f"规则“{name}”的目标班级为空，没有执行。"
    failed = [x for x in runs if x.get("status") != "Succeeded"]
    lines = [f"已运行“{name}”：{len(runs)} 个班级，成功 {len(runs) - len(failed)} 个"
             + (f"，失败 {len(failed)} 个" if failed else "") + "。"]
    lines.extend(f"· {x.get('className') or ''} {x.get('statusLabel') or x.get('status') or ''}"
                 + (f"：{x['message']}" if x.get("message") else "") for x in failed)
    return "\n".join(lines)


async def run_automation_command(service: "RemoteCiService", user: "ChatUser", args: list[str]) -> str:
    binding = service.require_binding(user)
    action = AUTOMATION_SUBCOMMANDS.get((args[0] if args else "列表").lower())
    if action is None:
        return AUTOMATION_HELP
    try:
        rules = await service._call(binding, "GET", "/api/automation/rules") or []
        if action == "list":
            return format_rules(rules, service.now().tzinfo)
        name = " ".join(args[1:]).strip()
        if not name:
            return f"用法：/rci 自动化 {args[0]} <规则名>"
        rule = find_rule(rules, name)
        path = f"/api/automation/rules/{rule.get('id')}"
        if action == "run":
            return format_run_result(rule, await service._call(binding, "POST", path + "/run", body={}))
        enabled = action == "enable"
        if bool(rule.get("enabled")) == enabled:
            return f"规则“{rule.get('name')}”已经是{'启用' if enabled else '停用'}状态。"
        # PUT 是整体保存：先读完整规则（含触发条件、动作、目标），只改 enabled。
        full = await service._call(binding, "GET", path)
        await service._call(binding, "PUT", path, body={**(full if isinstance(full, dict) else rule), "enabled": enabled})
        return f"已{'启用' if enabled else '停用'}自动化规则“{rule.get('name')}”。"
    except RemoteCiError as ex:
        if ex.status == 403:
            return "只有系统管理员可以管理自动化规则。"
        if ex.status == 404:
            return _missing("自动化规则") if action == "list" else f"RemoteCI 请求失败：{ex}"
        return f"RemoteCI 请求失败：{ex}"


# ---------- 应用守护 ----------

def describe_app_guard(policy: dict) -> str:
    if not policy.get("enabled"):
        return "未启用"
    parts = []
    if policy.get("restartAfterExit"):
        delay = int(policy.get("restartAfterExitDelayMinutes") or 0)
        parts.append(f"退出 {delay} 分钟后自启" if delay > 0 else "退出后立即自启")
    if policy.get("restartOnCrash"):
        parts.append("意外退出时重启")
    if policy.get("preventDeletion"):
        parts.append(f"禁止删除（每 {policy.get('backupIntervalHours') or 24} 小时备份）")
    return " / ".join(parts) or "已启用（未开启任何守护项）"


def format_app_guard(policy: dict, events: list[dict], tz: tzinfo | None) -> str:
    lines = [f"应用守护：{describe_app_guard(policy)}",
             f"恢复提示的管理方：{policy.get('organizationName') or 'RemoteCI'}"]
    if events:
        lines.append("最近守护事件：")
        for x in events[:APP_GUARD_EVENT_LIMIT]:
            at = datetime.fromtimestamp((x.get("atUnixMs") or 0) / 1000, tz).strftime("%m-%d %H:%M")
            detail = f"：{x['detail']}" if x.get("detail") else ""
            lines.append(f"· {at} {x.get('deviceName') or ''} {x.get('kindName') or x.get('kind') or ''}{detail}")
    else:
        lines.append("暂无守护事件。")
    lines.append("开关：/rci 守护 开|关；退出自启延时：/rci 守护 延时 <分钟>")
    return "\n".join(lines)


async def run_app_guard_command(service: "RemoteCiService", user: "ChatUser", args: list[str]) -> str:
    binding = service.require_binding(user)
    action = APP_GUARD_SUBCOMMANDS.get((args[0] if args else "查看").lower())
    if action is None:
        return APP_GUARD_HELP
    try:
        policy = await service._call(binding, "GET", "/api/settings/app-guard") or {}
        if action == "show":
            events = await service._call(binding, "GET", "/api/app-guard/events",
                                         params={"limit": APP_GUARD_EVENT_LIMIT}) or []
            return format_app_guard(policy, events if isinstance(events, list) else [], service.now().tzinfo)
        # PUT 是整体保存：在读到的完整策略上只改需要改的字段。
        if action == "delay":
            try:
                minutes = int(args[1]) if len(args) > 1 else -1
            except ValueError:
                minutes = -1
            if not 0 <= minutes <= APP_GUARD_MAX_DELAY:
                return f"用法：/rci 守护 延时 <分钟>，范围 0–{APP_GUARD_MAX_DELAY}，0 为立即。"
            changed = {**policy, "enabled": True, "restartAfterExit": True, "restartAfterExitDelayMinutes": minutes}
        else:
            enabled = action == "enable"
            if bool(policy.get("enabled")) == enabled:
                return f"应用守护已经是{'开启' if enabled else '关闭'}状态。"
            changed = {**policy, "enabled": enabled}
        saved = await service._call(binding, "PUT", "/api/settings/app-guard", body=changed) or changed
        return f"已保存全局应用守护：{describe_app_guard(saved)}。在线教室电脑已立即生效。"
    except RemoteCiError as ex:
        if ex.status == 403:
            return "只有系统管理员可以管理应用守护。"
        if ex.status == 404:
            return _missing("应用守护")
        return f"RemoteCI 请求失败：{ex}"
