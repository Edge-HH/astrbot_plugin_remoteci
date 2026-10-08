import asyncio

import pytest

from remoteci.makeup import format_overview, parse_follow, set_makeup

OVERVIEW = {
    "enabled": True,
    "status": {"lastSuccessAt": "2026-10-08T08:00:00+08:00", "lastError": None},
    "periods": [{
        "name": "国庆节", "offStart": "2026-10-01", "offEnd": "2026-10-07",
        "makeupDays": [
            {"date": "2026-09-20", "autoWeekday": 2, "followWeekday": 2, "followSource": "auto"},
            {"date": "2026-10-10", "autoWeekday": 3, "followWeekday": None, "followSource": "skip"},
        ],
    }],
    "staleOverrideDates": [],
}


class FakeService:
    def __init__(self):
        self.calls = []

    async def _call(self, binding, method, path, *, params=None, body=None):
        self.calls.append((method, path, body))
        return OVERVIEW


def test_format_overview_lists_periods_and_makeup_days():
    text = format_overview(OVERVIEW)
    assert "国庆节：2026-10-01 至 2026-10-07 放假" in text
    assert "2026-09-20（周日）调休上学，上周二的课" in text
    assert "2026-10-10（周六）调休上学，不补课" in text


def test_format_overview_reports_disabled_and_errors():
    text = format_overview({**OVERVIEW, "enabled": False, "status": {"lastError": "网络错误"}})
    assert "已关闭" in text
    assert "网络错误" in text


@pytest.mark.parametrize("word,expected", [
    ("周三", ("weekday", 3)), ("星期五", ("weekday", 5)), ("1", ("weekday", 1)),
    ("不补课", ("skip", None)), ("自动", ("auto", None)),
])
def test_parse_follow(word, expected):
    assert parse_follow(word) == expected


def test_parse_follow_rejects_weekend():
    with pytest.raises(ValueError):
        parse_follow("周六")


def test_set_makeup_calls_put_and_delete():
    service = FakeService()
    asyncio.run(set_makeup(service, {}, "2026-10-10", "周五"))
    asyncio.run(set_makeup(service, {}, "2026-10-10", "不补课"))
    asyncio.run(set_makeup(service, {}, "2026-10-10", "自动"))
    assert service.calls == [
        ("PUT", "/api/admin/holidays/overrides/2026-10-10", {"followWeekday": 5}),
        ("PUT", "/api/admin/holidays/overrides/2026-10-10", {"followWeekday": None}),
        ("DELETE", "/api/admin/holidays/overrides/2026-10-10", None),
    ]


def test_set_makeup_validates_input_without_calling_server():
    service = FakeService()
    assert "日期格式" in asyncio.run(set_makeup(service, {}, "10-10", "周五"))
    assert "只能是" in asyncio.run(set_makeup(service, {}, "2026-10-10", "周日"))
    assert service.calls == []
