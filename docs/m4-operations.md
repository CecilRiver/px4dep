# M4 运行与验收说明

本文件记录 M4（EKF2 → Commander 的显式跨模块端到端路径）的实现范围、连接
规则、运行方式、输出语义、验收判据和已知边界。实验设计背景见项目根目录
`README.md`；此前的基线见 `docs/m1-operations.md`、`docs/m2-operations.md`
和 `docs/m3-operations.md`。

M4 解决的问题是：M3 的字段敏感反向切片停在“EKF2 写入某个消息字段”这一步。
本阶段把该字段经 **明确的 uORB topic 与字段** 接到 Commander 一侧的读取者、
健康检查、健康标志和模式限制，从而得到
`配置参数 → 估计计算/判据 → 健康检查 → 模式可用性` 的完整静态链。

M4 仍不判断参数取值是否危险，也不生成参数候选；运行期证据与候选分级
由 M5 在 M4 路径之上完成（见 `docs/m5-operations.md`）。

## M4 范围

M4 不新增索引 TU。它复用 M3 的 90 个 TU（EKF2 45 个 + Commander 45 个），
并从这些 TU 的 Clang 记录中提取 `call` 站点，建立调用图边。跨模块路径的
起点只能是 M3 已产出的、汇点为消息字段（`topic_field`）的切片路径。

因此 M4 的覆盖面严格受 M3 的汇点识别范围约束：EKF2 未写入的消息字段、
未索引的函数、以及 helper 写入后由另一函数发布的模式（见“已知边界”）都
不会产生端到端路径。

## 显式连接规则

M4 只使用图中的显式边，按固定顺序拼接：

```text
topic_field  ──defines────▶  message_type
message_type ──instantiates▶  topic
topic        ──publishes──▶  uorb_endpoint（publication）
uorb_endpoint──uses_uorb──▶  function（operation=publish）
topic        ──subscribes─▶  uorb_endpoint（subscription）
uorb_endpoint──uses_uorb──▶  function（operation=copy/update）
```

具体约束：

1. **字段 → 消息类型**：汇点字段必须有一条 `defines` 边（来自 `msg/*.msg`
   解析），且该边的 `dst` 消息类型与字段声明的 `message_type` 一致。
2. **消息类型 → topic**：必须有 `instantiates` 边；消息类型与 topic 的
   `message_type` 字段一致。一个消息类型可以对应多个 topic，M4 不按名称
   包含关系选择，只保留真实 `instantiates` 边能到达的 topic。
3. **topic → 发布者**：发布 endpoint 必须 `publishes` 该 topic；使用该
   endpoint 的函数必须带 `operation=publish`，且函数位于
   `src/modules/ekf2/`。
4. **topic → 消费者**：订阅 endpoint 必须 `subscribes` 该 topic；使用该
   endpoint 的函数必须带 `operation=copy` 或 `update`，且函数位于
   `src/modules/commander/`。
5. **发布者来源**：M3 的汇点写入者（`sink_writers`，按 C++ USR 记录）必须
   能在函数索引中解析为同一个函数；否则记录为未解析，不做名称猜测。

`name_based_joins` 恒为 0；任何一步缺少边都会写入
`unresolved/cross_module.jsonl`，而不是用名称相似度补全。

## 调用图与消费者证据

M4 为已索引的函数对新增 `calls` 边（`caller_usr` 与 `callee_usr` 都能
解析为函数节点时才建边）。消费者证据从 Commander 侧的 uORB 使用函数出发，
沿 `calls` 边做有界 BFS：

- 深度上限 `MAX_CONSUMER_DEPTH=6`，函数数上限 `MAX_CONSUMER_FUNCTIONS=64`；
- 收集健康谓词（`reports_health`）、健康标志写入/读取
  （`writes_flag`/`reads_flag`）、模式限制（`restricts_arming`/
  `restricts_run`、`prevents_mode`/`checks_mode`）；
- `protection_chains` 只保留“读取健康标志的谓词自身 `selects_action`”的
  组合；
- 每条证据都保留边 ID、源码位置和调用链（调用链中的每一步也是 `calls` 边）。

注意：因为 M4 的函数级证据来自 `.cpp` 记录，它证明的是
“该 **topic** 的使用函数在调用深度内到达了这些健康谓词/标志”，而不是
“该 **字段** 的值直接驱动了这些谓词”。因此路径记录中
`consumer_field_binding` 固定为 `topic_level`。边界拼接本身（字段→消息→
topic）是字段级的，`boundary_confidence` 为 `high`。

## 运行

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src python3 -m px4dep.cli run-all
```

复用已有 run 只重建图和报告：

```bash
PYTHONPATH=src python3 -m px4dep.cli build-graph --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli report --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli validate --run <run_id>
```

成功输出新增：

```text
      cross module: <P> paths, <C> consumers, <H> health-evidence paths
[7/8] report ...
      M4 gate: PASS
```

## 输出

| 路径 | 内容 |
|---|---|
| `normalized/cross_module_edges.jsonl` | `calls` 边（合并图中 `relation=calls` 的子视图） |
| `graph/end_to_end_paths.jsonl` | 每条 EKF2→Commander 路径，7 个步骤、边界边 ID、消费者证据 |
| `unresolved/cross_module.jsonl` | 未能显式拼接的切片路径（按 `kind` 分类） |
| `reports/cross_module_metrics.json` | 调用图、路径、覆盖与未解析计数 |
| `reports/cross_module.csv` | 每条路径一行，便于人工审查 |
| `reports/summary.md` | M4 路径表、证据计数、边界说明与 M4 gate |
| `reports/metrics.json` | 包含 `cross_module`、`call_graph`、`m4_gates`、`m4_gate_passed` |

`graph/nodes.jsonl` 与 `graph/edges.jsonl` 仍是权威合并图；M4 的 `calls`
边也在其中。

## 路径记录字段

| 字段 | 含义 |
|---|---|
| `parameter` / `source` | 参数名与 M3 参数字段（编译期常量的 `parameter` 为空） |
| `sink` / `sink_kind` | 汇点字段与 M3 汇点类别（`criteria_state` 等） |
| `message_type` / `topic` | 显式 `defines`/`instantiates` 到达的消息类型与 topic |
| `producer_function` / `producer_module` | 发布函数（USR 解析，EKF2） |
| `consumer_function` / `consumer_module` | 订阅函数（Commander） |
| `consumer_evidence` | BFS 得到的谓词、标志、模式限制、保护链，附调用链 |
| `consumer_field_binding` | 固定 `topic_level`，说明消费者证据的粒度 |
| `name_based_joins` | 恒为 0 |
| `confidence` | 继承 M3：数据路径 `medium`，控制路径 `low` |
| `boundary_confidence` | 字段/消息/topic 拼接的置信度（`high`） |
| `steps` | 7 步：M3 参数→字段 1 步 + 边界 6 步，每步带 `relation` 与 `evidence.edge_id` |

## M4 完成判据

`reports/metrics.json` 的 `m4_gates` 为：

- `cross_module_paths_present`：至少一条端到端路径；
- `all_paths_cross_ekf2_commander`：所有路径的发布者在 EKF2、消费者在
  Commander；
- `all_paths_have_topic_field_hop`：所有路径都经过真实的字段节点；
- `all_paths_have_topic_hop`：所有路径都经过真实的 uORB topic；
- `all_paths_have_edge_evidence`：所有边界步骤都带边 ID；
- `all_paths_have_complete_boundary`：所有路径的 6 个边界步骤齐全；
- `no_name_based_joins`：`name_based_joins == 0`；
- `health_layer_reached`：至少一条路径到达健康谓词；
- `consumer_paths_have_steps`：所有路径都有非空步骤；
- `no_missing_m4_outputs` / `no_dangling_edges_m4`：输出完整、无悬空边。

## 当前基线结果

最新通过 run（`20261021T111550Z_5356d24e5fa5`，PX4 `5356d24e`）的关键指标：

| 指标 | 数量 |
|---|---:|
| `call` 记录 / `calls` 边 | 2935 / 2474 |
| 未索引调用记录 / 同函数调用 | 461 / 0 |
| M3 切片路径 / 其中消息字段汇点 | 575 / 258 |
| 无发布 topic 的消息字段切片路径 | 257 |
| 拼接成功的端到端路径 | 5 |
| 涉及参数 / topic / 发布者 / 消费者 | 1 / 1 / 1 / 5 |
| 到达健康谓词的路径 | 1 |
| 到达标志写入 / 模式限制的路径 | 1 / 1 |
| 到达保护动作的路径 | 0 |
| 名称连接 / 未解析记录 | 0 / 218 |

基线中唯一完整贯通的链路是：

```text
EKF2_GPS_CHECK
  → estimator_status_s::gps_check_fail_flags   (data, 深度 1)
  → estimator_status_s
  → topic estimator_status
  → EKF2::PublishStatus
  → EstimatorChecks::checkAndReport（5 个消费者之一）
  → EstimatorChecks::checkEstimatorStatusFlags / setModeRequirementFlags
```

`EstimatorChecks::checkAndReport` 的消费者证据包含 33 条健康谓词、10 个
健康标志写入（例如 `failsafe_flags_s::global_position_invalid`）、1 条
模式限制和 31 条保护链；这些保护链在本次基线中均未到达
`selects_action`，因此 `paths_with_protection_chain` 为 0，报告不把它作为
通过条件。

## 测试

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src:tests python3 -m pytest tests/test_cross_module.py -q   # M4 单元测试
PYTHONPATH=src:tests python3 -m pytest tests -q                        # 全部单元 + 集成
```

单元测试使用合成图，覆盖：字段/消息/topic/发布者/消费者完整拼接、缺少
`defines` 边时不按字段名连接、缺少 `publishes` 边时报告未解析、缺少
Commander 订阅时报告未解析、路径稳定 ID 与 CSV 行数、调用边要求两端都可
索引。

集成测试读取真实 PX4 与最近一次 run，检查 M4 gate、黄金路径
（`EKF2_GPS_CHECK` → `gps_check_fail_flags` → `estimator_status` →
`EKF2::PublishStatus` → `EstimatorChecks::checkAndReport`）的 7 个步骤与边
证据、`consumer_field_binding=topic_level`，以及 aid source 字段没有被按
消息类型批量展开（保持未解析而不是伪造路径）。

## M4 已知边界

1. **消费者证据是 topic 级**：Commander 侧没有 `member_access` 记录，无法
   证明订阅函数读取了具体字段。路径只能声明“该 topic 的消费者在调用深度
   内到达健康层”，`consumer_field_binding=topic_level` 明确记录这一点。
2. **helper 写入 + 另一函数发布不拼接**：257 条消息字段切片路径的
   `sink_writers` 是 `updateAidSourceStatus` 一类的 helper，它们不调用
   `publish`；真正的发布者是另一函数。同一消息类型可能对应多个 topic，
   按类型选 topic 就是猜测。这些路径保留在
   `unresolved/cross_module.jsonl`（基线 257 条路径，按参数+源字段去重后
   为 218 条 `m4_writer_publish_topic_unresolved` 记录），不进入端到端
   结果；M5 用这些记录的 `parameter` 把对应候选降级为 `needs_review`。
3. **调用图受索引范围限制**：只有两端都在 90 个 TU 内的调用才建边；461 条
   调用记录因一端未索引而丢弃。没有虚函数分派、函数指针或模板实例化解析。
4. **保护链不等于保护动作**：`protection_chains` 只表示“标志读取者”和
   “选择动作的谓词”在图中相连；没有 `selects_action` 时不会补全为
   `failsafe` 动作。基线中该字段为 0 是实际结果，不是缺失。
5. **不判断运行期因果性**：M4 是静态可达性。参数取值、传感器状态、实际
   触发条件与 ULog 到达性属于 M5，且只使用正常场景日志。
6. **不生成候选**：M4 只产出端到端路径；运行期可达性和候选分级由 M5
   完成，见 `docs/m5-operations.md`。M4 路径是后续审查的输入，不是漏洞
   或危险参数的结论。
7. **只读约束**：未修改 PX4 仓库、未修改参数、未注入传感器、未干预通信；
   M4 新增的 `calls` 边和路径只写入实验项目目录。
