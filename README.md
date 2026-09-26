# 实现多租户算力信用额度与超额复核基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/tenant_credit/`：多租户算力信用额度、预约冻结、超额复核与账期结算；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 多租户算力信用额度

`tenant_credit` 模块把预付（PREPAID）、后付（POSTPAID）和专项赠送（GRANT）额度分行记录，
每行标注适用资源、有效期和冻结/消耗/核销余额：

- 预约确认时按当前规则版本（来源优先级）先到期先用地选择额度行，跨月作业按 UTC 自然月
  切分预计消耗，冻结与资源预留在同一事务内落账，任一切片额度不足即整体回滚；
- 作业完成、取消或失败时按时间顺序把已执行消耗归属到月度切片，仅返还尚未执行部分；
- 超过额度的申请进入有期限的豁免复核队列，复核人不能批准自己提交的豁免，批准后仅可使用一次；
- 账期结算后禁止任何流水回写，规则更新只产生新版本，不回写已结算账期；
- 租户接口仅返回本机构汇总与本机构流水，财务与审计可通过解释接口和哈希链审计追踪每次
  扣减、返还和超额决定。

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
PYTHONPATH=src python3 -m tenant_credit.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析和芯片准入流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m tenant_credit.api --database credit.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。
