# 调休

RemoteCI 服务端自动从 holiday-cn 获取法定节假日和调休安排，并下发给各教室电脑：

- **放假日**：教室 ClassIsland 自动关闭课表。
- **调休上学日**（原本是周末但要上学）：自动开启一份临时课表，上某个工作日的课。

默认按"同一假期内最后几个放掉的工作日"自动推算补哪天的课，管理员可以逐天改。

## 查询（任何已登录账号）

`GET /api/holidays` 返回：

| 字段 | 含义 |
| --- | --- |
| `enabled` | 调休自动适配是否开启 |
| `status.lastSuccessAt` / `status.lastError` | 上次成功刷新时间 / 最近一次失败原因 |
| `periods[]` | 近期假期：`name`、`offStart`、`offEnd`（放假起止），`makeupDays[]` |
| `periods[].makeupDays[]` | `date`、`autoWeekday`（自动推算）、`followWeekday`（最终生效，1=周一 … 5=周五，null 表示不补课/无法推算）、`followSource`（`auto` / `manual` / `skip` / `unresolved`） |
| `staleOverrideDates` | 已失效的手动安排（那天已不是调休上学日） |

回答用户时说清楚：哪几天放假、哪天调休上学、上周几的课。`followSource` 为 `unresolved` 时提醒管理员手动指定。

## 修改（仅系统管理员）

修改前先向用户复述日期和补课安排，得到确认后再调用。

| 操作 | 请求 |
| --- | --- |
| 改成上周 N 的课 | `PUT /api/admin/holidays/overrides/{yyyy-MM-dd}`，body `{"followWeekday": N}`（N 为 1-5） |
| 这天不补课 | 同上，body `{"followWeekday": null}` |
| 恢复自动推算 | `DELETE /api/admin/holidays/overrides/{yyyy-MM-dd}` |
| 开关 / 换数据源 | `PUT /api/admin/holidays/settings`，body `{"enabled": true, "sourceUrlTemplate": null}`（自定义地址必须是包含 `{year}` 的 https 地址） |
| 立即刷新数据 | `POST /api/admin/holidays/refresh`，返回 `lastAttemptAt`、`lastSuccessAt`、`lastError` |

日期不是调休上学日、日期格式不对、`followWeekday` 不在 1-5 时返回 400。修改成功后返回最新的总览，以它为准向用户汇报。
