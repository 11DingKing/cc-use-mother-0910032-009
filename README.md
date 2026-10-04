# 人员资质到期巡检

本项目维护人员资质到期巡检的领域约定、角色边界与样例数据，并实现了可运行的
Python 后端：保存资质有效期、续期案件、临时凭证与关联授权，按可注入时钟生成
风险项（待补件 / 宽限 / 失效），重复巡检幂等，授权限制传播到未来排班但保留
历史服务依据。

## 领域规则

* **不误伤**：续期在到期前已提交、尚未核验时，自动进入 30 日管理宽限期；
  临时凭证可在管理宽限之外继续延长保护。撤回/驳回续期后保护立即终止。
* **不合规风险不漏**：无在途案件、补件逾期、宽限与凭证均到期的，判失效，
  自客观保护结束次日停业。
* **待补件**：只冻结新排班，不影响当日服务；**监管暂停**：立即停业，
  不产生到期风险项。
* **历史依据保留**：授权限制按窗口叠加与收口，服务记录按当日事实还原
  （`practice_was_lawful`），后续限制不回溯改写。
* **幂等巡检**：同一日期重复巡检，风险项、限制集合、排班取消结果稳定。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/qualification_inspection/`：巡检后端
  - `models.py`：资质、续期案件、临时凭证、监管暂停、授权、限制、排班与服务记录。
  - `policy.py`：可注入时钟与到期/宽限/失效、限制生效日的纯函数策略。
  - `service.py`：命令处理、幂等巡检对账、限制传播到未来排班。
  - `api.py` / `server.py`：标准库 HTTP 接口与启动入口。
- `docs/API.md`：接口说明（含每项限制何时生效）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、领域策略、服务状态机与 HTTP 端到端回归测试。

## 验证

```bash
python3 -m unittest discover -s tests -v   # 33 个测试
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```

## 启动（内存仓储示例）

```bash
python3 -m qualification_inspection.server --today 2026-10-04 --grace-days 30
```

接口见 `docs/API.md`。生产部署需把内存仓储替换为持久化实现。
