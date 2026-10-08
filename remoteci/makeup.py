"""调休补课：查询 RemoteCI 服务端的调休安排，管理员可修改某个调休上学日补哪天的课。

服务端接口：
- GET    /api/holidays
- PUT    /api/admin/holidays/overrides/{date}   {"followWeekday": 1-5 | null}
- DELETE /api/admin/holidays/overrides/{date}   恢复自动推算
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from .client import RemoteCiError

if TYPE_CHECKING:  # pragma: no cover
    from .service import ChatUser, RemoteCiService

WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五"}
DAY_NAMES = "一二三四五六日"
SOURCE_TEXT = {"auto": "自动推算", "manual": "手动指定", "skip": "手动指定", "unresolved": "无法自动推算，需管理员指定"}
WEEKDAY_WORDS = {f"{prefix}{name}": i for i, name in enumerate("一二三四五", 1) for prefix in ("周", "星期", "礼拜")}
WEEKDAY_WORDS.update({str(i): i for i in range(1, 6)})
SKIP_WORDS = {"不补课", "不上课", "跳过", "skip"}
AUTO_WORDS = {"自动", "恢复", "auto"}

MAKEUP_HELP = """调休补课
/rci 调休   近期假期与调休补课安排
/rci 调休 <YYYY-MM-DD> <周一…周五|不补课|自动>   修改补课安排（管理员）"""


def _weekday_label(iso: str) -> str:
    try:
        return "周" + DAY_NAMES[date.fromisoformat(iso).weekday()]
    except ValueError:
        return ""


def format_overview(data: dict) -> str:
    lines = []
    if not data.get("enabled"):
        lines.append("调休自动适配已关闭，教室课表不会随节假日自动调整。")
    periods = data.get("periods") or []
    if not periods:
        lines.append("近期没有法定假期或调休安排。")
    for period in periods:
        head = period.get("name") or "假期"
        if period.get("offStart"):
            head += f"：{period['offStart']} 至 {period['offEnd']} 放假"
        lines.append(head)
        for item in period.get("makeupDays") or []:
            follow = WEEKDAY_NAMES.get(item.get("followWeekday"))
            source = item.get("followSource")
            arrangement = f"上{follow}的课" if follow else ("不补课" if source == "skip" else "未确定补哪天的课")
            lines.append(f"  · {item['date']}（{_weekday_label(item['date'])}）调休上学，{arrangement}"
                         f"（{SOURCE_TEXT.get(source, '')}）")
    if stale := data.get("staleOverrideDates"):
        lines.append("已失效的手动安排：" + "、".join(stale))
    if error := (data.get("status") or {}).get("lastError"):
        lines.append(f"⚠ 最近一次刷新节假日数据失败：{error}")
    return "\n".join(lines)


def parse_follow(word: str) -> tuple[str, int | None]:
    value = (word or "").strip().lower()
    if value in AUTO_WORDS:
        return "auto", None
    if value in SKIP_WORDS:
        return "skip", None
    if value in WEEKDAY_WORDS:
        return "weekday", WEEKDAY_WORDS[value]
    raise ValueError(word)


async def list_makeup(service: "RemoteCiService", binding: dict) -> str:
    return format_overview(await service._call(binding, "GET", "/api/holidays"))


async def set_makeup(service: "RemoteCiService", binding: dict, day: str, follow: str) -> str:
    try:
        iso = date.fromisoformat((day or "").strip()).isoformat()
    except ValueError:
        return "日期格式应为 YYYY-MM-DD，例如 2026-10-10。"
    try:
        mode, weekday = parse_follow(follow)
    except ValueError:
        return "补课安排只能是 周一…周五、不补课 或 自动。"
    path = f"/api/admin/holidays/overrides/{iso}"
    if mode == "auto":
        data = await service._call(binding, "DELETE", path)
    else:
        data = await service._call(binding, "PUT", path, body={"followWeekday": weekday})
    return "补课安排已更新。\n" + format_overview(data)


async def run_makeup_command(service: "RemoteCiService", user: "ChatUser", args: list[str]) -> str:
    binding = service.require_binding(user)
    try:
        if not args:
            return await list_makeup(service, binding)
        if len(args) != 2:
            return MAKEUP_HELP
        return await set_makeup(service, binding, args[0], args[1])
    except RemoteCiError as ex:
        return f"RemoteCI 请求失败：{ex}"
