# 服务端档案

服务端档案库保存完整的 ClassIsland 档案 JSON（时间表、课表、科目、临时层），分两类：

- **全局模板**（`classId` 为 null）：只有系统管理员能看和改。
- **班级档案**：每个班级最多一份独立副本。系统管理员和本班班主任（需要本班“换课 / 科目教师”权限 16）可以管理。

保存与下发是两步：保存只写服务端，不影响教室电脑；下发才会改教室电脑上的 ClassIsland 档案。所有写操作都带修订号 `revision`，别人先改过时返回 409 `PROFILE_STALE`，此时重新读取后再操作。

## 查看

| 请求 | 说明 |
| --- | --- |
| `GET /api/profiles` | 管理员得到全部档案，班主任得到自己可管理班级的档案；列表不含 JSON，只有 `id`、`name`、`classId`、`sourceTemplateId`、`revision`、`updatedAt` 以及时间表/课表/科目数量 |
| `GET /api/profiles?classId=…` | 只看某个班级的档案 |
| `GET /api/profiles/{id}` | 单份档案，含完整 `profileJson` |
| `POST /api/profiles/preview` | body `{"profileJson":"…"}`，返回解析结果与校验错误，不保存 |

## 保存、复制、删除

| 请求 | 说明 |
| --- | --- |
| `PUT /api/profiles` | body `{"items":[{"id":null,"classId":null,"name":"…","profileJson":"…","revision":0}]}`；新建时 `id` 省略、`revision` 为 0，修改时带上读到的 `id` 和 `revision`。一次最多 100 份，任一无效则整批不保存 |
| `POST /api/profiles/{id}/copy` | body `{"revision":1,"name":"副本名"}`，复制全局模板（仅管理员） |
| `DELETE /api/profiles/{id}?revision=N` | 删除服务端档案，设备上的档案不受影响 |

档案 JSON 很大，通常不要在对话里手写；用户要改具体课程时，优先用换课（[control.md](control.md)）或建议到 WebUI“档案管理”页编辑。

## 从教室电脑收集

`POST /api/profiles/collect`，body `{"classIds":["…"]}`（1–100 个班级）。服务端读取每班在线电脑上 ClassIsland 当前正在用的档案（含临时层），返回 `results[]`：`classId`、`className`、`deviceName`、`success`、`message`、`profileJson`（仅成功时有）和 `errors`（仍需修正的校验问题）。

- 收集结果**不会自动保存**。要存进档案库，再用 `PUT /api/profiles` 保存：班级已有档案时带上它的 `id` 和 `revision` 覆盖，没有时新建（`classId` 填该班）；想当作全局模板时 `classId` 为 null（仅管理员）。
- 班级离线时返回“班级设备未在线”；插件太旧时返回需要升级插件。
- 只读操作，不改教室电脑；需要和保存档案相同的权限。

## 临时层

临时层是只在某一天替换当天课表的课表（ClassIsland 的 `IsOverlay=true` 课表，由档案的 `OrderedSchedules[日期]` 指向；可另带一张只给它用的临时层时间表）。换课和调休在教室电脑上产生的就是临时层，收集时会一并带回。

- 常规下发方式（`mode` 1/2/3）不包含临时层；教室电脑上原有的临时层会保留，课程数随新时间表补齐或截断，缺失的科目改为空课；只有它用的时间表被删掉时才随之移除。
- 想把档案里的临时层推到教室电脑，用 `mode: 4`（见下）。

## 下发到教室电脑

`POST /api/profiles/apply`，body：

```json
{
  "items": [{ "id": "…", "revision": 3 }],
  "mode": 1,
  "sections": 7,
  "classIds": ["…"],
  "groupIds": [],
  "connectionIds": [],
  "confirmReplace": false,
  "importProfileName": null,
  "restartAfter": false,
  "tempLayerIds": null,
  "replaceExistingTempLayers": false
}
```

- `mode` 必须显式选择：`1` 更新当前档案（合并）、`2` 整体替换所选类别（必须同时 `confirmReplace: true`）、`3` 创建并启用新档案（必须给 `importProfileName`）、`4` 作为临时层下发。
- `sections` 是位掩码：`1` 时间表、`2` 课表、`4` 科目，`7` 为全部；`mode: 4` 时不需要。
- `mode: 4` 只写入档案中按日期安排的临时层，不改常规课表：`tempLayerIds` 指定要下发的临时层课表 ID（省略为全部；多份档案批量下发时忽略），已过期的自动跳过；教室电脑同一天已有临时层或预定课表时，只有 `replaceExistingTempLayers: true` 才会替换，否则该电脑返回失败并说明日期。需要插件支持 `profile.temp-layer`。
- 一份全局模板可以下发到多个班级；班级档案只能下发到它自己的班级。同一批不能混用模板和班级档案。
- 返回 `results[]` 逐台设备报告 `success` 和 `message`；离线设备直接失败，不会排队。

下发会改变教室电脑正在使用的课表。先向用户复述：哪份档案、哪些班级、哪种方式、哪些类别（临时层下发则说明哪些日期、是否替换同日已有安排）；“整体替换”“创建并启用”和替换同日临时层要特别说明影响，得到明确同意后再调用。
