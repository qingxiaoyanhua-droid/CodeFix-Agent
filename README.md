# CodeFix-Agent

**AI-powered Code Fixing Agent with Chain-of-Thought, GRPO Reinforcement Learning, and Enterprise RAG**

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## 核心特性

### 1. ReAct + CoT 架构
- 小模型生成代码修复方案
- 大模型验证和错误分析
- 双模型协作确保修复质量

### 2. Compile-First 验证 + Ruff 质量兜底
- 编译器 + 测试用例作 ground truth，零幻觉
- 未通过则大模型介入，最多 3 轮迭代
- Ruff 静态分析兜底：编译通过后自动检查代码风格与可读性，有问题触发轻量优化 pass，无需大模型参与

### 3. AST 精准定位
- 基于 Python AST 模块的函数级定位
- **字节偏移替换**，彻底解决行号偏移问题

### 4. 六维 Reward 体系
| 维度 | 说明 |
|------|------|
| 格式奖励 | 输出格式正确性 |
| 语法奖励 | 代码可编译性 |
| AST奖励 | 语法树结构正确性 |
| 执行奖励 | 测试用例通过率 |
| 过程奖励 | 修复步骤合理性 |
| 语义奖励 | 代码语义一致性 |

### 5. 三层记忆架构
- **L1**：工作记忆（当前任务上下文）
- **L2**：反思记忆（失败经验，时间衰减权重）
- **L3**：技能记忆（成功修复 SOP）
- 参考 DECENTMEM 双池设计：E-pool 成功轨迹 / X-pool 候选策略

### 6. RAG + GRPO 训练
- BM25 + FAISS 混合检索，RRF 融合 + Cross Encoder 重排
- GRPOTrainer 多粒度奖励，防止 Reward Hacking
- 支持 PPO / DPO / GRPO 多种训练算法

### 7. 安全沙箱与可观测性
- 6 层沙箱（语言白名单 + 危险模式 + subprocess 隔离 + 临时目录 + 资源限制 + 退出码验证）
- Session 持久化：append-only JSONL，会话可中断恢复
- 审计日志轮转：按大小和行数自动轮转

---

## 项目结构

```
CodeFix-Agent/
├── src/                          # 源代码
│   ├── core/                     # 核心 Agent 模块
│   │   ├── cot_react_agent.py   # 主 Agent (ReAct + CoT + Compile-First)
│   │   ├── llm_judge.py          # LLM-as-a-Judge 评估
│   │   ├── online_router.py      # 模型路由
│   │   ├── session_manager.py    # Session 持久化
│   │   ├── state_manager.py      # 状态管理
│   │   ├── task_planner.py      # 任务规划
│   │   └── tool_selector.py      # 工具选择
│   ├── api/                      # API 服务
│   │   ├── api_server.py        # FastAPI 服务
│   │   ├── api_server_README.md # API 文档
│   │   └── deploy_server.sh      # 部署脚本
│   ├── memory/                   # 记忆系统
│   │   ├── dual_pool_memory.py   # E-pool / X-pool 双池记忆
│   │   └── decentmem_integration.py
│   ├── rag/                      # RAG 检索
│   │   ├── enterprise_rag_pipeline.py
│   │   └── build_rag_knowledgebase.py
│   ├── sandbox/                  # 代码执行沙箱
│   │   └── shell_sandbox.py
│   ├── tools/                    # 工具集
│   │   ├── ast_code_processor.py # AST 代码处理
│   │   ├── inject_bugs.py       # Bug 注入
│   │   └── fix_*.py             # 各类修复脚本
│   ├── eval/                     # 评测脚本
│   │   ├── eval_*.py            # 评测入口
│   │   └── test_*.py            # 测试脚本
│   └── training/                  # 训练辅助
│       ├── llm_critic_reward.py
│       └── process_reward_model.py
├── experiments/                   # 实验脚本
│   ├── pretrain/                 # 预训练数据处理
│   ├── finetune/                 # DPO / PPO 微调
│   └── grpo/                     # GRPO 强化学习训练
├── data/                         # 数据
│   ├── datasets/                  # 训练数据集
│   │   ├── raw_codefix/         # 原始修复数据
│   │   ├── cleaned/              # 清洗后数据
│   │   └── final/                # 最终训练数据
│   ├── benchmarks/               # 评测基准
│   │   └── quixbugs/            # QuixBugs 数据集
│   └── training/                  # 训练中间数据
├── logs/                         # 运行日志
│   ├── runs/                    # Agent 运行记录
│   ├── pipeline/                # 流水线评测结果
│   └── sessions/                # 会话 JSONL 记录
├── docs/                         # 文档
│   ├── interview/                # 面试相关记录
│   ├── notes/                   # 项目笔记
│   └── reference/               # 参考资料与论文
├── frontend/
│   └── index.html              # Web UI
├── tests/
│   └── test_infrastructure.py   # 基础设施测试
├── .env.example                 # 配置示例
├── requirements.txt              # Python 依赖
└── pyproject.toml               # 项目配置
```

---

## 安装

```bash
# 创建虚拟环境
python -m venv .venv
# Linux/Mac
source .venv/bin/activate
# Windows
.venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 安装 Ruff（代码质量检查）
pip install ruff

# 配置
cp .env.example .env
# 编辑 .env 设置模型路径和 API 配置

# 运行测试
pytest tests/ -v
```

---

## 快速开始

### Python API

```python
from src.core.cot_react_agent import CoTReActAgent

agent = CoTReActAgent(
    small_model="qwen2.5-coder:1.5b",
    large_model="gpt-4o"
)

result = agent.fix_code(
    buggy_code='def add(a, b): return a - b',
    test_cases=[{"input": (1, 2), "output": 3}]
)
print(result.fixed_code)
```

### API 服务

```bash
# 启动服务
python src/api/api_server.py --mode local --no-auth

# 打开前端
# 双击 frontend/index.html 或在浏览器访问
```

### 评测

```bash
# 本地评测
python src/eval/eval_local.py

# 基准测试
python src/eval/evaluate_on_benchmark.py
```

---

## 性能指标

| 指标 | 数值 |
|------|------|
| pass@1 | 54% → 87%（+33pp） |
| 首轮通过率 | 60% |
| 延迟降低 | 44% |
| 大模型调用减少 | 60% |
| L2 命中重试成功率提升 | +18% |
| Ruff 质量检查延迟 | < 50ms |
| RAG 检索延迟 | < 200ms |

---

## 训练

```bash
# 准备数据
python experiments/pretrain/prepare_training_data.py

# SFT 微调
python experiments/pretrain/sft_train.py --data data/datasets/final/training_data_100.json

# GRPO 强化学习
python experiments/grpo/grpo_codefix_train_cot_prm.py --model your-sft-model
```

---

## 技术栈

- **Python 3.8+ / FastAPI / Ruff / pytest**
- **模型**：Qwen2.5-Coder（本地 1.5B）/ GPT-4o / DeepSeek（云端 32B）
- **向量检索**：FAISS / BM25 / Cross Encoder
- **训练框架**：TRL（GRPO / DPO / PPO / SFT）
- **AST**：Python 内置 `ast` 模块
- **沙箱**：subprocess 隔离 + 资源限制

---

## License

MIT License - 详见 [LICENSE](LICENSE)
