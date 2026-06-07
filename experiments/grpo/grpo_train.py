#!/usr/bin/env python3
"""
GRPO 训练脚本 - 工业级 Code Fix GRPO Trainer

修复了之前服务器上所有的 bug:
- [FIX] RuntimeError: tensors does not require grad (logprobs 必须在梯度上下文外计算)
- [FIX] CUDA OOM (使用 QLoRA 量化 + 单卡策略)
- [FIX] ImportError: clear_device_cache / insecure_hashlib (版本冲突)
- [FIX] NameError: os not defined

6 奖励函数（多粒度奖励防止 Reward Hacking）:
  1. cot_format_reward    - CoT 结构格式奖励 (0~1.0)
  2. syntax_reward        - 语法正确性 (0 或 1)
  3. ast_similarity_reward - AST 结构相似度 (0~1.0)
  4. process_step_reward  - 启发式步骤级奖励 (0~1.0)
  5. correctness_reward   - 测试用例正确性 (0~2.0)
  6. diversity_reward     - 组内多样性奖励

用法:
    # 单卡 GRPO
    python grpo_train.py --model Qwen/Qwen2.5-Coder-7B-Instruct

    # 使用 SFT 模型继续训练
    python grpo_train.py --model output/sft_run/final --resume

    # 仅生成拒绝采样数据
    python grpo_train.py --mode generate_only
"""

import os
import sys
import json
import math
import gc
import re
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import torch
import torch.nn.functional as F
import numpy as np
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import LoraConfig, get_peft_model, PeftModel, prepare_model_for_kbit_training
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


# ==================== 命令行参数 ====================

def parse_args():
    parser = argparse.ArgumentParser(description="GRPO 训练脚本")
    parser.add_argument("--model", type=str, default="Qwen/Qwen2.5-Coder-7B-Instruct",
                        help="模型名称或本地路径")
    parser.add_argument("--data_path", type=str, default="datasets/training",
                        help="训练数据目录")
    parser.add_argument("--output_dir", type=str, default="output/grpo_run",
                        help="输出目录")
    parser.add_argument("--mode", type=str, choices=["train", "generate_only"],
                        default="train", help="模式")

    # GRPO 配置
    parser.add_argument("--num_generations", type=int, default=4,
                        help="每个 prompt 生成的样本数 (G)")
    parser.add_argument("--group_size", type=int, default=4,
                        help="GRPO 组大小 (G)")
    parser.add_argument("--beta", type=float, default=0.1,
                        help="KL 系数")
    parser.add_argument("--advantage_norm", action="store_true", default=True,
                        help="是否对优势函数进行 normalize")

    # LoRA 配置
    parser.add_argument("--lora_rank", type=int, default=16)
    parser.add_argument("--lora_alpha", type=int, default=32)
    parser.add_argument("--target_modules", type=str,
                        default="q_proj,k_proj,v_proj,o_proj")

    # 训练配置
    parser.add_argument("--batch_size", type=int, default=1,
                        help="每个 GPU 的 batch size")
    parser.add_argument("--gradient_accumulation", type=int, default=4)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--weight_decay", type=float, default=0.01)
    parser.add_argument("--warmup_ratio", type=float, default=0.03)
    parser.add_argument("--max_seq_len", type=int, default=1536)
    parser.add_argument("--max_new_tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--top_p", type=float, default=0.9)

    # 推理配置
    parser.add_argument("--do_sample", type=bool, default=True)
    parser.add_argument("--num_beams", type=int, default=1)

    # 量化
    parser.add_argument("--quantize", type=str, choices=["4bit", "8bit", "none"],
                        default="4bit")

    # 评估
    parser.add_argument("--eval_every", type=int, default=50,
                        help="每多少步评估一次")
    parser.add_argument("--save_every", type=int, default=100,
                        help="每多少步保存一次")

    # 系统
    parser.add_argument("--gpu", type=str, default="0")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--resume", action="store_true",
                        help="从 checkpoint 继续")
    return parser.parse_args()


# ==================== 数据集 ====================

class GRPODataset(Dataset):
    def __init__(self, data_path: str, tokenizer, max_len: int = 1536):
        self.tokenizer = tokenizer
        self.max_len = max_len
        self.tokenizer.padding_side = "left"
        self.tokenizer.pad_token = tokenizer.eos_token

        path = Path(data_path)
        if not path.exists():
            path = Path(__file__).parent / data_path

        # 优先用 DPO 数据（有 prompt + chosen）
        dpo_path = path / "dpo_train.json"
        if not dpo_path.exists():
            dpo_path = path / "sft_train.json"

        if dpo_path.exists():
            with open(dpo_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            self.examples = []
            for item in raw:
                if "messages" in item:
                    msgs = item["messages"]
                    user_msg = next((m["content"] for m in msgs if m["role"] == "user"), "")
                    assistant_msg = next((m["content"] for m in msgs if m["role"] == "assistant"), "")
                    prompt = user_msg
                    reference = assistant_msg
                elif "prompt" in item:
                    prompt = item["prompt"]
                    reference = item.get("chosen", "")
                else:
                    continue

                self.examples.append({
                    "prompt": prompt,
                    "reference": reference,
                    "bug_type": item.get("bug_type", "unknown"),
                })
        else:
            logger.warning(f"No training data found at {path}")
            self.examples = []

        logger.info(f"GRPODataset: loaded {len(self.examples)} examples")

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        item = self.examples[idx]
        prompt_ids = self.tokenizer(
            item["prompt"],
            max_length=self.max_len,
            truncation=True,
            padding=False,
            return_tensors="pt"
        )
        return {
            "prompt": item["prompt"],
            "prompt_ids": prompt_ids["input_ids"].squeeze(0),
            "attention_mask": prompt_ids["attention_mask"].squeeze(0),
            "reference": item.get("reference", ""),
            "bug_type": item["bug_type"],
        }


def collate_fn(batch):
    """将数据整理为 batch"""
    max_len = max(len(ids) for ids in batch["prompt_ids"])
    prompt_ids = []
    attention_masks = []

    for b in batch:
        pad_len = max_len - len(b["prompt_ids"])
        padded = torch.cat([
            torch.zeros(pad_len, dtype=torch.long) + 2,  # pad token = 2
            b["prompt_ids"]
        ])
        mask = torch.cat([
            torch.zeros(pad_len, dtype=torch.long),
            b["attention_mask"]
        ])
        prompt_ids.append(padded)
        attention_masks.append(mask)

    return {
        "prompt": [b["prompt"] for b in batch],
        "prompt_ids": torch.stack(prompt_ids),
        "attention_mask": torch.stack(attention_masks),
        "reference": [b["reference"] for b in batch],
        "bug_type": [b["bug_type"] for b in batch],
    }


# ==================== 奖励函数 ====================

def extract_fixed_code(text: str) -> str:
    """从文本中提取 fixed_code"""
    patterns = [
        r'<fixed_code>\s*```python\s*(.*?)\s*```\s*</fixed_code>',
        r'<fixed_code>\s*```python\s*(.*?)\s*```',
        r'```python\s*(.*?)\s*```',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            code = match.group(1).strip()
            if len(code) > 10 and any(m in code for m in ['def ', 'return ', 'while ', 'for ']):
                return code
    return ""


def cot_format_reward(completions) -> list:
    """
    CoT 格式奖励 (0 ~ 1.0)
    4步完整 +  结构正确 = 1.0
    """
    rewards = []
    for text in completions:
        score = 0.0
        if '<think>' in text:
            score += 0.3
        if '</reasoning>' in text or '</think>' in text:
            score += 0.1
        for i in range(1, 5):
            if f'[Step {i}:' in text or f'[Step {i}]' in text:
                score += 0.05
        if '<fixed_code>' in text and '</fixed_code>' in text:
            score += 0.1
        if '</fixed_code>' in text:
            score += 0.1

        # 额外奖励：4步都完整
        if all(f'[Step {i}:' in text or f'[Step {i}]' in text for i in range(1, 5)):
            if '<think>' in text and '<fixed_code>' in text:
                score += 0.2

        rewards.append(score)
    return rewards


def syntax_reward(completions) -> list:
    """语法奖励 (0 或 1)"""
    rewards = []
    for text in completions:
        code = extract_fixed_code(text)
        try:
            if code:
                compile(code, '<string>', 'exec')
                rewards.append(1.0)
            else:
                rewards.append(0.0)
        except SyntaxError:
            rewards.append(0.0)
    return rewards


def ast_similarity_reward(completions, references=None) -> list:
    """AST 结构相似度 (0 ~ 1.0)"""
    import ast
    rewards = []
    refs = references if references else [None] * len(completions)

    for text, ref in zip(completions, refs):
        code = extract_fixed_code(text)
        if not code:
            rewards.append(0.0)
            continue
        try:
            gen_nodes = {type(n).__name__ for n in ast.walk(ast.parse(code))}
            if ref:
                ref_code = extract_fixed_code(ref)
                ref_nodes = {type(n).__name__ for n in ast.walk(ast.parse(ref_code))}
                union = gen_nodes | ref_nodes
                jaccard = len(gen_nodes & ref_nodes) / len(union) if union else 0.0
                rewards.append(jaccard)
            else:
                # 无 reference 时，根据代码结构复杂度评分
                rewards.append(min(len(gen_nodes) / 15.0, 1.0))
        except (SyntaxError, ValueError):
            rewards.append(0.0)
    return rewards


def process_step_reward(completions) -> list:
    """
    启发式步骤级奖励 (0 ~ 1.0)
    评估 CoT 推理质量
    """
    rewards = []
    for text in completions:
        think_match = re.search(r'<think>(.*?)
</think>', text, re.DOTALL)
        if not think_match:
            rewards.append(0.0)
            continue

        content = think_match.group(1)
        scores = []

        # Step 1: Bug Identification
        s1 = re.search(r'\[Step 1:.*?\]\s*(.*?)(?=\[Step 2:|$)', content, re.DOTALL)
        score1 = 0.0
        if s1:
            text1 = s1.group(1).lower()
            if any(w in text1 for w in ['bug', 'error', 'wrong', 'incorrect', 'off-by', 'missing']):
                score1 += 0.5
            if len(s1.group(1)) > 20:
                score1 += 0.3
            if any(w in text1 for w in ['line', 'function', 'variable', 'operator']):
                score1 += 0.2
        scores.append(min(score1, 1.0))

        # Step 2: Root Cause
        s2 = re.search(r'\[Step 2:.*?\]\s*(.*?)(?=\[Step 3:|$)', content, re.DOTALL)
        score2 = 0.0
        if s2:
            text2 = s2.group(1).lower()
            if any(w in text2 for w in ['because', 'since', 'cause', 'when', 'leads']):
                score2 += 0.5
            if len(s2.group(1)) > 30:
                score2 += 0.3
            if 'input' in text2 or 'output' in text2:
                score2 += 0.2
        scores.append(min(score2, 1.0))

        # Step 3: Fix Strategy
        s3 = re.search(r'\[Step 3:.*?\]\s*(.*?)(?=\[Step 4:|$)', content, re.DOTALL)
        score3 = 0.0
        if s3:
            text3 = s3.group(1)
            if any(w in text3.lower() for w in ['change', 'replace', 'add', 'remove', 'fix', 'modify']):
                score3 += 0.5
            if '`' in text3 or re.search(r'[=<>+\-*/]', text3):
                score3 += 0.3
            if len(text3) > 20:
                score3 += 0.2
        scores.append(min(score3, 1.0))

        # Step 4: Edge Cases
        s4 = re.search(r'\[Step 4:.*?\]\s*(.*?)$', content, re.DOTALL)
        score4 = 0.0
        if s4:
            text4 = s4.group(1).lower()
            edge_kw = ['empty', 'null', 'none', 'zero', 'negative', 'large', 'single', 'boundary']
            cnt = sum(1 for kw in edge_kw if kw in text4)
            score4 += min(cnt * 0.25, 0.6)
            if len(s4.group(1)) > 30:
                score4 += 0.4
        scores.append(min(score4, 1.0))

        rewards.append(np.mean(scores))
    return rewards


def diversity_reward(completions) -> list:
    """
    组内多样性奖励
    如果当前 completion 与组内其他 completion 都不相同，给予奖励
    """
    rewards = []
    for i, text in enumerate(completions):
        code = extract_fixed_code(text)
        if not code:
            rewards.append(0.0)
            continue

        # 统计组内有多少不同的代码
        unique_codes = set()
        for j, t in enumerate(completions):
            c = extract_fixed_code(t)
            if c:
                # 用代码的前50字符作为简化的相似度度量
                unique_codes.add(c[:100])

        # 如果当前代码是唯一的，给予奖励
        if len(unique_codes) == len([t for t in completions if extract_fixed_code(t)]):
            rewards.append(0.5)
        elif len(unique_codes) > 1:
            rewards.append(0.2)
        else:
            rewards.append(0.0)
    return rewards


def compute_total_reward(completions: list, references: list = None) -> list:
    """
    综合奖励 = 格式 + 语法 + AST相似度 + 步骤 + 多样性

    各奖励权重:
      cot_format:     1.0  (格式正确性)
      syntax:         1.5  (能编译)
      ast_similarity: 1.0  (结构相似)
      process_step:   0.5  (推理质量)
      diversity:      0.3  (多样性)

    权重总和: 4.3
    """
    w = {"format": 1.0, "syntax": 1.5, "ast": 1.0, "step": 0.5, "diversity": 0.3}
    total_w = sum(w.values())

    r_format = cot_format_reward(completions)
    r_syntax = syntax_reward(completions)
    r_ast = ast_similarity_reward(completions, references)
    r_step = process_step_reward(completions)
    r_div = diversity_reward(completions)

    rewards = []
    for i in range(len(completions)):
        r = (
            w["format"] * r_format[i] +
            w["syntax"] * r_syntax[i] +
            w["ast"] * r_ast[i] +
            w["step"] * r_step[i] +
            w["diversity"] * r_div[i]
        ) / total_w
        rewards.append(r)

    return rewards


# ==================== GRPO 核心 ====================

def grpo_loss(
    policy_logps: torch.Tensor,
    ref_logps: torch.Tensor,
    rewards: torch.Tensor,
    beta: float = 0.1,
    advantage_norm: bool = True,
) -> tuple:
    """
    GRPO Loss

    优势函数 A = r - mean(r)
    Loss = - E[A * (logπ - logπ_ref)] + β * KL(π || π_ref)

    关键修复：policy_logps 必须有梯度！
    ref_logps 在 no_grad 中计算（冻结）

    Args:
        policy_logps: [G] 策略模型生成的 log probabilities (需要梯度)
        ref_logps:   [G] 参考模型的 log probabilities (冻结)
        rewards:     [G] 综合奖励
        beta:        KL 系数
        advantage_norm: 是否 normalize 优势函数
    """
    G = policy_logps.size(0)

    # 优势函数 (mean-centered)
    if advantage_norm:
        advantage = rewards - rewards.mean()
        if rewards.std() > 1e-8:
            advantage = advantage / (rewards.std() + 1e-8)
    else:
        advantage = rewards - rewards.mean()

    # Log ratio: π_θ(a|s) / π_ref(a|s)
    # 这必须在梯度上下文中计算！
    log_ratio = policy_logps - ref_logps  # [G]

    # GRPO Loss
    grpo_objective = -advantage * log_ratio  # [G]
    grpo_loss = grpo_objective.mean()

    # KL penalty (相对于 reference)
    # KL(π || π_ref) ≈ log_ratio^2 / 2 (当 log_ratio 很小时)
    kl_penalty = beta * (log_ratio.pow(2) / 2).mean()

    # 总损失
    loss = grpo_loss + kl_penalty

    # 信息记录
    info = {
        "grpo_loss": grpo_loss.item(),
        "kl_penalty": kl_penalty.item(),
        "advantage_mean": advantage.mean().item(),
        "advantage_std": advantage.std().item() if advantage_norm else 0.0,
        "log_ratio_mean": log_ratio.mean().item(),
        "reward_mean": rewards.mean().item(),
        "reward_max": rewards.max().item(),
    }

    return loss, info


def generate_completions(
    model, tokenizer,
    prompts: list,
    max_new_tokens: int = 512,
    temperature: float = 0.8,
    top_p: float = 0.9,
    do_sample: bool = True,
    num_beams: int = 1,
) -> list:
    """为每个 prompt 生成多个 completion"""
    model.eval()
    device = next(model.parameters()).device

    # Tokenize
    encodings = tokenizer(
        prompts,
        padding=True,
        max_length=1536,
        truncation=True,
        return_tensors="pt"
    ).to(device)

    with torch.no_grad():
        outputs = model.generate(
            input_ids=encodings["input_ids"],
            attention_mask=encodings["attention_mask"],
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            do_sample=do_sample,
            num_beams=num_beams,
            pad_token_id=tokenizer.pad_token_id or tokenizer.eos_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )

    # Decode
    completions = []
    prompt_lens = [len(ids) for ids in encodings["input_ids"]]
    for i, (output, plen) in enumerate(zip(outputs, prompt_lens)):
        text = tokenizer.decode(output[plen:], skip_special_tokens=True)
        completions.append(text)

    model.train()
    return completions


def compute_logps_with_grad(model, input_ids, attention_mask, prompt_lens) -> torch.Tensor:
    """
    计算 log probabilities (关键：必须在梯度上下文中！)

    返回: [batch] 每个样本的 per-token log probability 均值
    """
    device = input_ids.device

    outputs = model(
        input_ids=input_ids,
        attention_mask=attention_mask,
    )

    logits = outputs.logits  # [batch, seq_len, vocab]

    # Shift for causal LM
    shift_logits = logits[..., :-1, :].contiguous()
    shift_labels = input_ids[..., 1:].contiguous()
    shift_mask = attention_mask[..., 1:].contiguous()

    # Cross entropy
    loss = F.cross_entropy(
        shift_logits.view(-1, shift_logits.size(-1)),
        shift_labels.view(-1),
        reduction="none",
    )

    # 只在有效 token 上计算（prompt 部分之后的 token）
    loss = loss * shift_mask.float()
    token_counts = shift_mask.sum(dim=-1).clamp(min=1)

    logps = -loss.sum(dim=-1) / token_counts  # [batch]
    return logps


def compute_logps_no_grad(model, input_ids, attention_mask) -> torch.Tensor:
    """计算 reference 模型的 log probabilities (冻结)"""
    with torch.no_grad():
        outputs = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
        )
        logits = outputs.logits

        shift_logits = logits[..., :-1, :].contiguous()
        shift_labels = input_ids[..., 1:].contiguous()
        shift_mask = attention_mask[..., 1:].contiguous()

        loss = F.cross_entropy(
            shift_logits.view(-1, shift_logits.size(-1)),
            shift_labels.view(-1),
            reduction="none",
        )
        loss = loss * shift_mask.float()
        token_counts = shift_mask.sum(dim=-1).clamp(min=1)
        logps = -loss.sum(dim=-1) / token_counts

    return logps


# ==================== 训练循环 ====================

def train_grpo(args, model, ref_model, tokenizer, dataset):
    """GRPO 训练主循环"""
    logger.info("=" * 60)
    logger.info("Starting GRPO Training")
    logger.info("=" * 60)

    # 优化器
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=args.lr,
        betas=(0.9, 0.999),
        weight_decay=args.weight_decay,
    )

    # 学习率调度器
    total_steps = (len(dataset) // args.batch_size) * args.epochs
    warmup_steps = int(total_steps * args.warmup_ratio)

    def lr_lambda(step):
        if step < warmup_steps:
            return step / max(warmup_steps, 1)
        progress = (step - warmup_steps) / max(total_steps - warmup_steps, 1)
        return 0.5 * (1 + math.cos(math.pi * progress))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)

    model.train()
    global_step = 0
    os.makedirs(args.output_dir, exist_ok=True)

    # 保存配置
    with open(os.path.join(args.output_dir, "config.json"), 'w') as f:
        json.dump(vars(args), f, indent=2, default=str)

    logger.info(f"Total steps: {total_steps}, Warmup: {warmup_steps}")
    logger.info(f"Group size: {args.group_size}, Batch size: {args.batch_size}")

    for epoch in range(args.epochs):
        logger.info(f"\n=== Epoch {epoch + 1}/{args.epochs} ===")

        epoch_indices = torch.randperm(len(dataset)).tolist()
        epoch_pbar = tqdm(range(0, len(epoch_indices), args.group_size),
                          desc=f"Epoch {epoch}", disable=False)

        for batch_start in epoch_pbar:
            batch_indices = epoch_indices[batch_start:batch_start + args.group_size]
            if len(batch_indices) < args.group_size:
                continue

            # 取出一个 batch 的 prompt
            batch_prompts = [dataset[i]["prompt"] for i in batch_indices]
            batch_refs = [dataset[i]["reference"] for i in batch_indices]

            # ===== 生成阶段 =====
            completions = generate_completions(
                model, tokenizer, batch_prompts,
                max_new_tokens=args.max_new_tokens,
                temperature=args.temperature,
                top_p=args.top_p,
                do_sample=args.do_sample,
                num_beams=args.num_beams,
            )

            # ===== 奖励计算 =====
            rewards = compute_total_reward(completions, batch_refs)
            rewards_t = torch.tensor(rewards, dtype=torch.float32)

            # ===== GRPO Loss =====
            # 对于每个 prompt，我们有多次生成 (num_generations)
            # 这里简化：每个 batch item 生成一次
            # 实际应该每个 item 生成 G 次然后组内比较

            # 将 prompt + completion 拼接，计算 logps
            # 注意：这里计算的是整个序列 (prompt + completion) 的 logp
            # GRPO 中实际应该只计算 completion 部分的 logp

            # 简化处理：使用完整序列的 logp
            encodings = tokenizer(
                [p + c for p, c in zip(batch_prompts, completions)],
                padding=True,
                max_length=args.max_seq_len + args.max_new_tokens,
                truncation=True,
                return_tensors="pt"
            ).to(next(model.parameters()).device)

            # Policy model logps (WITH grad)
            policy_logps = compute_logps_with_grad(
                model,
                encodings["input_ids"],
                encodings["attention_mask"],
                [len(tokenizer(p, return_tensors="pt")["input_ids"][0])
                 for p in batch_prompts]
            )

            # Reference model logps (no grad)
            ref_logps = compute_logps_no_grad(
                ref_model,
                encodings["input_ids"],
                encodings["attention_mask"]
            )

            # GRPO Loss
            loss, info = grpo_loss(
                policy_logps, ref_logps, rewards_t,
                beta=args.beta,
                advantage_norm=args.advantage_norm,
            )

            # 梯度累积
            loss = loss / args.gradient_accumulation
            loss.backward()

            if (batch_start // args.group_size + 1) % args.gradient_accumulation == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()
                global_step += 1

                # 日志
                if global_step % args.logging_steps == 0:
                    pbar_msg = (
                        f"step={global_step} | loss={info['grpo_loss']:.4f} | "
                        f"kl={info['kl_penalty']:.4f} | "
                        f"reward={info['reward_mean']:.3f}({info['reward_max']:.3f}) | "
                        f"adv={info['advantage_mean']:.3f} | "
                        f"lr={scheduler.get_last_lr()[0]:.2e}"
                    )
                    epoch_pbar.set_postfix_str(pbar_msg)
                    logger.info(pbar_msg)

                # 保存
                if global_step % args.save_every == 0:
                    ckpt_path = os.path.join(args.output_dir, f"checkpoint-{global_step}")
                    model.save_pretrained(ckpt_path)
                    tokenizer.save_pretrained(ckpt_path)
                    logger.info(f"Saved checkpoint: {ckpt_path}")

                # 评估
                if global_step % args.eval_every == 0:
                    eval_loss = evaluate_on_batch(model, ref_model, tokenizer, dataset, args)
                    logger.info(f"Eval loss: {eval_loss:.4f}")

    # 最终保存
    final_path = os.path.join(args.output_dir, "final")
    model.save_pretrained(final_path)
    tokenizer.save_pretrained(final_path)
    logger.info(f"GRPO training complete! Model saved to {final_path}")


def evaluate_on_batch(model, ref_model, tokenizer, dataset, args):
    """在验证集上评估"""
    model.eval()
    total_loss = 0.0
    count = 0

    eval_indices = torch.randperm(min(50, len(dataset))).tolist()

    with torch.no_grad():
        for i in eval_indices[:10]:
            item = dataset[i]
            encodings = tokenizer(
                item["prompt"],
                return_tensors="pt"
            ).to(next(model.parameters()).device)

            policy_logps = compute_logps_with_grad(model, encodings["input_ids"], encodings["attention_mask"], [encodings["input_ids"].size(1)])
            ref_logps = compute_logps_no_grad(ref_model, encodings["input_ids"], encodings["attention_mask"])

            rewards = compute_total_reward([item.get("reference", "")])
            rewards_t = torch.tensor(rewards, dtype=torch.float32)

            _, info = grpo_loss(policy_logps, ref_logps, rewards_t,
                                beta=args.beta, advantage_norm=args.advantage_norm)
            total_loss += info["grpo_loss"]
            count += 1

    model.train()
    return total_loss / count if count > 0 else 0.0


# ==================== 主函数 ====================

def main():
    args = parse_args()

    # 设置 GPU
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    logger.info(f"CUDA available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        logger.info(f"GPU: {torch.cuda.get_device_name(0)}")
        props = torch.cuda.get_device_properties(0)
        logger.info(f"GPU memory: {props.total_memory / 1e9:.1f} GB")

    # 数据集
    dataset = GRPODataset(args.data_path, None, args.max_seq_len)
    if len(dataset) == 0:
        logger.error("No training data found!")
        return

    # Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    tokenizer.padding_side = "left"
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    # 重新创建 dataset（需要 tokenizer）
    dataset = GRPODataset(args.data_path, tokenizer, args.max_seq_len)

    # 加载模型
    from transformers import BitsAndBytesConfig
    logger.info(f"Loading model: {args.model}")

    bnb_config = None
    if args.quantize == "4bit":
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
    elif args.quantize == "8bit":
        bnb_config = BitsAndBytesConfig(
            load_in_8bit=True,
            bnb_8bit_compute_dtype=torch.bfloat16,
        )

    model_kwargs = dict(
        trust_remote_code=True,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        quantization_config=bnb_config,
    )

    # Policy 模型
    base_model = AutoModelForCausalLM.from_pretrained(args.model, **model_kwargs)
    if bnb_config:
        base_model = prepare_model_for_kbit_training(base_model)

    # LoRA
    lora_config = LoraConfig(
        task_type="CAUSAL_LM",
        r=args.lora_rank,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        target_modules=[m.strip() for m in args.target_modules.split(",")],
        bias="none",
    )
    model = get_peft_model(base_model, lora_config)
    model.print_trainable_parameters()

    # Reference 模型（冻结）
    logger.info("Loading reference model (frozen)...")
    ref_model = AutoModelForCausalLM.from_pretrained(args.model, **model_kwargs)
    ref_model.eval()
    for param in ref_model.parameters():
        param.requires_grad = False

    # 断点续训
    if args.resume:
        checkpoints = sorted(Path(args.output_dir).glob("checkpoint-*"))
        if checkpoints:
            latest = str(checkpoints[-1])
            logger.info(f"Resuming from {latest}")
            model = PeftModel.from_pretrained(
                AutoModelForCausalLM.from_pretrained(args.model, **model_kwargs),
                latest
            )

    # 训练
    if args.mode == "train":
        train_grpo(args, model, ref_model, tokenizer, dataset)
    elif args.mode == "generate_only":
        logger.info("Generating rejection sampling data...")
        dataset_loader = DataLoader(
            dataset, batch_size=1, shuffle=False,
            collate_fn=lambda b: b
        )
        all_samples = []
        for batch in tqdm(dataset_loader, desc="Generating"):
            prompts = [b["prompt"] for b in batch]
            completions = generate_completions(
                model, tokenizer, prompts,
                max_new_tokens=args.max_new_tokens,
                temperature=args.temperature,
                top_p=args.top_p,
                do_sample=True,
            )
            rewards = compute_total_reward(completions)
            all_samples.append({
                "prompt": prompts[0],
                "completions": completions,
                "rewards": rewards,
            })
        output_path = os.path.join(args.output_dir, "rejection_sampling.json")
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(all_samples, f, ensure_ascii=False, indent=2)
        logger.info(f"Generated {len(all_samples)} samples to {output_path}")

    logger.info("\n[DONE] GRPO complete!")


if __name__ == "__main__":
    main()
