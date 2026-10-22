"""原生卡片：把 RemoteCI 个人通知转换为飞书、钉钉、企业微信的卡片消息（纯函数，不依赖 AstrBot）。

AstrBot 的适配器收不到卡片按钮回调（飞书只订阅消息事件，企业微信丢弃 template_card_event，钉钉没有卡片回调），
所以按钮只做两件事：
- 打开链接：审批按钮打开 RemoteCI 签发的一次性签名链接（POST /api/me/action-links），在网页确认页再确认一次；
- 钉钉额外提供 dtmd 按钮：点击即在聊天里发出快捷回复（如“同意 a1b2c3d4”），由 quick_reply 处理。

字段名对照的官方文档：
- 飞书卡片 JSON 2.0：schema/header/body.elements，按钮 behaviors 的 open_url 跳转（default_url）。
- 钉钉机器人消息模板：sampleActionCard（title/text/singleTitle/singleURL）、
  sampleActionCard2~5（actionTitleN/actionURLN，竖排按钮）；
  dingtalk://dingtalkclient/page/link?url=…&pc_slide=false 在钉钉内打开网页，dtmd://dingtalkclient/sendMessage?content=… 发消息。
- 企业微信应用消息 template_card 的 text_notice：main_title.title（建议≤36 字）、sub_title_text（≤160 字）、
  horizontal_content_list（≤6 项，keyname≤5 字）、jump_list（≤3 项，title≤18 字）、card_action（text_notice 必填）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from .client import normalize_base_url
from .swaps import NOTIFY_HINTS, format_notification, notification_category, short_id

# 需要卡片（或文本推送附带链接）的通知类型。
CARD_KINDS = {"swap_requested", "swap_forced", "device_offline", "device_online", "automation_failed", "app_guard"}
# 需要向服务端申请一次性签名链接的通知类型 → 签名链接的 action。
ACTION_LINK_KINDS = {"swap_requested": ("swap.approve", "swap.reject"), "swap_forced": ("swap.revoke",)}
# 通知没有 link 字段时，“查看”按钮打开的站内页面。
DEFAULT_PAGES = {"swap_requested": "/SwapRequests", "swap_forced": "/SwapRequests",
                 "device_offline": "/Devices", "device_online": "/Devices", "automation_failed": "/Automation",
                 "app_guard": "/AppGuard"}
VIEW_TITLES = {"swap_requested": "在 RemoteCI 中查看", "swap_forced": "在 RemoteCI 中查看",
               "device_offline": "查看设备状态", "device_online": "查看设备状态", "automation_failed": "查看规则",
               "app_guard": "查看应用守护"}
# 飞书卡片标题栏颜色。
TONES = {"swap_requested": "blue", "swap_forced": "orange", "device_offline": "red", "device_online": "green",
         "automation_failed": "red", "app_guard": "orange"}

DINGTALK_MAX_BUTTONS = 5
WECOM_MAX_JUMPS = 3


@dataclass
class Button:
    title: str
    url: str = ""           # 点击打开的网页
    reply: str = ""         # 点击后在聊天里发出的文字（只有钉钉支持）
    style: str = "default"  # primary / danger / default


@dataclass
class Card:
    kind: str
    title: str
    body: str
    hint: str
    link: str                       # 整张卡片的“查看”链接
    short: str = ""                 # 换课申请 8 位短编号
    tone: str = "blue"
    buttons: list[Button] = field(default_factory=list)

    @property
    def url_buttons(self) -> list[Button]:
        return [b for b in self.buttons if b.url]


def site_path(link: str | None) -> str:
    """只接受站内相对路径（以单个 / 开头），避免通知内容把人引到别的网站。"""
    link = (link or "").strip()
    return link if link.startswith("/") and not link.startswith("//") and "\\" not in link else ""


def site_url(server_url: str, path: str) -> str:
    """服务器地址 + 站内路径；path 已是 http(s) 绝对地址（服务端签发的签名链接）时原样返回。"""
    if path.startswith(("http://", "https://")):
        return path
    return normalize_base_url(server_url) + "/" + path.lstrip("/")


def view_url(note: dict, server_url: str) -> str:
    kind = note.get("kind") or ""
    return site_url(server_url, site_path(note.get("link")) or DEFAULT_PAGES.get(kind, "/"))


def build_card(note: dict, server_url: str, action_links: dict[str, str] | None = None) -> Card | None:
    """按通知类型生成平台无关的卡片；action_links 为 {action: 签名链接}。不需要卡片的类型返回 None。"""
    kind = note.get("kind") or ""
    if kind not in CARD_KINDS:
        return None
    links = action_links or {}
    short = short_id(note.get("swapRequestId"))
    view = view_url(note, server_url)
    hint = NOTIFY_HINTS.get(kind, "")
    hint = hint.format(short=short) if short or "{short}" not in hint else ""
    card = Card(kind=kind, title=f"RemoteCI {notification_category(kind)} · {note.get('title') or ''}".rstrip(" ·"),
                body=(note.get("body") or "").strip(), hint=hint, link=view, short=short, tone=TONES.get(kind, "blue"))
    if kind == "swap_requested":
        if links.get("swap.approve"):
            card.buttons.append(Button("同意", url=links["swap.approve"], style="primary"))
        if links.get("swap.reject"):
            card.buttons.append(Button("拒绝", url=links["swap.reject"], style="danger"))
        if short:
            card.buttons.append(Button("直接回复“同意”", reply=f"同意 {short}"))
    elif kind == "swap_forced":
        if links.get("swap.revoke"):
            card.buttons.append(Button("撤回", url=links["swap.revoke"], style="danger"))
        if short:
            card.buttons.append(Button("直接回复“撤回”", reply=f"撤回 {short}"))
    card.buttons.append(Button(VIEW_TITLES[kind], url=view, style="default" if card.buttons else "primary"))
    return card


def text_message(note: dict, server_url: str) -> str:
    """不支持卡片的平台（QQ、Telegram 等）或卡片发送失败时的文本：通知 + 快捷回复提示 + 查看链接。

    一次性签名链接只放进卡片按钮，不写进聊天记录。
    """
    text = format_notification(note)
    kind = note.get("kind") or ""
    if kind in CARD_KINDS and server_url:
        text += f"\n{VIEW_TITLES[kind]}：{view_url(note, server_url)}"
    return text


def _cut(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------- 飞书 ----------

LARK_BUTTON_TYPES = {"primary": "primary_filled", "danger": "danger", "default": "default"}


def lark_card(card: Card) -> dict:
    """飞书卡片 JSON 2.0；通过 im/v1/messages（msg_type=interactive）发送，content 为本对象的 JSON 字符串。"""
    text = "\n\n".join(x for x in (card.body, card.hint) if x) or card.title
    elements: list[dict] = [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]
    buttons = [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": b.title},
        "type": LARK_BUTTON_TYPES.get(b.style, "default"),
        "behaviors": [{"type": "open_url", "default_url": b.url}],
    } for b in card.url_buttons]
    if buttons:
        elements.append({"tag": "column_set", "flex_mode": "flow", "horizontal_spacing": "8px", "columns": [
            {"tag": "column", "width": "auto", "elements": [button]} for button in buttons]})
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": card.title}, "template": card.tone},
        "body": {"elements": elements},
    }


# ---------- 钉钉 ----------

def dingtalk_link(url: str) -> str:
    """在钉钉内置浏览器打开（PC 端 pc_slide=false 时在独立窗口打开）。"""
    return f"dingtalk://dingtalkclient/page/link?url={quote(url, safe='')}&pc_slide=false"


def dingtalk_reply(content: str) -> str:
    """点击后由用户在当前会话发出 content。"""
    return f"dtmd://dingtalkclient/sendMessage?content={quote(content, safe='')}"


def dingtalk_card(card: Card) -> tuple[str, dict]:
    """返回 (msgKey, msgParam)，用于 /v1.0/robot/oToMessages/batchSend 或 /v1.0/robot/groupMessages/send。"""
    paragraphs = [f"#### {card.title}"] + [line for line in card.body.splitlines() if line.strip()]
    if card.hint:
        paragraphs.append(card.hint)
    text = "\n\n".join(paragraphs)
    buttons = [(b.title, dingtalk_reply(b.reply) if b.reply else dingtalk_link(b.url)) for b in card.buttons]
    buttons = buttons[:DINGTALK_MAX_BUTTONS]
    if not buttons:
        return "sampleMarkdown", {"title": card.title, "text": text}
    if len(buttons) == 1:
        return "sampleActionCard", {"title": card.title, "text": text,
                                    "singleTitle": buttons[0][0], "singleURL": buttons[0][1]}
    param = {"title": card.title, "text": text}
    for i, (title, url) in enumerate(buttons, 1):
        param[f"actionTitle{i}"] = title
        param[f"actionURL{i}"] = url
    return f"sampleActionCard{len(buttons)}", param


# ---------- 企业微信 ----------

def wecom_card(card: Card, touser: str, agentid: int | str) -> dict:
    """企业微信应用消息 template_card（text_notice），用 message/send 发送。"""
    template = {
        "card_type": "text_notice",
        "source": {"desc": "RemoteCI", "desc_color": 0},
        "main_title": {"title": _cut(card.title, 36)},
        "sub_title_text": _cut("\n".join(x for x in (card.body, card.hint) if x), 160),
        "jump_list": [{"type": 1, "title": _cut(b.title, 18), "url": b.url}
                      for b in card.url_buttons[:WECOM_MAX_JUMPS]],
        "card_action": {"type": 1, "url": card.link},
    }
    if card.short:
        template["horizontal_content_list"] = [{"keyname": "编号", "value": card.short}]
    if not template["sub_title_text"]:
        del template["sub_title_text"]
    return {"touser": touser, "msgtype": "template_card", "agentid": agentid, "template_card": template,
            "enable_duplicate_check": 0}
