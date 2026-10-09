---
name: remoteci
description: 以用户本人的 RemoteCI 账号调用 REST API，按该账号的权限查看和管理 ClassIsland 课堂。适用于：查课表、当前课程或老师的下一节课；向班级发通知、换课、音量电源等远程控制和多班集控；老师发起、审批、拒绝、撤回换课申请与强制换课，查看个人通知；管理员创建和管理班级、分组、人员与成员分配。
---

# RemoteCI

RemoteCI 服务端把各教室 ClassIsland 的课表和控制能力汇总成 REST API。每个请求都以某个账号的身份执行，服务端按这个账号在目标班级里的权限放行或返回 `403`，所以先连接、再认清身份，最后只做该账号有权做的事。

## 1. 连接

需要服务器地址和一种凭据。先读环境变量：`REMOTECI_BASE_URL`，以及 `REMOTECI_API_KEY` 或 `REMOTECI_USERNAME` + `REMOTECI_PASSWORD`。缺什么就问用户什么：先要服务器地址（如 `https://remoteci.example.com`），再请用户二选一：

- **API Key**：用户在 WebUI“个人账号 → API Key”创建，以 `rci_` 开头。账号需要“API 访问”权限，管理员、班主任、老师默认有，学生默认没有。
- **账号密码**：任何角色都能用，学生通常只能用这种方式。

API Key 直接放进请求头 `Authorization: Bearer $REMOTECI_API_KEY`。

账号密码先登录，换取令牌：

```bash
curl -fsS -X POST "$REMOTECI_BASE_URL/api/auth/login" -H "Content-Type: application/json" \
  -d "{\"username\":\"$REMOTECI_USERNAME\",\"password\":\"$REMOTECI_PASSWORD\",\"deviceName\":\"AI Agent\"}"
```

响应里的 `accessToken` 有效 1 小时，用作 `Authorization: Bearer <accessToken>`。同时返回的 `deviceSessionId` 和 `deviceSecret` 用于续期，保存在 shell 变量里。

- **续期**：令牌过期（`401`）时调用 `POST /api/auth/refresh`，请求体为 `{"deviceSessionId":"…","deviceSecret":"…"}`。新响应会同时换掉令牌和 `deviceSecret`，旧值立即失效。
- **限流**：登录和续期接口有频率限制，一个任务只登录一次。
- **退出**：任务结束时调用 `POST /api/auth/logout`（带令牌），移除这次产生的“AI Agent”设备会话。
- **未激活账号**：响应 `passwordPending: true` 表示账号还没设置密码，请用户先在 WebUI 登录页完成首次设置。

密码和密钥只经过环境变量、shell 变量和请求头或请求体传递。回复和日志里一律写作 `rci_…`、`***`，也不要把它们放进 URL。

完成标志：`GET /api/me` 返回 `200`。

## 2. 认清身份与权限

`GET /api/me` 返回账号信息，需要关注以下字段：

- `role`：`2` 是系统管理员，`1` 是其他账号。
- `roleKind`：`2` 管理员、`4` 班主任、`5` 老师、`1` 学生；其他值是自定义角色。
- `permissions`：全局权限位。
- `classes[]`：可访问的班级，每项含 `id`、`name`、该账号在本班的 `roleKind` 和 `permissions`。

能在某个班做什么，以该班的 `classes[].permissions` 为准：

| 位 | 权限 | 位 | 权限 |
| ---: | --- | ---: | --- |
| 1 | 查看课程与课表 | 64 | 保留权限位 |
| 2 | 概览 | 128 | 运行扩展 |
| 4 | 人员管理 | 256 | 主界面显隐 |
| 8 | 发送通知 / 清除提醒 | 512 | 发送语音 |
| 16 | 换课 / 科目教师 | 1024 | 修改用户名（兼容保留） |
| 32 | 音量与电源 | 2048 | API 访问 |
| 4096 | 老师主动换课（换课申请） | 8192 | 强制换课 |

各角色的默认权限：

- **管理员**：全部为 16383，并且能管理整个系统。
- **班主任**：在本班通常为 6875，即查看、概览、通知、换课、扩展、语音、API 和换课申请。也有“我的日程”，规则和老师相同。
- **老师**：全局为 6665，即查看、通知、语音、API 和换课申请。另有“我的日程”，按显示名与课表里的科目教师自动匹配。老师没有直接换课的 16 位，只能通过换课申请让对方老师审批；强制换课（8192）默认关闭，由系统管理员在“角色配置”中开启。

换课申请（4096、8192）是全局权限位，看 `GET /api/me` 的 `permissions`，不看 `classes[].permissions`。
- **学生**：只有 1，只能查看自己所在班级的课程和课表。

用户要做的事超出权限时，告诉他缺哪一位权限，并说明可以请系统管理员在 WebUI“人员权限”里授予。

## 3. 查看（所有角色）

以下请求都带 `?classId=<id>`，班级 ID 取自 `classes[]`；省略时服务端使用该账号的默认班级。

- `GET /api/me/classes`：列出可访问的班级。
- `GET /api/schedule?classId=…`：今天起七天的课表。返回 `days[]`，每天有 `date`、`revision` 和 `courses[]`；每节课含 `index`、`label`、`subject`、`startTime`、`endTime`、`teacher`。另有 `subjects[]`。
- `GET /api/state?classId=…`：当前课堂状态。`currentState` 取值：`1` 上课、`2` 课间、`3` 放学、`4` 预备、`0` 无课；另有 `currentSubject`、`nextClassSubject`、`onClassLeftTime`。
- `GET /api/extensions?classId=…`：本班可运行的插件扩展。
- `GET /api/extension-groups?classId=…`：本班设备上报的扩展插件分组及其设置字段；有权修改设置时还返回当前值 `values` 和 `canEditSettings: true`。修改方法见 [control.md](references/control.md#扩展插件设置)。

`404 尚无课表` 或 `404 尚无课程状态` 表示这个班的 ClassIsland 插件还没把数据同步到服务端，可能是插件离线或服务端刚重启，请用户稍后再试。

**老师和班主任的“我的日程”**（全局 `roleKind` 为 `5` 或 `4` 时可用）：

- `GET /api/me/schedule`：跨班聚合的七天日程。返回 `days[]`，每天的 `items[]` 按班级分组，含 `className` 和 `courses[]`。
- `GET /api/me/schedule/next`：返回 `current`（正在上的课）和 `next`（下一节），每项含 `className`、`course`、`startsAt`、`endsAt`。查询其他时刻时加 `?at=<ISO 8601>`，时间里的 `+` 写成 `%2B`。

回答“下节课去哪”时包含班级、科目、节次和起止钟点；有 `current` 时先说现在正在上什么。`startTime`/`endTime` 是教室当地钟点，直接引用；`startsAt`/`endsAt` 只用来计算“还有多久”。

日程为空时，按顺序排查，把第一个成立的原因告诉用户：

1. 账号不是老师角色。
2. 账号的 `displayName` 与课表里的教师名不一致。两者必须完全相同；多教师科目写成 `张三/李四` 时匹配其中一个即可。
3. 七天内已经没有这位老师的后续课程。
4. 插件尚未同步课表。

## 4. 控制教室与集控

用户要对班级执行操作时，先读 [control.md](references/control.md)：发通知、语音、换课、科目教师、主界面、音量、电源、扩展、插件与软件维护、远程终端、文件分发，或者同时对多个班级或分组广播。

## 5. 换课申请与个人通知（老师、班主任）

用户要和其他老师换课、处理别人发来的换课申请、强制换课或撤回被强制换走的课，或者问“有没有人找我换课”时，先读 [swap-requests.md](references/swap-requests.md)。

## 6. 系统管理（系统管理员）

用户要创建、改名或删除班级，管理分组、账号、成员分配、角色、访客页或插件配对码时，先读 [admin.md](references/admin.md)。

## 7. 服务端档案（系统管理员、班主任）

用户要查看、保存、复制、删除服务端档案库中的模板或班级档案，或者把档案下发到教室电脑时，先读 [profiles.md](references/profiles.md)。

## 8. 调休与节假日

用户问“哪天放假”“哪天调休上学、上周几的课”，或者管理员要修改调休补课安排、关闭调休适配时，先读 [holidays.md](references/holidays.md)。

## 错误码

| 状态 | 含义 |
| --- | --- |
| `401` | 凭据无效或过期：令牌先续期；API Key 可能已吊销，或账号失去了 API 权限 |
| `403` | 当前账号在该班没有对应权限，或该操作只允许系统管理员 |
| `404` | 对象不存在，或插件尚未同步数据 |
| `409` | 冲突：用户名已存在、最后一个管理员，或换课时课表已被别人修改（`SCHEDULE_STALE`）；换课申请的课位已变化（`SWAP_SLOT_CHANGED`）、申请已被处理（`SWAP_STATE_CONFLICT`），或当天这节课的强制换课已被撤回（`SWAP_FORCE_LOCKED`） |
| `400` | 请求无效；换课申请两节课都不是自己的（`SWAP_NOT_OWN`），或目标班级没有该学科（`SWAP_SUBJECT_MISSING`） |
| `503` / `504` | 教室端插件离线，或执行超时 |
