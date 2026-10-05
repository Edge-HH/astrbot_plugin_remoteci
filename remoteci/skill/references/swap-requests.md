# 换课申请与个人通知

老师（以及班主任）可以向其他老师发起**临时换课**申请，由对方老师审批后才写入教室端课表。永久换课仍只能由有换课权限（16）的班主任在课表页或 `POST /api/commands` 中操作，见 [control.md](control.md)。

所需权限都在 `GET /api/me` 的全局 `permissions` 中：

- `4096` 老师主动换课：可以发起、审批、拒绝换课申请。内置老师和班主任默认开启。
- `8192` 强制换课：不经审批立即生效。默认关闭，由系统管理员在“人员权限 → 角色配置”中开启。

## 选课：`GET /api/swap-requests/catalog`

返回所有班级今天起七天、已叠加临时任课老师的课表，以及本人任教的学科：

```json
{
  "teacherName": "王老师",
  "canForce": false,
  "mySubjects": ["数学"],
  "classes": [{
    "classId": "…", "className": "高一1班", "subjects": ["数学", "英语"],
    "days": [{ "date": "2026-10-06", "courses": [
      { "index": 2, "label": "第3节", "subject": "数学", "teacher": "王老师", "startTime": "10:00", "mine": true }
    ]}]
  }]
}
```

一节课用 `{classId, date, index}` 表示，三个值都从这里取，不要猜。

## 发起：`POST /api/swap-requests`

```json
{
  "mode": 1,
  "source": { "classId": "…", "date": "2026-10-06", "index": 2 },
  "target": { "classId": "…", "date": "2026-10-07", "index": 1 },
  "reason": "周一下午外出教研",
  "force": false
}
```

- **`mode: 1` 互换**：`source` 是要换走的课，`target` 是目标课。两节都可以跨班、跨日，也可以选别人的课，但至少有一节必须是自己的课（`mine: true`），否则返回 `400 SWAP_NOT_OWN`。跨班时，两边班级都要有对方的学科（按名称匹配），否则返回 `400 SWAP_SUBJECT_MISSING`。
- **`mode: 2` 替换**：省略 `source`，填 `target` 和 `subjectName`（必须在 `mySubjects` 中）。目标课会临时改成“我上的这门课”，即使目标班不是自己教的班，也会计入本人的“我的日程”。
- **`reason`**：必填，最多 200 字。
- **审批人**：对方课的老师。如果课表里的老师名匹配不到账号，由该班班主任审批。两节都是自己的课时不需要审批，直接生效。

响应是一条 `SwapRequestView`。`status` 取值：

| 值 | 状态 | 值 | 状态 |
| ---: | --- | ---: | --- |
| 1 | 待审批 | 5 | 已过期（日期已过仍未审批） |
| 2 | 已通过并生效 | 6 | 已强制换课 |
| 3 | 已拒绝 | 7 | 强制换课已被撤回 |
| 4 | 申请人已撤销 | | |

其他字段：

- `shortId`：8 位短编号，可以代替 `id` 用在下面所有路径里。
- `source` / `target`：申请时的课位快照，含 `className`、`label`、`subject`、`teacher`。
- 理由与审批：`reason`、`approverNames`、`decisionNote`。
- 当前账号能做的操作：`canDecide`（通过/拒绝）、`canRevoke`（撤回强制换课）、`canCancel`（撤销自己的申请）。

## 强制换课

**先确认再执行。** 提交 `"force": true` 前，先向用户复述两节课，并原样转告这句话：“仅在需要紧急换课时使用，请提前与对方沟通并达成一致。”用户明确同意后再提交。

- 强制换课立即生效，`status` 为 `6`，并通知对方老师和相关班主任。
- 对方老师可以撤回。撤回后课表恢复，申请人当天不能再强制换走这节课，返回 `409 SWAP_FORCE_LOCKED`。这时只能提交普通申请。
- 这个锁只针对“这位申请人 + 这一天 + 这节课”，不影响其他课、其他老师，也不影响下一周的同一节课。

## 查看与处理

- `GET /api/swap-requests?box=incoming`：发给我的（待我审批，或被强制换走、可撤回的）。
- `GET /api/swap-requests?box=outgoing`：我发起的。`box=all` 返回两者；可以加 `&status=1` 过滤。
- `GET /api/swap-requests/{id}`：单条详情。
- `POST /api/swap-requests/{id}/approve`，body 为 `{"note":"可选备注"}`：通过。服务端立刻按最新课表复核并下发临时换课，然后通知申请人和相关班主任。
- `POST /api/swap-requests/{id}/reject`，body 同上：拒绝，并通知申请人。
- `POST /api/swap-requests/{id}/cancel`：申请人撤销还在待审批的申请。
- `POST /api/swap-requests/{id}/revoke`：对方老师撤回强制换课。

处理失败的常见原因：

- `409 SWAP_SLOT_CHANGED`：课位在申请后又被改过，请申请人重新提交。
- `409 SWAP_STATE_CONFLICT`：申请已经被处理过。
- `503` / `504`：教室端插件离线或超时。这时申请仍是待审批，稍后可以再点通过。

## 个人通知

- `GET /api/me/notifications?unread=true`：未读通知；轮询时用 `?after=<上次最新的 createdAt>`。
- `POST /api/me/notifications/read`，body 为 `{"ids":["…"]}` 或 `{"all":true}`：标记已读。

每条通知含 `kind`、`title`、`body`、`swapRequestId`、`createdAt`、`readAt`。`kind` 取值：

| `kind` | 含义 |
| --- | --- |
| `swap_requested` | 有人向我申请换课 |
| `swap_approved` | 我的申请被通过 |
| `swap_rejected` | 我的申请被拒绝 |
| `swap_cancelled` | 申请人撤销了申请 |
| `swap_forced` | 我的课被强制换走 |
| `swap_revoked` | 我的强制换课被撤回 |
| `swap_homeroom_info` | 本班课表因换课临时变化（发给班主任） |

用户问“有没有人找我换课”时，先查 `box=incoming&status=1`。回答时给出申请人、两节课、理由和 `shortId`，再问是否通过。
