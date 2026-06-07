# AgentHarness 项目变更日志

## Step 0 → Step 1: SessionStore (Claude-style Append-Only Persistence)

**文件**: `cot_react_agent.py`

### 新增类

#### `SessionStore` (约150行)
Claude风格的append-only会话持久化层。

**输入**:
- `session_id`: 会话唯一标识符
- `metadata`: 会话元数据(dict)
- `event`: 事件字典
- `AgentResult`: agent执行结果对象

**输出**:
- `.jsonl` 文件: append-only事件流
- `.meta.json` 文件: 会话元数据
- `.state.json` 文件: 断点恢复状态

**关键方法**:

| 方法 | 输入 | 输出 | 触发时机 |
|------|------|------|---------|
| `create_session()` | session_id, metadata | .meta.json | fix_bug入口 |
| `append_user_message()` | content, metadata | 追加到.jsonl | 接收用户bug描述 |
| `append_model_output()` | content, tool_uses, metadata | 追加到.jsonl | 小模型/大模型输出 |
| `append_tool_result()` | tool_name, result, success, metadata | 追加到.jsonl | 代码执行结果 |
| `append_compact_boundary()` | summary, tokens_freed, metadata | 追加到.jsonl | 压缩事件 |
| `append_agent_result()` | AgentResult对象 | 追加到.jsonl | 成功/失败return |
| `save_checkpoint()` | state字典 | .state.json | fix_bug结尾 |
| `load_checkpoint()` | session_id | state字典 | resume时 |
| `fork_session()` | parent_id, fork_id | 新会话文件 | 会话分叉 |
| `load_session()` | session_id | List[dict] | 读取历史 |

**存储结构**:
```
runs/sessions/
├── <session_id>.jsonl      # 主会话记录 (append-only JSONL)
├── <session_id>.meta.json   # 会话元数据
└── <session_id>.state.json  # 断点恢复状态
```

### 修改的现有类

#### `CoTReActAgent.__init__`
- **新增参数**: `session_store: SessionStore = None`
- **新增字段**: `self._current_session_id: Optional[str] = None`

#### `CoTReActAgent.fix_bug()`
- **新增参数**: `session_id: str = None`
- **改动**:
  - 入口: 自动生成session_id或使用传入值
  - 创建SessionStore记录
  - 6个位置埋入session日志(用户消息、小模型输出、工具结果、大模型反馈、3个成功return、最终return)

---

## Step 1 → Step 2: CompactionPipeline (Claude-style 5-Layer Context Management)

**文件**: `cot_react_agent.py`

### 新增类

#### `CompactionPipeline` (约260行)
Claude风格的5层上下文压缩管道。

**输入**:
- `context_messages`: 当前消息列表 `List[dict]`
- `session_id`: 会话ID
- `iteration`: 当前迭代轮次

**输出**:
- `(compacted_messages, total_chars_freed)`: 压缩后的消息列表 + 释放的字符数

**5层管道**:

| Layer | 方法 | 阈值 | 触发条件 | 策略 | 释放单位 |
|-------|------|------|---------|------|---------|
| L1 | `_layer1_budget_reduction` | 8000字符 | 单个工具输出>8000 | 截断并加引用标记 | ~数千字符 |
| L2 | `_layer2_snip` | 30000字符 | 历史总字符>30000 | 保留首尾，压缩中间 | ~数千字符 |
| L3 | `_layer3_microcompact` | 无独立阈值 | 消息数>8 | 合并>3条连续tool_result | ~数百字符 |
| L4 | `_layer4_context_collapse` | 60000字符 | 总字符>60000 | 语义摘要替换中间历史 | ~数千字符 |
| L5 | `_layer5_auto_compact` | 80000字符 | 总字符>80000 | 降级按比例压缩50% | ~数千字符 |

**统计字段**:
```python
{
    "budget_reductions": 0,      # L1触发次数
    "snip_count": 0,             # L2触发次数
    "microcompact_count": 0,     # L3触发次数
    "context_collapse_count": 0,   # L4触发次数
    "auto_compact_count": 0,      # L5触发次数
    "total_tokens_freed": 0,      # 累计释放字符数
}
```

### 修改的现有类

#### `CoTReActAgent.__init__`
- **新增字段**: 
  - `self.compaction_pipeline = CompactionPipeline(...)`
  - `self._iteration_history: List[dict] = []`

#### `CoTReActAgent.fix_bug()`
- **新增**: 迭代开头调用 `compaction_pipeline.compact()`
- **改动**: `compacted_history` 参数传给 `_generate_first_attempt` 和 `_generate_retry`
- **新增**: 迭代结束时累积消息到 `_iteration_history`

#### `CoTReActAgent._generate_first_attempt()`
- **新增参数**: `compacted_history: List[dict] = None`
- **新增**: 在RAG前注入 `--- PRIOR ITERATION HISTORY (compacted) ---` 摘要块

#### `CoTReActAgent._generate_retry()`
- **新增参数**: `compacted_history: List[dict] = None`
- **新增**: 在reflection injection后注入压缩历史摘要

#### `CoTReActAgent` 新增方法
- `_format_compacted_history()`: 将压缩后的历史格式化为可读摘要字符串，注入prompt

---

## Step 2 → Step 3: IOLogger (Claude-style Complete Input/Output Tracing)

**文件**: `cot_react_agent.py`

### 新增类

#### `IOLogger` (约170行)
Claude风格的完整I/O追踪器，输入输出成对记录。

**关键方法**:

| 方法 | 输入 | 输出 | 触发时机 |
|------|------|------|---------|
| `log_model_input()` | session_id, model_name, system_prompt, user_prompt, metadata | 写入.jsonl，返回io_pair_id | 每次模型调用前 |
| `log_model_output()` | session_id, io_pair_id, model_name, raw_output, metadata | 写入.jsonl | 每次模型调用后 |
| `log_model_call()` | 输入+输出一次性记录 | 返回io_pair_id | 封装快捷方法 |
| `generate_io_summary()` | session_id, max_pairs | 格式化的I/O摘要字符串 | 调试/分析 |

**io_pair_id机制**:
- 每次模型调用生成唯一ID（12位UUID）
- 输入和输出通过 `io_pair_id` 配对
- SessionStore记录: `{"type": "model_input"/"model_output", "io_pair_id": "xxx", ...}`

### 修改的现有类

#### `CoTReActAgent.__init__`
- **新增字段**: `self.io_logger = IOLogger(session_store=self.session_store)`

#### `_generate_first_attempt()`
- 在 `small_model()` 调用前后埋入IOLogger记录

#### `_generate_retry()`
- 在 `small_model()` 调用前后埋入IOLogger记录

#### `_analyze_error()`
- 在 `large_model()` 调用前后埋入IOLogger记录

#### `_optimization_pass()`
- 在 `small_model()` 调用前后埋入IOLogger记录

---

## Step 3 → Step 4: Permission Gate + Recovery Manager

**文件**: `cot_react_agent.py`

### 新增类

#### `PermissionGate` (约190行)
Claude-style Deny-First 权限控制层。

**设计原则**:
- 默认拒绝：未经明确允许的操作一律禁止
- 白名单制：每个操作类别需要显式授权
- 可配置：运行时可动态调整权限策略
- 可审核：所有操作决策记录到session

**操作类型**:
- `code_execution`: 允许执行用户提交的代码 (默认开启)
- `file_write`: 允许写入文件系统 (默认关闭)
- `file_read`: 允许读取文件系统 (默认关闭)
- `network_request`: 允许发起网络请求 (默认关闭)
- `subprocess`: 允许创建子进程 (默认关闭)
- `env_modify`: 允许修改环境变量 (默认关闭)

**关键方法**:

| 方法 | 输入 | 输出 | 触发时机 |
|------|------|------|---------|
| `check()` | operation, context | True 或抛出 PermissionDeniedError | 每次操作前 |
| `check_and_execute()` | operation, fn, args, kwargs | fn执行结果 | 封装检查+执行 |
| `grant()` | operation | 无 | 运行时授权 |
| `revoke()` | operation | 无 | 运行时撤销 |
| `is_allowed()` | operation | True/False | 查询权限 |
| `get_op_counts()` | 无 | 操作计数字典 | 监控 |

**安全检查**:
- `file_write`: 检查文件扩展名白名单
- `code_execution`: 禁止危险操作模式（`rm -rf`, `os.system(`, `eval(` 等）

**存储事件**:
```json
{"type": "permission_decision", "operation": "code_execution", "decision": "allowed", "reason": "policy_passed", "context": {...}}
```

#### `RecoveryManager` (约180行)
Claude-style 恢复管理器。

**设计原则**:
- 定期保存checkpoint：每轮迭代后自动保存agent状态
- 可恢复：从checkpoint恢复继续执行
- 超时检测：防止无限循环
- 失败隔离：单次失败不影响主流程

**关键方法**:

| 方法 | 输入 | 输出 | 触发时机 |
|------|------|------|---------|
| `start_session()` | session_id, metadata | 无 | fix_bug入口 |
| `save_checkpoint()` | session_id, agent_state, metadata | .state.json | 迭代结束 |
| `load_checkpoint()` | session_id | checkpoint dict | resume时 |
| `check_timeout()` | 无 | True/False | 每次迭代前 |
| `should_checkpoint()` | iteration | True/False | 判断是否保存 |
| `handle_failure()` | session_id, error, agent_state | None或恢复dict | 异常处理 |
| `get_session_stats()` | session_id | 统计字典 | 查询状态 |

**Checkpoint保存内容**:
```python
{
    "iteration": 当前迭代号,
    "feedback_status": 当前feedback状态,
    "feedback_hint": 反馈提示,
    "previous_code": 上次代码,
    "previous_error": 上次错误,
    "buggy_code": 原始bug代码,
    "bug_description": bug描述,
    "exec_success": 执行是否成功,
}
```

### 修改的现有类

#### `CoTReActAgent.__init__`
- **新增字段**:
  - `self.permission_gate = PermissionGate(...)`
  - `self.recovery_manager = RecoveryManager(...)`

#### `CoTReActAgent.fix_bug()`
- **新增**: 入口初始化 `recovery_manager.start_session()` 和 `permission_gate.set_session()`
- **新增**: 每次迭代前 `recovery_manager.check_timeout()` 超时检测
- **新增**: 每次迭代结束 `recovery_manager.save_checkpoint()` (每iteration保存)
- **改动**: 代码执行处包裹 `permission_gate.check()` 权限检查

---

## 代码规模统计

| 阶段 | 总行数 | 增量 |
|------|--------|------|
| Step0 (原始) | ~2500 | - |
| Step1 后 | ~2680 | +180 |
| Step2 后 | ~2981 | +301 |
| Step3 后 | ~3198 | +217 |
| Step4 后 | ~3611 | +413 |

## 备份文件清单

| 文件名 | 内容 |
|--------|------|
| `cot_react_agent.py.bak_20260516_step0` | 原始版本 |
| `cot_react_agent.py.bak_20260516_step1_before_compaction` | Step2前 |
| `cot_react_agent.py.bak_20260516_step2_before_queryloop` | Step3前 |
| `cot_react_agent.py.bak_20260516_step3_before_permission` | Step4前 |
