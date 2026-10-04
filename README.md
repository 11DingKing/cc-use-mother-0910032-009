# 人员资质到期巡检

监管部门每月检查执业证和主诊资格。续期资料可能已提交但尚未核验，简单按日期停用会误伤
正常人员，忽略到期又产生合规风险。本服务保存资质有效期、续期案件、临时凭证和关联授权，
按**可注入时钟**生成风险项并区分**待补件、宽限和失效**；重复巡检、补证、撤回续期和
监管暂停形成**稳定状态**；授权限制**传播到未来排班但保留历史服务依据**，每项限制都
说明何时生效、何时解除。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/qualification_inspection/`：资质巡检后端实现。
  - `models.py`：实体与状态/风险/限制常量（零 ORM）。
  - `clock.py`：`Clock`/`SystemClock`/`FixedClock` 可注入时钟。
  - `policy.py`：纯函数风险判定（到期、宽限、待补件、失效）。
  - `repository.py`：内存仓储与原子写入的 JSON 文件仓储。
  - `service.py`：命令处理、幂等月巡检、限制重算与排班传播。
  - `api.py`：标准库 HTTP 接口（无第三方依赖）。
- `tools/check_contract.py`：契约摘要检查。
- `tools/run_server.py`：启动 HTTP 服务。
- `tests/`：契约、策略、服务生命周期与 HTTP 端到端测试。

## 领域规则

### 资质时态与风险类别

以巡检时钟日 `today` 对照资质到期日 `expiry_on`：

| 风险类别 | 触发条件 | 级别 | 含义 |
| --- | --- | --- | --- |
| `EXPIRING_SOON` 临期 | 到期日前 30 天内 | WARN | 提示提前续期，不阻断 |
| `GRACE` 宽限 | 已过期但续期在途（已提交/已补件），或有效临时凭证 | WARN | 可继续执业，不阻断 |
| `SUPPLEMENT_DUE` 待补件 | 核验要求补件；截止日前 WARN，逾期 BLOCK | WARN→BLOCK | 逾期即冻结，补证当日解除 |
| `LAPSED` 失效 | 过期且无在途续期，或续期被撤回/驳回，或超宽限上限 | BLOCK | 阻断未来排班 |
| `SUSPENDED` 监管暂停 | 存在生效中的监管暂停文书 | BLOCK | 即时生效，可限定范围 |

宽限在到期日后最多延续 **90 天**（`GRACE_AFTER_EXPIRY_DAYS`）；超期未核准则失效，
失效生效日回溯为宽限上限的次日，而非巡检发现当天。

### 续期案件状态（对应契约五态）

```
SUBMITTED（待核验）
  ├─ 要求补件 → SUPPLEMENT_REQUESTED（处置中：待补件）
  │                 ├─ 补交材料 → SUBMITTED 语义的 SUPPLEMENTED（处置中：已补证待核验）
  │                 └─ 超过截止日未补 → 风险升级 BLOCK（案件状态不变）
  ├─ 核准 → APPROVED（已决定）：资质有效期顺延，风险/限制清除
  ├─ 驳回 → REJECTED（已决定）：资质已过期则次日失效
  └─ 撤回 → WITHDRAWN（已归档）：资质已过期则撤回当日宽限终结、次日失效
```

### 限制何时生效（`effective_from` 含当日；`effective_to` 含当日）

| 限制 | 生效规则 | 解除/终止 |
| --- | --- | --- |
| `LAPSED` 资质失效停权 | 到期日**次日**；有在途续期则顺延至撤回/驳回或宽限上限次日 | 续期核准延长有效期时关闭，回填终止日 |
| `SUPPLEMENT_HOLD` 补件逾期冻结 | 补件截止日**次日** | 补证提交当日解除，终止日回填为补证日前一日 |
| `GRACE_WINDOW` 宽限/待补件提示 | 到期次日或补件要求日（WARN） | 计划至宽限上限日；提前终结时回填真实终止日 |
| `EXPIRING_SOON` 临期提示 | 到期前 30 天（WARN） | 到期日 |
| `REGULATORY_SUSPENSION` 监管暂停 | 暂停起始日**当日**即时生效；未来起始日为 `SCHEDULED` | 解除日当日有效，次日起失效；或按 `end_on` 自动到期 |

- 限制只影响 `effective_from` 起的**未来排班**；已发生服务通过 `ServiceRecord`
  保存当时的资质快照（证号、类型、到期日），事后停权不改变历史服务依据。
- 监管暂停可通过 `scope` 限定具体授权范围；不填则人员级全部阻断。
- 命令（登记、续期、补证、撤回、暂停等）成功后立即按当前时钟重算相关人员限制；
  月巡检额外生成/归并/清除风险项。

### 幂等巡检

`POST /inspections/run` 按 `period`（默认时钟月，`YYYY-MM`）幂等：

- 风险项按资质归并：重复巡检只刷新 `last_seen_on`（计入 `stable`），类别/级别变化才
  迁移并记录 `transitions`，风险消除则置为 `CLEARED` 并写 `resolved_on`。
- 同一周期的 `InspectionRun` 记录覆盖为一条，统计 `created/updated/cleared/stable`
  反映本次执行结果。

## 快速开始

```bash
python3 tools/run_server.py --port 8080 --store data/inspection.json
```

应用内直接使用（推荐配合 `FixedClock` 做确定性回放/测试）：

```python
from datetime import date
from qualification_inspection import QualificationService, FixedClock

svc = QualificationService(clock=FixedClock("2026-07-05"))
svc.register_person("P1", "张三")
svc.register_qualification("Q1", "P1", "PRACTICE_LICENSE", "L-1",
                           "2025-07-01", "2026-06-30")
svc.register_authorization("A1", "P1", "Q1", "门诊出诊")
svc.run_monthly_inspection()          # 已过期 5 天但无续期 → LAPSED
svc.submit_renewal("C1", "Q1")        # 提交续期 → 转为 GRACE，限制降级
svc.evaluate_schedule("P1", date(2026, 7, 5))
```

## HTTP 接口

所有接口请求/响应均为 JSON。业务冲突返回 `409`（`DOMAIN_ERROR`），参数错误 `400`，
不存在 `404`。响应中的每项限制均含 `effective_from` 与 `effective_to`。

### 写接口

| 方法与路径 | 说明 | 主要参数 |
| --- | --- | --- |
| `POST /persons` | 登记人员 | `person_id, name` |
| `POST /qualifications` | 登记资质及有效期 | `qual_id, person_id, qual_type, qual_number, issued_on, expiry_on` |
| `POST /authorizations` | 建立资质关联授权 | `authorization_id, person_id, qual_id, scope` |
| `POST /authorizations/{id}/revoke` | 撤销授权 | `on?` |
| `POST /renewals` | 提交续期案件（重复在途案件 409） | `case_id, qual_id, submitted_on?` |
| `POST /renewals/{id}/supplement-request` | 核验要求补件 | `items[], deadline, requested_on?` |
| `POST /renewals/{id}/supplement` | 补交材料（补证） | `doc_names[], submitted_on?` |
| `POST /renewals/{id}/approve` | 核准续期、顺延有效期 | `new_expiry_on, decided_on?` |
| `POST /renewals/{id}/reject` | 驳回续期 | `reason, decided_on?` |
| `POST /renewals/{id}/withdraw` | 撤回续期 | `on?` |
| `POST /credentials` | 签发临时凭证 | `cred_id, case_id, valid_from, valid_to` |
| `POST /credentials/{id}/void` | 作废临时凭证 | `reason, on?` |
| `POST /suspensions` | 监管暂停（即时生效） | `order_no, person_id, reason, start_on, end_on?, scope?` |
| `POST /suspensions/{id}/lift` | 解除暂停 | `lifted_on?` |
| `POST /service-records` | 固化历史服务依据 | `record_id, person_id, authorization_id, service_on, detail?` |
| `POST /inspections/run` | 执行/重放月巡检（幂等） | `period?` |

`qual_type` 取值：`PRACTICE_LICENSE`（执业证）、`ATTENDING_QUALIFICATION`（主诊资格）。
续期案件响应额外带 `contract_phase`（待核验/处置中/已决定/已归档）。

### 读接口

| 方法与路径 | 说明 |
| --- | --- |
| `GET /risks?person_id=&include_cleared=` | 风险项列表（默认仅未消除） |
| `GET /restrictions?person_id=&on=&include_inactive=` | 限制列表；`on` 过滤当日生效 |
| `GET /schedule?person_id=&day=` | 某日各授权是否可排班及命中的限制 |
| `GET /schedule-impact?person_id=&from_on=&through=` | 限制对未来排班的传播窗口 |
| `GET /persons` / `GET /qualifications` / `GET /health` | 基础查询 |

`GET /schedule-impact` 响应示例：

```json
{
  "person_id": "P1",
  "from_on": "2026-07-15",
  "through": "2027-01-11",
  "windows": [
    {
      "restriction_id": "DR-P1-Q1-LAPSED",
      "type": "LAPSED",
      "level": "BLOCK",
      "title": "资质失效停权",
      "effective_from": "2026-07-11",
      "effective_to": null,
      "reason": "续期案件 C1 已于 2026-07-10 撤回，宽限终结且未恢复有效资质",
      "affects_authorizations": ["A1"],
      "status": "ACTIVE"
    }
  ],
  "note": "限制仅传播到未来排班；已发生服务的历史依据见服务记录，不受限制影响"
}
```

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
