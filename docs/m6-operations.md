# M6 断链统计与定向 LLVM 兜底

本文件记录 M6（M1—M5 断链统计、高价值 unresolved 类型选择、定向 LLVM 14
pass、证据连接与残余断链）的实现范围、选择规则、证据分层、运行方式、输出、
验收判据和已知边界。实验设计背景见项目根目录 `README.md`；此前的基线见
`docs/m1-operations.md` 至 `docs/m5-operations.md`。

M6 的顺序是固定的：**先统计断链分布，再对得分最高的可 IR 解析类型实现
pass。** 没有先建通用 LLVM 框架。pass 只处理一个发布模板的调用点，IR 证据
只把消息实例绑定到 topic，不单独生成端到端路径，也不把“没有消费者”升级为
不可达结论。

## 统计范围

M6 读取每个权威的 per-stage unresolved 文件，而不是合并视图：

| stage | 文件 |
|---|---|
| M1 | `unresolved/parameters.jsonl`、`cpp_records.jsonl`、`uorb_schema.jsonl`、`aliases.jsonl`、`truncations.jsonl`、`calls.jsonl`、`topics.jsonl` |
| M2 | `unresolved/health_chain.jsonl` |
| M3 | `unresolved/ekf_slice.jsonl` |
| M4 | `unresolved/cross_module.jsonl` |
| M5 | `unresolved/ulog.jsonl` |

`unresolved/graph.jsonl` 是 M1/M2/M3 的合并重复视图，**不参与计数**，只用于
覆盖校验：非 M4 记录数必须等于合并视图记录数。基线 run 中：

| 项目 | 数量 |
|---|---:|
| M1 | 16 |
| M2 | 88 |
| M3 | 6 |
| M4 | 218 |
| M5 | 0 |
| 合计 | **328** |
| 非 M4 记录 / `graph.jsonl` | 110 / 110（一致） |

每条断链记录保留原始记录（`record` 字段）、源文件、源索引、stage、
`unresolved_kind`、根因、参数/sink/path 关联、置信度和
`interpretation=break_chain_inventory_not_unreachability_claim`。

## 根因映射

| unresolved kind | root cause | 可用定向 IR？ |
|---|---|---|
| `mode_set_dynamic` | `runtime_mode_values` | 否：模式集合来自运行期状态 |
| `protection_action_dynamic` | `runtime_protection_action` | 否：保护动作来自运行期选择 |
| `param_find_unresolved` | `runtime_parameter_name` | 否：参数名是运行期字符串 |
| `parameter_handle_not_resolved` | `runtime_parameter_handle` | 否：handle 需要运行期数据流 |
| `slice_depth_truncated` | `slice_depth_limit` | 否：M3 的切片深度上限 |
| `uorb_endpoint_without_topic` | `uorb_endpoint_topic_expression` | 否：低量且不在选定 pass 范围内 |
| `m4_writer_publish_topic_unresolved` | `writer_instance_to_topic_binding_missing` | **是**：M3 有写入者但没有消息实例到 topic 的绑定 |

未识别的 kind 归入 `unclassified_unresolved_kind`，仍然保留，不丢弃。

## 高价值类型选择

选择规则只考虑可用定向 IR 的类型：

```text
score = count + 5 × distinct_parameters + 3 × path_linked_records
```

基线结果：

| kind | count | 参数数 | 关联路径 | score |
|---|---:|---:|---:|---:|
| `m4_writer_publish_topic_unresolved` | 218 | 38 | 218 | **1062** |
| `mode_set_dynamic` | 72 | 0 | 0 | 72 |
| `protection_action_dynamic` | 16 | 0 | 0 | 16 |
| `param_find_unresolved` | 10 | 0 | 0 | 10 |
| `slice_depth_truncated` | 6 | 0 | 0 | 6 |
| `uorb_endpoint_without_topic` | 5 | 0 | 0 | 5 |
| `parameter_handle_not_resolved` | 1 | 0 | 0 | 1 |

只有 M4 类型被标为 IR 适用，因此它也是唯一候选。选择结果写入
`reports/break_chains.json` 的 `selection`。

## 定向 LLVM pass

### 范围

pass 名称 `px4dep-member-topic`，只处理：

- `EKF2::PublishAidSourceStatus<...>` 调用点；
- 该调用点上的 EKF2 发布成员（`EKF2` 结构体字段）；
- 该调用点使用的 `Ekf::aid_src_*` 访问器；
- `-O0 -g` IR 中仍然显式的结构体字段索引和 `__orb_*` 元数据操作数。

它不遍历参数、不追踪 M3 写入者、不修改模块、不生成路径。IR 采用
`-O0`，因为优化后成员字段索引和 ORB 元数据操作数可能被折叠。

### 构建与运行

依赖 LLVM 14：`clang++-14`、`opt-14`、`llvm-config-14`、`llvm-14-dev`。

```bash
bash cpp/px4dep_llvm_pass/build.sh
# -> .deps/bin/px4dep_member_topic.so
```

Python 侧从 `compile_commands.json` 取 `EKF2.cpp` 的编译条目，过滤掉
`-O*`、`-W*`、`-fdiagnostics-*`、`-c`、`-o` 等代码生成/诊断参数，保留
`-D*`、`-I*`、`-include`、`-isystem`、`-std=*`、`-g*` 和必要的语言参数，
然后执行：

```bash
clang++-14 <filtered flags> -O0 -g -S -emit-llvm \
  -o runs/<run>/llvm/EKF2.ll <abs EKF2.cpp>

PXDEP_LLVM_OUTPUT=runs/<run>/llvm/llvm_raw.jsonl \
opt-14 -load-pass-plugin .deps/bin/px4dep_member_topic.so \
  -passes=px4dep-member-topic -disable-output runs/<run>/llvm/EKF2.ll
```

实际命令、过滤后的参数、IR sha256、插件 sha256、Clang/opt 版本写入
`reports/llvm_metrics.json`；stdout/stderr 写入 `logs/llvm_ir.log` 和
`logs/llvm_pass.log`。

### 绑定记录

每条 `llvm_member_topic_binding` 包含：

- `accessor_function` / `accessor_mangled` / `message_type`；
- `ekf_member_field_index`（`Ekf` 结构体字段）；
- `ekf2_publication_field_index`（`EKF2` 结构体字段）；
- `topic` / `topic_global`（`__orb_*`）；
- `publication_constructor_*`（`uORB::PublicationMulti` 构造点）；
- `call_site_file/line/column` 和 `call_instruction`；
- `confidence=high`、`interpretation=message_instance_to_topic_binding_only`、
  `end_to_end_claim=false`。

基线：20 个发布调用点、20 条绑定、0 条 pass 内未解析、20 条 AST 交叉校验
全部通过。

### AST 交叉校验

对每条 LLVM 绑定：

1. 在 `raw/cpp_records.jsonl` 中按 `call_site_line` 找
   `EKF2::PublishAidSourceStatus` 调用；
2. 参数 1 必须是 `_ekf.aid_src_<name>()`，并与 LLVM accessor 名称匹配；
3. 参数 3 必须是 `_estimator_aid_src_<name>_pub`，且存在
   `uorb_decl(qualified_name="EKF2::<pub>", topics=[topic])`；
4. accessor 必须在 AST `member_access` 中返回 `Ekf::_aid_src_<name>`。

LLVM 证据与 AST 证据在记录中分开标注（`llvm_evidence_kind` /
`ast_evidence_kind` / `evidence_are_separate=true`），不用 LLVM 单独下结论。

### 作用域缺口

AST 中有 21 个 aid-source 发布 topic，pass 覆盖 20 个。
`estimator_aid_src_aux_global_position` 由 `AgpSource::_aid_src_pub` 发布
（`aux_global_position_control.hpp:95`），不在
`EKF2::PublishAidSourceStatus` 范围内。该缺口写入
`unresolved/llvm.jsonl`，原因是 `published by a member outside
EKF2::PublishAidSourceStatus`，不丢弃、不猜测。

## 连接规则

### AST 写入者到 EKF 成员

对全部 AST 记录做不动点传播：

1. 种子：`Ekf::_aid_src_*` 成员访问；`receiver_kind=this` 且
   `receiver_expr=_aid_src_*` 的直接访问；
2. `local_write`：把右侧成员/局部别名并入左侧变量；
3. `call_arg`：参数类型含 `estimator_aid_source` 且为引用/指针时，把实参
   成员或局部别名并入被调函数的形参；
4. 函数成员是它可以触达的实例集合，**不是单次执行**。

基线：4 次迭代，36 个 M4 写入者中 32 个可映射，4 个不可映射：

| writer | 原因 |
|---|---|
| `Ekf::fuseBodyFrameVelocity` | 已索引范围内没有调用点 |
| `Ekf::fuseYaw`（被选中的重载） | 接收本地消息，不是 EKF 成员 |
| `Ekf::resetYawByFusion` | 写本地 aid-source 消息，不写 EKF 成员 |
| `Ekf::runFakePosStateMachine` | 写入者 USR 与已索引调用点的重载变体不同 |

### M3 路径的写入者归属

1. 若路径步骤中 `from == sink` 且有 `caller`，优先使用该 caller 对应的
   `sink_writers`；
2. 否则退回该路径的全部 `sink_writers`，并记录
   `attribution=sink_writers_fallback`；
3. 只有该写入者映射到 EKF 成员、且该成员有 LLVM topic 绑定时，才产生
   候选 topic。消息类型必须一致。

基线：137 条路径使用直接写入者，120 条使用回退；两者都保留在每条 resolution
记录中。

### 收窄与残余

每条 M4 断链取所有关联 M3 路径的候选 topic 并集：

| 情况 | status | 后续根因 |
|---|---|---|
| 恰好 1 个 topic | `unique_topic_but_no_commander_consumer` | `no_commander_consumer_in_indexed_scope` |
| 多个 topic | `multiple_topics_and_no_commander_consumer` | M3 未选出单一消息实例；已索引范围无消费者 |
| 无 topic | `no_topic_binding` | 写入者目标是本地消息或未绑定成员 |

基线：

| 项目 | 数量 |
|---|---:|
| 唯一 topic | 30 |
| 多个 topic | 187 |
| 无 topic | 1 |
| 有候选 topic | 217 |
| 找到 Commander 消费者 | 0 |
| 端到端路径（M6 新建） | 0 |

唯一的无 topic 记录是 `EKF2_HDG_GATE` /
`estimator_aid_source1d_s::innovation`：其写入者 `Ekf::fuseYaw` 使用的是
本地消息实例，不是已发布的 EKF 成员。187 条多 topic 记录来自 M3 路径列出了
汇点的多个写入者，M6 不强行选择其中一个。

所有 21 个 `estimator_aid_src_*` topic 在已索引图中都没有 `subscribes` 边，
因此 LLVM 收窄不能创建端到端路径。M6 把这一点记录为
`no_commander_consumer_in_indexed_scope`，并明确：这是**已索引静态范围**的
观察，不是运行期不可达结论，也没有排除索引范围之外的消费者。

## 运行

完整流水线：

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src python3 -m px4dep.cli run-all
```

只对已有 run 重跑 M6、报告与校验：

```bash
PYTHONPATH=src python3 -m px4dep.cli llvm-fallback --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli report --run <run_id>
PYTHONPATH=src python3 -m px4dep.cli validate --run <run_id>
```

重建 pass 插件：

```bash
PYTHONPATH=src python3 -m px4dep.cli llvm-fallback --run <run_id> --rebuild
```

`run-all` 现在为 9 步，M6 位于候选之后、报告之前：

```text
[6/9] review candidates ...
[7/9] break chains + targeted LLVM pass ...
      <B> LLVM bindings; <U> unique / <M> multiple / <N> no-topic records
[8/9] report ...
      M6 gate: PASS
[9/9] validation ...
```

## 输出

| 路径 | 内容 |
|---|---|
| `evidence/break_chains.jsonl` | M1—M5 全部 328 条断链，含原始记录、根因、path 关联 |
| `reports/break_chains.json` | 分布、覆盖校验、选择规则与结果、pass 范围 |
| `reports/break_chains.csv` | 按 unresolved kind 的汇总 |
| `evidence/llvm.jsonl` | 20 条 LLVM 绑定 + AST 交叉校验 + 下游订阅状态 |
| `evidence/break_chain_resolution.jsonl` | 218 条 M4 断链的路径归属、候选 topic、下游与残余根因 |
| `unresolved/llvm.jsonl` | 218 条逐记录残余 + 4 条写入者缺口 + 1 条作用域缺口（共 223 条），以及 pass 自身的 unresolved（若有） |
| `reports/llvm_metrics.json` | 工具链、插件/IR 哈希、pass 统计、绑定/覆盖、连接指标、限制 |
| `reports/llvm_bindings.csv` | 人工复核的 topic/accessor/字段索引/交叉校验表 |
| `reports/break_chain_resolution.csv` | 每条 M4 断链的候选 topic 与残余原因 |
| `llvm/EKF2.ll` | `-O0 -g` LLVM IR |
| `llvm/llvm_raw.jsonl` | pass 原始 JSONL |
| `logs/llvm_ir.log`、`logs/llvm_pass.log` | 完整命令、cwd、输出、返回码 |

## M6 完成判据

`reports/metrics.json` 的 `m6_gates` 为：

- `break_chain_distribution_present`：断链总数非零且等于各 kind 计数之和；
- `distribution_covers_m1_to_m5`：分布覆盖 M1—M5 五个 stage；
- `unresolved_inventory_preserved`：非 M4 记录数与 `unresolved/graph.jsonl`
  一致，未因统计而丢项；
- `high_value_type_selected_from_distribution`：选择结果存在、类型为
  `m4_writer_publish_topic_unresolved`，且分数等于可适用类型的最大值；
- `llvm_toolchain_present`：clang 与 opt 可执行且版本可读；
- `llvm_pass_ran`：pass 有发布调用点，且绑定数与 `evidence/llvm.jsonl` 一致；
- `llvm_bindings_have_ir_evidence`：每条绑定都有非零 EKF/EKF2 字段索引和
  `__orb_*` topic 全局；
- `llvm_bindings_cross_checked_by_ast`：每条绑定都通过 AST 交叉校验；
- `llvm_scope_gap_recorded`：作用域外 topic 与原因已记录；
- `selected_type_narrowed`：218 条结果全部归入唯一/多个/无 topic，且至少有
  一条唯一 topic；
- `residual_root_cause_recorded`：每条 M4 结果都有后续根因，残余记录有原因；
- `no_llvm_only_end_to_end_paths`：M6 创建的端到端路径为 0，所有 LLVM 绑定
  和 resolution 都不带端到端声明；
- `no_missing_m6_outputs`：上述输出文件齐全。

## 已知边界

1. **M6 不产生端到端路径。** 它只把消息实例绑定到 topic；参数 → 汇点仍是
   M3 的证据，topic → 消费者仍需要显式的 `publishes`/`subscribes` 图边。
2. **多 topic 不是错误。** M3 对共享汇点列出所有写入者；当路径没有直接写入者
   步骤时，M6 保留全部候选，而不是按名称或类型偏好择优。
3. **无消费者是范围观察。** 已索引的 Commander TU 中没有
   `estimator_aid_src_*` 订阅者，这意味着当前索引无法关闭这条链，不意味着
   运行期没有消费者或该 topic 不可达。
4. **作用域缺口是显式的。** 21 个 aid-source 发布 topic 中有一个在
   `EKF2::PublishAidSourceStatus` 之外，已记录为 `unresolved/llvm.jsonl`
   条目，没有为了 20/20 的覆盖率而删除。
5. **IR 需要 `-O0`。** 字段索引和 ORB 元数据是 pass 的证据来源；优化后的 IR
   不再保证这些操作数可见，因此 `reports/llvm_metrics.json` 固定记录
   `optimization=O0`。
6. **PX4 仓库只读。** M6 只编译到 `runs/<run>/llvm/` 和 `.deps/`，不修改
   PX4 源码、参数或日志。
