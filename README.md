# px4dep：PX4 配置依赖分析

`px4dep` 是一个只读的静态分析工具，用来追踪 PX4 参数如何进入 EKF2 状态估计、
Commander 健康检查与故障保护逻辑，并把每条关系整理为可回溯的源码证据。

分析链路：

```text
配置参数
  -> EKF2 估计字段 / Commander 参数包装读取
  -> 健康与 arming 判据、模式限制、failsafe flag
  -> 保护动作类别
```

工具以 PX4 自带的结构化契约（参数 JSON/YAML、`.msg`、uORB、健康检查接口）
为骨架，在 EKF2 与 Commander 的有限范围内做字段敏感切片；只有 AST 无法解释的
消息实例绑定才使用定向 LLVM IR 兜底。所有结果都保留源码位置、构建条件、路径
步骤和置信度。

## 分析基线

| 项目 | 值 |
|---|---|
| PX4 版本 | v1.18.0-beta1 |
| 提交 | `5356d24e5fa5` |
| 构建变体 | `px4_sitl_default` |
| 分析模块 | `src/modules/ekf2`、`src/modules/commander` |

参数、主题和路径均绑定到当前构建变体；构建条件不满足的关系不会进入结果。

## 功能阶段

| 阶段 | 内容 |
|---|---|
| M1 | 参数目录、`.msg`/uORB 契约、参数包装与构建绑定、数据流基础图 |
| M2 | Commander 健康报告、mode capability、failsafe flag 与保护决策 |
| M3 | EKF2 参数到估计字段、状态 flag 和判据字段的字段敏感反向切片 |
| M4 | 通过显式 uORB 字段把 EKF2 路径接到 Commander 健康层 |
| M5 | 正常场景 ULog 可达性证据、候选分级与人工审查导出 |
| M6 | 断链统计与范围很窄的 LLVM 14 pass（消息实例到 topic 绑定） |
| M7 | Commander/Failsafe `px4::params::NAME` 包装读取切片（data/control/context） |

## 目录结构

```text
configs/      实验配置与汇点目录
cpp/          Clang LibTooling 索引器与定向 LLVM pass
schemas/      节点、边、证据和参数的 JSON Schema
scripts/      libclang 本地准备脚本
src/px4dep/   Python 分析器与命令行
tests/        单元测试、夹具和仓库集成测试
docs/         M1—M7 操作与实现说明
```

## 环境要求

- Linux（基线验证环境为 Ubuntu + Clang/LLVM 14）
- Python 3.10+
- `PyYAML`、`jsonschema`；读取 ULog 时需要 `pyulog`
- 本地 PX4 源码树及已完成的 SITL 构建，至少包含：
  - `build/px4_sitl_default/compile_commands.json`
  - `build/px4_sitl_default/parameters.json`
  - `build/px4_sitl_default/parameters.xml`
- 可选：`clang++-14`、`opt-14`、CMake，用于重建索引器和 LLVM pass

PX4 源码树只读；所有输出写入本项目 `runs/<run_id>/`。

## 快速开始

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[ulog]"

# 修改 configs/px4_sitl_default.yaml 中的 PX4 路径与 ULog 路径
px4dep run-all --config configs/px4_sitl_default.yaml --rebuild
```

`--rebuild` 会调用 `scripts/setup_libclang.sh`、Clang 索引器构建脚本和 LLVM
pass 构建脚本；本地工具链已就绪时可以省略。

也可以按阶段运行：

```bash
px4dep manifest --config configs/px4_sitl_default.yaml
px4dep index-cpp --run latest --rebuild
px4dep index-uorb --run latest
px4dep params --run latest
px4dep build-graph --run latest
px4dep ingest-ulog --run latest
px4dep candidates --run latest
px4dep llvm-fallback --run latest
px4dep report --run latest
px4dep validate --run latest
```

## 输出

每次运行在 `runs/<run_id>/` 下生成：

| 路径 | 内容 |
|---|---|
| `manifest.json` | 输入路径、提交、哈希和构建条件 |
| `raw/` | Clang 索引器与 LLVM pass 的原始记录 |
| `normalized/` | 参数、包装、调用、uORB 与切片边 |
| `graph/` | 节点、边、EKF2 路径、Commander 路径、端到端路径 |
| `evidence/` | 候选、断链、运行期证据和源码证据 |
| `unresolved/` | 分阶段未解析记录（不丢弃） |
| `reports/` | `summary.md`、指标 JSON、候选与路径 CSV |

`reports/summary.md` 是总览；`reports/candidates.csv` 和
`evidence/candidates.jsonl` 是候选审查入口，其中 JSONL 保留完整解释路径。

## 结果解读

- **data**：参数包装 token 精确出现在汇点表达式中；
- **control**：参数出现在汇点的外层条件中；
- **context**：同函数或两跳调用邻近，仅作线索，标记
  `context_only=true`，不构成依赖，也不写入依赖边；
- 运行期证据只说明主题/字段在给定正常日志中出现过；
- **未观测到不等于不可达**；
- 候选项是人工核查排序，不是漏洞、适航或飞行安全结论。

基线 run 的候选为 127 个（EKF2 52、Commander/Failsafe 75），其中 44 个仅有
context 路径，详见 `docs/m7-operations.md`。

## 测试

```bash
PYTHONPATH=src python3 -m pytest -q
```

仓库集成测试读取本地 PX4 树和最近的 run；缺少基线时自动跳过。

## 安全与合规边界

- PX4 仓库、构建产物和参数配置只读；
- 不做参数修改实验、传感器注入或通信干预；
- 运行期证据只使用授权、未修改参数的正常场景日志，且只用于可达性；
- 不生成参数篡改组合，不分析规避保护逻辑的方法；
- 输出用于代码审查与实验研究，不构成飞行安全或适航结论。

## 已知限制

- 只覆盖当前基线版本和 `px4_sitl_default` 构建变体；
- 表达式匹配依赖 Clang 记录的源码文本，不追踪运行期动态值；
- Commander context 路径只是函数邻近证据；
- LLVM pass 仅处理选定的消息实例绑定，不是通用污点分析；
- 静态路径和日志存在性都不证明因果关系。

## 文档

- `docs/m1-operations.md` — 骨架与解析
- `docs/m2-operations.md` — Commander 健康链
- `docs/m3-operations.md` — EKF2 切片
- `docs/m4-operations.md` — 跨模块路径
- `docs/m5-operations.md` — ULog 与候选
- `docs/m6-operations.md` — 断链与 LLVM 兜底
- `docs/m7-operations.md` — Commander/Failsafe 切片扩展

## 许可证

本仓库当前未附带开源许可证。对外发布或复用前请先补充合适的 `LICENSE`。
