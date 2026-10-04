# 接口说明：人员资质到期巡检后端

基础地址：`http://<host>:<port>`，请求/响应均为 JSON（`Content-Type: application/json`），
日期统一 ISO 格式 `YYYY-MM-DD`。启动：

```bash
python3 -m qualification_inspection.server --host 127.0.0.1 --port 8080 [--today 2026-10-04] [--grace-days 30]
```

`--today` 注入固定时钟用于回放/测试；生产使用系统时钟。当前仓储为内存实现。

## 核心判定规则（按注入时钟）

以资质 `valid_to`（核验通过后取 `new_valid_to`）为有效期截止日，**当日仍有效，次日才失效**：

| 情形 | 风险类别 | 授权后果 |
| --- | --- | --- |
| 未到期，无在途案件 | 无 | 正常执业、可排班 |
| 未到期，续期案件被要求补件 | `待补件` | 自补件要求提出日起**冻结新排班**，当日服务与历史服务不受影响 |
| 已过期，续期于到期前提交且待核验，30 日管理宽限期内 | `宽限` | 原授权范围内继续执业，可排入宽限截止日前的班 |
| 已过期，在途案件持有覆盖当日的临时凭证（可超出管理宽限） | `宽限` | 凭证窗口内继续执业 |
| 已过期且补件逾期 / 无在途案件 / 宽限与凭证均到期 | `失效` | **到期次日起停止执业** |
| 监管暂停（与到期无关，不生成风险项） | — | 自暂停指令生效日起停止执业；登记解除日后窗口自动收口 |

要点：续期资料“已提交但尚未核验”不会被简单按日期停用；撤回/驳回续期后
临时凭证立即失去依据，下次巡检转失效。

## 限制何时生效（`GET /authorizations/{id}`）

每条限制返回：

```json
{
  "kind": "practice_suspended",
  "source_type": "qualification",
  "source_id": "Q1",
  "reason": "失效：资质已于 2026-10-04 到期……",
  "effective_from": "2026-10-05",
  "effective_to": null,
  "is_active": true,
  "effect_explanation": "……；生效日 2026-10-05（含当日），至 解除事件登记前持续有效；停止一切执业，取消覆盖期内未来排班，历史服务记录保留；状态：当前生效中"
}
```

* `effective_from`：含当日生效。失效类 = 有效期截止次日；待补件冻结 = 补件
  要求提出日；监管暂停 = 指令生效日（支持未来日期，登记即传播）。
* `effective_to`：含当日的最后有效日；`null` 表示解除事件登记前持续有效。
  解除/续期核验通过后，巡检把开放窗口回写收口，**历史窗口保留**。
* `scheduling_frozen`：只冻结新排班（含未来候选排班），不停止当日执业。
* `practice_suspended`：停止一切执业，取消覆盖期内的未来排班。

## 端点

### 登记与授权

* `POST /qualifications`
  `{id?, person_id, qual_type, valid_from, valid_to}`
* `POST /authorizations`
  `{id?, qualification_id, service_code, service_name, granted_from, granted_to?}`
* `GET /authorizations/{id}` — 实时执业/排班能力与全部限制（含生效说明）。

### 续期案件（状态机）

`submitted → supplementing → submitted（补证后回待核验）→ verified / rejected`，
任意在途状态可 `withdrawn`。

* `POST /renewals`
  `{id?, qualification_id, required_documents: [...], submitted_documents?: [...]}`
* `POST /renewals/{id}/supplement-request` `{due}` — 材料不齐时要求补件（当日冻结新排班）。
* `POST /renewals/{id}/supplement-documents` `{document_names: [...]}` — 补证；材料齐回待核验，冻结持续到核验。
* `POST /renewals/{id}/verify` `{new_valid_to}` — 核验通过，写入新有效期。
* `POST /renewals/{id}/reject` `{reason}`
* `POST /renewals/{id}/withdraw`

约束：已有在途案件不能重复提交；材料不齐不能核验通过；终局案件不能再操作。

### 临时凭证与监管暂停

* `POST /permits`
  `{id?, qualification_id, renewal_case_id, valid_from, valid_to, scope_note?}`
  — 必须关联在途续期案件；案件撤回/驳回后凭证立即失效。
* `POST /suspensions`
  `{id?, qualification_id, reason, effective_from?}` — `effective_from` 缺省为今日，可指定未来日。
* `POST /suspensions/{id}/lift` `{effective_date?}` — 登记解除日（缺省今日，可指定未来日）。

### 巡检

* `POST /inspections/run` `{as_of?}` — 对全部资质巡检并对账；
  返回 `{as_of, risk_opened, risk_resolved, schedules_cancelled}`。
  同一日期重复执行：风险项、限制集合、排班取消结果全部稳定（0/0/0）。
* `GET /risks?qualification_id=...` — 当前未解决风险项（`待补件/宽限/失效`）。

### 排班与历史服务依据

* `POST /schedules` `{id?, authorization_id, scheduled_on}`
  — 新增未来排班时按“排班当日”投影限制：到期日之后、补件冻结、监管暂停、
  超出宽限/凭证窗口一律拒绝（400）。
* `GET /schedules` — 全部排班及取消原因。
* `POST /service-records` `{authorization_id, service_date, service_name}`
  — 历史服务记录不可变。
* `GET /service-records/{id}/basis`
  — 还原服务当日授权是否在窗口内、当日生效限制、`practice_was_lawful`；
  之后的失效/暂停不改写历史结论。

### 时钟

* `GET /clock` — 当前巡检时钟日期。

## 状态映射（契约五状态）

巡检对账后资质状态：`登记`（无风险无在途案件）/ `待核验`（续期在途）/
`处置中`（存在未解决风险项或活动监管暂停）/ `已决定`（核验通过/驳回等终局）
/ `已归档`（只读）。
