"""节假日判定：法定节假日数据（含调休）+ WebUI 中手动添加的假期区间（寒暑假等）。

法定节假日数据来自 NateScarlet/holiday-cn（经 jsDelivr 分发），按年份缓存到插件数据目录，
网络不可用时沿用缓存；没有任何数据时只按手动区间判断。
"""

from __future__ import annotations

import json
import time as _time
from datetime import date
from pathlib import Path

HOLIDAY_URLS = [
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json",
]
REFRESH_SECONDS = 24 * 3600


def _next_day(iso: str) -> str:
    return date.fromordinal(date.fromisoformat(iso).toordinal() + 1).isoformat()


class HolidayCalendar:
    def __init__(self, cache_dir: Path):
        self._cache_dir = cache_dir
        # year -> {date_iso: (name, is_off_day)}
        self._years: dict[int, dict[str, tuple[str, bool]]] = {}
        self._fetched_at: dict[int, float] = {}

    # ---------- 数据加载 ----------

    def load_year_data(self, year: int, payload: dict) -> None:
        days = {}
        for item in payload.get("days") or []:
            if item.get("date"):
                days[item["date"]] = (item.get("name") or "", bool(item.get("isOffDay")))
        self._years[year] = days

    def _cache_file(self, year: int) -> Path:
        return self._cache_dir / f"holiday-{year}.json"

    def load_cache(self) -> None:
        for path in self._cache_dir.glob("holiday-*.json"):
            try:
                year = int(path.stem.split("-")[1])
                self.load_year_data(year, json.loads(path.read_text("utf-8")))
                self._fetched_at.setdefault(year, path.stat().st_mtime)
            except (ValueError, OSError, json.JSONDecodeError):
                continue

    async def refresh(self, session, years: list[int], force: bool = False) -> list[str]:
        """拉取指定年份的数据；返回错误信息列表（成功为空）。session 为 aiohttp.ClientSession。"""
        errors = []
        for year in years:
            if not force and _time.time() - self._fetched_at.get(year, 0) < REFRESH_SECONDS and year in self._years:
                continue
            ok, last = False, ""
            for template in HOLIDAY_URLS:
                try:
                    async with session.get(template.format(year=year), timeout=15) as resp:
                        if resp.status != 200:
                            continue
                        payload = json.loads(await resp.text())
                    self.load_year_data(year, payload)
                    self._cache_dir.mkdir(parents=True, exist_ok=True)
                    self._cache_file(year).write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
                    self._fetched_at[year] = _time.time()
                    ok = True
                    break
                except Exception as ex:  # noqa: BLE001 - 网络错误统一降级为缓存
                    last = str(ex)
                    continue
            if not ok:
                self._fetched_at[year] = _time.time() - REFRESH_SECONDS + 3600  # 一小时后再试
                errors.append(f"{year} 年节假日数据获取失败" + (f"：{last}" if last else ""))
        return errors

    def has_year(self, year: int) -> bool:
        return year in self._years

    # ---------- 判定 ----------

    def official(self, day: date) -> tuple[str, bool] | None:
        return self._years.get(day.year, {}).get(day.isoformat())

    def check(self, day: date, settings: dict) -> tuple[bool, str]:
        """返回 (是否放假暂停, 原因)。settings 即 store 中的 holidays 段。"""
        iso = day.isoformat()
        for item in settings.get("ranges") or []:
            start, end = item.get("start") or "", item.get("end") or item.get("start") or ""
            if start and start <= iso <= end:
                return True, item.get("name") or "自定义假期"
        if settings.get("use_official", True):
            info = self.official(day)
            if info is not None:
                name, off = info
                return (True, name) if off else (False, f"{name}调休上课")
            if settings.get("weekend_as_holiday", False) and day.weekday() >= 5:
                return True, "周末"
        elif settings.get("weekend_as_holiday", False) and day.weekday() >= 5:
            return True, "周末"
        return False, ""

    def upcoming(self, start: date, settings: dict, limit: int = 8) -> list[dict]:
        """WebUI 展示：从 start 起的法定假期与手动区间。"""
        items = []
        if settings.get("use_official", True):
            for year in sorted(self._years):
                for iso, (name, off) in sorted(self._years[year].items()):
                    if iso < start.isoformat():
                        continue
                    last = items[-1] if items else None
                    # 连续的同名同类日期合并成一个区间（调休上班日单独成条）。
                    if last and last["name"] == name and last["off"] == off and _next_day(last["end"]) == iso:
                        last["end"] = iso
                        continue
                    items.append({"name": name, "start": iso, "end": iso, "off": off, "source": "official"})
        for item in settings.get("ranges") or []:
            if (item.get("end") or item.get("start", "")) >= start.isoformat():
                items.append({"name": item.get("name") or "自定义假期", "start": item.get("start"),
                              "end": item.get("end") or item.get("start"), "off": True, "source": "custom"})
        items.sort(key=lambda x: x["start"])
        return items[:limit]
