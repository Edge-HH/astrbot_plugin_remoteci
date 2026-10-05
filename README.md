# astrbot_plugin_remoteci

[AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：把 [RemoteCI](https://github.com/Edge-HH/RemoteCI) 接入 QQ、Telegram、飞书等聊天平台。

> [!WARNING]
> 开发中。需要 AstrBot ≥ 4.9（插件页面、插件 Web API）。

## 功能

- **对话连接账号**：私聊发送 API Key 或用户名密码完成绑定，也可以直接用自然语言说。账号密码登录后只保存续期凭据，不保存密码；群聊中拒绝绑定。
- **RemoteCI Skill 的全部能力**：查课表、我的日程、下节课、教室状态；通知、老师来了、换课、音量电源、扩展、集控广播；班级/分组/账号/成员/配对码/备份等系统管理。服务端按账号权限鉴权，高风险操作需要先确认。
- **指令 + 自然语言**：`/rci 帮助` 查看全部指令；其余操作直接用自然语言（LLM 工具 `remoteci_*`）。
- **群聊只在 @ 机器人时处理**，且群聊不做个人主动提醒。
- **老师主动提醒**（个人私聊）：
  - 上学时推送当天个人日程（默认 07:00）；
  - 自己最后一节课下课时推送明天的个人日程（当天无课改用备用时刻，也可改为固定时间）；
  - 课前提醒（默认提前 10 分钟）；
  - 自己的课被换了提醒；
  - 班主任额外接收所管班级的课表变动（含任课教师变化）。
  - 老师可以用指令或自然语言开关每一项、修改当日/次日推送时间。
- **班级群定时课表**：在 WebUI 把群设为“班级群”并显式开启后，按设定时间推送班级课表。
- **节假日自动暂停**：法定节假日（含调休，数据来自 [holiday-cn](https://github.com/NateScarlet/holiday-cn)）、可选“周末视为假期”、自定义寒暑假区间。
- **内置 WebUI**（AstrBot 管理面板 → 插件 → RemoteCI → 页面）：查看聊天身份与 RemoteCI 姓名的绑定关系，设置会话角色（老师 / 班主任 / 班级群），统一提醒默认值，节假日，推送记录。风格与 RemoteCI 服务端 WebUI 一致。

## 指令

```text
/rci 绑定 <API Key> [服务器]       /rci 登录 <用户名> <密码> [服务器]
/rci 我 | 解绑
/rci 今天 | 明天 | 本周 | 周三 | 10-05     我的日程
/rci 下节                                正在上和下一节
/rci 班级 [班级名] [日期]                  班级课表
/rci 状态 [班级名] | 班级列表 | 假期
/rci 提醒                                查看提醒设置
/rci 提醒 开|关 <当日|次日|课前|换课|班级换课|全部>
/rci 提醒 当日 07:30 | 次日 20:00 | 次日 下课 | 课前 15 | 重置
/rci 通知 <班级名> <内容> | 老师来了 <班级名>
```

`/rci` 也可以写成 `/课表`。

## 配置

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `default_server_url` | 默认 RemoteCI 服务器地址，填写后绑定时可省略 | 空 |
| `timezone` | 学校时区 | `Asia/Shanghai` |
| `poll_minutes` | 换课检查间隔（分钟） | `5` |
| `group_require_at` | 群聊仅在 @ 机器人时处理 | `true` |

## 数据

保存在 `data/plugin_data/astrbot_plugin_remoteci/`：`state.json`（绑定、会话、设置、推送记录）、`snapshots.json`（换课比对基线）、`holiday-<年>.json`（节假日缓存）。凭据（API Key 或续期密钥）以明文保存在该目录，请保护好 AstrBot 数据目录。

## 开发

业务逻辑在 `remoteci/`，不依赖 AstrBot，可以直接测试：

```bash
pip install pytest aiohttp
python -m pytest -q
```

`remoteci/skill/` 是 RemoteCI 仓库 `skills/remoteci/` 的副本，供 `remoteci_reference` 工具读取；RemoteCI 新增功能时需要同步 API、Skill 和本插件。
