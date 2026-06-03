# OpenClaw Agent 架构与设计规则（完整版）

> 核心模块：大模型统筹层（仓库级）+ 决策层 + 状态层 + 工具层 + 执行层 + 验证层 + 三层记忆系统
> 适用场景：函数级评测（QuixBugs）+ 仓库级修复（requests 真实项目）

---

## 零、架构演进历程

```
线性管道（v1）→ 带记忆的单Agent（v2）→ 五层架构（v3）→ 双模式分层（最终）
  buggy code → execute → output     ↑多了L2/L3     ↑决策+状态+工具分层   ↑大模型统筹+小模型修复
```

### v3 vs 最终版的核心区别

| 缺失项 | v3 现状 | OpenClaw 最终 |
|--------|---------|--------------|
| 大模型统筹层 | 无，函数级直接修 | **仓库级：大模型定位 → 小模型修复** |
| 任务入口 | 直接执行，无规划 | **TaskPlanner 决策入口** |
| 意图识别 | 无 | **IntentClassifier 识别 BUG_FIX/CRASH_FIX/SYNTAX_FIX** |
| 任务拆解 | 无 | **PlanGenerator 生成有序 PlanStep 序列** |
| 多轮上下文 | 依赖外部 | **StateManager 完整管理** |
| 中间变量 | 无 | **变量命名空间 + 跨步骤传递** |
| 历史记录 | 无 | **ExecutionTrace 完整轨迹** |
| 循环检测 | 无 | **LoopDetector 防止死循环** |
| 工具选择 | 硬编码 | **ToolSelector 动态路由 + fallback** |
| 条件路由 | 无 | **ConditionalRouter 根据执行结果切换工具** |

### Scale-Adaptive 双模式

**模式 A（函数级）：小模型直接修**
```
Bug → TaskPlanner → 小模型CoT → Harness验证 → PASS/FAIL
```

**模式 B（仓库级）：大模型协调 grep + AST + 小模型修复**
```
Bug → pytest报错信息
          ↓
   大模型协调者（7B/32B）
     ├─ 调用 grep 工具 → 搜关键词 → 候选文件
     ├─ 调用 AST.locate_function() → 函数字节偏移
     ├─ 整合结果 → 确定目标文件+函数
          ↓
   小模型细粒度修复（上下文已缩小到目标函数）
          ↓
   Harness验证 → git apply / 函数级替换 → pytest
```

为什么大模型协调而不是扫描：
- 大模型直接扫仓库 token 不够且不准
- 大模型当决策者，grep+AST 当执行者，各司其职
- grep 毫秒级缩小候选，AST 精确定位，大模型只做语义判断

---

## 一、双模式架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                      决策层 TaskPlanner                     │
│  意图识别 → 任务拆解 → 规划生成（PlanStep 序列）          │
└──────────────────────────┬────────────────────────────────┘
                           │
┌──────────────────────────▼────────────────────────────────┐
│                    状态层 StateManager                      │
│  执行轨迹 | 变量命名空间 | 循环检测 | 上下文摘要           │
└──────────────────────────┬────────────────────────────────┘
                           │
┌──────────────────────────▼────────────────────────────────┐
│                   工具层 ToolSelector                       │
│  工具注册表 | 条件路由 | fallback 策略 | 执行统计           │
└──────────────────────────┬────────────────────────────────┘
                           │
┌──────────────────────────▼────────────────────────────────┐
│                     执行层 CoT Engine                        │
│  小模型(1.5B) CoT 四步推理 | 大模型(7B) Hint 分析        │
└──────────────────────────┬────────────────────────────────┘
                           │
┌──────────────────────────▼────────────────────────────────┐
│                    验证层 Harness                           │
│  compile() → exec() → 测试用例 → PASS/FAIL                │
└─────────────────────────────────────────────────────────────┘

返回执行结果给 StateManager → 更新上下文 → 继续下一个 PlanStep
```

---

## 二、OpenClaw Agent 核心设计

### 2.1 设计原则

OpenClaw 不是单一大模型，而是**多组件协同的 Agent 框架**：

```
OpenClaw = CoT推理引擎 + 三层记忆系统 + 条件升级策略
```

**核心设计原则**：
1. **Compiler as Ground Truth** — 编译器和测试用例是唯一的事实来源，零幻觉
2. **Conditional Escalation** — 简单问题用小模型直出，复杂问题才升级大模型
3. **Memory as Infrastructure** — 记忆不是附加功能，是 Agent 持续进化的基础设施

### 2.2 工作流程（完整五层链路）

```
用户输入: buggy_code + bug_description + error_message
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│ 决策层 TaskPlanner                                                │
│   ├── IntentClassifier: 意图识别（BUG_FIX / CRASH_FIX / SYNTAX_FIX）
│   ├── ComplexityEstimator: 复杂度评估（1-5级）
│   └── PlanGenerator: 生成有序 PlanStep 序列
│   输出: TaskPlan{task_id, intent, complexity, steps[]}
└────────────────────────────┬────────────────────────────────────┘
                             │ TaskPlan
┌────────────────────────────▼────────────────────────────────────┐
│ 状态层 StateManager                                               │
│   ├── add_step() / complete_step() / fail_step()
│   ├── 变量命名空间: set_var() / get_var()
│   ├── 循环检测: LoopDetector
│   └── build_context_summary(): 生成上下文摘要注入 prompt
│   输入: 执行结果 → 更新状态 → 输出下一个工具选择信号
└────────────────────────────┬────────────────────────────────────┘
                             │ loop_detected / harness_result / retry_count
┌────────────────────────────▼────────────────────────────────────┐
│ 工具层 ToolSelector                                               │
│   ├── ToolRegistry: 工具注册表（9个内置工具）
│   ├── ConditionalRouter: 条件路由
│   │     compile_fail  → 概率调用 large_model_analyze
│   │     syntax_error  → 语法修复
│   │     rag_miss     → rag_fallback
│   │     loop_detected → 终止
│   └── select_fallback(): 工具失败时降级策略
│   输出: tool_name + inputs → 执行层
└────────────────────────────┬────────────────────────────────────┘
                             │ ToolResult
┌────────────────────────────▼────────────────────────────────────┐
│ 执行层 CoT Engine                                                 │
│   ├── 小模型(1.5B) + L1/L2/L3 记忆 → CoT 四步推理 + <fixed_code>
│   ├── 大模型(7B) 仅分析 CoT 错误步骤，给 hint（不亲自修）
│   └── 提取 fixed_code 交给验证层
└────────────────────────────┬────────────────────────────────────┘
                             │ fixed_code
┌────────────────────────────▼────────────────────────────────────┐
│ 验证层 Harness                                                    │
│   Phase 1: compile()  → 捕获 SyntaxError
│   Phase 2: exec()     → 捕获 NameError/TypeError/RuntimeError
│   Phase 3: 测试用例   → 捕获 AssertionError
│   输出: PASS / FAIL + error_message
└────────────────────────────┬────────────────────────────────────┘
                             │
                    回到状态层 → 更新上下文 → 继续下一个 PlanStep
```

### 2.3 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 小模型 CoT 后谁来验证 | 编译器，不是大模型 | 编译器零幻觉，大模型可能幻觉 |
| 大模型负责什么 | 只给 hint，不亲自修 | 大模型修的代码也可能有幻觉 |
| 最多重试几轮 | 3轮 | 统计：90%可修复bug在前3轮完成 |
| 超过3轮怎么办 | 存入L2，不浪费算力 | 第4轮成功率趋近0 |
| L2/L3 存在哪 | 本地 JSON 文件 | 不占显存，推理时按需加载 |
| 记忆如何检索 | Embedding语义 + 关键词 + 信任加权 | 三路并行，取交集保证相关性 |

---

## 三、Harness 测试框架

### 3.1 定位

Harness 是 OpenClaw Agent 的**验证基础设施**，负责代码的正确性验证。核心原则：**Harness 是 Ground Truth，不是评分系统**。

### 3.2 三阶段执行

```
Phase 1: compile()
  用 Python compile() 捕获 SyntaxError
  → 通过 → 进入 Phase 2
  → 失败 → 返回错误信息

Phase 2: exec() 隔离执行
  exec(code, {}) 在隔离 namespace 执行
  捕获 NameError / TypeError / RuntimeError
  → 通过 → 进入 Phase 3
  → 失败 → 返回错误信息

Phase 3: 测试用例执行
  对每个 (args, expected_output) 执行
  namespace[func](*args) → 比较输出
  捕获 AssertionError / 返回值不匹配
  → 全部通过 → PASS
  → 任一失败 → FAIL + 哪个用例失败
```

### 3.3 关键工程细节

**函数名对齐问题**（踩坑记录）：
- 模型生成 `def fixed_function(n)` 但 Harness 期望 `def bitcount(n)`
- 解决：提取后做函数名修复，将生成代码的函数名替换为 Harness 期望的原函数名

**参数解包问题**（踩坑记录）：
- `args = []`（空数组）时，`*[]` 解包成 0 个参数，但函数需要 1 个参数
- 解决：空数组时用 `**{}` 调用而非 `*[]`

**Generator 函数问题**：
- 某些函数的返回值是 generator（如 flatten）
- 解决：在 Harness 验证阶段对返回值做 `list()` 转换后再比较

**浮点精度问题**：
- sqrt 等浮点运算的 expected 值有精度误差
- 解决：添加 0.01 容忍度

**stdout 隔离**：
- 用 `old_stdout → StringIO → restore` 捕获 print 输出
- 防止代码执行时打印内容干扰 Agent 输出

### 3.4 评测指标

```python
pass_at_1 = 1.0 if any(r["success"] for r in results[:1]) else 0.0   # 第一次就成功
pass_at_5 = 1.0 if any(r["success"] for r in results[:min(5, len(results))]) else 0.0
pass_at_10 = 1.0 if any(r["success"] for r in results) else 0.0
```

---

## 四、L2 Reflexion（失败教训库）

### 4.1 核心思想

> Reflexion 的本质：**用语言记忆失败教训，让 Agent 避免重蹈覆辙**

不是传统的 RL 权重更新，而是**在 prompt 层面注入历史教训**。

### 4.2 存储格式

```json
{
    "reflection_id": "uuid-string",
    "bug_pattern": "initialization_error",
    "root_cause": "total初始化为1而非0，导致累加结果多1",
    "failed_strategies": [
        "只改了return值但没改total初始值",
        "只加了判断但逻辑反了"
    ],
    "lesson_learned": "修复累加类bug必须先检查所有初始化语句",
    "suggested_approach": "先写edge case再改循环",
    "usefulness_score": 0.85,
    "hit_count": 3,
    "last_updated": "2026-04-10"
}
```

### 4.3 触发条件

- **写入条件**：3轮修复全部失败
- **抽取方式**：大模型从失败经历中生成结构化 JSON

### 4.4 验证环（usefulness_score 动态更新）

```
初始：usefulness_score = 0.5

命中某条 L2 且修复成功 → usefulness_score += 0.1
命中某条 L2 但仍然失败 → usefulness_score -= 0.2

usefulness_score < 0.2 → 软删除（不再优先召回）
usefulness_score > 0.9 → 高信任，优先召回
```

### 4.5 检索逻辑

三个维度并行检索，结果合并：

1. **Embedding 语义相似度**（Top-3）
   - 用 all-MiniLM-L6-v2 将 reflection 的 `lesson_learned` 转为向量
   - 与当前 bug 的 `bug_description` 计算余弦相似度

2. **bug_type 关键词精确匹配**
   - 当前 bug_type 与 reflection 的 `bug_pattern` 精确匹配
   - 命中则加权

3. **usefulness_score 信任加权**
   - 高分 reflection 在合并排序时权重更大
   - 保证越经验证的教训优先被使用

### 4.6 上下文注入控制

| 约束 | 值 | 说明 |
|------|-----|------|
| L2 最多注入条数 | 3条 | 防止上下文膨胀 |
| 每条最大 tokens | ~100 | lesson_learned 压缩到100 tokens |
| 总 tokens 上限 | 500 | L2+L3 总计不超过500 tokens |
| relevance_score 阈值 | 0.65 | 低于此分数不注入 |
| 关键词重叠要求 | ≥1个 | bug_keywords 必须有重叠 |

---

## 五、L3 Hermes Skill（技能沉淀库）

### 5.1 核心思想

> Hermes 的本质：**把成功的修复流程提取为可复用的 SOP 模板**

不是记录"什么错了"，而是总结"什么做对了"，下次遇到类似场景直接调用。

### 5.2 存储格式

```json
{
    "skill_id": "uuid-string",
    "name": "Fix-OffByOne-Loop-Boundary",
    "trigger_keywords": ["loop", "range", "index", "boundary", "off-by-one"],
    "description": "修复循环边界 off-by-one 错误的标准流程",
    "complexity_score": 2,
    "steps": [
        "Step 1: 确认循环是开区间还是闭区间",
        "Step 2: 用最小输入验证下界（n=1）",
        "Step 3: 用最小输入验证上界（n=2）",
        "Step 4: 对比 buggy 和 fixed 的 range 参数差异"
    ],
    "verification_hint": "跑边界 case: n=1, n=2",
    "version": 1,
    "success_count": 7,
    "failure_count": 1,
    "deprecation_score": 0.12,
    "last_updated": "2026-04-12"
}
```

### 5.3 触发条件

**必须同时满足**：
1. 修复成功（测试用例全部通过）
2. **且**（复杂度 score ≥ 1 **或** 大模型介入过）

**为什么加复杂度门控**：
- 简单 bug（如 `a - b` → `a + b`）小模型一次过，不抽取 skill
- 避免 skill 库过载，同时保证 skill 质量

### 5.4 验证环（success/failure 计数）

```
初始：success_count=0, failure_count=0, deprecation_score=0.0

命中某条 L3 且修复成功 → success_count += 1
命中某条 L3 但修复失败 → failure_count += 1

deprecation_score = failure_count / (success_count + failure_count)

deprecation_score > 0.7 → 标记为 stale（过期）
连续3次失败 → 硬删除该 skill
```

### 5.5 增量打补丁（Hermes 风格）

当某条 skill 在使用时失败：
- **不重写**整个 skill（浪费 token）
- 只在 `steps` 末尾**追加**新的 failed_attempt
- `version += 1`

```json
{
    "steps": [
        "Step 1: ...",
        "Step 2: ...",
        "Step 3: ...",
        "Step 4: ..."
    ],
    "version": 2,
    "failed_patches": [
        "v1时尝试: 对称处理，但没有覆盖单向遍历场景",
        "v2时修正: 增加单向边界检查"
    ]
}
```

### 5.6 Markdown 导出

每条 skill 可导出为人类可读的 Markdown：

```markdown
# Fix-OffByOne-Loop-Boundary

## 触发关键词
loop, range, index, boundary, off-by-one

## 步骤
1. 确认循环是开区间还是闭区间
2. 用最小输入验证下界（n=1）
3. 用最小输入验证上界（n=2）
4. 对比 buggy 和 fixed 的 range 参数差异

## 验证提示
跑边界 case: n=1, n=2

## 统计
- 成功率: 7/8 (87.5%)
- 版本: 2
```

---

## 六、三层记忆对比

| 维度 | L1 工作记忆 | L2 Reflexion | L3 Hermes |
|------|------------|-------------|-----------|
| 内容来源 | 当前会话 | 历史失败教训 | 历史成功经验 |
| 内容性质 | "这次遇到什么" | "上次踩什么坑" | "上次怎么成的" |
| 触发写入 | 自动 | 3轮全失败 | 成功+复杂度门控 |
| 验证机制 | 无（当前会话） | usefulness_score 动态更新 | deprecation_score 过期降权 |
| 存储形式 | 内存变量 | JSON 文件 | JSON + Markdown |
| 检索方式 | 直接访问 | Embedding+关键词+信任加权 | 关键词+复杂度+版本 |
| 上下文注入 | 始终 | relevance_score ≥ 0.65 | relevance_score ≥ 0.65 |
| 面试一句话 | "当前 bug 的上下文" | "失败后语言总结教训" | "成功后提取可复用 SOP" |

---

## 七、与其他架构的关系

### 7.1 OpenClaw vs Reflexion（原始论文）

| 组件 | Reflexion 原版 | OpenClaw 实现 |
|------|-------------|--------------|
| 反馈来源 | LLM 自我反思 | 测试用例（更可靠） |
| 记忆形式 | Verbal buffer | 结构化 JSON + 验证环 |
| 策略更新 | 改 prompt | 改 prompt + GRPO 训练 |
| 验证环 | 无 | usefulness_score 动态更新 |
| 适用场景 | 开放式任务 | 可验证的代码修复任务 |

### 7.2 OpenClaw vs Hermes（原始设计）

| 组件 | Hermes 原版 | OpenClaw 实现 |
|------|-----------|--------------|
| 触发门控 | 无 | complexity_score ≥ 1 |
| 失败处理 | 全量重写 | 增量打补丁 |
| 过期机制 | 无 | deprecation_score > 0.7 → stale |
| Skill 格式 | Markdown | JSON + Markdown 导出 |
| 持久化 | 磁盘 | JSON 文件 + 向量数据库 |

### 7.3 OpenClaw vs ReAct

OpenClaw 是 **ReAct 的增强版**：

| 维度 | ReAct | OpenClaw |
|------|-------|----------|
| 思考-行动循环 | ✓ | ✓ |
| 条件升级策略 | ✗ | ✓（Compile-First） |
| 三层记忆系统 | ✗ | ✓ |
| 多模型协作 | ✗ | ✓（1.5B生成+7B验证） |
| GRPO 训练 | ✗ | ✓ |

---

## 八、设计规则（设计决策的依据）

### 规则1：记忆是基础设施，不是可选项

> "记忆不是附加功能，是 Agent 持续进化的基础设施"

- L2 解决"同一个坑踩两次"的问题
- L3 解决"成功经验无法复用"的问题
- 两者都不占显存（存在磁盘），推理时按需加载

### 规则2：Ground Truth 优于 LLM 自我判断

> "编译器验证比 LLM 自我反思更可靠"

- 格式对不对 → 看 CoT 标签是否存在
- 代码对不对 → 看编译和测试是否通过
- 不要让 LLM 评价自己的输出质量

### 规则3：简单问题不过度设计

> "60% 的简单 bug 不应该触发复杂的 Agent 流程"

- Compile-First 策略：编译器能判断的，不需要 LLM
- 小模型能修对的，不需要大模型
- 减少不必要的推理开销

### 规则4：验证环保证记忆质量

> "没有验证环的记忆系统会逐渐腐化"

- L2 的 usefulness_score 持续更新，低质量教训自动衰减
- L3 的 deprecation_score 跟踪 skill 有效性，过期 skill 自动降权
- 避免记忆库成为垃圾场

### 规则5：上下文预算是硬约束

> "L2/L3 注入有上限，超过则截断"

- 三个硬约束：token 预算（≤500）、条数上限（≤3条）、关键词重叠（≥1）
- 防止记忆注入导致注意力涣散

---

## 九、面试话术汇总

### 话术1：整体介绍
> "我的 Agent 系统叫 OpenClaw，核心是 Compiler-First ReAct + 三层记忆。编译器作为 Ground Truth 验证代码正确性，简单 bug 直接过，复杂的才升级大模型。记忆系统分三层：L1 是当前工作记忆，L2 是失败教训库（Reflexion），L3 是成功 SOP 库（Hermes），两者都有验证环保证记忆质量。"

### 话术2：Reflexion（L2）
> "L2 是失败教训库。每次修复失败后，大模型把失败原因总结成结构化 JSON 存入磁盘，包含 bug_pattern、root_cause、failed_strategies 和 lesson_learned。下次遇到类似 bug，先检索相关教训注入上下文，让模型避免重蹈覆辙。usefulness_score 动态更新——命中且成功就加分，命中但失败就减分，低分教训自动降权。"

### 话术3：Hermes（L3）
> "L3 是技能卡片库。成功修复复杂 bug 后，把这次怎么修的提取成 SOP 模板，包含触发关键词、步骤列表和验证提示。只对复杂度≥1 或大模型介入过的 case 抽取，避免 skill 过载。增量打补丁机制——skill 用多了失败时，不重写整个 skill，只追加 failed_attempt，节省 token。"

### 话术4：Harness
> "Harness 是评测框架，分三阶段：compile() 捕获语法错误，exec() 隔离执行捕获运行时错误，测试用例执行验证功能正确性。关键工程细节是函数名对齐——模型生成的函数名可能和 harness 期望的不一致，需要在提取后修复。"

### 话术5：为什么需要三层记忆
> "类比人的学习过程：遇到新 bug 时先看当前上下文（L1），再想以前踩过什么坑（L2），最后查有没有现成的 SOP 可用（L3）。三者同时工作，互补长短。L2 防止重复犯错，L3 加速解决已知问题，两者都不占显存只在推理时注入。"

### 话术6：会不会上下文爆炸
> "三个硬约束防止爆炸：①token 预算上限 L2+L3 不超过上下文 30%；②必须有关键词重叠才能注入；③按 relevance_score 排序后截断，不是全量注入。实际注入量很小（通常 3 条记忆，总共几百 tokens）。"
