# 控制教室与集控

## 单个班级：`POST /api/commands?classId=<id>`

请求体为 `{"command": <编号>, ...载荷}`。服务端按账号在该班的权限鉴权，然后把命令交给这个班在线的 ClassIsland 插件，并等待最多 15 秒的回执：

```bash
curl -fsS -X POST "$REMOTECI_BASE_URL/api/commands?classId=$CLASS_ID" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"command":2,"notification":{"title":"班会通知","message":"下午第 8 节改为班会","isNotificationEffectEnabled":true}}'
```

回执格式为 `{"success":true,"code":"OK","message":"…","data":"…"}`。`data` 只在终端输出、文件保存路径等场景出现。

| 编号 | 操作 | 载荷 | 所需权限位 |
| ---: | --- | --- | ---: |
| 2 | 发送通知 | `notification`，见下 | 8 |
| 3 | 清除当前提醒 | 无 | 8 |
| 9 | 语音消息 | `voiceMessage`：`{"format":"pcm_s16le_16000_mono","audioBase64":"…"}`，16 kHz 单声道 16 位 PCM，最长 60 秒 | 512 |
| 1 | 换课 | `scheduleChange`，见下 | 16 |
| 21 | 设置科目教师 | `subjectTeacher`：`{"subjectId":"…","teacherName":"王明"}`，`teacherName` 为空表示清除 | 16 |
| 4 | 显示 / 隐藏主界面 | `mainMenuVisible`：`true` 或 `false` | 256 |
| 6 | 音量 | `volume`：`{"level":0-100,"muted":false}`，两项都可选 | 32 |
| 5 | 电源 | `powerAction`：`1` 关机、`2` 重启、`3` 睡眠、`4` 休眠 | 32 |
| 7 | 运行插件扩展 | `extensionId`，以及可选的 `extensionArgs` 键值对；扩展列表来自 `GET /api/extensions`。参数按扩展声明校验：数字须在 `min`/`max` 内，开关只能是 `"true"`/`"false"`，选项须是 `options` 之一 | 128 |

`notification` 的字段：

- `title`、`message`：通知标题和正文。
- 可选开关（布尔值）：`isNotificationEffectEnabled`（强调特效）、`isNotificationSoundEnabled`（提醒音效）、`isSpeechEnabled`（语音朗读）、`isNotificationTopmostEnabled`（置顶主界面）、`isRollingEnabled`（正文滚动显示；省略时按旧客户端处理为滚动，需要静态正文时必须显式传 `false`）。
- 可选数值：`durationSeconds`（持续时间，1-3600 秒）、`repeatCounts`（重复次数，1-10 次）。标题最多 60 字、正文最多 500 字，越界返回 `INVALID_REQUEST`。开启滚动时正文滚动 `repeatCounts` 遍；关闭时整条提醒依次显示 `repeatCounts` 次。
- 正文超过 30 字时建议开启 `isRollingEnabled`，否则静态正文可能显示不全。

`scheduleChange` 的字段：

- `date`：七天内的 `yyyy-MM-dd`。
- `mode`：`1` 交换、`2` 替换。
- `sourceIndex`：原节次在当天 `courses` 中从 0 开始的位置。
- `targetIndex`：交换时必填。
- `replacementSubjectId`：替换时必填，取自课表的 `subjects[]`。
- `expectedRevision`：当天的 `days[].revision`，用来防止覆盖别人刚做的修改。
- `permanent`：`true` 时写入源课表，之后每周都生效；默认 `false`，只改当天。

先 `GET /api/schedule` 取得 `index`、`revision` 和科目 ID 再提交。返回 `409 SCHEDULE_STALE` 说明有人刚改过课表，重新读取后再提交。

### 软件与设备维护（仅系统管理员）

以下命令要求系统管理员，并且教室端插件声明了对应能力：

| 编号 | 操作 | 载荷 |
| ---: | --- | --- |
| 12 | 刷新软件版本清单 | 无 |
| 10 | 升级插件 | `softwareUpgrade`：`{"pluginIds":["…"],"force":false}`，`pluginIds` 省略表示全部插件 |
| 11 | 升级 ClassIsland | 无 |
| 20 | 重启 ClassIsland（不重启 Windows） | 无 |
| 13 / 14 / 15 | 安装 / 卸载 / 启停插件 | `pluginManagement`：`{"action":1 安装 \| 2 卸载 \| 3 启用 \| 4 禁用,"pluginIds":["…"],"restartAfter":false}` |
| 16 | 远程插件管理策略 | `pluginManagementPolicy`：`{"allowRemoteInstall":true,"allowRemoteUninstall":true}` |
| 17 | 分发 ClassIsland 档案 | `profileDistribution`，见下 |
| 18 | 新增或替换时间表 | `timeLayoutUpdate`，见下 |
| 19 | 加入 ClassIsland 集控 | `managementJoin`：`{"presetJson":"<ManagementPreset.json 内容>"}` |
| 22 | 远程终端 | `terminalCommand`：`{"command":"hostname","workingDirectory":null,"timeoutSeconds":10}`，等同 `cmd /d /c`，结果在 `data` 里 |
| 23 | 文件分发 | `fileDistribution`：`{"fileName":"a.pdf","contentBase64":"…","targetFolder":1 桌面 \| 2 下载 \| 3 文档,"overwrite":false}`，文件最大 10 MB |

`profileDistribution` 的字段：

- `profileJson`：ClassIsland 档案 JSON。
- `sections`：位掩码，`1` 时间表、`2` 课表、`4` 科目。
- 可选：`importProfileName`、`replaceCurrentProfile`（默认 `false`）、`enableImportedProfile`（默认 `true`）、`replaceExisting`、`restartAfter`。

`timeLayoutUpdate` 的字段：

- `name`：时间表名称。
- `activate`：是否设为当前时间表。
- `points[]`：每项为 `{"startTime":"08:00","endTime":"08:40","timeType":0,"breakName":null}`，`timeType` 为 `0` 上课、`1` 课间。
- 可选：`timeLayoutId`（替换已有时间表）、`restartAfter`。

## 服务端档案管理

新档案功能通过 WebUI 的独立页面操作：管理员进入“档案管理”(`/Profiles`) 维护全局模板、批量编辑和下发；本班班主任进入班级菜单“档案”(`/ClassProfiles`) 管理本班副本，且需要本班“换课 / 科目教师”权限（16）。模板分配为独立副本；设备当前档案不会自动同步，可用“从设备收集”（命令 `26 ReadProfile`，能力 `profile.read`）读取后作为草稿编辑再保存；班级离线时也能保存服务端档案。

档案页面使用命令 `25 ApplyProfile` 和能力 `profile.apply`，包含更新当前档案、整体替换所选类别、创建并启用新档案、作为临时层下发（另需能力 `profile.temp-layer`）四种显式选择方式。`25` 与 `26` 都属于 `serverOnly`：只能经档案管理页面或档案接口 `POST /api/profiles/apply`、`POST /api/profiles/collect` 发起，`POST /api/commands`、广播、手机/手表命令通道和局域网直连都拒绝。查看、保存和下发服务端档案的接口见 [profiles.md](profiles.md)；能力缺失时说明需要升级教室端插件。

上面的命令 `17`、`18` 仅保留原有管理员 API 兼容语义。按旧接口操作时仍使用原载荷和权限；它们不会读写服务端档案库，也不能代替命令 `25`。新页面保存与设备下发分开，下发结果逐设备报告，离线不会自动排队。

## 扩展插件设置

其他 ClassIsland 插件可以声明“插件设置”（例如提醒间隔、播报音色）。修改设置不走 `/api/commands`（命令 `24` 在该接口和广播接口都会被拒绝），使用专用接口：

1. `GET /api/extension-groups?classId=…` 读取分组。每个分组有 `id`、`displayName`、`settings[]`（字段 `key`、`label`、`type` 为 `1` 文本 / `2` 数字 / `3` 开关 / `4` 选项、`options` 与对应显示名 `optionLabels`、`min`、`max`、`required`）、当前值 `values`（无权修改该插件时为 `null`）、`canEditSettings`（当前账号能否修改）和 `allowClassAdmin`（该插件是否已开放给班主任）。
2. `PUT /api/classes/{classId}/extension-groups/{groupId}/settings`，请求体 `{"values":{"interval":"15"}}`。只放要修改的字段，其余保持原值；值一律是字符串，开关用 `"true"`/`"false"`，选项填 `options` 里的原值而不是显示名。
3. 返回与 `/api/commands` 相同的 `CommandResult`；`400` 表示字段或取值不合法，按 `message` 修正；`202` 且 `code` 为 `QUEUED` 表示该班插件离线，设置已保存，插件上线后会自动补发，如实告诉用户“尚未生效、上线后自动生效”。

**谁能改**：系统管理员可以改任意班级；班主任只能改系统管理员逐个开放给班级自行管理的插件（分组的 `allowClassAdmin` 为 `true`），且本班有“扩展”权限（128），只作用于自己担任班主任的班级，否则 `403`。新插件默认都不开放。

**开放或收回（仅系统管理员）**：`PUT /api/extension-groups/{groupId}/class-admin-access`，请求体 `{"allowClassAdmin":true}`；对全部班级生效，插件尚未上报时也可以先设置。开放后班主任的 WebUI 侧栏出现“扩展插件”入口，只列出已开放的插件。

**多个班级**：没有批量接口，按班循环调用上面的 `PUT`，并逐班汇报结果；某班返回“没有上报该扩展分组”表示那台教室电脑没装这个插件。

## 多个班级或分组：`POST /api/commands/broadcast`

```json
{"command":2,"notification":{"title":"年级大会","message":"15:30 报告厅集合"},"classIds":[],"groupIds":["<分组 id>"]}
```

- **支持的命令**：只有 `2` 通知、`3` 清除提醒、`5` 电源（带 `powerAction`）和 `9` 语音。
- **目标**：`classIds` 和 `groupIds` 至少填一项。分组会展开为它包含的班级，班级 ID 来自 `GET /api/me/classes`，分组 ID 来自 `GET /api/class-groups`（管理员）。
- **鉴权**：服务端逐班按账号权限判断。
- **返回**：`results[]`，每班一项，含 `classId` 和 `success`。逐项汇报给用户，不要只说“已完成”。

升级、安装插件、终端等维护命令没有广播接口，按班循环调用 `/api/commands`。

## 先确认再执行

以下操作执行前，先向用户复述目标班级和具体内容，等用户明确同意：

- 电源（`5`）、重启 ClassIsland（`20`）。
- 卸载或禁用插件、修改插件策略。
- 覆盖档案、替换时间表。
- 远程终端、文件分发。
- 修改扩展插件设置，尤其是同时改多个班级。
- 向一个分组或全校广播。
- 强制换课（`POST /api/swap-requests` 带 `"force": true`），见 [swap-requests.md](swap-requests.md#强制换课)。
