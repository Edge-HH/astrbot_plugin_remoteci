"""自定义课程管理：使用当前私聊绑定的认证信息，不接受账号或班级参数。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID


async def list_courses(service, user) -> str:
    if not user.is_private:
        return "自定义课程仅自己可见，请在私聊中查询和管理。"
    binding = service.require_binding(user)
    items = await service._call(binding, "GET", "/api/me/schedule/courses") or []
    if not items:
        return "你还没有自定义课程。"
    return "你的自定义课程：\n" + "\n".join(
        f"· {x['subject']} {x['startsAt']}–{x['endsAt']} {x.get('location', '')}\n"
        f"  编号：{x['id']}；提前提醒：{x.get('reminderMinutes')} 分钟；上课提醒：{x.get('notifyAtStart', True)}"
        for x in items)


async def save_course(service, user, subject: str, starts_at: str, ends_at: str, location: str = "",
                      reminder_minutes: int = 10, notify_at_start: bool = True, course_id: str = "") -> str:
    if not user.is_private:
        return "自定义课程仅自己可见，请在私聊中查询和管理。"
    binding = service.require_binding(user)
    try:
        start, end = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (starts_at, ends_at))
        if start.utcoffset() is None or end.utcoffset() is None or end <= start:
            raise ValueError()
        path = "/api/me/schedule/courses"
        if course_id:
            path += "/" + str(UUID(course_id))
    except ValueError:
        return "请提供含时区的起止时间（如 2026-10-17T08:00:00+08:00），修改时请使用完整课程编号。"
    result = await service._call(binding, "PUT" if course_id else "POST", path, body={
        "subject": subject, "location": location, "startsAt": start.isoformat(), "endsAt": end.isoformat(),
        "reminderMinutes": None if reminder_minutes == -1 else reminder_minutes, "notifyAtStart": notify_at_start,
    })
    service._cache.clear()
    return f"已{'修改' if course_id else '添加'}自定义课程：{result['subject']}\n编号：{result['id']}\n已同步到 RemoteCI 个人日程。"


async def delete_course(service, user, course_id: str) -> str:
    if not user.is_private:
        return "自定义课程仅自己可见，请在私聊中查询和管理。"
    binding = service.require_binding(user)
    try:
        course_id = str(UUID(course_id))
    except ValueError:
        return "请使用完整课程编号。"
    await service._call(binding, "DELETE", f"/api/me/schedule/courses/{course_id}")
    service._cache.clear()
    return "已删除自定义课程，所有设备将同步更新。"
