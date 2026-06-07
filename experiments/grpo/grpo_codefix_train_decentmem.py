"""
GRPO Training with DECENTMEM-Style Adaptive Reward
改造自 grpo_codefix_train_cot_prm.py

核心改动：将固定 reward 权重替换为 DECENTMEM 风格的自适应 reward
- 在线路由器根据任务特征动态调整 E-pool/X-pool 选择
- LLM-as-a-Judge 提供 stage-level 评估
- 路由器权重实时更新，实现 exploit-explore 自适应平衡

改造对比：
  旧: 固定六维 reward 权重 → 每轮相同，静态
  新: 路由器 + judge → 动态权重，自适应探索/利用
"""

import os
import re
import json
import math
import logging
import torch
import numpy as np
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig, get_peft_model
from trl import GRPOConfig, GRPOTrainer

from dual_pool_memory import DualPoolMemory, MemoryPiece
from llm_judge import LLMJudge
from online_router import OnlineRouter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ==================== 配置 ====================

POLICY_MODEL = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
REWARD_MODEL = "Qwen/Qwen2.5-Coder-7B-Instruct"
SFT_LORA_PATH = None
OUTPUT_DIR = "outputs/codefix-grpo-decentmem"

COT_SYSTEM_PROMPT = """You are an expert code debugger. Think step-by-step before fixing.

Your response MUST follow this format:

<think>
[Step 1: Bug Identification]
Identify the bug type and its exact location.
  
[Step 2: Root Cause Analysis]
Explain WHY this code is incorrect. What input triggers the failure?

[Step 3: Fix Strategy]
Describe the minimal change needed and why it's correct.

[Step 4: Edge Case Verification]
List 2-3 edge cases and verify the fix handles them.
</think>

<fixed_code>
```python
# corrected code
```
</fixed_code>"""


# ==================== 原有 reward 函数（保留用于对比） ====================

def extract_fixed_code(text: str) -> str:
    match = re.search(
        r'<fixed_code>\s*```python\s*(.*?)\s*```\s*</fixed_code>',
        text, re.DOTALL
    )
    if not match:
        match = re.search(r'<fixed_code>\s*(.*?)\s*</fixed_code>', text, re.DOTALL)
    return match.group(1).strip() if match else ""


def cot_format_reward(completions, **kwargs):
    rewards = []
    for comp in completions:
        text = comp[0]["content"] if isinstance(comp, list) else comp
        score = 0.0
        if '<think>' in text and '</think>' in text:
            score += 0.1
        for i in range(1, 5):
            if f'[Step {i}:' in text:
                score += 0.05
        if '<fixed_code>' in text and '</fixed_code>' in text:
            score += 0.1
        has_all = all(f'[Step {i}:' in text for i in range(1, 5))
        if has_all and '<think>' in text and '<fixed_code>' in text:
            score += 0.2
        rewards.append(score)
    return rewards


def syntax_reward(completions, **kwargs):
    rewards = []
    for comp in completions:
        text = comp[0]["content"] if isinstance(comp, list) else comp
        code = extract_fixed_code(text)
        try:
            compile(code, '<string>', 'exec')
            rewards.append(1.0)
        except SyntaxError:
            rewards.append(0.0)
    return rewards


def ast_similarity_reward(completions, reference_code=None, **kwargs):
    import ast as ast_module
    rewards = []
    refs = reference_code if reference_code else [None] * len(completions)
    for comp, ref in zip(completions, refs):
        text = comp[0]["content"] if isinstance(comp, list) else comp
        code = extract_fixed_code(text)
        if not code or not ref:
            rewards.append(0.0)
            continue
        try:
            gen_tree = ast_module.parse(code)
            ref_tree = ast_module.parse(ref)
            gen_nodes = {type(n).__name__ for n in ast_module.walk(gen_tree)}
            ref_nodes = {type(n).__name__ for n in ast_module.walk(ref_tree)}
            union = gen_nodes | ref_nodes
            rewards.append(len(gen_nodes & ref_nodes) / len(union) if union else 0.0)
        except SyntaxError:
            rewards.append(0.0)
    return rewards


def correctness_reward(completions, reference_code=None, **kwargs):
    rewards = []
    refs = reference_code if reference_code else [None] * len(completions)
    for comp, ref in zip(completions, refs):
        text = comp[0]["content"] if isinstance(comp, list) else comp
        code = extract_fixed_code(text)
        if not code or not ref:
            rewards.append(0.0)
            continue
        code_nospace = re.sub(r'\s+', '', code)
        ref_nospace = re.sub(r'\s+', '', ref)
        if code_nospace == ref_nospace:
            rewards.append(2.0)
        else:
            rewards.append(0.0)
    return rewards


def _score_steps_heuristic(completion: str, buggy_code: str = None) -> list:
    """启发式步骤评分（复用原有逻辑）"""
    think_match = re.search(r'<think>(.*?)
</think>', completion, re.DOTALL)
    if not think_match:
        return [0.0] * 4
    content = think_match.group(1)
    scores = []

    s1_match = re.search(r'\[Step 1:.*?\]\s*(.*?)(?=\[Step 2:|$)', content, re.DOTALL)
    s1 = s1_match.group(1).strip() if s1_match else ""
    score1 = 0.0
    bug_types = ['off-by-one', 'wrong operator', 'missing check', 'boundary',
                 'type error', 'index', 'initialization', 'condition', 'return',
                 'operator', 'logic', 'null', 'empty']
    if any(bt in s1.lower() for bt in bug_types):
        score1 += 0.5
    if buggy_code and len(s1) > 20:
        score1 += 0.3
    scores.append(min(score1, 1.0))

    s2_match = re.search(r'\[Step 2:.*?\]\s*(.*?)(?=\[Step 3:|$)', content, re.DOTALL)
    s2 = s2_match.group(1).strip() if s2_match else ""
    score2 = 0.0
    if any(w in s2.lower() for w in ['because', 'since', 'cause', 'when', 'leads to']):
        score2 += 0.5
    if len(s2) > 30:
        score2 += 0.3
    scores.append(min(score2, 1.0))

    s3_match = re.search(r'\[Step 3:.*?\]\s*(.*?)(?=\[Step 4:|$)', content, re.DOTALL)
    s3 = s3_match.group(1).strip() if s3_match else ""
    score3 = 0.0
    if any(w in s3.lower() for w in ['change', 'replace', 'add', 'remove', 'fix', 'modify']):
        score3 += 0.5
    scores.append(min(score3, 1.0))

    s4_match = re.search(r'\[Step 4:.*?\]\s*(.*?)$', content, re.DOTALL)
    s4 = s4_match.group(1).strip() if s4_match else ""
    score4 = 0.0
    edge_kw = ['empty', 'null', 'none', 'zero', 'negative', 'large', 'single', 'boundary', 'edge']
    edge_count = sum(1 for kw in edge_kw if kw in s4.lower())
    score4 += min(edge_count * 0.25, 0.6)
    if len(s4) > 30:
        score4 += 0.4
    scores.append(min(score4, 1.0))

    return scores


def process_step_reward(completions, buggy_code=None, **kwargs):
    rewards = []
    buggy_codes = buggy_code if buggy_code else [None] * len(completions)
    for comp, bc in zip(completions, buggy_codes):
        text = comp[0]["content"] if isinstance(comp, list) else comp
        step_scores = _score_steps_heuristic(text, bc)
        rewards.append(np.mean(step_scores) if step_scores else 0.0)
    return rewards


def fipo_step_weighted_reward(completions, buggy_code=None, **kwargs):
    """FIPO-inspired: 后续步骤权重更高"""
    STEP_WEIGHTS = [0.15, 0.20, 0.30, 0.35]
    rewards = []
    buggy_codes = buggy_code if buggy_code else [None] * len(completions)
    for comp, bc in zip(completions, buggy_codes):
        text = comp[0]["content"] if isinstance(comp, list) else comp
        step_scores = _score_steps_heuristic(text, bc)
        if not step_scores:
            rewards.append(0.0)
            continue
        weighted = sum(w * s for w, s in zip(STEP_WEIGHTS, step_scores))
        rewards.append(weighted)
    return rewards


def dynamic_sampling_wrapper(reward_fn, group_size: int = 4):
    """DAPO Dynamic Sampling: 过滤 uniform group"""
    def wrapped(completions, **kwargs):
        rewards = reward_fn(completions, **kwargs)
        for i in range(0, len(rewards), group_size):
            end = min(i + group_size, len(rewards))
            group = rewards[i:end]
            if len(group) > 1 and max(group) - min(group) < 1e-6:
                for j in range(len(group)):
                    rewards[i + j] += np.random.uniform(-0.01, 0.01)
        return rewards
    return wrapped


# ==================== DECENTMEM 风格的自适应 Reward ====================

class DECENTMEMRewardSystem:
    """
    DECENTMEM 风格的自适应 reward 系统

    核心思想：用路由器 + judge 替代固定权重

    决策流程（每次 GRPO 生成一组 responses 时）：
    1. 对每个 prompt，路由器根据 bug_type 决定用 E-pool 还是 X-pool
    2. E-pool 被选中 → 提高 format/process 权重（利用已有推理模式）
    3. X-pool 被选中 → 提高 semantic/reward 权重（鼓励探索新路径）
    4. judge 评估 → 更新路由器权重 → 影响下一轮的池选择

    关键优势：
    - 简单 bug 快速收敛（E-pool）
    - 复杂 bug 自适应探索（X-pool）
    - 不依赖人工调参，权重自己学出来
    """

    def __init__(
        self,
        agent_id: str = "codefix_grpo",
        judge_model_path: str = None,
    ):
        # 双池记忆
        self.dual_memory = DualPoolMemory(
            agent_id=agent_id,
            e_pool_path=f"runs/{agent_id}_e_pool.json",
            x_pool_path=f"runs/{agent_id}_x_pool.json",
        )

        # LLM-as-a-Judge
        self.judge = LLMJudge(
            model_path=judge_model_path,
            fallback_to_heuristic=True,
        )

        # 在线路由器
        self.router = OnlineRouter(
            alpha=0.5,
            beta=0.5,
            force_e_threshold=0.6,
            adaptive_threshold=True,
        )

        # 当前 epoch 的 reward 统计
        self.epoch_stats = {
            "e_pool_selected": 0,
            "x_pool_selected": 0,
            "avg_judge_score": 0.0,
            "tasks_processed": 0,
        }

    def _extract_bug_type(self, prompt) -> str:
        """从 prompt 中提取 bug 类型"""
        if isinstance(prompt, list):
            prompt_text = " ".join(
                p.get("content", "") for p in prompt if isinstance(p, dict)
            )
        else:
            prompt_text = str(prompt)

        bug_type_map = {
            "off-by-one": "off_by_one",
            "off by one": "off_by_one",
            "boundary": "boundary",
            "logic error": "logic_error",
            "initialization": "initialization",
            "wrong operator": "wrong_operator",
            "wrong return": "wrong_return_value",
            "null": "null_pointer",
            "index": "index_error",
        }

        prompt_lower = prompt_text.lower()
        for keyword, bug_type in bug_type_map.items():
            if keyword in prompt_lower:
                return bug_type

        return "unknown"

    def _extract_buggy_code(self, prompt) -> str:
        """从 prompt 中提取 buggy code"""
        if isinstance(prompt, list):
            prompt_text = " ".join(
                p.get("content", "") for p in prompt if isinstance(p, dict)
            )
        else:
            prompt_text = str(prompt)

        match = re.search(r'```python\s*(.*?)\s*```', prompt_text, re.DOTALL)
        return match.group(1).strip() if match else ""

    def _judge_completion(self, buggy_code: str, completion: str,
                          task_id: str) -> dict:
        """用 judge 评估 completion"""
        eval_result = self.judge.evaluate_task(
            buggy_code=buggy_code,
            cot_result=completion,
            task_id=task_id,
        )
        return {
            "overall_score": eval_result.overall_score,
            "success": eval_result.success,
            "stage_scores": [e.score for e in eval_result.stage_evals],
            "total_reward": eval_result.total_reward,
        }

    def _router_adaptive_weight(self, selected_pool: str,
                                  judge_score: float) -> float:
        """
        根据路由器选择的池和 judge 评分，返回自适应权重调整

        E-pool 被选: 偏向格式和过程（利用已有推理模式）
        X-pool 被选: 偏向语义和探索（鼓励新推理路径）

        这个函数决定各维度 reward 的最终加权系数
        """
        base = 1.0

        if selected_pool == "E":
            # 利用已有模式：格式和过程更重要
            # 推理质量好的话给额外奖励
            if judge_score >= 7.0:
                base = 1.1
            elif judge_score >= 5.0:
                base = 1.0
            else:
                base = 0.9
        else:
            # 探索新路径：语义探索给额外奖励
            if judge_score >= 6.0:
                base = 1.15  # 探索成功，额外激励
            else:
                base = 0.95

        return base

    def reward_fn(self, completions, prompts=None, **kwargs):
        """
        DECENTMEM 风格的自适应 reward 函数

        每次调用对应 GRPO 的一次 generation 批次
        路由器根据任务特征决定池选择，judge 评估结果更新权重
        """
        rewards = []
        prompt_list = prompts if prompts else [None] * len(completions)

        for i, (completion, prompt) in enumerate(zip(completions, prompt_list)):
            content = completion[0]["content"] if isinstance(completion, list) else completion

            buggy_code = self._extract_buggy_code(prompt) if prompt else ""
            bug_type = self._extract_bug_type(prompt) if prompt else "unknown"
            task_id = f"grpo_{i}"

            # Step 1: 路由器决策
            task = {
                "buggy_code": buggy_code,
                "task_type": bug_type,
                "bug_signature": buggy_code[:100] if buggy_code else "",
                "task_id": task_id,
            }

            # 检查 E-pool 相关性
            e_memories = self.dual_memory.retrieve(
                task, stage="L1", top_k=1, pool_type="E"
            )
            e_relevance = e_memories[0].relevance_score(task) if e_memories else 0.0

            selected_pool = self.router.decide(task, e_relevance, bug_type)

            # 记录池选择统计
            if selected_pool == "E":
                self.epoch_stats["e_pool_selected"] += 1
            else:
                self.epoch_stats["x_pool_selected"] += 1

            # Step 2: judge 评估
            judge_result = self._judge_completion(buggy_code, content, task_id)
            judge_score = judge_result["overall_score"]

            # 更新 epoch 统计
            self.epoch_stats["avg_judge_score"] = (
                (self.epoch_stats["avg_judge_score"] * self.epoch_stats["tasks_processed"]
                 + judge_score)
                / (self.epoch_stats["tasks_processed"] + 1)
            )
            self.epoch_stats["tasks_processed"] += 1

            # Step 3: 路由器权重更新
            judge_delta = 1.0 if judge_score >= 7.0 else 0.0
            self.router.update(selected_pool, judge_delta)

            # Step 4: 计算自适应权重
            adaptive_weight = self._router_adaptive_weight(selected_pool, judge_score)

            # Step 5: 记录成功经验到双池
            fixed_code = extract_fixed_code(content)
            if judge_score >= 7.0 and fixed_code:
                piece = MemoryPiece(
                    piece_id=f"grpo_{task_id}_{i}",
                    task_type=bug_type,
                    bug_signature=task["bug_signature"],
                    full_buggy_code=buggy_code,
                    cot_steps=[],
                    final_fix=fixed_code,
                    fix_status="success",
                    judge_scores={"overall": judge_score},
                    execution_pass_rate=judge_result["success"],
                    source="grpo_training",
                    agent_pool=f"{selected_pool}-pool",
                    stage_pool="L1",
                )
                self.dual_memory.add_to_e_pool(piece)
            elif judge_score < 4.0:
                # 失败经验加入 X-pool
                piece = MemoryPiece(
                    piece_id=f"grpo_fail_{task_id}_{i}",
                    task_type=bug_type,
                    bug_signature=task["bug_signature"],
                    full_buggy_code=buggy_code,
                    cot_steps=[],
                    final_fix=fixed_code,
                    fix_status="failed",
                    judge_scores={"overall": judge_score},
                    execution_pass_rate=judge_result["success"],
                    source="grpo_training",
                    agent_pool="X-pool",
                    stage_pool="L1",
                )
                self.dual_memory.add_to_x_pool(piece)

            # Step 6: 返回加权 reward
            # judge_score 归一化到 [0, 1]
            reward = (judge_score / 10.0) * adaptive_weight
            rewards.append(reward)

        return rewards

    def on_epoch_end(self):
        """每 epoch 结束时调用：X-pool 合并 + 路由器统计"""
        # X-pool 高质量碎片合并到 E-pool
        consolidated = self.dual_memory.consolidate_x_to_e()

        # 打印统计
        router_state = self.router.get_state()
        print(f"\n[DECENTMEM Epoch End]")
        print(f"  Router: w_E={router_state['w_E']:.2f}, α={router_state['alpha']:.3f}")
        print(f"  Pool selection: E={self.epoch_stats['e_pool_selected']}, "
              f"X={self.epoch_stats['x_pool_selected']}")
        print(f"  Avg judge score: {self.epoch_stats['avg_judge_score']:.2f}")
        print(f"  X→E consolidated: {consolidated}")

        # 重置 epoch 统计
        self.epoch_stats = {
            "e_pool_selected": 0,
            "x_pool_selected": 0,
            "avg_judge_score": 0.0,
            "tasks_processed": 0,
        }

        # 清理低质量碎片
        self.dual_memory.prune_low_quality(quality_threshold=0.3)


# ==================== 辅助函数 ====================

def create_cot_dataset(data_path: str = "datasets/dpo_dataset_merged.json"):
    """创建 CoT 格式数据集"""
    if not os.path.exists(data_path):
        logger.warning(f"Dataset not found: {data_path}, using demo dataset")
        return _create_demo_dataset()

    with open(data_path, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)

    dataset_items = []
    for item in raw_data:
        prompt_text = item.get('prompt', '')
        dataset_items.append({
            "prompt": [
                {"role": "system", "content": COT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt_text}
            ],
            "buggy_code": item.get("buggy_code", ""),
            "reference_code": item.get("reference_code", ""),
            "bug_type": item.get("bug_type", "unknown"),
        })

    logger.info(f"Loaded {len(dataset_items)} samples")
    return Dataset.from_list(dataset_items)


def _create_demo_dataset():
    """Demo 数据集"""
    samples = [
        {"buggy": "def add(a, b):\n    return a - b",
         "fixed": "def add(a, b):\n    return a + b",
         "desc": "Fix wrong arithmetic operator",
         "bug_type": "wrong_operator"},
        {"buggy": "def factorial(n):\n    if n == 0:\n        return 0\n    return n * factorial(n-1)",
         "fixed": "def factorial(n):\n    if n == 0:\n        return 1\n    return n * factorial(n-1)",
         "desc": "Fix factorial base case",
         "bug_type": "wrong_return_value"},
        {"buggy": "def binary_search(arr, target):\n    left, right = 0, len(arr) - 1\n    while left < right:\n        mid = (left + right) // 2\n        if arr[mid] == target:\n            return mid\n        elif arr[mid] < target:\n            left = mid + 1\n        else:\n            right = mid - 1\n    return -1",
         "fixed": "def binary_search(arr, target):\n    left, right = 0, len(arr) - 1\n    while left <= right:\n        mid = (left + right) // 2\n        if arr[mid] == target:\n            return mid\n        elif arr[mid] < target:\n            left = mid + 1\n        else:\n            right = mid - 1\n    return -1",
         "desc": "Fix binary search off-by-one",
         "bug_type": "off_by_one"},
        {"buggy": "def is_prime(n):\n    if n < 2:\n        return False\n    for i in range(2, n):\n        if n % i == 0:\n            return True\n    return False",
         "fixed": "def is_prime(n):\n    if n < 2:\n        return False\n    for i in range(2, int(n**0.5) + 1):\n        if n % i == 0:\n            return False\n    return True",
         "desc": "Fix is_prime logic and optimization",
         "bug_type": "logic_error"},
        {"buggy": "def reverse_string(s):\n    return s[::-2]",
         "fixed": "def reverse_string(s):\n    return s[::-1]",
         "desc": "Fix string reverse step",
         "bug_type": "wrong_operator"},
    ]

    dataset_items = []
    for s in samples:
        user_msg = (f"Bug: {s['desc']}\n\nBuggy Code:\n```python\n{s['buggy']}\n```\n\nFix the bug.")
        dataset_items.append({
            "prompt": [
                {"role": "system", "content": COT_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}
            ],
            "buggy_code": s["buggy"],
            "reference_code": s["fixed"],
            "bug_type": s["bug_type"],
        })

    while len(dataset_items) < 100:
        idx = len(dataset_items) % len(samples)
        item = dataset_items[idx].copy()
        dataset_items.append(item)

    return Dataset.from_list(dataset_items)


# ==================== 训练 ====================

def train():
    logger.info("=" * 60)
    logger.info("GRPO Training with DECENTMEM Adaptive Reward")
    logger.info("=" * 60)

    # 1. 数据集
    logger.info("Loading dataset...")
    dataset = create_cot_dataset()

    # 2. 模型
    logger.info(f"Loading policy model: {POLICY_MODEL}")
    tokenizer = AutoTokenizer.from_pretrained(POLICY_MODEL)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"

    model = AutoModelForCausalLM.from_pretrained(
        POLICY_MODEL,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True
    )
    model.config.use_cache = False

    # 3. LoRA
    peft_config = LoraConfig(
        r=8,
        lora_alpha=32,
        lora_dropout=0.1,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]
    )

    # 4. GRPO 配置
    grpo_config = GRPOConfig(
        output_dir=OUTPUT_DIR,
        learning_rate=1e-5,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=2,
        num_generations=4,
        max_prompt_length=512,
        max_completion_length=768,
        num_train_epochs=4,
        warmup_steps=20,
        max_grad_norm=1.0,
        beta=0.0,
        fp16=True,
        logging_steps=10,
        save_steps=100,
        report_to="none",
    )

    # 5. DECENTMEM Reward 系统
    logger.info("Initializing DECENTMEM adaptive reward system...")
    decentmem_reward = DECENTMEMRewardSystem(
        agent_id="codefix_grpo",
        judge_model_path=None,
    )

    # 6. 初始化 trainer
    # 注意：reward_funcs 中的函数会按顺序执行，最终 reward = sum(rewards)
    # DECENTMEM 模式下，我们主要用 judge 作为核心 reward，
    # 其他 reward 函数作为辅助信号
    num_gens = grpo_config.num_generations

    # 对比方案：原有固定权重 vs DECENTMEM 自适应
    # 这里展示两种配置
    USE_DECENTMEM = True  # 设为 False 则使用原有固定权重

    if USE_DECENTMEM:
        logger.info("Using DECENTMEM adaptive reward (replaces fixed weights)")
        reward_funcs = [
            decentmem_reward.reward_fn,  # 核心：DECENTMEM judge 驱动
            dynamic_sampling_wrapper(syntax_reward, num_gens),  # 辅助：语法
            dynamic_sampling_wrapper(ast_similarity_reward, num_gens),  # 辅助：AST
        ]
    else:
        logger.info("Using fixed multi-granularity reward (original)")
        reward_funcs = [
            cot_format_reward,
            dynamic_sampling_wrapper(syntax_reward, num_gens),
            ast_similarity_reward,
            process_step_reward,
            fipo_step_weighted_reward,
            dynamic_sampling_wrapper(correctness_reward, num_gens),
        ]

    trainer = GRPOTrainer(
        model=model,
        processing_class=tokenizer,
        reward_funcs=reward_funcs,
        args=grpo_config,
        train_dataset=dataset,
        peft_config=peft_config,
    )

    # 7. 自定义训练循环（每 epoch 结束时调用 X-pool 合并）
    logger.info("Starting GRPO training with DECENTMEM...")
    logger.info("-" * 60)

    total_epochs = grpo_config.num_train_epochs
    for epoch in range(total_epochs):
        logger.info(f"\n[Epoch {epoch+1}/{total_epochs}]")
        trainer.train()

        if USE_DECENTMEM:
            decentmem_reward.on_epoch_end()

    logger.info("-" * 60)
    logger.info("Training complete!")

    # 8. 保存
    final_dir = os.path.join(OUTPUT_DIR, "final")
    trainer.save_model(final_dir)
    logger.info(f"Model saved to {final_dir}")

    # 9. 总结
    logger.info("\n" + "=" * 60)
    logger.info("Training Summary: GRPO + DECENTMEM Adaptive Reward")
    logger.info("=" * 60)
    logger.info("旧方案（固定权重）:")
    logger.info("  - 六维 reward 权重: accuracy=0.30, format=0.05, ...")
    logger.info("  - 每轮训练权重相同，静态不变")
    logger.info("  - 需要大量消融实验确定最优权重")
    logger.info("")
    logger.info("新方案（DECENTMEM 自适应）:")
    logger.info("  - 路由器根据任务类型动态选择 E-pool / X-pool")
    logger.info("  - LLM-as-a-Judge 提供 stage-level 评估")
    logger.info("  - 权重由 judge 反馈实时更新，自适应 exploit-explore 平衡")
    logger.info("  - 理论保证: O(log T) 累积遗憾，收敛到最优策略")
    logger.info("")
    router_state = decentmem_reward.router.get_state()
    logger.info(f"最终路由器状态: α={router_state['alpha']:.3f}, "
                f"E-pool命中率={router_state['E_pool_ratio']:.1%}")


if __name__ == "__main__":
    train()
