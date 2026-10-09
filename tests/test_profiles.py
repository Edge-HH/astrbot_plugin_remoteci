import asyncio
import json

import pytest

from remoteci.profiles import apply_profile, collect_profiles, format_profiles, parse_sections, run_profile_command, summarize_profile
from remoteci.service import ServiceError

CLASS_A = "11111111-0000-0000-0000-00000000000a"
BINDING = {"profile": {"classes": [{"id": CLASS_A, "name": "高一(1)班"}]}}
ROWS = [
    {"id": "t1", "name": "统一模板", "classId": None, "revision": 2, "timeLayoutCount": 1, "classPlanCount": 5, "subjectCount": 9},
    {"id": "c1", "name": "高一1班档案", "classId": CLASS_A, "revision": 4, "timeLayoutCount": 1, "classPlanCount": 5, "subjectCount": 9},
]


class FakeService:
    def __init__(self):
        self.calls = []

    async def _call(self, binding, method, path, *, params=None, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return ROWS
        return {"success": True, "message": "完成 1 台，失败 0 台。",
                "results": [{"success": True, "targetName": "教室电脑", "message": "已更新当前档案及必要依赖"}]}

    def resolve_class(self, binding, ref):
        for c in binding["profile"]["classes"]:
            if ref in c["name"]:
                return c
        raise ServiceError(f"找不到班级“{ref}”。")


def run(coro):
    return asyncio.run(coro)


def test_format_profiles_shows_owner_and_counts():
    text = format_profiles(ROWS, BINDING)
    assert "统一模板（全局模板）" in text
    assert "高一1班档案（高一(1)班）" in text
    assert "课表 5" in text


def test_parse_sections():
    assert parse_sections(None) == 7
    assert parse_sections("课表、科目") == 6
    with pytest.raises(ValueError):
        parse_sections(["作息"])


def test_class_profile_applies_to_its_own_class():
    service = FakeService()
    text = run(apply_profile(service, BINDING, "高一1班", "更新", sections="课表"))
    assert "已按“更新当前档案”下发" in text
    method, path, body = service.calls[-1]
    assert (method, path) == ("POST", "/api/profiles/apply")
    assert body["items"] == [{"id": "c1", "revision": 4}]
    assert body["mode"] == 1 and body["sections"] == 2 and body["classIds"] == [CLASS_A]


def test_template_requires_target_classes_and_replace_requires_confirmation():
    service = FakeService()
    assert "请指定要下发到哪些班级" in run(apply_profile(service, BINDING, "统一模板", "更新"))
    assert "请先向用户确认" in run(apply_profile(service, BINDING, "统一模板", "替换", ["高一"]))
    assert all(call[0] == "GET" for call in service.calls)

    run(apply_profile(service, BINDING, "统一模板", "替换", ["高一"], confirm_replace=True))
    body = service.calls[-1][2]
    assert body["mode"] == 2 and body["confirmReplace"] is True and body["classIds"] == [CLASS_A]


def test_create_requires_device_profile_name():
    service = FakeService()
    assert "新档案名" in run(apply_profile(service, BINDING, "统一模板", "新建", ["高一"]))
    run(apply_profile(service, BINDING, "统一模板", "新建", ["高一"], import_name="新学期"))
    assert service.calls[-1][2]["importProfileName"] == "新学期"


def test_unknown_mode_and_profile_are_reported():
    service = FakeService()
    assert "应用方式只能是" in run(apply_profile(service, BINDING, "统一模板", "随便"))
    assert "找不到档案" in run(apply_profile(service, BINDING, "不存在", "更新"))


PROFILE_JSON = json.dumps({
    "Name": "设备档案",
    "TimeLayouts": {"l1": {"Name": "作息", "Layouts": []}, "l2": {"Name": "作息（临时层）", "IsOverlay": True, "Layouts": []}},
    "ClassPlans": {"p1": {"Name": "周一"}, "p2": {"Name": "周二"}, "o1": {"Name": "周一（临时层）", "IsOverlay": True}},
    "Subjects": {"s1": {"Name": "语文"}},
    "OrderedSchedules": {"2026-10-12T00:00:00+08:00": {"ClassPlanId": "o1"}, "2026-10-13T00:00:00": {"ClassPlanId": "p2"}},
})


class CollectService(FakeService):
    def __init__(self, existing=None, errors=None):
        super().__init__()
        self.existing = existing
        self.errors = errors or []

    async def _call(self, binding, method, path, *, params=None, body=None):
        self.calls.append((method, path, body if body is not None else params))
        if path == "/api/profiles/collect":
            return {"success": True, "message": "已收集 1 个班级，失败 0 个。收集结果尚未保存。", "results": [
                {"classId": CLASS_A, "className": "高一(1)班", "success": True, "message": "已收集", "profileJson": PROFILE_JSON, "errors": self.errors}]}
        if method == "GET":
            return [self.existing] if self.existing else []
        return [{"revision": (self.existing or {}).get("revision", 0) + 1}]


def test_summary_counts_regular_objects_and_temp_layer_dates():
    assert summarize_profile(PROFILE_JSON) == "时间表 1、课表 2、科目 1、临时层 1（2026-10-12）"


def test_collect_defaults_to_the_only_class_and_does_not_save():
    service = CollectService()
    text = run(collect_profiles(service, BINDING))
    assert "临时层 1（2026-10-12）" in text and "尚未保存" in text and "加“保存”重试" in text
    assert [call[:2] for call in service.calls] == [("POST", "/api/profiles/collect")]
    assert service.calls[0][2] == {"classIds": [CLASS_A]}


def test_collect_and_save_overwrites_existing_class_profile_with_revision():
    service = CollectService(existing={"id": "c1", "name": "高一1班档案", "classId": CLASS_A, "revision": 4, "sourceTemplateId": "t1"})
    text = run(collect_profiles(service, BINDING, ["高一"], save=True))
    assert "已保存为服务端档案（修订 5）" in text
    method, path, body = service.calls[-1]
    assert (method, path) == ("PUT", "/api/profiles")
    item = body["items"][0]
    assert (item["id"], item["revision"], item["sourceTemplateId"], item["name"]) == ("c1", 4, "t1", "高一1班档案")
    assert item["profileJson"] == PROFILE_JSON


def test_collect_with_errors_is_not_saved():
    service = CollectService(errors=["课表“周一”的课程数量与关联时间表的上课时段数量不一致"])
    text = run(collect_profiles(service, BINDING, save=True))
    assert "需在 WebUI 档案页修正后保存" in text
    assert all(call[0] != "PUT" for call in service.calls)


def test_temp_layer_mode_ignores_sections_and_passes_replace_flag():
    service = FakeService()
    run(apply_profile(service, BINDING, "高一1班", "临时层", sections="作息", replace_temp_layers=True))
    body = service.calls[-1][2]
    assert body["mode"] == 4 and body["sections"] == 0 and body["replaceExistingTempLayers"] is True
    run(apply_profile(service, BINDING, "高一1班", "更新", replace_temp_layers=True))
    assert service.calls[-1][2]["replaceExistingTempLayers"] is False


class CommandService(FakeService):
    def require_binding(self, user):
        return BINDING


def test_command_replaces_same_day_temp_layers_only_when_user_writes_replace():
    service = CommandService()
    run(run_profile_command(service, None, ["下发", "高一1班", "临时层"]))
    assert service.calls[-1][2]["replaceExistingTempLayers"] is False
    run(run_profile_command(service, None, ["下发", "统一模板", "临时层", "高一", "替换"]))
    body = service.calls[-1][2]
    assert body["replaceExistingTempLayers"] is True and body["classIds"] == [CLASS_A]
    run(run_profile_command(service, None, ["下发", "高一1班", "更新", "替换"]))
    assert service.calls[-1][2]["replaceExistingTempLayers"] is False
