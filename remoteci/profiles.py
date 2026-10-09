"""服务端档案：查看档案库，从教室电脑收集，把已保存的档案下发到教室电脑（系统管理员或本班班主任）。

服务端接口：
- GET  /api/profiles                 管理员得到全部档案，班主任得到可管理班级的档案
- POST /api/profiles/collect         {classIds}，读取教室电脑当前档案（含临时层），只返回不保存
- PUT  /api/profiles                 {items:[{id,classId,sourceTemplateId,revision,name,profileJson}]}
- POST /api/profiles/apply           {items:[{id,revision}], mode, sections, classIds, confirmReplace, importProfileName,
                                      replaceExistingTempLayers}
档案内容的编辑需要逐节操作，留在 WebUI“档案管理”页完成；聊天里只做查看、收集与下发。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .client import RemoteCiError

if TYPE_CHECKING:  # pragma: no cover
    from .service import ChatUser, RemoteCiService

MODES = {
    "更新": 1, "合并": 1, "更新当前档案": 1, "merge": 1,
    "替换": 2, "整体替换": 2, "replace": 2,
    "新建": 3, "创建": 3, "新建并启用": 3, "create": 3,
    "临时层": 4, "作为临时层下发": 4, "temp": 4, "overlay": 4,
}
MODE_TEXT = {1: "更新当前档案", 2: "整体替换所选类别", 3: "创建并启用新档案", 4: "作为临时层下发"}
SECTIONS = {"时间表": 1, "课表": 2, "科目": 4, "全部": 7, "all": 7}
REPLACE_WORDS = ("替换", "替换同日", "replace")

PROFILE_HELP = """服务端档案（管理员或本班班主任）
/rci 档案   查看服务端档案库
/rci 档案 收集 [班级名…] [保存]   读取教室电脑当前档案（含临时层），加“保存”则存为该班服务端档案
/rci 档案 下发 <档案名> <更新|新建|临时层> [班级名…]   下发到教室电脑（类别默认全部）
/rci 档案 下发 <档案名> 临时层 [班级名…] 替换   同一天已有临时层时明确替换
编辑档案内容请在 RemoteCI WebUI 的“档案管理”页完成。"""


def _class_names(binding: dict) -> dict[str, str]:
    return {c.get("id"): c.get("name", "") for c in (binding.get("profile") or {}).get("classes") or []}


def format_profiles(rows: list[dict], binding: dict) -> str:
    if not rows:
        return "服务端档案库为空，或你没有可管理的班级档案。"
    names = _class_names(binding)
    lines = ["服务端档案："]
    for row in rows:
        owner = "全局模板" if not row.get("classId") else names.get(row["classId"], "班级档案")
        lines.append(f"· {row.get('name')}（{owner}） 时间表 {row.get('timeLayoutCount', 0)}、课表 "
                     f"{row.get('classPlanCount', 0)}、科目 {row.get('subjectCount', 0)}，修订 {row.get('revision')}")
    return "\n".join(lines)


def parse_mode(word: str) -> int:
    value = (word or "").strip().lower()
    if value in MODES:
        return MODES[value]
    raise ValueError("应用方式只能是：更新（合并到当前档案）、替换（整体替换所选类别）、新建（创建并启用新档案）"
                     "或 临时层（只下发按日期安排的临时层）。")


def parse_sections(words: list[str] | str | None) -> int:
    if not words:
        return 7
    if isinstance(words, str):
        words = words.replace("，", ",").replace("、", ",").replace(",", " ").split()
    mask = 0
    for word in words:
        if word not in SECTIONS:
            raise ValueError("档案类别只能是：时间表、课表、科目 或 全部。")
        mask |= SECTIONS[word]
    return mask


def find_profile(rows: list[dict], ref: str) -> dict:
    ref = (ref or "").strip()
    exact = [r for r in rows if r.get("id") == ref or r.get("name") == ref]
    if len(exact) == 1:
        return exact[0]
    partial = [r for r in rows if ref and ref in (r.get("name") or "")]
    if len(exact) > 1 or len(partial) > 1:
        raise ValueError("匹配到多份档案：" + "、".join(r["name"] for r in (exact or partial)) + "，请写完整档案名。")
    if partial:
        return partial[0]
    raise ValueError(f"找不到档案“{ref}”。")


def _field(node: dict, name: str, default=None):
    """档案 JSON 的已知字段按宿主规则不区分大小写。"""
    for key, value in (node or {}).items():
        if key.lower() == name.lower():
            return value
    return default


def _is_overlay(node: dict) -> bool:
    return _field(node, "IsOverlay", False) is True


def summarize_profile(profile_json: str) -> str:
    """常规对象数量，以及按日期安排的临时层日期。"""
    profile = json.loads(profile_json)
    layouts = _field(profile, "TimeLayouts", {}) or {}
    plans = _field(profile, "ClassPlans", {}) or {}
    plan_by_id = {key.lower(): value for key, value in plans.items()}
    dates = []
    for key, schedule in (_field(profile, "OrderedSchedules", {}) or {}).items():
        plan = plan_by_id.get(str(_field(schedule, "ClassPlanId", "")).lower())
        if plan and _is_overlay(plan):
            dates.append(str(key)[:10])
    text = (f"时间表 {sum(not _is_overlay(v) for v in layouts.values())}、课表 {sum(not _is_overlay(v) for v in plans.values())}、"
            f"科目 {len(_field(profile, 'Subjects', {}) or {})}")
    return text + (f"、临时层 {len(dates)}（{'、'.join(sorted(dates))}）" if dates else "、无临时层")


async def list_profiles(service: "RemoteCiService", binding: dict) -> str:
    return format_profiles(await service._call(binding, "GET", "/api/profiles") or [], binding)


def _target_classes(service: "RemoteCiService", binding: dict, classes: list[str] | None) -> list[dict]:
    if classes:
        return [service.resolve_class(binding, name) for name in classes]
    own = (binding.get("profile") or {}).get("classes") or []
    if len(own) == 1:
        return own
    raise ValueError("请说明要从哪些班级收集档案。")


async def collect_profiles(service: "RemoteCiService", binding: dict, classes: list[str] | None = None,
                           save: bool = False) -> str:
    """读取教室电脑当前档案。save=True 时把没有校验问题的结果保存为对应班级的服务端档案（覆盖已有版本）。"""
    try:
        targets = _target_classes(service, binding, classes)
    except ValueError as ex:
        return str(ex)
    result = await service._call(binding, "POST", "/api/profiles/collect", body={"classIds": [c["id"] for c in targets]})
    lines = [result.get("message", "收集完成。")]
    for item in result.get("results") or []:
        name = item.get("className") or "班级"
        if not item.get("success"):
            lines.append(f"✗ {name}：{item.get('message')}")
            continue
        line = f"✓ {name}：{summarize_profile(item['profileJson'])}"
        errors = item.get("errors") or []
        if errors:
            line += f"；有 {len(errors)} 项问题需在 WebUI 档案页修正后保存（如：{errors[0]}）"
        elif save:
            rows = await service._call(binding, "GET", "/api/profiles", params={"classId": item["classId"]}) or []
            existing = rows[0] if rows else None
            saved = await service._call(binding, "PUT", "/api/profiles", body={"items": [{
                "id": existing["id"] if existing else None,
                "classId": item["classId"],
                # 保留来源模板关系：班主任不能改变它，改变会被服务端拒绝。
                "sourceTemplateId": existing.get("sourceTemplateId") if existing else None,
                "revision": existing["revision"] if existing else 0,
                "name": existing["name"] if existing else f"{name}档案",
                "profileJson": item["profileJson"],
            }]}) or [{}]
            line += f"；已保存为服务端档案（修订 {saved[0].get('revision')}）"
        lines.append(line)
    if not save:
        lines.append("需要存为服务端档案时，请确认后加“保存”重试，或在 WebUI 档案页编辑后保存。")
    return "\n".join(lines)


async def apply_profile(service: "RemoteCiService", binding: dict, profile: str, mode: str,
                        classes: list[str] | None = None, sections: list[str] | str | None = None,
                        import_name: str = "", confirm_replace: bool = False,
                        replace_temp_layers: bool = False) -> str:
    try:
        mode_value = parse_mode(mode)
        section_mask = 0 if mode_value == 4 else parse_sections(sections)
        row = find_profile(await service._call(binding, "GET", "/api/profiles") or [], profile)
    except ValueError as ex:
        return str(ex)
    if row.get("classId"):
        class_ids = [row["classId"]]
    elif classes:
        class_ids = [service.resolve_class(binding, name)["id"] for name in classes]
    else:
        return f"“{row['name']}”是全局模板，请指定要下发到哪些班级。"
    if mode_value == 2 and not confirm_replace:
        return "整体替换会清空设备上所选类别的全部内容，请先向用户确认，再以 confirm_replace=true 重试。"
    if mode_value == 3 and not import_name.strip():
        return "创建并启用新档案需要填写设备上的新档案名。"
    body = {
        "items": [{"id": row["id"], "revision": row["revision"]}],
        "mode": mode_value,
        "sections": section_mask,
        "classIds": class_ids,
        "confirmReplace": mode_value == 2,
        "importProfileName": import_name.strip() or None,
        "restartAfter": False,
        "replaceExistingTempLayers": mode_value == 4 and replace_temp_layers,
    }
    result = await service._call(binding, "POST", "/api/profiles/apply", body=body)
    lines = [f"“{row['name']}”已按“{MODE_TEXT[mode_value]}”下发：{result.get('message', '')}"]
    for item in result.get("results") or []:
        lines.append(f"{'✓' if item.get('success') else '✗'} {item.get('targetName')}：{item.get('message')}")
    return "\n".join(lines)


async def run_profile_command(service: "RemoteCiService", user: "ChatUser", args: list[str]) -> str:
    binding = service.require_binding(user)
    try:
        if not args:
            return await list_profiles(service, binding)
        if args[0] in ("收集", "collect"):
            names = [word for word in args[1:] if word not in ("保存", "save")]
            return await collect_profiles(service, binding, names or None, save=len(names) != len(args) - 1)
        if args[0] in ("下发", "应用", "apply") and len(args) >= 3:
            mode = parse_mode(args[2])
            if mode == 2:
                return "整体替换会清空设备上所选类别，聊天指令不支持；请在 WebUI 档案页确认后下发，或直接告诉我并确认。"
            # 用户在指令里亲自写出“替换”即是对覆盖同日临时层的明确确认，只对临时层方式有效。
            replace = mode == 4 and any(word in REPLACE_WORDS for word in args[3:])
            classes = [word for word in args[3:] if word not in REPLACE_WORDS] if mode == 4 else args[3:]
            return await apply_profile(service, binding, args[1], args[2], classes or None, replace_temp_layers=replace)
        return PROFILE_HELP
    except ValueError as ex:
        return str(ex)
    except RemoteCiError as ex:
        return f"RemoteCI 请求失败：{ex}"
