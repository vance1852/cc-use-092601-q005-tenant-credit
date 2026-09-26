# 实现多租户算力信用额度与超额复核基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/credit_ledger/`：多租户算力信用额度（预付/后付/专项赠送）、冻结消耗、跨月切分与超额复核；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m credit_ledger.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入以及信用额度跨月冻结/返还/超额复核流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m credit_ledger.api --database credit.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 多租户算力信用额度

`credit_ledger` 把预付、后付、专项赠送额度分别建账，记录适用资源与有效期，并在预约确认时
把额度冻结与资源预留放在同一个 SQLite 事务内原子落账，避免“排队后才发现余额不足”。

- **额度来源**：`POST /credits` 建立 `prepaid`/`postpaid`/`grant` 账户，各自记录总额、适用
  `products`、`valid_from`/`valid_to`，余额 = 总额 − 冻结 − 消耗。
- **选择规则**：`POST /rules` 发布版本化的来源优先级（如先预付、再赠送、后付兜底）与是否允许
  自动超额；每次落账记录所用 `rule_revision`，规则更新只对新预约生效，不回写已结算账期。
- **跨月切分**：预约按可注入时钟沿 UTC 月界切分，预计工时与金额按时间占比分摊到各账期，
  段金额之和恒等于预计总额（末段吸收量化误差）。
- **确认与占用**：来源充足时同事务冻结额度并扣减资源池工时；任一失败整体回滚。
- **超额复核**：余额不足且不允许自动超额时，预约进入有期限（默认 48 小时）的复核队列，
  到期自动作废；`POST /reviews/{id}/decision` 审批，复核人不能批准自己提交的豁免。
- **取消/失败/完成**：按当前时钟把每个账期段拆成“已执行消耗”和“未执行返还”，只返还尚未
  执行部分，资源工时同步按比例退回。
- **账期结算**：`POST /periods/close` 关闭账期，关闭后任何触及该账期的冻结/消耗都被拒绝，
  历史流水标记为 `settled` 且可继续查询。
- **租户隔离与可解释**：`tenant` 角色只能看本机构的额度汇总、自己的流水与预约；`finance`、
  `reviewer`、`auditor` 可跨租户查询，并通过 `GET /ledger/entry/{id}` 解释每次扣减、返还
  和超额决定（关联账户、冻结明细、预约、复核单与规则版本）。所有写操作进入哈希链审计表。

