import asyncio

import pytest

from remoteci.profiles import apply_profile, format_profiles, parse_sections
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
