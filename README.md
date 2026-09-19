# 医疗影像模型准入与持续监测后端

面向多家医院试用开源腹部 CT 模型的**准入治理后端**。本服务**不是诊断模型本身**，
不执行任何影像推理；它回答三类管理问题：

1. **这个版本在本院能不能用？** —— 不可混淆的版本登记 + 本院去标识化病例集的逐格本地验证；
2. **上线后是否仍然可靠？** —— 医生采纳/改判/漏报反馈形成漂移信号，越限自动降级到「仅提示」并触发复审；
3. **线上跑的到底是不是本院批准的那个版本？** —— 每次调用核对 release 与指纹，异常全部留证。

「专家级」宣传不能替代本院结论：研究结果来自特定设备、协议和病例分布，
准入只能依据本院数据按病种 × 协议 × 急诊亚组逐格判定，**不产生任何总平均指标**。

## 核心规则

- **版本不可混淆**：发布版本（release）由代码提交、权重 SHA-256、运行环境镜像/依赖锁、
  运行阈值、适用病种与禁用场景共同确定指纹；任一要素变化即新版本，旧版本冻结保留。
- **逐格准入**：每个「病种 × 扫描协议 ×（常规/急诊）」格子单独复算敏感性、特异性、AUC
  及 95% 置信区间（敏感性/特异性用 Wilson，AUC 用 Hanley-McNeil），并核对样本量。
  - 任一格点估计不达标或覆盖缺格 → **驳回（reject）**；
  - 仅样本量或置信区间不足 → **观察（observe）**，不能上线；
  - 全部格子通过 → **批准（approve）**。
  - 急诊病种必须同时提交常规与急诊亚组；急诊门槛更高（敏感性 0.90、漏报容忍更低）。
- **协议范围**：病种只在目录声明支持的扫描协议下可被准入，协议外证据直接拒绝。
- **患者级数据不出机构**：接口仅接受汇总计数（TP/FP/TN/FN、样本量、聚合 AUC），
  出现 `patient_id`/`mrn`/`study_uid`/病例明细列表等字段一律 422 拒绝。
- **双人审批**：上线、替换权重、阈值调整、回滚、暂停/恢复、退役均需申请人之外的
  **两名不同审批人**；申请时冻结证据引用，第二人批准瞬间原子执行并固化证据链。
  - `threshold_change` 只允许阈值不同；代码/权重/环境变化必须按权重替换重新准入。
  - 回滚目标必须是本院曾正式上线的版本；已退役版本不得回滚。
- **漂移处置**：滑动窗口（默认近 5 批 / 90 天）复算漏报率与改判率，点估计越限且分母足够时
  `active → advisory`（结果仅可提示，不得作为独立诊断依据），同时开立复审；
  复审关闭前恢复上线必须提交**晚于降级时间**的重新验证并双人批准，窗口基准随之重置。
- **机构隔离**：每家医院持独立令牌（仅存 SHA-256），只能读写本院数据，无任何跨机构查询。

病种目录见 `domain/catalog.py`，当前收录 **123** 种腹部病症（其中 **53** 种急诊标记）、
15 个器官/区域、7 类扫描协议。

## 运行

```bash
python3 service.py --check          # 离线复算：准入判定 + 漂移回放 + 调用核对
python3 service.py --port 8000      # HTTP 服务（内存台账）
DATA_FILE=./ledger.json ADMIN_TOKEN=... python3 service.py --port 8000
```

`--check` 使用 `fixtures.py` 的合成数据端到端演示：三家医院分别得到
approve / reject / observe，患者级字段与协议不符被拒，双人审批上线，五批反馈后漂移降级，
重新验证恢复，以及指纹/协议越界调用核对。无第三方依赖，仅需 Python 3 标准库。

## HTTP 接口

管理员接口用 `X-Admin-Token`；医院接口用 `Authorization: Bearer <机构令牌>`。

| 方法 & 路径 | 说明 |
|---|---|
| `GET /health` | 服务身份与健康检查（向后兼容基线契约） |
| `POST /admin/releases` | 登记不可变发布版本 |
| `POST /admin/tenants` | 登记医院并签发令牌 |
| `POST /v1/validations` | 提交本地验证（汇总计数），返回逐格复算报告与 decision |
| `GET /v1/validations[?release_id=]` | 本院验证记录列表 |
| `GET /v1/validations/{id}` | 单份验证证据（含 evidence_sha256） |
| `GET /v1/releases/{id}` | 查看发布版本登记信息 |
| `GET /v1/deployments/{release_id}` | 本院档案状态（observation/approved/rejected/retired） |
| `GET /v1/serving` | 当前线上形态与版本快照 |
| `POST /v1/changes` | 创建变更审批单（activate/weights_swap/threshold_change/rollback/degrade/suspend/resume/retire） |
| `POST /v1/changes/{id}/approve` | 审批（需调两次、两名不同审批人） |
| `POST /v1/changes/{id}/reject` | 驳回审批单 |
| `GET /v1/changes` | 本院审批单列表 |
| `POST /v1/feedback` | 录入一批使用反馈（病种×亚组汇总计数），可能触发自动降级 |
| `GET /v1/drift/signals?release_id=` | 当前各窗口信号（normal/warning/breach） |
| `GET /v1/drift/events` | 漂移降级事件 |
| `GET /v1/reviews[?status=open]` | 漂移复审 |
| `POST /v1/calls/verify` | 线上调用版本核对，返回 matched/advisory_only/mismatch_reason |
| `GET /v1/calls/events?mismatch_only=1` | 核对留证（仅本院） |

验证提交示例（每个格子仅含计数与聚合 AUC；敏感性/特异性由服务端复算）：

```json
{
  "release_id": "rel-0001",
  "dataset_id": "ds-hospital-a-2026q2",
  "scope_protocols": ["noncontrast", "portal_venous"],
  "cells": [
    {"condition": "appendicitis", "protocol": "noncontrast", "stratum": "routine",
     "n_pos": 48, "n_neg": 80, "tp": 45, "fn": 3, "tn": 74, "fp": 6, "auc": 0.91}
  ]
}
```

## 代码结构

```
service.py              运行入口：HTTP 路由、鉴权、--check 离线回放
app.py                  应用门面：装配各领域服务 + 写操作串行锁
storage.py              JSON 文件台账（原子写；无路径时为纯内存）
fixtures.py             评估清单与合成模拟指标（准入场景/漂移时间线）
domain/
  catalog.py            123 种腹部病症、急诊标记、器官与协议支持范围
  stats.py              Wilson 区间、Hanley-McNeil AUC 区间
  registry.py           不可变 release 指纹登记、医院租户与令牌
  admission.py          汇总计数校验、逐格复算、approve/observe/reject 判定
  lifecycle.py          档案/线上状态机、双人审批、证据冻结与回滚
  drift.py              滑窗漂移信号、自动降级与复审、线上调用核对
  privacy.py            患者级字段/病例明细的统一拦截
tests/                  93 个 unittest（统计、准入、审批、漂移、隐私、HTTP、持久化）
```

## 测试

```bash
npm test                 # 运行健康契约 + 全部领域测试
python3 -m unittest discover -s tests -t .
python3 service.py --check
```

## 边界与说明

- 台账为单文件 JSON（原子替换），适合准入治理这类低频、强审计场景；
  高并发推理流量不应直接写入本服务，`/v1/calls/verify` 可由网关机周期性批量上报。
- 漂移反馈中的「采纳/改判/纠正/确认」是由科室质控按批次汇总的计数，不是逐患者埋点。
- 所有证据（验证报告、审批链、执行快照、漂移事件、调用异常）均带 SHA-256 与时间戳，
  可供院内医工/质控事后复核。
