# 系统管理

除特别注明外，本文所有接口都只允许系统管理员（`GET /api/me` 的 `role` 为 `2`）调用，API Key 和账号密码登录得到的令牌都可以用。

两类非管理员也能调用部分接口：

- **拥有“人员管理”（权限位 4）的账号**：可以列出、创建、编辑、删除普通账号，并能读取角色列表、访客设置和生成插件配对码；不能创建、编辑或删除管理员账号。
- **本班班主任**：在系统管理员设置的“班主任权限”范围内，可以修改本班班级名称、头像（见下方“班级信息与头像”），手动拉取本班课表，以及修改本班扩展插件设置。

**系统管理员（部署所有者）**：首次部署时在 WebUI 初始化向导中创建的账号，`GET /api/users` 中 `isSystemOwner` 为 `true`。它只能由本人维护：其他账号（包括其他管理员）编辑、停用、重置密码、删除它都会返回 403。

## 首次部署

服务端不再自动创建管理员和默认班级。全新部署时：

1. `GET /api/setup`（无需登录）返回 `{"needsSystemAdmin":true,"needsFirstClass":true}`。
2. `POST /api/setup/system-admin`（无需登录，只在还没有任何账号时可用），请求体 `{"username":"admin","displayName":"系统管理员","password":"…"}`，返回与登录相同的令牌。密码必须由用户提供，不要自己编造。
3. 用返回的令牌 `POST /api/classes` 新建第一个班级。

已有账号后再调用第 2 步返回 403。

修改后重新 `GET` 一遍对应列表，确认每个新建或修改的对象都和用户要求一致，再向用户汇报。删除班级、分组或账号前，先向用户复述将被删除的对象，等用户确认。

## 内置角色

| 角色 | `roleId` | 用途 |
| --- | --- | --- |
| 学生 | `11111111-1111-1111-1111-111111111111` | 只读查看 |
| 管理员 | `22222222-2222-2222-2222-222222222222` | 系统管理员 |
| 班主任 | `44444444-4444-4444-4444-444444444444` | 管理所在班级，并按显示名拥有“我的日程”（原名“班管理员”） |
| 老师 | `55555555-5555-5555-5555-555555555555` | 按显示名自动绑定任教课程 |

服务端没有默认班级：所有班级都可以改名和删除，新账号只加入创建时指定的班级。

自定义角色的接口：

- 列表：`GET /api/roles`（人员管理即可调用）。
- 创建：`POST /api/roles`，请求体 `{"name":"教务","defaultPermissions":<位掩码>}`。
- 修改：`PUT /api/roles/{id}`，请求体同上。
- 删除：`DELETE /api/roles/{id}`。

## 班级

| 操作 | 请求 |
| --- | --- |
| 列表 | `GET /api/classes`，返回 `id`、`name`、`memberCount`、`pluginCount`、`groupIds`、`visitorEnabled` |
| 创建 | `POST /api/classes`，请求体 `{"name":"高一(4)班"}`，返回 `201` 和新班级 |
| 改名 | `PUT /api/classes/{id}`，请求体 `{"name":"…"}`；班主任改本班用 `PUT /api/classes/{id}/info` |
| 访客页公开课表 | `PUT /api/classes/{id}/visitor`，请求体 `{"enabled":true}` |
| 设置所属分组 | `PUT /api/classes/{id}/groups`，请求体 `{"groupIds":["…"]}`，整体替换 |
| 删除 | `DELETE /api/classes/{id}`，同时删除成员关系和插件凭据，该班电脑需要重新配对 |
| 批量操作 | `POST /api/classes/batch`，请求体 `{"classIds":["…"],"operation":"enablevisitor" \| "disablevisitor" \| "delete"}` |
| 头像 | 见下方“班级信息与头像” |

## 班级信息与头像

系统管理员可改任意班级；本班班主任在“班主任权限”允许时可改本班（`GET /api/settings/class-self-service` 的 `canRename`、`canChangeAvatar`）。没有权限返回 403。

| 操作 | 请求 |
| --- | --- |
| 改班名 | `PUT /api/classes/{id}/info`，请求体 `{"name":"高一(4)班"}`，成功返回 `204`；重名返回 400 |
| 上传头像 | `PUT /api/classes/{id}/avatar`，请求体为原始图片字节，加请求头 `X-Avatar-Type: image/png`（也可以是 `image/jpeg` 或 `image/webp`），不超过 256 KB，成功返回 `204` |
| 清除头像 | `DELETE /api/classes/{id}/avatar`，清除后各端显示默认的班级图标 |
| 读取头像 | `GET /api/classes/{id}/avatar`（无需登录），未设置时返回 404 |

`GET /api/me/classes` 的 `hasAvatar` 表示班级是否已上传头像。

## 分组

分组可以嵌套，通常一个年级建一个组。

| 操作 | 请求 |
| --- | --- |
| 列表 | `GET /api/class-groups`，返回 `id`、`name`、`depth`、`classCount` |
| 创建 | `POST /api/class-groups`，请求体 `{"name":"高一年级","parentId":null}` |
| 改名或移动 | `PUT /api/class-groups/{id}`，请求体 `{"name":"…","parentId":null}` |
| 设置组内班级 | `PUT /api/class-groups/{id}/classes`，请求体 `{"classIds":["…"]}`，整体替换直属班级 |
| 删除 | `DELETE /api/class-groups/{id}`，子分组上移一级，班级本身不受影响 |

## 账号

| 操作 | 请求 |
| --- | --- |
| 列表 | `GET /api/users`，返回 `id`、`username`、`displayName`、`role`、`roleId`、`roleName`、`grantedPermissions`、`effectivePermissions`、`enabled` |
| 创建 | `POST /api/users`，请求体 `{"username":"wangming","displayName":"王明","password":"…","role":1,"roleId":"<角色 id>","grantedPermissions":0,"classId":"<班级 id>"}`；`classId` 可选，填写后账号以同一角色加入该班，省略则不加入任何班级（老师按课表姓名自动绑定任教班级，不必填写）；创建管理员时 `role` 为 `2` |
| 编辑 | `PUT /api/users/{id}`，请求体 `{"displayName":"…","role":1,"roleId":"…","grantedPermissions":0,"enabled":true}` |
| 重置密码 | `POST /api/users/{id}/password`，请求体 `{"password":"…"}` |
| 删除 | `DELETE /api/users/{id}`，不能删除最后一个管理员，系统管理员账号任何人都不能删除 |
| 批量导入 | `POST /api/users/batch-import`（仅系统管理员），见下 |

批量导入的请求体为 `{"text":"…","defaultClassId":"<可选>","defaultRoleId":"<可选>"}`。`text` 每行一个账号：

```text
ID,用户名,班级,角色,密码
```

- 班级和角色都写名称。
- 密码留空时账号处于待激活状态，用户首次登录时自己设置密码。
- 返回 `{"created":n,"failures":["…"]}`，把 `failures` 逐条转告用户。

新建账号时，不要自己编造密码。请用户提供；或者用批量导入、留空密码，让用户本人激活。

`grantedPermissions` 是在角色默认权限之外附加的权限位。例如给学生账号开通 API Key，就附加 2048。

**老师、班主任绑定课表**：老师或班主任账号的 `displayName` 必须与课表里科目的教师名完全一致，才会出现在“我的日程”里。

- 显示名由系统管理员通过 `PUT /api/users/{id}` 修改。
- 科目的教师名用控制命令 `21` 按班设置，见 [control.md](control.md)。

## 把人分配到班级

成员接口会整体替换整个列表，所以要先读取、合并、再写回：

1. `GET /api/classes/{id}/members`，返回 `[{userId, username, displayName, roleId, roleName}]`。
2. 在现有列表上增删要调整的成员。
3. `PUT /api/classes/{id}/members`，请求体 `{"members":[{"userId":"…","roleId":"<学生或班主任角色 id>"}]}`。

班主任就是 `roleId` 为班主任的成员；一个账号可以在不同班级担任不同角色。

## 教室电脑接入

| 操作 | 请求 |
| --- | --- |
| 班级固定配对码 | `POST /api/plugin/pairing-code`，请求体 `{"classId":"…","persistent":true}`，返回 `{"pairCode":"…"}`；人员管理即可调用 |
| 一次性配对码 | 同上，去掉 `persistent`；必须填写 `classId`，否则返回 400 |
| 统一连接码 | 请求体 `{"unified":true}`；用它接入的设备先进入“未分配”，再由管理员在 WebUI 班级管理里分配到班级 |
| 已接入的插件 | `GET /api/plugins/credentials` |
| 吊销 | `DELETE /api/plugins/credentials/{id}`，并立即断开该插件 |

每个班的配对码只对应一台设备：新设备接入后，同班的旧连接会被断开。

配对码填在教室电脑 ClassIsland 的“RemoteCI 设置”里，与服务器地址一起填写。

## 其他设置

| 操作 | 请求 |
| --- | --- |
| 访客页自动进入 | `GET` 或 `PUT /api/visitor`，请求体 `{"autoEnter":true}`；人员管理即可调用 |
| 通知是否署名 | `GET /api/settings/notifications` 任何登录账号都可读取；`PUT` 的请求体为 `{"forceSenderInTitle":true}` |
| 课表自动拉取间隔 | `GET` 或 `PUT /api/settings/schedule-pull`，请求体 `{"intervalMinutes":30}`；全局设置，只有系统管理员能 `PUT` |
| 班主任权限（班级自治） | `GET` 任何登录账号可读；`PUT /api/settings/class-self-service`，请求体 `{"canRename":true,"canChangeAvatar":true,"canPullSchedule":true}`，三项都要填，对全部班级生效；扩展插件设置改为逐个插件开放，见 [control.md](control.md) 的“扩展插件设置” |
| 服务端状态 | `GET /api/admin/status`，需要“概览”权限 |
| 系统信息 | `GET /api/admin/system` |
| 检查更新 | `POST /api/admin/updates/check` |
| 备份 | `GET` 或 `POST /api/admin/backups` 列出或创建；`DELETE /api/admin/backups/{name}` 删除 |
| 恢复备份 | `POST /api/admin/backups/{name}/restore`，会覆盖当前数据，执行前必须获得用户确认 |

## 示例：新建一个年级

按顺序执行：

1. 创建分组。
2. 逐个创建班级，记下 ID。
3. 设置组内班级。
4. 批量导入老师和学生账号。
5. 为每个班写入成员。
6. 为每个班生成固定配对码，交给教室电脑填写。
7. 重新读取班级、分组和成员列表，向用户汇报结果。
