# M5 运行与验收说明

本文件记录 M5（ULog 运行期可达证据、候选分级与人工审阅导出）的实现范围、
日志约束、状态语义、候选规则、运行方式、输出、验收判据和已知边界。实验
设计背景见项目根目录 `README.md`；此前的基线见 `docs/m1-operations.md`、
`docs/m2-operations.md`、`docs/m3-operations.md` 和 `docs/m4-operations.md`；
后续的断链统计与定向 LLVM 兜底见 `docs/m6-operations.md`。

M5 只回答一个运行期问题：**在唯一一份授权的正常场景日志中，静态路径边界
的 topic 和字段是否出现过。** 它不计算相关性、因果、参数取值效果或安全
结论。日志中“未观测到”不是“不可达”，报告、记录和 gate 都显式保留这一点。

## M5 范围

M5 不新增索引 TU，也不改变 M1—M4 的静态图。它读取：

- `graph/nodes.jsonl` 中的 `topic` / `topic_field` 节点；
- `graph/end_to_end_paths.jsonl`（M4 的显式跨模块路径）；
- `graph/paths.jsonl`（M3 的字段敏感切片路径，用于候选）；
- `unresolved/cross_module.jsonl`（M4 未拼接记录，用于候选降级）；
- 配置中声明的一份 ULog。

运行期证据分为四类记录：`runtime_log`、`runtime_topic`、`runtime_field`、
`runtime_path` / `runtime_flag`。静态证据与运行期证据在
`reports/summary.md` 中分栏展示，不合并成一个“已确认”结论。

## 日志声明与选择

日志在配置中显式声明：

```yaml
ulog:
  path: /home/cecilriver/PX4-Autopilot/build/px4_sitl_default/rootfs/log/2026-10-16/12_23_31.ulg
  scenario: normal_sitl_unmodified_parameters
```

也可以对单次运行覆盖：

```bash
PYTHONPATH=src python3 -m px4dep.cli ingest-ulog --run <run_id> --ulog <path>
```

分析器记录日志的 sha256、大小、`ver_sw`、起止时间、初始参数和变更参数，
并写入 manifest。可自动核验的条件只有：

1. 日志 `ver_sw` 与构建 commit 一致（`commit_match`）；
2. 日志期间 `changed_parameters` 为空（当前基线为 0）。

“正常场景”“未修改参数飞行”是操作者对来源的声明，分析器不根据日志内容
推断场景，也不会把声明当作证据。日志只用于 topic/field/path 出现性检查，
不用于参数影响分析。

## 状态语义

| 状态 | 含义 |
|---|---|
| `observed` | topic 有数据实例且样本数 > 0；字段至少有一个有限（非 NaN）样本 |
| `not_observed` | 消息格式在日志中注册，但没有该 topic 的数据；或字段全部缺失/NaN |
| `not_logged` | 日志的 `message_formats` 中没有该消息格式（logger 未注册） |
| `not_applicable_constant` | `.msg` 常量字段，不参与日志记录 |

路径级支持级别：

| `runtime_support` | 条件 |
|---|---|
| `field_observed` | 边界 topic 和汇点字段都 `observed` |
| `topic_observed` | topic 出现，但字段未观测到 |
| `boundary_not_observed` | topic 未观测到（`not_observed` 或 `not_logged`） |
| `no_explicit_topic` | M4 没有把该参数的汇点绑定到显式 topic；运行期证据不适用 |
| `no_runtime_record` | 路径没有对应的运行期记录（只可能是输入不完整） |

每个 `runtime_topic` / `runtime_field` / `runtime_path` / `runtime_flag`
记录都带 `not_observed_means_unreachable: false`；`runtime_path` 另带
`interpretation: presence_only_not_causality`。`runtime_metrics.json` 的
`unreachable_claims` 必须为 0，并由 M5 gate 检查。

## 运行期连接规则

1. **边界 topic**：直接使用 M4 路径记录中的 `topic`，不按名称猜测。
2. **边界字段**：由 M4 路径的 `sink`（`message_type::field`）取字段名；
   在日志 dataset 中匹配同名列、数组列（`field[i]`）和嵌套列
   （`field.member`）。多实例（`multi_id`）的有限样本数相加，实例列表保留。
3. **图 topic/field**：对所有 `topic` 节点及其 `topic_field` 做同样的
   `observed` / `not_observed` / `not_logged` 分类，形成覆盖统计。
4. **failsafe flag**：只检查 M4 `consumer_evidence` 中 `flags_written` /
   `flags_read` 指向的 `failsafe_flags_s::<field>`，在 `failsafe_flags`
   topic 中查同名字段；不根据健康标志名称推测。
5. **未观测项**：写入 `unresolved/ulog.jsonl`，按
   `ulog_path_topic_not_observed`、`ulog_path_topic_not_logged`、
   `ulog_path_field_not_observed`、`ulog_flag_not_observed`、
   `ulog_flag_not_logged` 分类。这些记录是“需要更多日志/证据”，不是
   不可达结论。

## 候选规则

候选是**人工审查排序项**，不是漏洞、危险参数、适航或飞行安全结论。每条
候选必须同时满足：

1. 参数是当前构建中的真实参数（图里的 `parameter` 节点且 `in_build=true`）；
2. 至少有一条 M3 程序语义路径（`data_depends_on` 或 `control_depends_on`），
   或一条 M4 端到端路径；
3. 解释路径非空（M4 链或 M3 切片步骤）。

仅按参数名匹配、只有日志参数列表、或只有消息名称相似性的项不会成为候选。
`parameter` 为空的编译期常量路径不进入候选。

| `status` | 条件 |
|---|---|
| `eligible` | 有程序路径，且没有关联的 M4 未拼接记录 |
| `needs_review` | 同一参数存在 M4 未拼接路径；保留而不是丢弃 |

运行期只调整审查优先级，不改变静态路径是否存在：

| 评分项 | 分值 |
|---|---:|
| 至少一条数据依赖路径 | +4 |
| 仅控制依赖路径 | +1 |
| 额外汇点类别（每种至多 +1，最多 +3） | +1/种 |
| 汇点为消息字段或判据状态 | +2 |
| 到达健康谓词 | +3 |
| 写入 failsafe flag | +2 |
| 到达模式限制 | +2 |
| 到达保护动作 | +3 |
| 运行期字段观测 | +3 |
| 运行期 topic 观测（字段未观测） | +2 |
| 存在未拼接路径 | -2 |
| 最短静态路径深度 > 4 | -1 |

`review_priority`：`high` ≥ 10，`medium` ≥ 5，其余 `low`。该分数只用于
安排人工阅读顺序，不是风险评分。

候选记录保留：

- `slice_path_ids` / `end_to_end_path_ids` / `alternative_path_ids`；
- 最短解释路径：M4 时给出
  `参数 → 汇点字段 → 消息类型 → topic → 发布者 → 消费者 → 健康谓词/标志`，
  M3 时给出切片步骤（含文件、行号、关系）；
- `runtime_topics` / `runtime_fields` / `runtime_flags` 与
  `runtime_note`（presence only）；
- `unresolved_paths` 与 `notes`（降级、控制依赖、未观测、参数变更等）；
- `disclaimer`：审查项，不是漏洞或安全结论。

## 运行

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src python3 -m px4dep.cli run-all
```

只重建运行期证据和候选：

```bash
PYTHONPATH=src python3 -m px4dep.cli ingest-ulog --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli candidates --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli report --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli validate --run <run_id>
```

`run-all` 在 M5 基线时为 8 步；加入 M6 后扩展为 9 步（M6 位于候选之后、
报告之前，见 `docs/m6-operations.md`）。M5 输出：

```text
[5/8] ULog runtime evidence ...
runtime: <F>/<P> paths field-observed, <T>/<N> graph topics observed, <U> unresolved
[6/8] review candidates ...
      <C> candidates (<E> end-to-end)
[7/8] report ...
      M5 gate: PASS
```

## 输出

| 路径 | 内容 |
|---|---|
| `evidence/runtime.jsonl` | `runtime_log`、topic、field、path、flag 记录 |
| `unresolved/ulog.jsonl` | 未观测/未记录项，按 kind 分类 |
| `reports/runtime_metrics.json` | 日志对应、覆盖率、路径支持级别、flag 与未观测计数 |
| `evidence/candidates.jsonl` | 候选权威记录，含解释路径、评分理由、运行期列 |
| `reports/candidate_metrics.json` | 候选数量、分级、端到端/数据/控制、运行期支持分布 |
| `reports/candidates.csv` | 人工审阅表（由 JSONL 派生） |
| `reports/summary.md` | M5 日志对应、静态/运行期分栏、候选表与 M5 gate |
| `reports/metrics.json` | 包含 `runtime`、`candidates`、`m5_gates`、`m5_gate_passed` |
| `manifest.yaml` / `manifest.json` | 新增 ULog 路径、sha256、大小、场景声明、用途 |

## M5 完成判据

`reports/metrics.json` 的 `m5_gates` 为：

- `runtime_evidence_present`：日志存在且产生运行期记录；
- `log_matches_build_commit`：日志 `ver_sw` 与构建 commit 一致；
- `all_m4_paths_have_runtime_record`：每条 M4 路径都有运行期记录；
- `runtime_interpretation_reachability_only`：解释固定为 `reachability_only`；
- `not_observed_not_marked_unreachable`：`unreachable_claims == 0` 且
  `not_observed_is_not_unreachable` 为真；
- `candidates_present`：至少一个候选；
- `all_candidates_are_build_parameters`：所有候选都是构建内参数；
- `all_candidates_have_program_path`：程序语义路径数不少于候选数；
- `no_name_only_candidates`：`name_only_candidates == 0`；
- `all_candidates_have_explanation`：每个候选都有解释文本；
- `all_candidates_have_runtime_column`：每个候选都有 `runtime_support`；
- `no_missing_m5_outputs`：上述输出文件齐全。

验证器另外检查 ULog 输入哈希、重复运行期/候选 ID、运行期路径必须引用
M4 路径 ID、候选必须是构建内参数且带解释文本。

## 当前基线结果

最新通过 run（`20261021T111550Z_5356d24e5fa5`，PX4 `5356d24e`）的关键指标：

| 指标 | 数量 |
|---|---:|
| 日志大小 / 时长 | 40,343,801 字节 / 152.32 s |
| 日志 sha256 | `58da4454dc3b50ad890fe3ff42257c914a7f07c7d048074f85fbfffdd096b13b` |
| 日志 `ver_sw` = 构建 commit | PASS |
| 初始参数 / 日志期间变更参数 | 1111 / 0 |
| 日志消息格式 / 已观测 topic | 149 / 85 |
| 图 topic：observed / not_observed / not_logged | 85 / 64 / 185 |
| 图 field：observed / not_observed / not_logged / constant | 1160 / 658 / 1406 / 1699 |
| M4 路径：topic observed / field observed | 5 / 5（共 5） |
| failsafe flag：observed / not_observed / not_logged | 14 / 0 / 0 |
| 运行期未解析记录 / 不可达断言 | 0 / 0 |
| 运行期记录总数 | 5277 |

候选基线：

| 指标 | 数量 |
|---|---:|
| 候选参数 | 52 |
| `eligible` / `needs_review` | 14 / 38 |
| `high` / `medium` / `low` | 1 / 8 / 43 |
| 端到端路径候选 | 1 |
| 有数据依赖路径 / 仅控制依赖 | 29 / 23 |
| 运行期 `field_observed` | 1 |
| 无显式 topic（运行期不适用） | 51 |
| 关联到未拼接 M4 路径的记录 | 186 |

基线中唯一同时具备端到端路径和运行期字段观测的候选：

```text
EKF2_GPS_CHECK
  → estimator_status_s::gps_check_fail_flags
  → estimator_status（运行期 field_observed）
  → EKF2::PublishStatus
  → EstimatorChecks::checkAndReport
  → EstimatorChecks::checkGps / Report::armingCheckFailure
```

其分数为 16（`high`），原因是数据路径、判据状态汇点、健康谓词、failsafe
标志写入、模式限制和运行期字段观测；`status=eligible`，
`log_parameter_present=true`，`log_parameter_changed=false`。其余候选没有
显式 topic 绑定，`runtime_support=no_explicit_topic`，运行期证据不适用；
其中 38 个因存在未拼接路径而为 `needs_review`。

## 测试

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src:tests python3 -m pytest tests/test_ulog_evidence.py -q  # ULog 单元测试
PYTHONPATH=src:tests python3 -m pytest tests/test_candidates.py -q     # 候选单元测试
PYTHONPATH=src:tests python3 -m pytest tests -q                        # 全部单元 + 集成
```

单元测试使用合成 `LogIndex` 和图，覆盖：observed 仅表示出现性、格式存在但
无数据为 `not_observed`、格式缺失为 `not_logged`、多实例样本求和、字段缺失
时 topic 仍可达、常量字段不适用、无日志时不伪造负结论、flag 缺失报告无
负结论；以及候选的构建参数约束、端到端解释、控制依赖降级、未拼接路径
`needs_review`、未观测不写成不可达、稳定 ID 与 CSV。

集成测试读取真实 PX4 和最近一次 run，检查 M5 gate、日志 commit 对应、
黄金路径 `EKF2_GPS_CHECK → estimator_status_s::gps_check_fail_flags →
estimator_status` 的字段观测、所有运行期记录均为 presence-only、候选均带
解释并且 `EKF2_GPS_CHECK` 为 `field_observed` / `high` / `eligible`、
CSV 与 JSONL 一致。

## M5 已知边界

1. **只用一份正常场景日志**：覆盖取决于该次 SITL 的启动模式、logger 配置
   和飞行时长；统计只描述这份日志，不外推到其他场景。
2. **出现性不是因果**：字段出现在日志中，不代表参数取值影响了它，也不
   代表相关保护逻辑被触发。`runtime_note` 与 `interpretation` 固定记录这
   一点；本阶段不计算相关性、置信区间或反事实。
3. **消费者证据仍是 topic 级**：M4 的 `consumer_field_binding=topic_level`
   未改变；运行期 field 观测证明边界字段出现过，不证明 Commander 读取了
   该字段。
4. **`not_logged` 取决于 logger 配置**：未注册的消息格式不会出现，这与
   “代码不可达”无关；报告中按 `not_logged` 单列，不并入 `not_observed`。
5. **运行期不改变静态置信度**：`confidence` 仍来自 M3（数据 `medium`、
   控制 `low`）；运行期支持只影响审查优先级。
6. **候选优先级是启发式排序**：分数与 `high/medium/low` 不是风险评级，
   不包含参数取值、阈值余量或实际触发概率；不搜索、不输出参数篡改组合。
7. **未拼接路径保守保留**：M4 的 186 条参数关联未拼接记录使 38 个候选为
   `needs_review`，不会为了得到“干净”候选而丢弃这些路径。
8. **只读与正常日志约束**：未修改 PX4 仓库，未修改参数，未注入传感器，
   未干预通信；日志只用于出现性检查，所有输出写入实验项目目录。
