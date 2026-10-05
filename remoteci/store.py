"""插件持久化状态：绑定、会话角色、提醒默认值、节假日设置、推送去重与日志。

全部保存在插件数据目录下的 JSON 文件中（写入时先写临时文件再替换，避免半截文件）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from copy import deepcopy
from pathlib import Path

# 老师提醒的统一默认值；个人可通过指令或自然语言覆盖其中任意一项。
DEFAULT_REMINDERS = {
    "today_enabled": True,        # 上学时提醒当天个人日程
    "today_time": "07:00",
    "tomorrow_enabled": True,     # 提醒次日个人日程
    "tomorrow_mode": "last_class",  # last_class：自己最后一节课结束时；fixed：固定时刻
    "tomorrow_time": "20:00",     # fixed 模式下的时刻
    "tomorrow_fallback": "17:30",  # last_class 模式下当天没有课时改用的时刻
    "before_enabled": True,       # 课前提醒
    "before_minutes": 10,
    "change_enabled": True,       # 自己的课被换了提醒
    "class_change_enabled": True,  # 班主任：班里的课被换了提醒
    "skip_empty_days": True,      # 没课的日子不推送当日/次日日程
}

REMINDER_KEYS = set(DEFAULT_REMINDERS)

ROLE_AUTO, ROLE_NONE, ROLE_TEACHER, ROLE_HEAD, ROLE_CLASS_GROUP = "auto", "none", "teacher", "headteacher", "class_group"
SESSION_ROLES = {ROLE_AUTO, ROLE_NONE, ROLE_TEACHER, ROLE_HEAD, ROLE_CLASS_GROUP}

DEFAULT_STATE = {
    "version": 1,
    "paused": False,
    "reminders": DEFAULT_REMINDERS,
    "holidays": {"use_official": True, "weekend_as_holiday": False, "ranges": []},
    "bindings": {},
    "sessions": {},
    "sent": {},
    "log": [],
}

DEFAULT_SESSION_PUSH = {
    "enabled": False,       # 会话级定时推送（班级群的课表推送需要在 WebUI 显式开启）
    "content": "personal",  # personal：个人日程；class：班级课表
    "today_time": "07:00",
    "tomorrow_time": "20:00",
    "today_enabled": True,
    "tomorrow_enabled": True,
}

LOG_LIMIT = 300


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.dir / "state.json"
        self._snap_path = self.dir / "snapshots.json"
        self._lock = asyncio.Lock()
        self.state: dict = self._load(self._state_path, DEFAULT_STATE)
        self.snapshots: dict = self._load(self._snap_path, {})
        self._migrate()

    @staticmethod
    def _load(path: Path, default: dict) -> dict:
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return deepcopy(default)

    def _migrate(self) -> None:
        for key, value in DEFAULT_STATE.items():
            self.state.setdefault(key, deepcopy(value))
        merged = deepcopy(DEFAULT_REMINDERS)
        merged.update({k: v for k, v in self.state["reminders"].items() if k in REMINDER_KEYS})
        self.state["reminders"] = merged
        for key, value in DEFAULT_STATE["holidays"].items():
            self.state["holidays"].setdefault(key, deepcopy(value))
        for session in self.state["sessions"].values():
            push = deepcopy(DEFAULT_SESSION_PUSH)
            push.update(session.get("push") or {})
            session["push"] = push
            session.setdefault("role", ROLE_AUTO)

    async def save(self) -> None:
        async with self._lock:
            self._write(self._state_path, self.state)

    async def save_snapshots(self) -> None:
        async with self._lock:
            self._write(self._snap_path, self.snapshots)

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, path)

    # ---------- 绑定 ----------

    @property
    def bindings(self) -> dict:
        return self.state["bindings"]

    @property
    def sessions(self) -> dict:
        return self.state["sessions"]

    def binding_for(self, user_key: str) -> dict | None:
        return self.bindings.get(user_key)

    def effective_reminders(self, binding: dict) -> dict:
        prefs = deepcopy(self.state["reminders"])
        prefs.update({k: v for k, v in (binding.get("prefs") or {}).items() if k in REMINDER_KEYS})
        return prefs

    # ---------- 会话 ----------

    def touch_session(self, umo: str, *, kind: str, name: str, platform: str, user_key: str | None) -> dict:
        session = self.sessions.get(umo)
        if session is None:
            session = {
                "umo": umo, "kind": kind, "name": name, "platform": platform,
                "role": ROLE_AUTO, "binding_key": user_key if kind == "private" else None,
                "class_id": None, "watch_class_ids": None, "push": deepcopy(DEFAULT_SESSION_PUSH),
                "created_at": time.time(),
            }
            self.sessions[umo] = session
        if name:
            session["name"] = name
        if kind == "private" and user_key:
            session["binding_key"] = user_key
        session["last_seen"] = time.time()
        return session

    # ---------- 去重与日志 ----------

    def was_sent(self, key: str) -> bool:
        return key in self.state["sent"]

    def mark_sent(self, key: str) -> None:
        self.state["sent"][key] = time.time()

    def prune_sent(self, max_age_days: int = 10) -> None:
        cutoff = time.time() - max_age_days * 86400
        self.state["sent"] = {k: v for k, v in self.state["sent"].items() if v >= cutoff}

    def add_log(self, kind: str, target: str, text: str, ok: bool = True) -> None:
        self.state["log"].append({"at": time.time(), "kind": kind, "target": target, "text": text[:500], "ok": ok})
        if len(self.state["log"]) > LOG_LIMIT:
            del self.state["log"][: len(self.state["log"]) - LOG_LIMIT]
