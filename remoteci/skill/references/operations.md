# 集控运维

设备状态、操作记录、自动化规则和课时统计。可见范围：系统管理员看全部班级；班主任（在该班的班内角色为“班主任”）只看自己担任班主任的班级；其他账号返回 403。自动化只有系统管理员能管理。

## 设备状态

`GET /api/devices/health` 返回已分配班级的教室电脑列表，每项含：

- `className`、`deviceName`、`online`（是否在线）、`lastSeenAt`、`offlineSince`（离线开始时间）
- `pluginVersion`、`classIslandVersion`、`outdated`（插件版本低于服务端，需要升级）
- `currentCourse`（如“上课中 · 数学”）、`volumePercent`、`muted`
- `profileDriftCount`：最近一次档案差异检查发现的差异数，0 或空表示一致或未检查

回答“哪些教室电脑离线”时列出班级、设备名和离线多久；有 `outdated` 的设备建议升级插件（升级方法见 [control.md](control.md)）。

离线告警由服务端自动发送：设备在班级在校时段（第一节课前 30 分钟到最后一节下课）连续离线超过阈值（默认 10 分钟）时，系统管理员和该班班主任会收到个人通知 `device_offline`，恢复后收到 `device_online`。阈值等设置只能在 WebUI“系统配置 › 集控运维”修改。

## 操作记录

`GET /api/audit?classId=&from=&to=&command=&success=&limit=&before=` 按时间倒序返回 `{items, nextBefore}`。每项含 `createdAt`、`actorName`（谁）、`sourceLabel`（入口：WebUI、手机 App、手表、API Key、自动化、换课申请等）、`sourceDetail`、`className`、`deviceName`、`commandLabel`（操作）、`summary`（内容摘要）、`success`（null 表示还在等回执）、`resultMessage`。

- 用户问“谁把三班的电脑关了”“今天谁发过通知”时用它，按 `classId` 和日期筛选，必要时用 `command`（如电源为 `5`、通知为 `2`）缩小范围。
- 下一页把 `nextBefore` 作为 `before` 传回。导出 Excel 用 `GET /api/audit/export`（同样的筛选参数）。
- 记录不含语音、文件内容或档案 JSON；默认保留 180 天。

## 自动化规则（系统管理员）

| 请求 | 说明 |
| --- | --- |
| `GET /api/automation/rules` | 规则列表，每项含 `id`、`name`、`enabled`、`description`（可读的触发与动作）、`lastRunAt`、`lastSuccess`、`lastResult` |
| `GET /api/automation/rules/{id}` | 完整规则 |
| `POST /api/automation/rules` | 新建 |
| `PUT /api/automation/rules/{id}` | 整体保存：先 GET 完整规则，只改需要改的字段（例如 `enabled`）再整体提交 |
| `DELETE /api/automation/rules/{id}` | 删除规则及其执行记录 |
| `POST /api/automation/rules/{id}/run` | 立即对全部目标班级执行一次，返回各班 `status`（`Succeeded`/`Failed`）与 `message` |
| `GET /api/automation/runs?ruleId=&limit=` | 执行记录 |

规则结构：

```json
{
  "name": "放学 30 分钟后关机",
  "enabled": true,
  "trigger": { "kind": 2, "event": 3, "delayMinutes": 30 },
  "target": { "classIds": ["…"], "groupIds": [] },
  "actions": [
    { "kind": 1, "notification": { "title": "电脑即将关机", "message": "请保存好文件。", "durationSeconds": 20 } },
    { "kind": 5, "powerAction": 1 }
  ],
  "validFrom": null,
  "validTo": null
}
```

- `trigger.kind`：`1` 定时（`weekdays` 1=周一…7=周日，`time` 为 `HH:mm` 教室本地时间，`schoolDaysOnly` 为 true 时放假日跳过、调休上学日照常）；`2` 课表事件（`event` 1 上课、2 下课、3 放学，可选 `subject`，`delayMinutes` 0–180）；`3` 教室电脑上线（`delaySeconds` 0–600）。
- `actions[].kind`：`1` 通知、`2` 清除提醒、`3` 主界面（`mainMenuVisible`）、`4` 音量（`volume: {level, muted}`）、`5` 电源（`powerAction` 1 关机、2 重启、3 睡眠、4 休眠）、`6` 扩展（`extensionId`、`extensionArgs`）、`7` 下发该班档案的临时层（`replaceExistingTempLayers`）。最多 5 个动作。
- 规则以最后保存它的系统管理员身份执行。创建或修改关机、重启类规则前，向用户复述触发时间和目标班级并确认；“立即运行”同样需要确认。

## 课时统计

| 请求 | 说明 |
| --- | --- |
| `GET /api/me/lesson-stats?from=&to=` | 本人课时（按显示名匹配），任何账号可用 |
| `GET /api/lesson-stats?from=&to=&classId=&teacher=&includeProfile=` | 系统管理员看全部，班主任看本班 |
| `GET /api/lesson-stats/export` | 导出 Excel（汇总 + 明细） |

返回 `{from, to, lessonCount, teachers:[{teacher, total, substitute, substituted, classes:[{className, total}]}]}`：`total` 实际上课节数（含代课），`substitute` 其中代别人上的，`substituted` 本该自己上而被别人代的。日期为 `yyyy-MM-dd`，省略时为本月。台账从服务端升级后逐节累积，服务端停机期间的课无法补录；默认不含按档案推算的课。

## 应用守护（系统管理员）

应用守护让教室电脑上的 ClassIsland 在被退出、崩溃或删除后自动恢复运行，由插件在教室电脑上部署独立的守护进程执行。

| 请求 | 说明 |
| --- | --- |
| `GET /api/settings/app-guard` | 全局策略 |
| `PUT /api/settings/app-guard` | 保存全局策略并立即下发给在线设备 |
| `GET /api/devices/{credentialId}/app-guard` | 某台设备的生效策略，`overridden` 表示是否单独设置 |
| `PUT /api/devices/{credentialId}/app-guard` | 让该设备改用单独策略 |
| `DELETE /api/devices/{credentialId}/app-guard` | 该设备恢复沿用全局 |
| `GET /api/app-guard/events?credentialId=&limit=` | 最近守护事件，最多 500 条 |

`credentialId` 取自 `GET /api/devices/health` 的同名字段。

策略字段：

- `enabled`：总开关。
- `restartAfterExit`、`restartAfterExitDelayMinutes`：退出自启及其延时，0–1440 分钟，0 为立即。“退出 40 分钟后自启”即 `true` 加 `40`。
- `restartOnCrash`：意外退出时重启。
- `preventDeletion`：禁止删除。定时备份到教室电脑的 `%LOCALAPPDATA%\RemoteCI\Guard`，被删除后自动恢复、重新启动，并弹窗提示。
- `backupIntervalHours`：备份间隔，1–168 小时。
- `organizationName`：只读，是恢复提示“当前ClassIsland由xx统一管理”中的名称，只能在 WebUI“系统配置 › 组织信息”修改。

`PUT` 是整体保存：先 GET，只改需要改的字段，再整体提交。

事件 `kind` 取值：

- `restored`：已自动恢复；
- `restore-failed`：恢复失败；
- `crash-restarted`：意外退出后重启；
- `exit-restarted`：退出后自启；
- `crash-loop-paused`：频繁崩溃，暂停重启；
- `backup-completed`：备份完成；
- `backup-failed`：备份失败。

回答时用事件里的 `kindName` 与 `detail`。自动恢复和恢复失败会给系统管理员发个人通知 `app_guard`。

## 只能在 WebUI 完成的操作

- 设备状态大屏、离线告警阈值与通知对象、操作记录保留天数：WebUI“设备状态”“系统配置 › 集控运维”。
- 应用守护恢复提示里的组织名：WebUI“系统配置 › 组织信息”。
- 电子班牌链接（教室门口班牌屏的私密链接）：班级区“电子班牌”，系统管理员或本班班主任生成，链接只显示一次。
- 档案历史与设备差异的可视化对比：见 [profiles.md](profiles.md#历史版本与设备差异)。
