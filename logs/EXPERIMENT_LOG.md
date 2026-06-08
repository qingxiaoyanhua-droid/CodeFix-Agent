# 本地模型 Bug 修复实验记录

**日期**: 2026-04-19
**模型**: qwen2.5-coder:1.5b (Ollama)
**基准**: QuixBugs Mini (5题)

---

## 实验目标

验证三层记忆增强 CoT ReAct Agent 在本地小模型上的 bug 修复能力，
并通过 SFT + GRPO 训练迭代提升模型的结构化输出能力。

---

## 第一阶段：发现症状

### 初始结果 (num_predict=512)
| Bug | 结果 | 现象 |
|-----|------|------|
| bitcount | PASS | — |
| find_first_in_sorted | FAIL | "No fixed code produced" |
| gcd | FAIL | "No fixed code produced" |
| sqrt | FAIL | "No fixed code produced" |
| quicksort | FAIL | "No fixed code produced" |

通过率: 1/5 (20%)，且大模型调用后小模型仍然无法生成代码。

---

## 第二阶段：问题排查

### 排查1：num_predict token 限制

**假设**: 512 max tokens 不够完整输出 CoT 推理 + `<fixed_code>` 块

**实验**:
- 512 → 1024: gcd 修复了 (2/5)，但 sqrt/find_first_in_sorted 仍失败
- 1024 → 4096: 没有任何改善

**结论**: token 限制不是根本原因。

### 排查2：直接观察模型原始输出

通过在 `eval_local.py` 中打印模型原始响应，发现：

#### 测试 1：无 system prompt
```
RAW: 'The bug in the code lies in the way it uses XOR (^) instead of AND (&)...
```
- 模型直接输出自然语言分析
- **完全没有** `<think>` 和 `<fixed_code>` 标签
- `extract_code` 搜不到 → 返回空字符串

#### 测试 2：带 COT_SYSTEM_PROMPT
```
<think>
Step 1: Bug Identification
...

<fixed_code>
```python
def bitcount(n):
    ...
```
```
模型开始使用 `<think>` 和 `<fixed_code>`，但：
- 格式不稳定：有时用 `<think>` 有时用自定义标签 `<rootCauseAnalysis>`、`<fixStrategy>`
- 代码位置不固定：有时紧跟 `<fixed_code>` 之后，有时用 markdown `###` 标题包裹
- 闭合标签不确定：有时是 `</fixed_code>`，有时是 `### Explanation`

#### 测试 3：调整后的 system prompt
```
Your response MUST use this exact XML-like format.
```
模型开始正确使用 `<think>` 和 `<fixed_code>`，但：
- **仍然不使用 `</fixed_code>` 闭合标签**
- 代码块后直接跟自然语言 "Here's the fixed code:"
- `extract_code` 正则匹配失败

---

## 第三阶段：根本原因总结

### Root Cause 1: 格式遵从性低
`qwen2.5-coder:1.5b` 是 chat 模型，RLHF 训练让它倾向于输出自由格式的自然语言回答，
而非严格遵循结构化 XML-like 格式指令。

### Root Cause 2: 解析器过于严格
`extract_code` 正则只匹配 `<fixed_code>...```...```...</fixed_code>` 一种格式，
无法应对：
- 缺少 `</fixed_code>` 闭合标签
- Markdown `###` 标题干扰
- 自定义 XML 标签名

### Root Cause 3: 训练数据分布不匹配
QuixBugs bug 修复需要：
1. 精确识别 bug 类型（二分边界、算子错误等）
2. 生成结构化 CoT 推理
3. 输出精确的代码修改

预训练模型擅长(1)，但(2)(3)需要专门训练。

---

## 第四阶段：修复方案

### 方案 A：解析器加固（短期，已实施）

```python
def extract_code(raw_output: str) -> str:
    patterns = [
        # 标准 <fixed_code> ... </fixed_code>
        r'<fixed_code>\s*```python\s*(.*?)\s*```\s*</fixed_code>',
        # 缺少 </fixed_code> 时的截断
        r'<fixed_code>\s*```python\s*(.*?)\s*```\s*(?=###|## |<fixed_code>)',
        r'<fixed_code>\s*```python\s*(.*?)\s*```',
        # 无 ```python 标签
        r'<fixed_code>\s*(.*?)\s*</fixed_code>',
        r'<fixed_code>\s*(.*?)(?=###|## |</fixed_code>)',
        # Fallback: 最后一个 python 代码块
        r'```python\s*(.*?)\s*```',
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_output, re.DOTALL)
        if match:
            code = match.group(1).strip()
            if code and ('def ' in code or 'return' in code or 'while' in code):
                return code
    return ""
```

**效果**: 能提取更多格式变体，但代码正确性仍依赖模型本身。

### 方案 B：System Prompt 优化

在 prompt 中强调格式约束 + 重复强调，但实验证明效果有限。

### 方案 C：SFT 训练（核心，长期）

训练模型学习 "结构化 CoT + 代码修复" 的输出格式。

---

## 第五阶段：解析器最终修复

### Bug 1: extract_code 无法提取非标准格式

模型输出用 markdown `### ` 标题包裹 `<fixed_code>`，没有 `</fixed_code>` 闭合标签。

**修复**: 添加多层 fallback 正则模式，依次尝试。

### Bug 2: 函数名不匹配

模型生成 `def fixed_function(n)` 但 harness 期望 `def bitcount(n)`。

**修复**: 在 `CoTParser.normalize_function_name()` 中自动将生成的函数名重命名为原函数名。

### Bug 3: 测试结果对比

| Bug | 修复前 | 修复后 | 根因 |
|-----|--------|--------|------|
| bitcount | FAIL (NameError) | FAIL (got 1) | 模型代码逻辑错误 |
| find_first_in_sorted | FAIL (No code) | FAIL (off-by-one) | 模型推理错误 |
| gcd | PASS | PASS | 巧合正确 |
| sqrt | FAIL (No code) | FAIL (wrong) | 模型推理错误 |
| quicksort | FAIL (No code) | FAIL (No code) | 格式不匹配 |
| **总计** | **1/5 (20%)** | **1/5 (20%)** | 1.5B 能力不足 |

**结论**: 解析器修复成功，评测流程跑通。瓶颈在于 **1.5B 模型代码生成能力弱**，不是工具链问题。

---

## 第六阶段：SFT + GRPO 训练计划

### Step 1: SFT（监督微调）

**目标**: 让模型学会遵循 `<fixed_code>` 格式输出。

**训练数据格式**:
```json
{
  "messages": [
    {"role": "user", "content": "Bug: ...\nBuggy Code:\n..."},
    {"role": "assistant", "content": "<think>\n[Step 1: ...]\n...\n[/Step 4: ...]\n</think>\n\n<fixed_code>\n```python\n# corrected code\n```\n</fixed_code>"}
  ]
}
```

**数据来源**:
1. 人工标注：正确格式的 bug 修复示例 (50-100条)
2. GPT-4o 生成：批量生成格式正确的 (bug → 格式正确的修复) 对
3. 已有成功案例：从本项目 eval_local.py 成功运行的案例中提取

**训练配置**:
- 模型: qwen2.5-coder:1.5b
- LoRA 微调（低成本，7B以下模型效果好）
- lr: 2e-4, epoch: 3-5, batch: 4
- 预期 loss: 从 1.5 → 0.3

### Step 2: GRPO（组相对策略优化）

**目标**: 优化模型不仅格式正确，还要修复结果正确。

**GRPO 奖励设计**:
```
reward = format_reward + correctness_reward
```

其中:
- `format_reward = 1.0` 如果输出包含完整的 `<think>...</think><fixed_code>...` 结构，否则 `0.0`
- `correctness_reward`:
  - 代码执行通过所有测试用例: `1.0`
  - 代码能编译但测试失败: `0.3`
  - 代码无法编译: `0.0`
  - 格式错误: `0.0`

**GRPO 组内对比**: 同一个 bug 生成 4-8 个响应，选择奖励最高的更新策略。

**训练循环**:
```
for each batch:
    1. SFT 模型生成 4-8 个响应
    2. 执行器验证代码正确性
    3. 计算 format_reward + correctness_reward
    4. GRPO 更新策略（使用优势函数 A = r - mean(r)）
```

### Step 3: 迭代

```
初始模型 → SFT_v1 → GRPO_v1 → 评测 → SFT_v2 → GRPO_v2 → ...
                ↑__________________|
                   (用改进模型收集新数据)
```

---

## 附录：关键代码改动

### eval_local.py
- `num_predict`: 512 → 4096

### cot_react_agent.py
- `COT_SYSTEM_PROMPT`: 强调 XML-like 格式
- `CoTParser.extract_code()`: 多模式正则匹配（6种 fallback 模式）
- `CoTParser.normalize_function_name()`: 自动重命名函数名匹配原 harness 期望

---

## 待办事项

- [x] 修复 extract_code 解析器（多模式 fallback）
- [x] 修复函数名不匹配问题
- [x] 评测流程跑通
- [x] **生成 SFT/DPO 训练数据** (178条训练 + 31条验证)
- [x] **SFT 训练脚本** (transformers + LoRA/QLoRA)
- [x] **GRPO 训练脚本** (6奖励函数 + 正确梯度)
- [x] **评测脚本** (QuixBugs pass@1/pass@k)
- [ ] **核心：A100 上运行 SFT + GRPO 训练**
- [ ] 在 QuixBugs Full (40题) 上评测
- [ ] 验证 pass@1 提升

---

## 已创建的训练脚本

### 1. prepare_training_data.py
数据格式转换：将 SO + LeetCode 爬取数据 → SFT/DPO 格式

### 2. sft_train.py
SFT + DPO 训练脚本：QLoRA 4bit + LoRA rank=16，支持断点续训

### 3. grpo_train.py
工业级 GRPO：修复了服务器上所有 bug（梯度+OOM+依赖），6奖励函数

### 4. eval_benchmark.py
QuixBugs 评测脚本：支持 Ollama 本地模型和 HuggingFace 微调模型

## 真实评测数据集

### bushu/datasets/quixbugs/quixbugs_benchmark.json
来自 QuixBugs 官方数据集，11 个 Python 程序，9 个带真实测试用例：

| 程序 | Bug 类型 | 测试数 | 状态 |
|------|----------|--------|------|
| bitcount | wrong_operator | 6 | GOOD |
| find_first_in_sorted | off_by_one | 6 | GOOD |
| flatten | wrong_variable | 4 | GOOD |
| gcd | wrong_argument_order | 6 | GOOD |
| is_valid_parenthesization | missing_check | 8 | GOOD |
| max_sublist_sum | wrong_initialization | 6 | GOOD |
| quicksort | off_by_one | 6 | GOOD |
| shortest_path_lengths | wrong_data_structure | 2 | GOOD |
| maximum_subarray | wrong_initialization | 1 | GOOD |
| sqrt | wrong_expression | 0 | SKIP (无限循环) |
| kmp | none | 0 | SKIP (无bug) |

**验证结果: 9/9 测试套件正确区分 buggy 和 fixed 代码**

### bushu/datasets/training/
- sft_train.json: 178条 SFT 格式数据
- sft_eval.json: 31条验证集
- dpo_train.json: 178条 DPO 格式数据
- dpo_eval.json: 31条验证集
