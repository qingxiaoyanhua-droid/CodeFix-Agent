#!/usr/bin/env python3
"""
GRPO 训练脚本（集成 LLM Critic 奖励）
Qwen2.5-Coder-7B-Instruct + RAG + ReAct Agent
"""

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig, get_peft_model
from trl import GRPOConfig, GRPOTrainer
import os
import json
import re


# ==================== 配置 ====================

MODEL_NAME = "/data/models/model_cache/Qwen/Qwen2.5-Coder-7B-Instruct"
OUTPUT_DIR = "outputs/codefix-grpo-llm-critic"

# GRPO 超参数
LEARNING_RATE = 5e-6
BATCH_SIZE = 2
NUM_GENERATIONS = 4  # 每个 prompt 生成 4 个
MAX_COMPLETION_LENGTH = 512
NUM_TRAIN_EPOCHS = 1

# LLM Critic 配置
USE_LLM_CRITIC = True  # 是否启用大模型裁判
CRITIC_MODEL = "qwen-plus"  # 使用的裁判模型


# ==================== 数据集准备 ====================

def create_grpo_dataset():
    """加载合并的高质量数据集"""
    merged_path = "datasets/dpo_dataset_merged.json"
    
    if os.path.exists(merged_path):
        with open(merged_path, 'r', encoding='utf-8') as f:
            dpo_data = json.load(f)
        print(f"✓ 从文件加载数据集：{len(dpo_data)} 条")
    else:
        print("⚠️ 数据集不存在")
        return Dataset.from_list([])
    
    # 转换为 GRPO 格式
    grpo_data = []
    for item in dpo_data:
        grpo_data.append({
            "prompt": item['prompt'],
            "bug_type": item.get('bug_type', 'unknown'),
            "source": item.get('source', 'unknown'),
            # 提取 bug 信息用于 LLM Critic
            "bug_description": item.get('description', ''),
            "buggy_code": extract_buggy_code(item['prompt'])
        })
    
    return Dataset.from_list(grpo_data)


def extract_buggy_code(prompt: str) -> str:
    """从 prompt 中提取 buggy code"""
    match = re.search(r'```python\s*(.*?)\s*```', prompt, re.DOTALL)
    return match.group(1).strip() if match else ""


# ==================== 奖励函数（混合奖励） ====================

class HybridReward:
    """混合奖励系统：格式 + 语法 + LLM Critic"""
    
    def __init__(self, use_llm_critic=True):
        self.use_llm_critic = use_llm_critic
        
        if use_llm_critic:
            try:
                from llm_critic_reward import LLMPatchCritic
                self.critic = LLMPatchCritic(model_name=CRITIC_MODEL)
                print("✅ LLM Critic 已初始化")
            except Exception as e:
                print(f"⚠️ LLM Critic 初始化失败：{e}")
                self.critic = None
                self.use_llm_critic = False
        else:
            self.critic = None
    
    def compute_reward(self, completions, prompts=None, **kwargs):
        """
        计算混合奖励
        
        Reward = 格式奖励 (0.2) + 语法奖励 (0.3) + LLM Critic (0.5)
        """
        scores = []
        
        for i, completion in enumerate(completions):
            score = 0.0
            
            # 1. 格式奖励（0.2）
            has_reasoning = '<reasoning>' in completion and '</reasoning>' in completion
            has_fixed_code = '<fixed_code>' in completion and '</fixed_code>' in completion
            
            if has_reasoning and has_fixed_code:
                score += 0.2
            elif has_fixed_code:
                score += 0.1
            
            # 2. 语法奖励（0.3）
            match = re.search(r'<fixed_code>(.*?)</fixed_code>', completion, re.DOTALL)
            if match:
                code = match.group(1).strip()
                try:
                    compile(code, '<string>', 'exec')
                    score += 0.3  # 语法正确
                except SyntaxError:
                    pass
            
            # 3. LLM Critic 奖励（0.5）
            if self.critic and prompts:
                prompt = prompts[i] if i < len(prompts) else ""
                bug_desc, buggy_code = self._extract_bug_info(prompt)
                
                if match:
                    patch_code = match.group(1).strip()
                    
                    # 调用大模型评估
                    eval_result = self.critic.evaluate_patch(
                        bug_desc,
                        buggy_code,
                        patch_code
                    )
                    
                    # LLM 分数（0-10 归一化到 0-0.5）
                    llm_score = (
                        eval_result["correctness"] * 0.6 +
                        eval_result["code_quality"] * 0.3 +
                        eval_result["minimality"] * 0.1
                    ) / 10.0
                    score += llm_score * 0.5
            
            scores.append(score)
        
        return scores
    
    def _extract_bug_info(self, prompt: str):
        """从 prompt 中提取 bug 信息"""
        bug_match = re.search(r'Bug:\s*(.*?)\n\n', prompt, re.DOTALL)
        bug_description = bug_match.group(1).strip() if bug_match else ""
        
        code_match = re.search(r'```python\s*(.*?)\s*```', prompt, re.DOTALL)
        buggy_code = code_match.group(1).strip() if code_match else ""
        
        return bug_description, buggy_code


# ==================== 训练函数 ====================

def train_grpo():
    """训练 GRPO 模型"""
    print("=" * 60)
    print("Qwen2.5-Coder-7B GRPO 训练（LLM Critic 版）")
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
    
    # 4. 初始化混合奖励
    print("\n🏆 初始化奖励系统...")
    reward_fn = HybridReward(use_llm_critic=USE_LLM_CRITIC)
    
    # 5. GRPO 训练配置
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
    
    # 6. 初始化训练器
    print("\n🔧 初始化训练器...")
    trainer = GRPOTrainer(
        model=model,
        args=grpo_config,
        train_dataset=dataset,
        reward_function=reward_fn.compute_reward,
        tokenizer=tokenizer,
    )
    
    # 7. 开始训练
    print("\n🚀 开始 GRPO 训练...")
    print("-" * 60)
    trainer.train()
    print("-" * 60)
    print("✓ 训练完成！")
    
    # 8. 保存模型
    final_dir = os.path.join(OUTPUT_DIR, "final")
    print(f"\n💾 保存模型到 {final_dir}")
    trainer.save_model(final_dir)
    tokenizer.save_pretrained(final_dir)
    
    print("\n" + "=" * 60)
    print("训练完成！")
    print("=" * 60)
    
    # 9. 打印训练统计
    print("\n📊 训练统计:")
    print(f"  数据量：{len(dataset)} 条")
    print(f"  Batch size: {BATCH_SIZE}")
    print(f"  num_generations: {NUM_GENERATIONS}")
    print(f"  预计时长：约 20-30 分钟")
    print(f"  输出目录：{OUTPUT_DIR}")


if __name__ == "__main__":
    train_grpo()
