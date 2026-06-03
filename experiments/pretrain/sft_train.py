#!/usr/bin/env python3
"""
SFT 训练脚本 - QLoRA 微调 qwen2.5-coder-7B-Instruct

功能:
- SFT (Supervised Fine-Tuning) + DPO (Direct Preference Optimization)
- QLoRA: 4-bit NF4 量化 + LoRA (rank=16, alpha=32)
- 支持断点续训
- 6xA100 / 单卡 A100 / 多卡自动适配

用法:
    # 单卡 SFT
    python sft_train.py --model Qwen/Qwen2.5-Coder-7B-Instruct --mode sft

    # 单卡 DPO
    python sft_train.py --model Qwen/Qwen2.5-Coder-7B-Instruct --mode dpo

    # 断点续训
    python sft_train.py --resume_from output/sft_run/checkpoint-100

    # 完整参数
    python sft_train.py \
        --model Qwen/Qwen.5-Coder-7B-Instruct \
        --mode sft \
        --data_path datasets/training \
        --output_dir output/sft_run \
        --lora_rank 16 \
        --lora_alpha 32 \
        --batch_size 2 \
        --gradient_accumulation 8 \
        --epochs 3 \
        --lr 2e-4 \
        --gpu 0
"""

import os
import sys
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset
from transformers import (
    AutoTokenizer, AutoModelForCausalLM,
    BitsAndBytesConfig, TrainingArguments, Trainer,
    DataCollatorForLanguageModeling
)
from peft import (
    LoraConfig, get_peft_model, prepare_model_for_kbit_training,
    TaskType, PeftModel
)
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


# ==================== 命令行参数 ====================

def parse_args():
    parser = argparse.ArgumentParser(description="SFT/DPO 训练脚本")
    parser.add_argument("--model", type=str, default="Qwen/Qwen2.5-Coder-7B-Instruct",
                        help="模型名称或本地路径")
    parser.add_argument("--data_path", type=str, default="datasets/training",
                        help="训练数据目录（包含 sft_train.json 等）")
    parser.add_argument("--output_dir", type=str, default="output/sft_run",
                        help="输出目录")
    parser.add_argument("--mode", type=str, choices=["sft", "dpo", "both"],
                        default="sft", help="训练模式")

    # LoRA 配置
    parser.add_argument("--lora_rank", type=int, default=16)
    parser.add_argument("--lora_alpha", type=int, default=32)
    parser.add_argument("--lora_dropout", type=float, default=0.05)
    parser.add_argument("--target_modules", type=str,
                        default="q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj",
                        help="LoRA 目标模块")

    # 训练配置
    parser.add_argument("--batch_size", type=int, default=2)
    parser.add_argument("--gradient_accumulation", type=int, default=8)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--weight_decay", type=float, default=0.01)
    parser.add_argument("--warmup_ratio", type=float, default=0.03)
    parser.add_argument("--max_seq_len", type=int, default=2048)
    parser.add_argument("--gradient_checkpointing", action="store_true", default=True,
                        help="启用梯度检查点节省显存")
    parser.add_argument("--bf16", action="store_true", default=True,
                        help="使用 BF16 精度")
    parser.add_argument("--fp16", action="store_true",
                        help="使用 FP16 精度（无 BF16 时）")

    # 优化器
    parser.add_argument("--adam_beta1", type=float, default=0.9)
    parser.add_argument("--adam_beta2", type=float, default=0.999)
    parser.add_argument("--lr_scheduler", type=str, default="cosine")

    # 系统
    parser.add_argument("--gpu", type=str, default="0", help="GPU ID，逗号分隔多卡")
    parser.add_argument("--num_workers", type=int, default=4)
    parser.add_argument("--save_steps", type=int, default=50)
    parser.add_argument("--eval_steps", type=int, default=50)
    parser.add_argument("--logging_steps", type=int, default=10)
    parser.add_argument("--resume_from", type=str, default=None,
                        help="从指定 checkpoint 继续训练")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--quantize", type=str, choices=["4bit", "8bit", "none"],
                        default="4bit", help="量化位数")
    return parser.parse_args()


# ==================== 数据集 ====================

class SFTDataset(Dataset):
    """SFT 数据集"""

    def __init__(self, data_path: str, tokenizer: AutoTokenizer, max_len: int = 2048):
        self.tokenizer = tokenizer
        self.max_len = max_len
        self.tokenizer.padding_side = "right"
        self.tokenizer.truncation = True

        with open(data_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)

        # 支持两种格式：
        # 1. messages 格式: [{"role": "user", "content": ...}, {"role": "assistant", "content": ...}]
        # 2. 扁平格式: {"prompt": ..., "chosen": ...}
        self.examples = []
        for item in raw:
            if "messages" in item:
                text = self._messages_to_text(item["messages"])
            elif "prompt" in item and "chosen" in item:
                text = self._build_text(item["prompt"], item["chosen"])
            else:
                continue
            self.examples.append(text)

        logger.info(f"SFTDataset: loaded {len(self.examples)} examples from {data_path}")

    def _messages_to_text(self, messages):
        text = ""
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "user":
                text += f"<|user|>\n{content}<|end|>\n"
            elif role == "assistant":
                text += f"<|assistant|>\n{content}<|end|>\n"
        return text

    def _build_text(self, prompt, response):
        return f"<|user|>\n{prompt}<|end|>\n<|assistant|>\n{response}<|end|>\n"

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        text = self.examples[idx]
        result = self.tokenizer(
            text,
            max_length=self.max_len,
            truncation=True,
            padding=False,
            return_tensors=None
        )
        result["labels"] = result["input_ids"].copy()
        return result


class DPODataset(Dataset):
    """DPO 数据集"""

    def __init__(self, data_path: str, tokenizer: AutoTokenizer, max_len: int = 2048):
        self.tokenizer = tokenizer
        self.max_len = max_len

        with open(data_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)

        self.examples = []
        for item in raw:
            if "prompt" not in item or "chosen" not in item or "rejected" not in item:
                continue
            prompt = item["prompt"]
            chosen = item["chosen"]
            rejected = item["rejected"]
            self.examples.append({
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected
            })

        logger.info(f"DPODataset: loaded {len(self.examples)} examples from {data_path}")

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        item = self.examples[idx]
        return item


def dpo_collate_fn(batch, tokenizer, max_len):
    """将 DPO 数据整理为 batch"""
    prompts = [f"<|user|>\n{x['prompt']}<|end|>\n<|assistant|>\n" for x in batch]
    chosens = [x['chosen'] + "<|end|>\n" for x in batch]
    rejecteds = [x['rejected'] + "<|end|>\n" for x in batch]

    # Tokenize
    prompt_ids = tokenizer(prompts, max_length=max_len, truncation=True,
                           padding="max_length", return_tensors="pt")
    chosen_ids = tokenizer(chosens, max_length=max_len, truncation=True,
                           padding="max_length", return_tensors="pt")
    rejected_ids = tokenizer(rejecteds, max_length=max_len, truncation=True,
                             padding="max_length", return_tensors="pt")

    return {
        "prompt_input_ids": prompt_ids["input_ids"],
        "prompt_attention_mask": prompt_ids["attention_mask"],
        "chosen_input_ids": chosen_ids["input_ids"],
        "chosen_attention_mask": chosen_ids["attention_mask"],
        "rejected_input_ids": rejected_ids["input_ids"],
        "rejected_attention_mask": rejected_ids["attention_mask"],
    }


# ==================== 模型加载 ====================

def load_model_and_tokenizer(args):
    """加载模型和 tokenizer（带 QLoRA）"""
    logger.info(f"Loading model: {args.model}")
    logger.info(f"Quantization: {args.quantize}")

    # Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        args.model,
        trust_remote_code=True,
        padding_side="right"
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id
    tokenizer.padding_side = "right"

    # 量化配置
    bnb_config = None
    if args.quantize == "4bit":
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.bfloat16 if args.bf16 else torch.float16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
        )
        logger.info("Using 4-bit NF4 quantization (QLoRA)")
    elif args.quantize == "8bit":
        bnb_config = BitsAndBytesConfig(
            load_in_8bit=True,
            bnb_8bit_compute_dtype=torch.bfloat16 if args.bf16 else torch.float16,
        )
        logger.info("Using 8-bit quantization")

    # 加载模型
    model_kwargs = {
        "trust_remote_code": True,
        "torch_dtype": torch.bfloat16 if args.bf16 else (torch.float16 if args.fp16 else torch.float32),
        "device_map": "auto",
    }
    if bnb_config:
        model_kwargs["quantization_config"] = bnb_config

    model = AutoModelForCausalLM.from_pretrained(args.model, **model_kwargs)

    # 梯度设置
    if bnb_config:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=args.gradient_checkpointing)
    elif args.gradient_checkpointing:
        model.gradient_checkpointing_enable()

    # LoRA 配置
    target_modules = [m.strip() for m in args.target_modules.split(",")]
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=args.lora_rank,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        target_modules=target_modules,
        bias="none",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    return model, tokenizer


# ==================== SFT 训练 ====================

def compute_sft_metrics(eval_pred):
    """计算 SFT 评估指标"""
    predictions, labels = eval_pred
    # 简化：只计算 token 级别的准确率
    mask = labels != -100
    correct = (predictions[mask] == labels[mask]).sum()
    total = mask.sum()
    return {"token_accuracy": float(correct) / float(total) if total > 0 else 0.0}


def train_sft(args, model, tokenizer):
    """SFT 训练"""
    logger.info("=" * 60)
    logger.info("Starting SFT Training")
    logger.info("=" * 60)

    # 数据
    base_dir = Path(__file__).parent
    train_path = base_dir / args.data_path / "sft_train.json"
    eval_path = base_dir / args.data_path / "sft_eval.json"

    if not train_path.exists():
        logger.error(f"Training data not found: {train_path}")
        return

    train_dataset = SFTDataset(str(train_path), tokenizer, args.max_seq_len)
    eval_dataset = SFTDataset(str(eval_path), tokenizer, args.max_seq_len) if eval_path.exists() else None

    # 数据整理器
    data_collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer,
        mlm=False,  # Causal LM
        return_tensors="pt"
    )

    # 训练参数
    training_args = TrainingArguments(
        output_dir=args.output_dir,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        weight_decay=args.weight_decay,
        warmup_ratio=args.warmup_ratio,
        lr_scheduler_type=args.lr_scheduler,
        logging_dir=f"{args.output_dir}/logs",
        logging_steps=args.logging_steps,
        save_steps=args.save_steps,
        eval_steps=args.eval_steps,
        evaluation_strategy="steps" if eval_dataset else "no",
        save_total_limit=3,
        bf16=args.bf16,
        fp16=args.fp16 and not args.bf16,
        dataloader_num_workers=args.num_workers,
        remove_unused_columns=False,
        report_to=["tensorboard"],
        seed=args.seed,
        optim="paged_adamw_8bit",  # QLoRA 推荐优化器
        group_by_length=True,
        length_column_name="length",
        save_safetensors=True,
        load_best_model_at_end=True if eval_dataset else False,
    )

    # Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=data_collator,
        compute_metrics=compute_sft_metrics if eval_dataset else None,
    )

    # 断点续训
    if args.resume_from:
        logger.info(f"Resuming from: {args.resume_from}")
        trainer.train(resume_from_checkpoint=args.resume_from)
    else:
        trainer.train()

    # 保存最终模型
    trainer.save_model(f"{args.output_dir}/final")
    trainer.save_state()
    logger.info(f"SFT training complete. Model saved to {args.output_dir}/final")


# ==================== DPO 训练 ====================

def dpo_loss(
    policy_chosen_logps: torch.Tensor,
    policy_rejected_logps: torch.Tensor,
    reference_chosen_logps: torch.Tensor,
    reference_rejected_logps: torch.Tensor,
    beta: float = 0.1,
    label_smoothing: float = 0.0,
) -> torch.Tensor:
    """
    DPO Loss (来自 TRL 库):
    loss = - log_sigmoid(policy_chosen - policy_rejected - beta * (ref_chosen - ref_rejected))

    关键：所有 logps 必须有梯度！
    policy_logps: 模型生成的 logprobs（需要梯度）
    reference_logps: reference 模型的 logprobs（不需要梯度）
    """
    # 关键修复：policy logps 必须在梯度上下文中计算
    # reference logps 是冻结的（no_grad）
    pi_chosen = policy_chosen_logps
    pi_rejected = policy_rejected_logps
    ref_chosen = reference_chosen_logps
    ref_rejected = reference_rejected_logps

    # Log ratio differences
    logits = beta * ((pi_chosen - pi_rejected) - (ref_chosen - ref_rejected))

    # Apply label smoothing
    if label_smoothing > 0:
        logits = torchigmoid(logits)  # placeholder, will be overwritten
        labels = (1 - label_smoothing) * torch.ones_like(logits) + label_smoothing * 0.5
        loss = F.binary_cross_entropy_with_logits(logits, labels)
    else:
        loss = -F.logsigmoid(logits).mean()

    return loss


def log sigmoid(x):
    return torch.log(torch.sigmoid(x))


def torchigmoid(x):
    return torch.sigmoid(x)


def dpo_forward_pass(model, batch, tokenizer, max_len):
    """
    执行 DPO 前向传播，返回 log probabilities。

    重要：logps 必须在 torch.no_grad() 之外计算才有梯度！
    """
    device = model.device

    # ==== Reference Model (no grad) ====
    with torch.no_grad():
        ref_chosen_output = model(
            input_ids=batch["chosen_input_ids"].to(device),
            attention_mask=batch["chosen_attention_mask"].to(device),
        )
        ref_rejected_output = model(
            input_ids=batch["rejected_input_ids"].to(device),
            attention_mask=batch["rejected_attention_mask"].to(device),
        )

        ref_chosen_logps = _get_logps(
            ref_chosen_output.logits, batch["chosen_input_ids"].to(device), tokenizer
        )
        ref_rejected_logps = _get_logps(
            ref_rejected_output.logits, batch["rejected_input_ids"].to(device), tokenizer
        )

    # ==== Policy Model (WITH grad) ====
    # 关键：policy 前向传播不在 no_grad 中，所以有梯度！
    policy_chosen_output = model(
        input_ids=batch["chosen_input_ids"].to(device),
        attention_mask=batch["chosen_attention_mask"].to(device),
    )
    policy_rejected_output = model(
        input_ids=batch["rejected_input_ids"].to(device),
        attention_mask=batch["rejected_attention_mask"].to(device),
    )

    policy_chosen_logps = _get_logps(
        policy_chosen_output.logits, batch["chosen_input_ids"].to(device), tokenizer
    )
    policy_rejected_logps = _get_logps(
        policy_rejected_output.logits, batch["rejected_input_ids"].to(device), tokenizer
    )

    return {
        "policy_chosen_logps": policy_chosen_logps,
        "policy_rejected_logps": policy_rejected_logps,
        "reference_chosen_logps": ref_chosen_logps,
        "reference_rejected_logps": ref_rejected_logps,
    }


def _get_logps(logits: torch.Tensor, input_ids: torch.Tensor, tokenizer) -> torch.Tensor:
    """
    计算序列的对数概率（per-token log probability 均值，乘以序列长度）

    logits: [batch, seq_len, vocab_size]
    input_ids: [batch, seq_len]
    """
    # 只在非 padding token 上计算
    shift_logits = logits[..., :-1, :].contiguous()
    shift_labels = input_ids[..., 1:].contiguous()

    # 交叉熵
    loss = F.cross_entropy(
        shift_logits.view(-1, shift_logits.size(-1)),
        shift_labels.view(-1),
        reduction="none",
    )

    # 只考虑有效 token
    mask = (shift_labels != tokenizer.pad_token_id) & (shift_labels != tokenizer.unk_token_id)
    mask = mask.float()
    if mask.sum() == 0:
        return torch.tensor(0.0, device=logits.device)

    # Per-sequence 平均 log probability
    logps = -(loss * mask).sum(dim=-1) / mask.sum(dim=-1)
    return logps


def train_dpo(args, model, tokenizer):
    """DPO 训练"""
    logger.info("=" * 60)
    logger.info("Starting DPO Training")
    logger.info("=" * 60)

    base_dir = Path(__file__).parent

    # 保存一份 reference 模型
    ref_model_path = f"{args.output_dir}/reference_model"
    if not os.path.exists(ref_model_path):
        logger.info("Saving reference model (this may take a while)...")
        model.save_pretrained(ref_model_path)
        tokenizer.save_pretrained(ref_model_path)

    # 加载 reference 模型
    from transformers import AutoModelForCausalLM
    ref_model = AutoModelForCausalLM.from_pretrained(
        ref_model_path,
        torch_dtype=model.dtype,
        device_map="auto",
        trust_remote_code=True,
    )
    ref_model.eval()
    logger.info("Reference model loaded")

    # 数据
    train_path = base_dir / args.data_path / "dpo_train.json"
    eval_path = base_dir / args.data_path / "dpo_eval.json"
    train_dataset = DPODataset(str(train_path), tokenizer, args.max_seq_len)
    eval_dataset = DPODataset(str(eval_path), tokenizer, args.max_seq_len) if eval_path.exists() else None

    # 优化器
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=args.lr,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.weight_decay,
    )

    # 学习率调度器
    total_steps = (len(train_dataset) // args.batch_size) * args.epochs // args.gradient_accumulation
    warmup_steps = int(total_steps * args.warmup_ratio)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_steps)

    # 梯度缩放器
    scaler = torch.amp.GradScaler("cuda") if args.bf16 else None

    global_step = 0
    best_eval_loss = float("inf")

    model.train()

    for epoch in range(args.epochs):
        logger.info(f"\nEpoch {epoch + 1}/{args.epochs}")
        epoch_loss = 0.0
        epoch_steps = 0
        optimizer.zero_grad()

        epoch_iterator = tqdm(
            DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True,
                       collate_fn=lambda b: dpo_collate_fn(b, tokenizer, args.max_seq_len)),
            desc=f"Epoch {epoch}",
            disable=False
        )

        for step, batch in enumerate(epoch_iterator):
            # DPO forward
            with torch.amp.autocast("cuda", dtype=torch.bfloat16 if args.bf16 else torch.float16):
                logps = dpo_forward_pass(model, batch, tokenizer, args.max_seq_len)
                loss = dpo_loss(
                    logps["policy_chosen_logps"],
                    logps["policy_rejected_logps"],
                    logps["reference_chosen_logps"],
                    logps["reference_rejected_logps"],
                    beta=0.1,
                )

            # 梯度累积
            loss_item = loss.item()
            loss = loss / args.gradient_accumulation
            epoch_loss += loss_item

            if scaler:
                scaler.scale(loss).backward()
            else:
                loss.backward()

            if (step + 1) % args.gradient_accumulation == 0:
                if scaler:
                    scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                    optimizer.step()

                scheduler.step()
                optimizer.zero_grad()
                global_step += 1

                if global_step % args.logging_steps == 0:
                    lr = scheduler.get_last_lr()[0]
                    logger.info(
                        f"Step {global_step}: loss={loss_item:.4f}, lr={lr:.2e}, "
                        f"pi_chosen={logps['policy_chosen_logps'].mean().item():.2f}, "
                        f"pi_rejected={logps['policy_rejected_logps'].mean().item():.2f}"
                    )

                if global_step % args.save_steps == 0:
                    checkpoint_path = f"{args.output_dir}/checkpoint-{global_step}"
                    model.save_pretrained(checkpoint_path)
                    logger.info(f"Saved checkpoint: {checkpoint_path}")

                if eval_dataset and global_step % args.eval_steps == 0:
                    eval_loss = evaluate_dpo(model, ref_model, eval_dataset, tokenizer, args)
                    logger.info(f"Eval loss: {eval_loss:.4f}")
                    if eval_loss < best_eval_loss:
                        best_eval_loss = eval_loss
                        best_path = f"{args.output_dir}/best"
                        model.save_pretrained(best_path)
                        logger.info(f"New best model saved: {best_path}")

        avg_epoch_loss = epoch_loss / (len(train_dataset) // args.batch_size)
        logger.info(f"Epoch {epoch + 1} average loss: {avg_epoch_loss:.4f}")

    # 最终保存
    model.save_pretrained(f"{args.output_dir}/final")
    logger.info(f"DPO training complete. Model saved to {args.output_dir}/final")


def evaluate_dpo(policy_model, ref_model, eval_dataset, tokenizer, args):
    """评估 DPO 模型"""
    policy_model.eval()
    ref_model.eval()

    total_loss = 0.0
    count = 0

    eval_loader = DataLoader(
        eval_dataset, batch_size=args.batch_size, shuffle=False,
        collate_fn=lambda b: dpo_collate_fn(b, tokenizer, args.max_seq_len)
    )

    with torch.no_grad():
        for batch in eval_loader:
            with torch.amp.autocast("cuda", dtype=torch.bfloat16 if args.bf16 else torch.float16):
                logps = dpo_forward_pass(policy_model, batch, tokenizer, args.max_seq_len)
                loss = dpo_loss(
                    logps["policy_chosen_logps"],
                    logps["policy_rejected_logps"],
                    logps["reference_chosen_logps"],
                    logps["reference_rejected_logps"],
                    beta=0.1,
                )
            total_loss += loss.item()
            count += 1

    policy_model.train()
    return total_loss / count if count > 0 else 0.0


# ==================== 主函数 ====================

def main():
    args = parse_args()

    # 设置 GPU
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    logger.info(f"CUDA available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        logger.info(f"GPU: {torch.cuda.get_device_name(0)}")
        logger.info(f"GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    # 设置随机种子
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    # 创建输出目录
    os.makedirs(args.output_dir, exist_ok=True)

    # 保存配置
    config_path = os.path.join(args.output_dir, "config.json")
    with open(config_path, 'w') as f:
        json.dump(vars(args), f, indent=2, default=str)
    logger.info(f"Config saved to {config_path}")

    # 加载模型
    model, tokenizer = load_model_and_tokenizer(args)

    # 训练
    if args.mode in ("sft", "both"):
        train_sft(args, model, tokenizer)

    if args.mode in ("dpo", "both"):
        # DPO 需要重新加载模型（因为 SFT 会修改权重）
        if args.mode == "both":
            # 加载 SFT 训练好的模型
            sft_path = f"{args.output_dir}/final"
            if os.path.exists(sft_path):
                logger.info(f"Loading SFT model from {sft_path} for DPO")
                model = PeftModel.from_pretrained(
                    AutoModelForCausalLM.from_pretrained(
                        args.model,
                        trust_remote_code=True,
                        torch_dtype=torch.bfloat16 if args.bf16 else torch.float16,
                        device_map="auto",
                    ),
                    sft_path
                )
            else:
                logger.info("No SFT model found, using base model for DPO")
                model, tokenizer = load_model_and_tokenizer(args)

        train_dpo(args, model, tokenizer)

    logger.info("\n[DONE] Training complete!")


if __name__ == "__main__":
    main()
