# M7 Commander/Failsafe 参数切片扩展

本文件记录 M7（Commander/Failsafe 参数包装成员切片）的实现范围、路径规则、
汇点分类、证据分层、输出、运行方式、验收判据和已知边界。实验设计背景见项目
根目录 `README.md`；M1—M6 的实现记录见 `docs/m1-operations.md` 至
`docs/m6-operations.md`。

M7 不改变 M1—M6 的门槛：`graph/paths.jsonl` 仍然只包含 EKF2 局部路径，
`normalized/ekf_slice_edges.jsonl` 仍然只包含 EKF2 切片边，
`unresolved/graph.jsonl` 和 M6 断链统计仍然不包含 Commander 记录。M7 的
输出放在独立的 `commander_*` 文件和 `analysis_scope=commander_slice` 的候选
记录中。

## 分析范围

M1 的 Clang LibTooling 提取同时覆盖 EKF2 和 Commander 两组翻译单元。M7 使用
其中 Commander 组的 45 个翻译单元，以及参数目录中当前构建变体的参数。

| 项目 | 数量 |
|---|---:|
| 参数包装成员节点（两组提取合计） | 242 |
| 其中声明在 `src/modules/commander/` | 105 |
| 其中声明在 `src/modules/ekf2/`（不计入 Commander 指标） | 137 |
| Commander 包装字段名（去重） | 89 |
| 构建内 Commander 包装参数 | 89 |

包装节点的索引覆盖两组提取，以便 Commander 代码直接读取外部包装时仍能匹配；
但 `parameters_with_wrapper`、`parameters_without_path` 等 M7 指标只统计
Commander 模块内声明的包装。

## 路径规则

切片源是 `px4::params::NAME` 包装成员，例如 `Failsafe::_param_com_qc_act`。
切片汇点分为四层：

| sink kind | 语义层 | 说明 |
|---|---|---|
| `health_predicate` | health | `checkAndReport` 中的健康/arming 判定参数 |
| `mode_restriction` | mode | mode capability 表达式、mode fallback 检查 |
| `failsafe_flag_write` / `failsafe_flag_read` | failsafe_flag | `failsafe_flags` 字段写入/读取 |
| `protection_action_option` / `protection_action` / `protection_check` | protection_action / protection_condition | failsafe 动作选项、动作枚举、保护条件 |

路径分为三类，互不替代：

- **data**：包装字段 token 精确出现在汇点自身的表达式文本中（健康报告实参、
  failsafe 条件/动作选项、failsafe flag 写入右值、mode 表达式等）。置信度
  `medium`，写入 `data_depends_on` 边。
- **control**：包装字段 token 出现在汇点的外层 `if` / `switch` / 三元条件中。
  置信度 `low`，写入 `control_depends_on` 边。
- **context**：同一函数内，或从读取函数沿反向调用边至多 `MAX_CONTEXT_HOPS=2`
  跳可达的汇点。它是函数级邻近证据，**不是**数据或控制依赖；所有 context
  路径标记 `context_only=true`、`confidence=low`，只在路径记录中出现，不写入
  图边。

匹配使用精确 token 正则
`(?<![A-Za-z0-9_])field(?![A-Za-z0-9_])`，因此 `_param_com_wind_max` 不会
误匹配 `_param_com_wind_max_act`。同名字段出现在多个类时，先用汇点调用者类
消歧；仍无法唯一确定时写入 `commander_wrapper_field_ambiguous`，不猜测。

context 路径按 `(parameter, wrapper)` 分组，按跳数和汇点类别排序，每个参数最
多保留 6 条，优先覆盖不同 sink kind。被截断的数量记录在
`context_paths_suppressed`，不静默丢弃。

只有当前构建变体内的参数才产生路径；包装对应的参数不在构建内时写入
`commander_parameter_not_in_build`。

## 未解析记录

M7 的未解析记录只写入 `unresolved/commander_slice.jsonl`，**不**进入
`unresolved/graph.jsonl`，也不改变 M6 的 `by_stage` 统计。

| kind | 含义 |
|---|---|
| `commander_parameter_without_path` | 构建内 Commander 包装参数没有任何 data/control/context 路径 |
| `commander_read_site_without_nearby_sink` | `.get()` 读取点在同函数或两跳内没有汇点 |
| `commander_wrapper_field_ambiguous` | 同名字段无法按调用者类唯一消歧 |
| `commander_parameter_not_in_build` | 包装字段匹配到表达式，但参数不在当前构建目录 |

## 输出

| 文件 | 内容 |
|---|---|
| `graph/commander_paths.jsonl` | 全部 Commander 切片路径（含 context 标记） |
| `normalized/commander_slice_edges.jsonl` | 仅 data/control 依赖边 |
| `unresolved/commander_slice.jsonl` | 上述未解析记录 |
| `reports/commander_slice_metrics.json` | 指标、路径 sink kind 分布、无路径参数 |
| `reports/commander_slice.csv` | 路径表 |

候选报告新增列 `analysis_scope`、`commander_paths`、`context_only`。
`reports/candidates.csv` 的前五列保持不变：
`candidate_id,parameter,status,review_priority,score`。

## 候选评分

- data 路径：`+4`；
- 只有 control 路径：`+1`；
- 只有 context 路径：`+0`，降级原因 `function_context_only`；
- 含 context-only 路径或存在未拼接路径：状态为 `needs_review`；
- `review_priority` 只用于人工审查排序，不是漏洞、适航或飞行安全结论。

## 运行

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src python3 -m px4dep.cli build-graph --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli ingest-ulog --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli candidates --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli llvm-fallback --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli report --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli validate --run <run_id>
```

`run-all` 会按同样顺序执行，并在 build-graph 阶段打印 Commander 切片统计。

## 基线结果

基线 run `20261021T120745Z_5356d24e5fa5`（PX4 `5356d24e5fa5`，
`px4_sitl_default`）：

| 指标 | 值 |
|---|---:|
| Commander 包装参数（构建内） | 89 |
| 有静态路径的参数 | 75 |
| 直接 data 路径参数 / control 路径参数 | 17 / 23 |
| 仅 context 路径的参数 | 44 |
| data / control / context 路径 | 17 / 43 / 382 |
| data / control 边 | 17 / 43 |
| 无路径包装参数 | 14 |
| 未解析记录 | 18 |

路径 sink kind 分布：`health_predicate` 207、`protection_check` 66、
`failsafe_flag_read` 51、`mode_restriction` 41、`failsafe_flag_write` 36、
`protection_action` 29、`protection_action_option` 12。

候选结果：127 条候选，其中 Commander 75 条；44 条仅含 context 路径。

## 验收判据

| gate | 判据 |
|---|---|
| `commander_slice_paths_present` | 至少存在一条路径 |
| `commander_data_paths_present` | 至少存在一条 data 路径 |
| `commander_control_paths_present` | 至少存在一条 control 路径 |
| `commander_context_paths_labelled` | context 路径数等于 context_only 路径数，且全部有读取点 |
| `commander_paths_have_expression` | 非 context 路径都有表达式/条件，所有路径都有 steps |
| `commander_sink_layers_present` | 路径覆盖 health、mode、failsafe flag、protection action option 四层 |
| `commander_parameters_are_build_parameters` | 有路径参数数不超过包装参数数 |
| `no_missing_commander_outputs` | 五个 M7 输出文件均存在 |

`reports/metrics.json` 记录 `commander_slice`、`commander_slice_gates` 和
`commander_slice_gate_passed`；`validate` 另行检查重复路径 ID、非构建参数、
空 steps 和 context 标注。

单元测试：`tests/test_commander_slice.py`（8 项），覆盖 direct/control/
context、精确 token、同名消歧、非构建参数、context 上限，以及同一
`failsafe_flags` 字段多个写入点时后续站点的包装读取仍能匹配。完整测试集
113 项通过。

## 边界

- PX4 仓库只读；本扩展不修改 PX4 源码、参数或构建产物；
- 不做参数修改实验、传感器注入或通信干预；
- 运行期证据只使用正常场景日志，且只用于 topic/field/path 可达性；
- 不搜索、不输出参数篡改组合，不讨论规避保护逻辑的方法；
- 候选是待人工核查项，不是漏洞、失效或适航结论；
- context 路径不是依赖关系，不得当作 data/control 证据引用；
- 所有路径保留构建条件、置信度、未知项和源码位置；
- 路径缺失不等于不可达。
