# CodeFix-Agent 项目上下文

> 给AI助手的项目概述 - 便于快速理解和接手

---

## 一、项目目标

**CodeFix-Agent** 是一个基于LLM的代码修复Agent，采用 ReAct + CoT 架构，实现工业级的代码修复能力。

**核心特点**：
- 小模型生成 + 大模型验证的双模型协作
- 六维Reward体系（格式/语法/AST/执行/过程/语义）
- 三层记忆架构（L1工作记忆 / L2反思记忆 / L3技能记忆）
- GRPO强化学习训练

---

## 二、已完成的工作（第一阶段）

### 1. AST代码处理模块

**文件**: `ast_code_processor.py` (新建)

**核心功能**:
- 基于Tree-sitter的精确函数定位和替换
- **字节偏移替换**（解决传统行号偏移问题）
- 支持Python/Java/JavaScript多语言
- 自动降级（Tree-sitter不可用时用Python ast模块）

**关键方法**:
```python
class ASTCodeProcessor:
    def find_function_by_name(code, func_name) -> FunctionInfo
    def replace_function(code, func_name, new_code) -> str  # 字节偏移替换
    def fix_imports(code, missing_imports) -> str
    def analyze_dependencies(code, func_name) -> List[str]
```

### 2. 集成到主Agent

**文件**: `cot_react_agent.py` (修改)

**新增内容**:
- AST处理器初始化
- 5个AST辅助方法：`_extract_function_by_name`, `_replace_function_in_code`, `_find_function_by_error_line`, `_fix_imports_in_code`, `_analyze_function_dependencies`

### 3. Ruff代码质量检查完善

**文件**: `cot_react_agent.py` (修改)

**改进**:
- 返回值扩展：`(needs_optimization, issues, issue_count)`
- 添加分数阈值机制
- 规则集：E/F/W/C90/N/Q/UP

---

## 三、核心架构决策

### 验证顺序（已确定）
```
编译验证 → 测试执行 → Ruff检查 → LLM深度审查（可选）
           ↑           ↑
      先保证做对    再考虑优雅
```

### 测试验证
```
✅ 所有AST处理器测试通过
✅ 主Agent模块导入成功
```

---

## 四、重要讨论记录

### 1. LLM as Judge 选型
- 小模型（1.5B/7B）：简单风格审查
- 大模型（32B+）：复杂问题分析
- **核心原则**：能用小模型/规则解决的不用LLM

### 2. 函数调用图（Call Graph）
**目的**：解决上下文爆炸和Lost in Middle问题

**方案**：按需加载分析路径上的函数，而非一次性加载整个文件

```
main() → calculate() → validate()
         ↑              ↑
      调用时才加载    调用时才加载
```

### 3. RepoZero论文启发（ACE框架）
**参考论文**：`参考论文\从0生成代码仓库.md`

**核心思想**：
- Coding Agent + Testing Agent 协作
- 迭代式代码-测试反馈循环
- Test-time Scaling（重试机制）提升成功率65%

### 4. 测试用例自动生成
**问题**：答案参照的正确性如何保证？

**方案**：
1. 修复后的代码作为临时oracle
2. 双向验证（buggy代码应失败，fixed代码应成功）
3. 交叉验证（多模型生成多个测试用例互相验证）

### 5. 训推一致性
**问题**：小模型SFT/GRPO都是代码级修复，用于仓库级修复会有不一致

**方案**：
1. 分层架构：大模型策略层 + 小模型执行层
2. In-Context Learning：仓库级示例作为prefix
3. 渐进式扩展：代码级 → 多函数 → 文件级 → 仓库级

---

## 五、下一步计划

### 第二阶段：多函数/文件级修复
- [ ] 实现Call Graph构建
- [ ] 动态上下文管理
- [ ] 文件依赖分析

### 第三阶段：工具调用能力
- [ ] 测试用例自动生成
- [ ] 报告生成工具（Markdown格式）
- [ ] 编译、Git等工具集成

### 第四阶段：记忆架构优化
- [ ] L1/L2/L3记忆精细化
- [ ] 跨项目经验复用

---

## 六、项目文件结构

```
repobugfix_complete_project/
├── ast_code_processor.py          # AST代码处理引擎（新建）
├── cot_react_agent.py             # 主Agent（修改：集成AST+完善Ruff）
├── process_reward_model.py         # PRM模块（6维Reward，7B+PRMHead）
├── grpo_codefix_train_cot_prm.py   # GRPO训练脚本
├── reflection_memory.py            # 反思记忆
├── skill_manager.py                # 技能管理器
├── enterprise_rag_pipeline.py      # RAG检索管道
├── 执行器/executor.py              # 代码执行器
├── 开发记录/
│   └── 开发记录_2026-05-15.md      # 本开发记录
├── 聊天记录/
│   ├── 豆包5.15聊天规划.md         # 升级路线图
│   └── cursor_chat_record5.15.md   # 详细讨论
└── 参考论文/
    ├── 从0生成代码仓库.md          # RepoZero论文
    └── 可追溯多智能体架构.md       # 多Agent架构
```

---

## 七、关键设计理念

1. **Compile-First**：编译器作为ground truth，避免LLM幻觉
2. **零幻觉工具**：Ruff做质量检查，纯规则无幻觉
3. **渐进式加载**：按需加载，避免上下文爆炸
4. **小模型优先**：能用小模型解决的不用大模型

---

*文档创建时间: 2026-05-16*
*用途: 便于其他AI快速理解项目上下文*
