# 换课申请与个人通知

老师（以及班主任）可以向其他老师发起**临时换课**申请，由对方老师审批后写成服务端的“调课记录”，再由教室端插件生成当天的临时层生效。永久换课仍只能由有换课权限（16）的班主任在课表页或 `POST /api/commands` 中操作，见 [control.md](control.md)。教室端插件版本过旧时，提交或审批返回 `422 CAPABILITY_UNSUPPORTED`，请用户先升级 RemoteCI 插件。

所需权限都在 `GET /api/me` 的全局 `permissions` 中：

- `4096` 老师主动换课：可以发起、审批、拒绝换课申请。内置老师和班主任默认开启。
- `8192` 强制换课：不经审批立即生效。默认关闭，由系统管理员在“人员权限 → 角色配置”中开启。

## 选课：`GET /api/swap-requests/catalog`

返回所有班级（已叠加调课）的可选日期范围与今天起 7 天的课程，以及本人任教的学科。换课可选今天起 30 天内的任意一天：

```json
{
  "teacherName": "王老师",
  "canForce": false,
  "mySubjects": ["数学"],
  "classes": [{
    "classId": "…", "className": "高一1班", "subjects": ["数学", "英语"],
    "minDate": "2026-10-06", "maxDate": "2026-11-04", "myDates": ["2026-10-06", "2026-10-20"],
    "days": [{ "date": "2026-10-06", "courses": [
      { "index": 2, "label": "第3节", "subject": "数学", "teacher": "王老师", "startTime": "10:00", "mine": true }
    ]}]
  }]
}
```

`minDate`～`maxDate`（含）是可选日期，`myDates` 是其中本人有课的日期；`days` 只含今天起 7 天。更远的日期用 `GET /api/swap-requests/catalog/day?classId=…&date=yyyy-MM-dd` 读取当天课程（返回 `{date, courses[]}`，放假或没有课时 `courses` 为空；超出范围或教室端插件过旧、未同步该日时返回 400 和中文说明）。

一节课用 `{classId, date, index}` 表示，三个值都从这里或按天读取的结果中取，不要猜。

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
- **重复申请**：同一对课位（不分先后、不分谁提交）只能有一份待审批申请，否则返回 `409 SWAP_STATE_CONFLICT`。

### 冲突提示（需要确认）

提交和审批时，服务端会检查两类需要用户确认的冲突，未确认时返回 `409 SWAP_CONFLICTS`，错误体带 `conflicts[]`：

- `teacher_clash`：换课后某位老师在同一时间（按真实上下课时间）还要在另一个班上课。
- `slot_pending`：这节课还有别的待审批申请，这次生效后它们会失效。

每项含 `kind`、`message`、`requiresAcknowledgement`，以及 `className`、`date`、`label`、`subject`、`teacher`、`startTime`、`endTime` 等。`slot_adjusted`（这节课当天已调整过）只作提示，不需要确认。

**收到 `SWAP_CONFLICTS` 时不要自动重试。** 把每条 `message` 原样告诉用户，问是否仍要继续（例如合班上课）。用户同意后，在原请求体里加 `"acknowledgeConflicts": true` 重新提交；审批时在 `approve` 的 body 里加同一字段。想提前知道有没有冲突，可以先调 `POST /api/swap-requests/preview`（body 与发起相同，不写入任何数据，返回 `{"conflicts":[…]}`）。

响应是一条 `SwapRequestView`。`status` 取值：

| 值 | 状态 | 值 | 状态 |
| ---: | --- | ---: | --- |
| 1 | 待审批 | 5 | 已过期（日期已过仍未审批） |
| 2 | 已通过并生效 | 6 | 已强制换课 |
| 3 | 已拒绝 | 7 | 已撤回（强制换课被对方撤回，或班主任/管理员撤回） |
| 4 | 申请人已撤销 | 8 | 已失效（见下） |

其他字段：

- `shortId`：8 位短编号，可以代替 `id` 用在下面所有路径里。
- `source` / `target`：申请时的课位快照，含 `className`、`label`、`subject`、`teacher`。
- 理由与审批：`reason`、`approverNames`、`decisionNote`。
- 当前账号能做的操作：`canDecide`（通过/拒绝）、`canRevoke`（撤回强制换课）、`canCancel`（撤销自己的申请）、`canRevert`（班主任或系统管理员撤回已生效的换课）。
- `needsAction`：是否计入“待我处理”。可审批的申请，或还没看过的强制换课为 `true`；统计待处理数时按它计数。
- `delivery`：已通过或已强制的申请在教室端的状态。`pending` 表示教室电脑离线或还没确认，上线后自动生效；`applied` 表示已生效。回答时如果是 `pending`，要说明“教室电脑暂未确认”。
- `invalidReason`：已失效（8）时的原因。
- `acknowledgedConflicts`：提交或审批时确认过的冲突。

### 已失效（8）

两种情况会让申请变成已失效，并给相关老师发 `swap_invalidated` 通知：

- 还在待审批时，同一节课已经有别的换课生效了。
- 已经生效后，教室端无法再应用这次换课，例如班级档案下发改了这天的基础课表，或这天改成了放假。这时同一申请涉及的其他节次也一起撤销，不会只留半个换课。

已失效的申请不能再审批；如果还需要换课，请用户重新提交。

## 强制换课

**先确认再执行。** 提交 `"force": true` 前，先向用户复述两节课，并原样转告这句话：“仅在需要紧急换课时使用，请提前与对方沟通并达成一致。”用户明确同意后再提交。

- 强制换课立即生效，`status` 为 `6`，并通知对方老师和相关班主任。
- 对方老师可以撤回。撤回后课表恢复（撤回不依赖教室端当前课表，不会因为课位又变过而失败），申请人当天不能再强制换走这节课，返回 `409 SWAP_FORCE_LOCKED`。这时只能提交普通申请。
- 这个锁只针对“这位申请人 + 这一天 + 这节课”，不影响其他课、其他老师，也不影响下一周的同一节课。
- 对方老师看过即视为已处理：打开“待我处理”、查看详情或点开通知后，这条强制换课不再计入待处理数（`needsAction` 变为 `false`），但仍可撤回。

## 查看与处理

- `GET /api/swap-requests?box=incoming`：发给我的（待我审批，或被强制换走、可撤回的）。
- `GET /api/swap-requests?box=outgoing`：我发起的。`box=all` 返回两者；可以加 `&status=1` 过滤。
- `GET /api/swap-requests/{id}`：单条详情。查看强制换课详情会自动标记为已看过。
- `POST /api/swap-requests/seen`，body 为 `{"ids":["…"]}`：向用户展示了强制换课后调用，标记为已看过，返回 `{"pending":剩余待处理数}`。
- `POST /api/swap-requests/{id}/approve`，body 为 `{"note":"可选备注"}`（确认冲突时加 `"acknowledgeConflicts": true`）：通过。服务端立刻按最新课表复核、写入调课记录并下发教室端，然后通知申请人和相关班主任。教室电脑离线时照样通过，`delivery` 为 `pending`，上线后自动生效。
- `POST /api/swap-requests/{id}/reject`，body 同上：拒绝，并通知申请人。
- `POST /api/swap-requests/{id}/cancel`：申请人撤销还在待审批的申请。
- `POST /api/swap-requests/{id}/revoke`：对方老师撤回强制换课。
- `POST /api/swap-requests/{id}/revert`：相关班级的班主任或系统管理员撤回已通过或已强制的换课，课表恢复并通知申请人和对方老师。班主任可以用 `GET /api/swap-requests/{id}` 查看本班相关换课（例如从 `swap_homeroom_info` 通知里的 `swapRequestId` 打开）。

处理失败的常见原因：

- `409 SWAP_CONFLICTS`：需要用户确认冲突，见上文“冲突提示”。
- `409 SWAP_SLOT_CHANGED`：课位在申请后又被改过，或教室端无法应用这次换课，请申请人重新提交。
- `409 SWAP_STATE_CONFLICT`：申请已经被处理过或已失效。
- `422 CAPABILITY_UNSUPPORTED`：教室端插件版本过旧，请先升级插件。

## 调课记录

服务端把每次临时调整都记成调课记录：换课申请产生的、班主任在课表页（或手机、手表）直接临时换课产生的，以及老师在 ClassIsland 里直接修改临时层产生的。

- `GET /api/classes/{classId}/schedule-adjustments?from=yyyy-MM-dd`：该班调课记录（默认从今天起）。每项含 `date`、`index`、`fromSubject`、`toSubject`、`teacher`、`origin`（`swap` 换课申请 / `direct` 直接换课 / `classisland` 教室端修改 / `backfill` 升级迁移）、`status`（`active` / `reverted` / `invalidated` / `expired`）、`delivery`（`pending` / `applied` / `conflict`）、`effective`（是否为该节当前生效的那条）、`reason`、`swapRequestId`、`createdByName`。
- `DELETE /api/schedule-adjustments/{id}`：本班班主任或系统管理员删除一条直接换课或教室端修改产生的调课，教室端随之恢复。来自换课申请的调课要用上面的 `revert` 撤回。删除前先向用户确认。

`GET /api/schedule` 返回的课程里，`adjustmentPending: true` 表示服务端已记录、教室端还没生效的调课；`baseSubjectId` 表示这节课当天被调整过（值为原本的基础科目）。

## 个人通知

- `GET /api/me/notifications?unread=true`：未读通知；轮询时用 `?after=<上次最新的 createdAt>`。
- `POST /api/me/notifications/read`，body 为 `{"ids":["…"]}` 或 `{"all":true}`：标记已读。

每条通知含 `kind`、`title`、`body`、`swapRequestId`、`createdAt`、`readAt`，部分通知还有 `classId` 和 `link`（站内相对路径，如 `/Devices`）。`kind` 取值：

| `kind` | 含义 |
| --- | --- |
| `swap_requested` | 有人向我申请换课 |
| `swap_approved` | 我的申请被通过 |
| `swap_rejected` | 我的申请被拒绝 |
| `swap_cancelled` | 申请人撤销了申请 |
| `swap_forced` | 我的课被强制换走 |
| `swap_revoked` | 我的强制换课被撤回 |
| `swap_homeroom_info` | 本班课表因换课临时变化（发给班主任） |
| `swap_invalidated` | 换课申请已失效（同一节课已有其他换课生效，或教室端无法再应用） |
| `device_offline` | 教室电脑在在校时段离线超过阈值（发给系统管理员和该班班主任） |
| `device_online` | 告警过的教室电脑重新上线 |
| `automation_failed` | 我保存的自动化规则执行失败 |

用户问“有没有人找我换课”时，先查 `box=incoming&status=1`。回答时给出申请人、两节课、理由和 `shortId`，再问是否通过。

## 一次性确认链接（聊天机器人卡片）

`POST /api/me/action-links`，body `{"action":"swap.approve","targetId":"<申请 id>"}`（也可为 `swap.reject`、`swap.revoke`），返回 `{url, path, expiresAt}`。链接 24 小时有效、只能成功使用一次：打开只显示确认页，点确认才执行。只有当前账号确实能执行该动作时才会签发（否则 400）。适合放在聊天卡片的按钮上；在对话里直接帮用户审批时，仍用上面的 `approve`/`reject`/`revoke` 接口。
