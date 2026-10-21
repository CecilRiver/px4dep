# M2 运行与验收说明

本文件记录 M2（健康层 → 保护层结构闭合）的实现范围、运行方式、输出、判据和
已知边界。实验设计背景见项目根目录 `README.md`；M1 基线见
`docs/m1-operations.md`。

M2 的目标不是生成参数候选，而是先把以下四类结果在图上明确分开：

| 类别 | 图上的节点/边 | 典型来源 |
|---|---|---|
| 告警 / 健康报告 | `predicate`（`effect=health_warning/arming_warning/...`） | `Report::healthFailure`、`Report::armingCheckFailure` |
| 模式可用性 / 模式禁止 | `mode_capability` + `requires_mode`、`restricts_arming`、`restricts_run`、`checks_mode`、`prevents_mode` | `required_modes`、`modeCanRun`、`checkModeFallback` |
| failsafe flag | `health_flag` + `writes_flag`、`reads_flag`、`maps_to_field` | `failsafe_flags_s` 成员写入/读取、`CHECK_FAILSAFE` |
| 保护动作决策 | `protection_decision` + `selects_action`、`assigns_action` | `FailsafeBase::Action`、动作 options |

## M2 范围

`configs/px4_sitl_default.yaml` 在 M1 的 7 个 TU 基础上增加了 Commander 的
全部健康检查与 failsafe 框架，共 45 个 TU：

- `src/modules/commander/HealthAndArmingChecks/checks/*.cpp`；
- `src/modules/commander/failsafe/framework.cpp`；
- `src/modules/commander/failsafe/failsafe.cpp`。

框架级匹配器单独记录在 `configs/sink_catalog.yaml`。该文件只描述 API
契约（方法、`CHECK_FAILSAFE` 宏、`FailsafeBase::Action` 枚举），
不包含候选参数名，也不用于搜索参数改动组合。其哈希写入 manifest 并参与
validation。

## 运行

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src python3 -m px4dep.cli run-all
```

若 Clang 工具尚未构建，或需要强制重建：

```bash
PYTHONPATH=src python3 -m px4dep.cli run-all --rebuild
```

成功输出包含：

```text
[4/6] evidence graph ...
      <N> nodes, <M> edges
      health chain: 157 health, 20 mode checks, 32 failsafe checks, 11 decisions
[5/6] report ...
      M1 gate: PASS
      M2 gate: PASS
[6/6] validation ...
      PASS
```

也可复用已有 run 只重建图和报告：

```bash
PYTHONPATH=src python3 -m px4dep.cli build-graph --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli report --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli validate --run <run_id>
```

## 输出

| 路径 | 内容 |
|---|---|
| `normalized/health_chain.jsonl` | M2 节点：`predicate`、`health_flag`、`mode_capability`、`protection_decision`、`health_component` |
| `normalized/health_chain_edges.jsonl` | M2 关系边（M1 关系不在此文件） |
| `unresolved/health_chain.jsonl` | 动态 mode 集合、动态动作 options 等未解析项 |
| `reports/health_chain_metrics.json` | 分层计数、severity、flag 映射、动作分布、边计数 |
| `reports/health_chain.csv` | 每个谓词一行，便于人工审查 |
| `reports/summary.md` | “M2 健康—保护链结构”与 M2 gate |
| `reports/metrics.json` | 包含 `m2_gates` 与 `m2_gate_passed` |

`graph/nodes.jsonl` 和 `graph/edges.jsonl` 仍是权威合并图；上述
`normalized/health_chain*.jsonl` 是同一批节点的分视图。

## 关系语义

| 关系 | 语义 | 置信度 |
|---|---|---|
| `reports_health` | 函数包含一个健康/告警报告调用点 | high |
| `reports_component` | 报告指向某个 `health_component_t` | high |
| `reports_flag` | 同一函数内的健康报告与 flag 写入的结构关联 | low（`same_function_scope`） |
| `writes_flag` / `reads_flag` | 函数直接写/读 `failsafe_flags_s` 字段 | high |
| `maps_to_field` | `health_flag` 映射到 `.msg` 中的 `failsafe_flags_s` 字段 | high |
| `requires_mode` | 报告只在给定 mode group / nav state 下生效 | high（静态 mode 集） |
| `restricts_arming` / `restricts_run` | 报告或显式调用清除 arming/run 位 | high |
| `checks_mode` / `queries_mode` | 运行时查询某模式的可用性 | high |
| `prevents_mode` | 否定形式的模式可用性判断 | high |
| `selects_action` | failsafe 条件选择保护动作 | high（字面量 options） |
| `assigns_action` | 函数把某个动作写入 `_selected_action` / options | high |

`reports_flag` 不是数据流。它只表示“同一函数里既有健康报告，也有该 flag
写入”，用于帮助审查，不能单独作为因果结论。

## M2 完成判据

`reports/metrics.json` 的 `m2_gates` 为：

- `health_layer_present`：存在健康报告谓词与 arming-check 谓词；
- `warning_layer_present`：存在 warning 级别结果；
- `mode_layer_present`：存在 `mode_capability` 节点及模式查询；
- `arming_prohibition_present` / `run_prohibition_present`：存在对应禁止边；
- `mode_query_present`：存在 `checks_mode` 或 `queries_mode`；
- `failsafe_flag_layer_present`：flag 节点、写入和读取均存在；
- `health_flags_mapped_to_message`：全部 flag 映射到 `.msg` 字段；
- `protection_condition_present`：存在 `CHECK_FAILSAFE` / `checkFailsafe`；
- `protection_decision_present`：存在动作节点与 `selects_action`；
- `action_return_or_assignment_present`：存在动作返回或赋值；
- `layers_not_collapsed`：健康、模式禁止、保护条件、动作决策同时存在；
- `no_missing_m2_outputs` / `no_dangling_edges_m2`：输出完整、无悬空边。

## 当前基线结果

最新通过 run 的关键指标（会随源码/范围变化）：

| 指标 | 数量 |
|---|---:|
| 健康/告警谓词 | 157 |
| arming-check 谓词 | 93 |
| mode check 调用 | 20 |
| mode availability 谓词 | 19 |
| `health_flag` | 47（全部映射到 `.msg`） |
| flag 写入 / 读取（函数级） | 58 / 175 |
| failsafe 条件 | 32 |
| 动态动作 options | 16 |
| 保护动作决策 | 11 |
| 动作赋值引用 | 100 |
| M2 未解析项 | 88 |

## 测试

```bash
cd /home/cecilriver/px4_config_dependency_experiment
python3 -m pytest tests/test_health_chain.py -q     # M2 单元测试
python3 -m pytest tests -q                          # 全部单元 + 集成
```

集成测试读取真实 PX4 基线和最近一次 run，覆盖：

- M2 gate 全部通过；
- `flight_time_limit_exceeded` 被 `CHECK_FAILSAFE` 读取并选择 `Action::RTL`；
- `mode_capability` 节点与 `modeCanRun` 查询；
- `position_accuracy_low` 的写入、读取与 `.msg` 字段映射；
- 告警、模式禁止、failsafe flag、动作决策四类节点/边不混同。

## M2 已知边界

1. **动态 mode 集合**：`required_modes`、`affected_modes`、`user_intended_mode`
   等运行期变量不能静态展开，保留为 `dynamic:*` 节点并记录
   `mode_set_dynamic`，不猜测为具体 mode group。
2. **动态动作 options**：`from...Param(...)` 形式的动作选项记录为
   `action_dynamic`，不强行归入 `RTL`/`Land` 等具体动作。
3. **同函数 flag 关联是低置信结构边**：`reports_flag` 不追踪局部变量或跨函数
   数据流；M3/M4 才建立参数与状态估计/阈值之间的关系。
4. **动作返回值较少**：当前框架多以 `options.action = ...` 赋值；图用
   `assigns_action` 表达，`returns_action` 可能为 0，这是源码形态而非缺失。
5. **未做参数影响判断**：M2 不生成 `candidates.csv` 条目，也不判断某个参数
   是否危险；候选需等健康阈值与保护策略层连接完成后才建立。
6. **只读与正常日志约束**：不修改 PX4 参数、不注入传感器、不干预通信；
   M2 未使用 ULog。运行时可达性从 M5 开始，且只用正常场景日志。
