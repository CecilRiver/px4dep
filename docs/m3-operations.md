# M3 运行与验收说明

本文件记录 M3（EKF2 字段敏感反向切片）的实现范围、运行方式、输出语义、
验收判据和已知边界。实验设计背景见项目根目录 `README.md`；M1/M2 基线见
`docs/m1-operations.md` 和 `docs/m2-operations.md`，跨模块路径见
`docs/m4-operations.md`。

M3 解决的是“配置参数如何进入状态估计内核，并到达消息字段、控制/故障标志
或判据状态”这一静态链路问题。它仍然不判断参数取值是否危险，也不生成
参数候选。

## M3 范围

`configs/px4_sitl_default.yaml` 在 M2 的 45 个 TU 基础上加入 EKF2 核心估计器、
全部 aid source 与 EKF2 模块入口，共 90 个 TU：

- `src/modules/ekf2/EKF/*.cpp`（13 个核心 TU：control、covariance、ekf、
  estimator_interface、各 fusion/control 等）；
- `src/modules/ekf2/EKF/**/*.cpp`（32 个 aid source、bias、output predictor、
  yaw estimator 等）；
- `src/modules/ekf2/EKF2.cpp`、`src/modules/ekf2/EKF2Selector.cpp`；
- M2 原有的 43 个 Commander TU（40 个 health check + failsafe framework +
  failsafe + Commander）。

范围仍由 `ExcludeDirs` 和构建变体 `px4_sitl_default` 约束；不索引 PX4 中的
其他模块，也不根据名称相似度补全未索引的调用者。

## 提取的 C++ 记录

Clang 工具在 M1/M2 记录之外新增五类字段流记录：

| `record` | 含义 | 关键字段 |
|---|---|---|
| `member_access` | 对字段的读/写，以及写入右侧的字段/局部引用 | `qualified_name`、`field`、`parent_class`、`receiver_expr`、`receiver_kind`、`access`、`rhs_members`、`rhs_locals`、`control_conditions` |
| `local_write` | 局部变量或函数参数的声明/赋值 | `variable`、`write_kind`、`rhs_members`、`rhs_locals` |
| `call_arg` | 实参到形参的绑定 | `callee_usr`、`param_name`、`arg_index`、`members`、`locals` |
| `call_result` | 局部变量接收函数返回值 | `callee_usr`、`target_kind`、`target_name` |
| `return_ref` | `return` 表达式引用的字段/局部 | `members`、`locals`、`text` |

所有记录带 `caller_usr`、`caller`、源码位置和 TU；控制条件额外带
`kind`（if/switch/ternary/while/for）、条件文本和 `line/column`。

## 汇点类别

| 汇点 | 节点 kind | 识别规则 |
|---|---|---|
| control flag | `estimator_flag`（`flag_domain=control`） | 写入 `estimator::filter_control_status_u` 的字段 |
| fault flag | `estimator_flag`（`flag_domain=fault`） | 写入 `estimator::fault_status_u` 的字段 |
| 判据状态 / innovation | `slice_sink` 或已有 `topic_field` | 写入 `estimator_aid_source*_s`、`estimator_status_s`、`estimator_innovations_s` 等判据字段 |
| 发布消息字段 | `topic_field` 或 `slice_sink` | 在 `uORB::Publication::publish()` 所在函数中写入的 `*_s` 字段 |

只有“写入”才建立汇点；读取字段不会单独产生候选路径。已有 `.msg` 字段节点
直接复用，不复制节点；找不到 schema 节点的消息字段记录为
`slice_message_field_not_in_schema`。

## 运行

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src python3 -m px4dep.cli run-all
```

若 Clang 工具尚未构建：

```bash
PYTHONPATH=src python3 -m px4dep.cli run-all --rebuild
```

复用已有 run 只重建图和报告：

```bash
PYTHONPATH=src python3 -m px4dep.cli build-graph --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli report --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli validate --run <run_id>
```

成功输出包含：

```text
[4/6] evidence graph ...
      ekf slice: <S> sinks, <B> parameter fields bound, <D> data / <C> control edges, <P> paths
[5/6] report ...
      M3 gate: PASS
[6/6] validation ...
      PASS
```

## 输出

| 路径 | 内容 |
|---|---|
| `normalized/ekf_slice.jsonl` | 新增的汇点节点：`estimator_flag`、`slice_sink` |
| `normalized/ekf_slice_edges.jsonl` | `data_depends_on` / `control_depends_on` 边 |
| `graph/paths.jsonl` | 每个（汇点、参数字段、依赖类别）的反向路径，含逐步证据 |
| `unresolved/ekf_slice.jsonl` | 未绑定的参数包装字段、未在 schema 中的消息字段、深度/条目截断 |
| `reports/ekf_slice_metrics.json` | 汇点、参数绑定、边/路径计数 |
| `reports/ekf_slice.csv` | 每条边一行，便于人工审查 |
| `reports/summary.md` | M3 汇点表、数据/控制边、跨函数路径与 M3 gate |
| `reports/metrics.json` | 包含 `ekf_slice`、`m3_gates` 与 `m3_gate_passed` |

`graph/nodes.jsonl` 和 `graph/edges.jsonl` 是权威合并图；
`normalized/ekf_slice*.jsonl` 是同一批节点的分视图。

## 关系语义

| 关系 | 语义 | 置信度 |
|---|---|---|
| `data_depends_on` | 汇点的值经赋值、局部变量、实参/返回值或内存前驱到达配置字段 | medium |
| `control_depends_on` | 字段写入所在的 if/switch/ternary/循环条件引用了配置字段 | low |

数据路径的步骤 `kind` 包括：

- `rhs_member`：汇点写入右侧直接引用字段；
- `rhs_local` / `local_definition`：经局部变量中转；
- `memory_write`：共享类成员 `receiver_expr.field` 的写入前驱；
- `call_argument`：实参 → 形参；
- `return_value`：返回值 → 接收变量。

控制路径的步骤 `kind` 包括 `control_condition`、`write_control_condition`、
`local_control_condition`、`call_control_condition`、
`return_control_condition`。`mask_symbols` 保留条件中参与位掩码的符号文本。

数据边与控制边不合并：同一个参数可以同时产生两类边，但 `relation`、
`confidence` 和路径 `category` 始终分开。`flow_sensitive=false` 和
`memory_predecessors=true` 明确表示共享类成员按字段名匹配，未做程序点敏感的
def-use 排序。

## M3 完成判据

`reports/metrics.json` 的 `m3_gates` 为：

- `parameter_field_binding_complete`：使用到的 `estimator::parameters` 字段
  全部能连接到 `config_field` 节点；
- `parameter_source_reached`：路径确实到达参数节点，而不是停在字段边界；
- `data_dependency_edges_present` / `control_dependency_edges_present`：
  两类边均存在；
- `data_and_control_separate`：合并图中同时存在两种关系，且计数独立；
- `control_flag_sink_present` / `fault_flag_sink_present` /
  `message_field_sink_present` / `criteria_state_sink_present`：四类汇点均出现；
- `cross_function_path_present`：至少一条路径跨函数（内存前驱、实参或返回值）；
- `slice_paths_have_steps`：每条路径都有非空步骤；
- `no_parameter_field_unbound`：不存在未绑定参数包装字段；
- `no_missing_m3_outputs` / `no_dangling_edges_m3`：输出完整、无悬空边。

## 当前基线结果

最新通过 run 的关键指标（会随源码/范围变化）：

| 指标 | 数量 |
|---|---:|
| 索引 TU | 90 |
| `member_access` 记录 | 5785 |
| `local_write` 记录 | 2285 |
| `call_arg` / `call_result` / `return_ref` 记录 | 3926 / 367 / 209 |
| `estimator::parameters` 字段（使用/绑定/未绑定） | 133 / 133 / 0 |
| control flag 汇点 | 50 |
| fault flag 汇点 | 11 |
| 判据状态汇点 | 76 |
| 发布消息字段汇点 | 230 |
| 数据依赖边 / 控制依赖边 | 80 / 495 |
| 反向路径 / 跨函数路径 | 575 / 439 |
| 到达的参数字段节点 | 61 |
| 边界字段（无本地写入） | 416 |
| 截断的汇点 | 6 |

`reports/ekf_slice.csv` 中有 9 个配置字段没有对应的 PX4 参数名（例如
`estimator::parameters::reset_timeout_max`，类型为 `const unsigned int`）。
这些是编译期常量而非可配置参数，因此 `parameter` 列为空；它们仍是合法的
静态依赖源，但不进入后续参数候选评估。

## 测试

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src:tests python3 -m pytest tests/test_ekf_slice.py -q   # M3 单元测试
PYTHONPATH=src:tests python3 -m pytest tests -q                     # 全部单元 + 集成
```

单元测试使用合成记录，覆盖：数据/控制依赖分离、局部变量链、跨函数类成员
内存前驱、实参跨函数传递、消息字段汇点复用、未绑定参数不猜测、稳定 ID、
CSV 行数。集成测试读取真实 PX4 基线和最近一次 run，检查 M3 gate、
汇点节点 kind、两种依赖关系以及参数绑定完整性。

## M3 已知边界

1. **流不敏感的内存前驱**：同一 `receiver_expr.field` 的所有写入都作为
   读取的候选前驱，不区分程序点、循环轮次或调用时序。路径中的
   `memory_write` 是“可能前驱”，不是唯一证明。
2. **接收者表达式归一化有限**：`this->` 前缀会剥离，`_control_status.flags`
   等成员链保留为同一接收者；通过函数参数传入的引用、数组下标、容器元素
   可能落到不同 key，记录为边界字段。
3. **深度与条目上限**：`MAX_DEPTH=12`，每个汇点最多 `4000` 个工作项。
   截断写入 `unresolved/ekf_slice.jsonl`（`slice_depth_truncated`、
   `slice_item_limit`），不会把截断误报为完整路径。
4. **只追踪字段与局部变量**：不对指针别名、虚函数分派、模板实例化做跨函数
   解析；未索引 TU 中的 endpoint 仍是边界。
5. **消息字段汇点要求写入发生在 publish 函数内**：通过 helper 修改消息后
   再由另一函数 publish 的情形，可能只形成 `slice_sink` 边界或不被识别。
   M4 不会按消息类型猜测 topic：这类路径保留在
   `unresolved/cross_module.jsonl`，而不是拼接成端到端路径
   （见 `docs/m4-operations.md`“已知边界”第 2 条）。
6. **不代表运行期因果性**：M3 只给静态可达路径，不判断参数取值、传感器场景
   或实际触发；运行期可达性从 M5 开始，且只使用正常场景 ULog。
7. **M3 本身不生成候选**：M3 的边由 M4 用于拼接跨模块端到端路径；M5
   再结合运行期证据把这些路径汇总为 `candidates.csv` 中的人工审查排序项
   （见 `docs/m5-operations.md`），而不是 M3 直接给出参数审查候选。
8. **只读约束**：未修改 PX4 仓库、未修改参数、未注入传感器、未干预通信。
