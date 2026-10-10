"""RemoteCI REST API 客户端。

每个绑定的聊天用户对应一份凭据：
- API Key（`rci_…`）：直接作为 Bearer 令牌。
- 账号密码：登录一次换取 accessToken（1 小时）+ deviceSessionId/deviceSecret，
  之后只保存续期凭据，不保存密码；401 时自动续期并换掉 deviceSecret。
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

import aiohttp

DEVICE_NAME = "AstrBot"


class RemoteCiError(Exception):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def __str__(self) -> str:
        hint = {
            401: "凭据无效或已过期",
            403: "当前账号没有这项权限",
            404: "对象不存在，或教室端插件尚未同步数据",
            409: "冲突（课表可能刚被别人修改，请重新读取）",
            503: "教室端插件离线",
            504: "教室端执行超时",
        }.get(self.status, "")
        base = f"HTTP {self.status}" if self.status else "网络错误"
        return f"{base}：{self.message}" + (f"（{hint}）" if hint and hint not in self.message else "")


def normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


async def _read_json(resp: aiohttp.ClientResponse) -> Any:
    text = await resp.text()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _error_message(data: Any, fallback: str) -> tuple[str, str]:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data.get("title") or fallback), str(data.get("code") or "")
    if isinstance(data, str) and data:
        return data[:300], ""
    return fallback, ""


class RemoteCiClient:
    """无状态的请求器；凭据保存在 binding 字典中，变化后通过 on_auth_changed 回写。"""

    def __init__(self, session_factory: Callable[[], aiohttp.ClientSession], timeout: float = 25):
        self._session_factory = session_factory
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ---------- 登录 ----------

    async def login(self, base_url: str, username: str, password: str) -> dict:
        data = await self._raw("POST", base_url, "/api/auth/login", None, {
            "username": username, "password": password, "deviceName": DEVICE_NAME,
        })
        if isinstance(data, dict) and data.get("passwordPending"):
            raise RemoteCiError(400, "账号尚未激活：请先在 RemoteCI WebUI 登录页完成首次设置密码")
        if not isinstance(data, dict) or not data.get("accessToken"):
            raise RemoteCiError(500, "登录响应缺少 accessToken")
        return self._session_auth(data)

    async def logout(self, binding: dict) -> None:
        if (binding.get("auth") or {}).get("type") != "session":
            return
        try:
            await self.request(binding, "POST", "/api/auth/logout", retry_auth=False)
        except RemoteCiError:
            pass

    @staticmethod
    def _session_auth(data: dict) -> dict:
        return {
            "type": "session",
            "access_token": data.get("accessToken"),
            "expires_at": time.time() + 55 * 60,
            "device_session_id": data.get("deviceSessionId"),
            "device_secret": data.get("deviceSecret"),
        }

    async def _refresh(self, binding: dict) -> None:
        auth = binding.get("auth") or {}
        data = await self._raw("POST", binding["server_url"], "/api/auth/refresh", None, {
            "deviceSessionId": auth.get("device_session_id"), "deviceSecret": auth.get("device_secret"),
        })
        if not isinstance(data, dict) or not data.get("accessToken"):
            raise RemoteCiError(401, "续期失败")
        new_auth = self._session_auth(data)
        new_auth["device_session_id"] = data.get("deviceSessionId") or auth.get("device_session_id")
        binding["auth"] = new_auth

    # ---------- 通用请求 ----------

    async def request(self, binding: dict, method: str, path: str, *, params: dict | None = None,
                      body: Any = None, retry_auth: bool = True,
                      on_auth_changed: Callable[[dict], Awaitable[None]] | None = None,
                      content: bytes | None = None, headers: dict | None = None) -> Any:
        """content 不为空时以原始字节作为请求体（例如班级头像），headers 追加到请求头。"""
        auth = binding.get("auth") or {}
        if auth.get("type") == "session" and auth.get("expires_at", 0) < time.time():
            await self._refresh_and_save(binding, on_auth_changed)
            auth = binding["auth"]
        token = auth.get("api_key") if auth.get("type") == "api_key" else auth.get("access_token")
        try:
            return await self._raw(method, binding["server_url"], path, token, body, params, content, headers)
        except RemoteCiError as ex:
            if ex.status == 401 and retry_auth and auth.get("type") == "session":
                await self._refresh_and_save(binding, on_auth_changed)
                return await self._raw(method, binding["server_url"], path, binding["auth"]["access_token"], body,
                                       params, content, headers)
            raise

    async def _refresh_and_save(self, binding, on_auth_changed) -> None:
        await self._refresh(binding)
        if on_auth_changed:
            await on_auth_changed(binding)

    async def _raw(self, method: str, base_url: str, path: str, token: str | None,
                   body: Any = None, params: dict | None = None,
                   content: bytes | None = None, extra_headers: dict | None = None) -> Any:
        headers = {"Accept": "application/json", **(extra_headers or {})}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None and v != ""}
        url = normalize_base_url(base_url) + "/" + path.lstrip("/")
        try:
            session = self._session_factory()
            async with session.request(method.upper(), url, params=clean_params or None,
                                       json=body if body is not None and content is None else None,
                                       data=content,
                                       headers=headers, timeout=self._timeout) as resp:
                data = await _read_json(resp)
                if resp.status >= 400:
                    message, code = _error_message(data, resp.reason or "请求失败")
                    raise RemoteCiError(resp.status, message, code)
                return data
        except RemoteCiError:
            raise
        except aiohttp.ClientError as ex:
            raise RemoteCiError(0, f"无法连接 RemoteCI 服务器：{ex}") from ex
        except TimeoutError as ex:
            raise RemoteCiError(0, "连接 RemoteCI 服务器超时") from ex
