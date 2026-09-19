# 医疗影像模型准入台账

本项目记录医疗影像模型在不同医院的准入与持续监测。一个发布版本由代码、权重、运行环境和阈值共同确定，适用器官、扫描协议、禁用场景与发布版本关联，不能只用模型名称区分。

本地验证指标按医院、病种和急诊亚组保存，包括样本量、敏感性、特异性、AUC 与置信区间。患者级资料不离开所属机构。运行状态可在观察、批准、仅提示、暂停和退役之间变化，每次变化引用相应证据。

本服务不是诊断模型本身：仓库中的评估清单（`checklists/`）与模拟指标（`data/`）用于复算准入结论、回放漂移处置，并核对线上调用是否确实命中该院获批版本。

## 领域规则

### 发布版本：内容即身份

`release_id` 是代码、权重、运行环境、阈值、适用器官、扫描协议、禁用场景的内容哈希。同样的内容重复登记得到同一个版本；换掉权重或调整阈值就是另一个版本，无法混淆。

### 本地验证与准入复算

每家医院以去标识化病例集的聚合指标提交本地验证，按病种 × 急诊亚组记录样本量、敏感性、特异性、AUC 与置信区间。复算规则（`registry/checklist.py`）：

- 任一指标明显低于清单阈值 → **驳回**；
- 样本量不足、指标处于临界带、置信区间下限不足、缺少必需亚组 → **观察**；
- 全部达标 → **批准**。

任何亚组的不足都不能被总体平均掩盖。提交中夹带患者级字段（如 `patient_id`）会被拒绝。

### 漂移信号与降级

批准上线后，医生采纳（adopt）、改判（override）、漏报（miss）反馈在滑动窗口内汇总。改判率或漏报率达到清单阈值时：先自动降级到**仅提示**，同时开启**复审**；持续触达只记录，不重复降级。

### 双人审批的变更

替换权重、调整阈值、回滚必须提交变更申请并引用当时证据（验证批次、漂移报告或复审编号），由两名不同于发起人的审批人批准后生效。换权重 / 调阈值会产生新版本并回到观察状态，须重新本地验证；回滚恢复该院此前的部署状态。

### 线上调用核对

每次线上调用记录审计：命中当前获批版本（`hit`，区分完整模式 / 仅提示模式）或未命中（`miss`，原因包括版本不符、状态未授权、版本未登记、该院无获批版本）。

### 数据边界

患者级资料不进入台账；本地验证、反馈、漂移与调用审计数据仅所属机构（请求头 `X-Hospital-Id`）或台账运营方（`X-Role: operator`）可读，跨机构查询一律拒绝。该身份约定为演示级，生产环境应替换为正式认证。

## 运行

```bash
python3 service.py --check                       # 基础检查
python3 service.py --port 8000                   # 启动服务（--data-file 可加载/保存快照）
python3 service.py --evaluate data/simulated_scenario.json      # 复算准入结论
python3 service.py --replay-drift data/simulated_scenario.json  # 回放漂移处置
python3 service.py --verify-calls data/simulated_scenario.json  # 核对线上调用
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/checklist` | 当前评估清单 |
| POST | `/releases` | 登记发布版本（内容即身份，幂等） |
| GET | `/releases`、`/releases/{id}` | 版本目录 |
| POST | `/hospitals` | 登记医院 |
| GET | `/hospitals` | 医院目录 |
| POST | `/validations` | 提交本地验证（机构范围内），返回复算结论 |
| GET | `/validations?hospital_id=` | 本院验证批次（机构范围） |
| GET | `/deployments?hospital_id=` | 本院部署状态与当前版本 |
| POST | `/deployments/status` | 人工置 观察/暂停/退役（仅运营方，须引用证据） |
| POST | `/feedback` | 提交采纳/改判/漏报反馈，返回漂移处置 |
| GET | `/drift?hospital_id=` | 漂移报告与复审记录 |
| POST | `/change-requests` | 发起变更（须附证据） |
| POST | `/change-requests/{id}/approve` | 审批变更（两人、非发起人） |
| GET | `/change-requests?hospital_id=` | 变更记录 |
| POST | `/verify-call` | 核对一次线上调用并留痕 |
| GET | `/call-audits?hospital_id=` | 调用核对审计 |

## 测试

```bash
npm test          # 等价于 python3 -m unittest -v service_contract test_domain
```
