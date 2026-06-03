#!/usr/bin/env python3
"""
GRPO 训练脚本（使用合并的高质量数据 - 187 条）
Qwen2.5-Coder-7B-Instruct
"""

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig, get_peft_model
from trl import GRPOConfig, GRPOTrainer
import os
import json


# ==================== 配置 ====================

MODEL_NAME = "/data/models/model_cache/Qwen/Qwen2.5-Coder-7B-Instruct"
OUTPUT_DIR = "outputs/codefix-grpo-v2"

# GRPO 超参数
LEARNING_RATE = 5e-6
BATCH_SIZE = 2  # 减小 batch 以适应 187 条数据
NUM_GENERATIONS = 4  # 每个 prompt 生成 4 个
MAX_COMPLETION_LENGTH = 512
NUM_TRAIN_EPOCHS = 1


# ==================== 数据集准备 ====================

def create_grpo_dataset():
    """加载合并的高质量数据集"""
    merged_path = "datasets/dpo_dataset_merged.json"
    
    if os.path.exists(merged_path):
        with open(merged_path, 'r', encoding='utf-8') as f:
            dpo_data = json.load(f)
        print(f"✓ 从文件加载数据集：{len(dpo_data)} 条")
    else:
        print("⚠️ 数据集不存在，请先生成")
        return Dataset.from_list([])
    
    # 转换为 GRPO 格式（只需要 prompt）
    grpo_data = []
    for item in dpo_data:
        grpo_data.append({
            "prompt": item['prompt'],
            "bug_type": item.get('bug_type', 'unknown'),
            "source": item.get('source', 'unknown')
        })
    
    return Dataset.from_list(grpo_data)


# ==================== 奖励函数 ====================

def compute_reward(completions, **kwargs):
    """
    计算奖励分数
    基于格式、语法、质量等维度
    """
    import re
    
    scores = []
    for completion in completions:
        score = 0.0
        
        # 1. 格式奖励（检查 XML 标签）
        has_reasoning = '<reasoning>' in completion and '</reasoning>' in completion
        has_fixed_code = '<fixed_code>' in completion and '</fixed_code>' in completion
        
        if has_reasoning and has_fixed_code:
            score += 0.5
        elif has_fixed_code:
            score += 0.25
        
        # 2. 语法奖励（检查 Python 语法）
        match = re.search(r'<fixed_code>(.*?)</fixed_code>', completion, re.DOTALL)
        if match:
            code = match.group(1).strip()
            try:
                compile(code, '<string>', 'exec')
                score += 1.0
            except SyntaxError:
                pass
        
        # 3. 代码质量奖励（简洁性）
        if match:
            code = match.group(1).strip()
            lines = code.split('\n')
            if len(lines) <= 10:
                score += 0.5
            elif len(lines) <= 20:
                score += 0.25
        
        scores.append(score)
    
    return scores


# ==================== 训练函数 ====================

def train_grpo():
    """训练 GRPO 模型"""
    print("=" * 60)
    print("Qwen2.5-Coder-7B GRPO 训练（高质量数据版）")
    print("=" * 60)
    
    # 1. 加载数据集
    print("\n📚 加载数据集...")
    dataset = create_grpo_dataset()
    print(f"✓ 数据集大小：{len(dataset)} 条")
    
    if len(dataset) == 0:
        print("❌ 数据集为空，退出")
        return
    
    # 2. 加载模型和 tokenizer
    print(f"\n🤖 加载模型...")
    print(f"模型：{MODEL_NAME}")
    
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True
    )
    model.config.use_cache = False
    
    # 3. 配置 LoRA
    peft_config = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]
    )
    
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()
    
    # 4. GRPO 训练配置
    grpo_config = GRPOConfig(
        output_dir=OUTPUT_DIR,
        learning_rate=LEARNING_RATE,
        per_device_train_batch_size=BATCH_SIZE,
        num_generations=NUM_GENERATIONS,
        max_completion_length=MAX_COMPLETION_LENGTH,
        num_train_epochs=NUM_TRAIN_EPOCHS,
        logging_steps=10,
        save_steps=50,
        warmup_steps=10,
        gradient_accumulation_steps=2,
        fp16=True,
        report_to="none",
    )
    
    # 5. 初始化训练器
    print("\n🔧 初始化训练器...")
    trainer = GRPOTrainer(
        model=model,
        args=grpo_config,
        train_dataset=dataset,
        reward_function=compute_reward,
        tokenizer=tokenizer,
    )
    
    # 6. 开始训练
    print("\n🚀 开始 GRPO 训练...")
    print("-" * 60)
    trainer.train()
    print("-" * 60)
    print("✓ 训练完成！")
    
    # 7. 保存模型
    final_dir = os.path.join(OUTPUT_DIR, "final")
    print(f"\n💾 保存模型到 {final_dir}")
    trainer.save_model(final_dir)
    tokenizer.save_pretrained(final_dir)
    
    print("\n" + "=" * 60)
    print("训练完成！")
    print("=" * 60)


if __name__ == "__main__":
    train_grpo()
