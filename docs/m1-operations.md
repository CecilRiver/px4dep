# M1 运行与验收说明

本文件记录 M1（高置信骨架）的可复现运行方式、输出内容、测试方式和已知边界。
实验设计背景见项目根目录 `README.md`。

## 环境前提

- PX4 源码：`/home/cecilriver/PX4-Autopilot`（只读；本工具不写入、不修改参数）
- 构建基线：`build/px4_sitl_default`
- Python：3.10+，依赖 `PyYAML`、`jsonschema`（测试另需 `pytest`）
- 本机 Clang 14 及其开发头文件。项目为无 root 环境准备了本地依赖：
  `.deps/libclang14`，Clang 工具二进制位于 `.deps/bin/px4dep_clang_tool`。

若 `.deps/bin/px4dep_clang_tool` 不存在，可执行：

```bash
cd /home/cecilriver/px4_config_dependency_experiment
cpp/px4dep_clang_tool/build.sh
```

重新构建后可用 `run-all --rebuild` 强制重建工具。

## 运行完整 M1

```bash
cd /home/cecilriver/px4_config_dependency_experiment
PYTHONPATH=src python3 -m px4dep.cli run-all
```

成功时会打印：

```text
run_id: <timestamp>_<commit>
[1/6] clang index ...
[2/6] uORB schema ...
[3/6] parameter catalog ...
[4/6] evidence graph ...
[5/6] report ... M1 gate: PASS
[6/6] validation ... PASS
```

也可分阶段运行，默认使用最近的 run：

```bash
PYTHONPATH=src python3 -m px4dep.cli manifest            # 新建 run 并记录输入哈希
PYTHONPATH=src python3 -m px4dep.cli index-cpp           # Clang AST 索引
PYTHONPATH=src python3 -m px4dep.cli index-uorb          # .msg 解析
PYTHONPATH=src python3 -m px4dep.cli params              # 参数目录
PYTHONPATH=src python3 -m px4dep.cli build-graph         # 合并证据图
PYTHONPATH=src python3 -m px4dep.cli report              # 摘要与 M1 gate
PYTHONPATH=src python3 -m px4dep.cli validate            # 模式/ID/悬空边/输入哈希
```

指定历史 run：在分阶段命令后加 `--run <run_id>`。

## 输出

所有输出写入 `runs/<run_id>/`，不写入 PX4 仓库：

| 路径 | 内容 |
|---|---|
| `manifest.yaml` / `manifest.json` | 源码提交、dirty 状态、输入文件哈希、工具与编译器版本 |
| `normalized/` | 参数、C++ 实体、wrapper、uORB endpoint、topic 等规范化记录 |
| `graph/nodes.jsonl`、`graph/edges.jsonl` | 权威证据图（JSONL） |
| `graph/dependency.graphml` | 便于图形工具查看的导出副本 |
| `evidence/edge_evidence.jsonl` | 每条边的源码位置、规则、置信度、构建条件 |
| `evidence/source.jsonl` | 节点与边的源码证据汇总 |
| `reports/summary.md` | 人读摘要与 M1 gate |
| `reports/metrics.json` | 机器可读指标与 gate 明细 |
| `reports/traceability.csv` | 参数 → wrapper → 配置字段的可审查证据 |
| `reports/candidates.csv` | 人工审查候选表；由 M5 从 M3/M4 路径和运行期证据生成 |
| `unresolved/` | 明确分类的未解析记录 |

### 稳定 ID

所有实体与边 ID 由 SHA-256（截断 20 位）生成，输入为：commit、build variant、
实体/关系类型、限定名或 USR，以及必要的声明位置。相同输入重复运行 ID 不变；
测试对此有覆盖。

## 测试

```bash
cd /home/cecilriver/px4_config_dependency_experiment
python3 -m pytest tests -q                    # 单元 + 集成
python3 -m pytest tests -m 'not integration'  # 仅单元/夹具测试
```

- `tests/fixtures/`：最小 `.msg`、参数 JSON/YAML、构建宏与 C++ guard 夹具。
- `tests/golden/`：夹具对应的节点/边金标准摘要。
- `tests/integration/`：读取真实 PX4 基线与最近一次 run，验证：
  - `.msg` 主题/字段/嵌套类型解析；
  - `EKF2_SENS_EN` → wrapper → `estimator::parameters::ekf2_sens_en` 链路；
  - `EKF2::_attitude_pub` 发布 `vehicle_attitude`；
  - 最新 run 的 validation 与 M1 gate；
  - 重复解析 ID 稳定。
  若本地没有 PX4 基线或 run，相应测试会 skip。

## M1 已知边界

1. **翻译单元范围固定**：当前只索引 `configs/px4_sitl_default.yaml` 中列出的
   TU。M1 基线为 7 个；M2 扩展到 Commander 全部健康检查与 failsafe 框架，
   M3 进一步加入 EKF2 核心估计器与全部 aid source，共 90 个；M4 不新增 TU，
   而是在同一范围上建立 `calls` 边和显式 uORB 跨模块路径
   （见 `docs/m2-operations.md`、`docs/m3-operations.md`、
   `docs/m4-operations.md`）。声明在其他 TU 中的 endpoint（例如
   `AuxGlobalPosition::_agp_sub` 的初始化位于未索引的
   `aux_global_position.cpp`）会保留为 unresolved，而不是猜测。
2. **参数句柄数据流未实现**：`param_find*` 的参数名可直接连接；`param_get` /
   `param_set` 接收 `param_t` 句柄，M1 不追踪句柄来源，因此只报告
   `parameter_handle_not_resolved`。
3. **函数参数/循环变量形式的 uORB endpoint**：例如把 `Subscription &sub`
   作为函数参数传入、或遍历 `SubscriptionMultiArray` 的循环变量，可能只有调用点
   或没有 topic 绑定，保留为 `uorb_endpoint_without_topic`。
4. **构建条件是静态宏环境**：依据 `px4_boardconfig.h`、`boardconfig` 和
   compile commands 的 `-D/-U` 评估；无法求值的条件原样保留并标记
   `unknown`。AST 仍决定实体是否存在于当前构建。
5. **多实例不是字段级路径**：M1 只建立 endpoint → topic 的实例/别名边；
   topic 字段级跨模块路径在 M4 建立。
6. **不做因果判断**：`traceability.csv` 只陈述静态绑定证据；M5 的
   `candidates.csv` 是人工审查排序，不是危险参数结论。参数名称相似性不
   产生任何边。
7. **只读与正常日志约束**：不修改 PX4 参数、不注入传感器数据；M1 不使用
   ULog。运行时可达性从 M5 开始，且只使用正常场景日志。
8. **源码基线包含既有 dirty 改动**：manifest 记录 dirty 状态与相关输入哈希；
   复现时应使用同一 commit 与构建目录。
