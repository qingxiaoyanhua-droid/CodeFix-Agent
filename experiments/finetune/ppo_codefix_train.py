#!/usr/bin/env python3
"""
代码修复 PPO 训练脚本（对比实验）
基于 TRL 库，与 GRPO 对比

PPO vs GRPO 核心区别：
- PPO: 需要独立的 critic 模型，逐样本优化
- GRPO: 无需 critic，组内相对优势计算，更稳定
"""

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoModelForSequenceClassification, AutoTokenizer
from peft import LoraConfig, get_peft_model
from trl import PPOTrainer, PPOConfig, AutoModelForCausalLMWithValueHead
import re
import os


# ==================== 配置 ====================

MODEL_NAME = "Qwen/Qwen2.5-Coder-1.5B-Instruct"  # 与 GRPO 保持一致
OUTPUT_DIR = "outputs/codefix-ppo"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# PPO 超参数（与 GRPO 对比）
LEARNING_RATE = 5e-6  # 与 GRPO 一致
BATCH_SIZE = 4  # 与 GRPO 一致
PPO_BATCH_SIZE = 2  # PPO 内部 batch size
MAX_LENGTH = 512  # 与 GRPO 一致
MINI_BATCH_SIZE = 2  # PPO mini batch size
PPO_EPOCHS = 4  # PPO 更新轮数


# ==================== 数据集准备 ====================

def create_ppo_dataset():
    """
    创建 PPO 数据集
    只需 prompt，奖励函数在训练时计算
    """
    
    prompts = [
        "Bug: Fix the addition function\n\nBuggy Code:\n```python\ndef add(a, b):\n    return a - b\n```\n\nFix the bug.",
        "Bug: Fix the factorial function\n\nBuggy Code:\n```python\ndef factorial(n):\n    if n == 0:\n        return 0\n    return n * factorial(n-1)\n```\n\nFix the bug.",
        "Bug: Fix the binary search\n\nBuggy Code:\n```python\ndef binary_search(arr, target):\n    left, right = 0, len(arr) - 1\n    while left < right:\n        mid = (left + right) // 2\n        if arr[mid] == target:\n            return mid\n        elif arr[mid] < target:\n            left = mid + 1\n        else:\n            right = mid - 1\n    return -1\n```\n\nFix the bug.",
        "Bug: Fix the reverse string function\n\nBuggy Code:\n```python\ndef reverse_string(s):\n    return s[::-2]\n```\n\nFix the bug.",
        "Bug: Fix the count vowels function\n\nBuggy Code:\n```python\ndef count_vowels(text):\n    vowels = 'aeiou'\n    count = 0\n    for char in text:\n        if char in vowels:\n            count += 1\n    return count\n```\n\nFix the bug to handle uppercase letters.",
    ]
    
    # 扩展数据集（实际使用应收集 100-500 条）
    while len(prompts) < 100:
        prompts.extend(prompts[:5])
    
    prompts = prompts[:100]
    
    dataset = Dataset.from_list([{"prompt": p} for p in prompts])
    return dataset


# ==================== 奖励函数 ====================

def compute_reward(text: str) -> float:
    """
    计算生成文本的奖励
    与 GRPO 奖励函数保持一致以便对比
    """
    total_reward = 0.0
    
    # 1. 格式奖励（权重 0.5）
    has_reasoning = bool(re.search(r'<reasoning>.*?</reasoning>', text, re.DOTALL))
    has_fixed_code = bool(re.search(r'<fixed_code>.*?</fixed_code>', text, re.DOTALL))
    if has_reasoning and has_fixed_code:
        total_reward += 0.5
    elif has_fixed_code:
        total_reward += 0.25  # 增量奖励
    
    # 2. 语法奖励（权重 1.0）
    match = re.search(r'<fixed_code>(.*?)</fixed_code>', text, re.DOTALL)
    if match:
        code = match.group(1).strip()
        try:
            compile(code, '<string>', 'exec')
            total_reward += 1.0
        except SyntaxError:
            pass
    
    # 3. 代码质量奖励（权重 0.5）
    if match:
        code = match.group(1).strip()
        lines = code.split('\n')
        # 简洁性奖励
        if len(lines) <= 10:
            total_reward += 0.5
        elif len(lines) <= 20:
            total_reward += 0.25
    
    # 4. 正确性奖励（权重 2.0）- 这里简化为关键词匹配
    # 实际使用应与参考答案对比
    keywords = ['return', '+', '1', 'fix', 'correct']
    if match:
        code = match.group(1).lower()
        for kw in keywords:
            if kw in code:
                total_reward += 0.4  # 最多 2.0
    
    return total_reward


def collator(data):
    """数据整理函数"""
    return dict((key, [d[key] for d in data]) for key in data[0])


# ==================== 训练函数 ====================

def train_ppo():
    """训练 PPO 模型"""
    print("=" * 60)
    print("代码修复 PPO 训练（对比实验）")
    print("=" * 60)
    
    # 1. 加载数据集
    print("\n📚 加载数据集...")
    dataset = create_ppo_dataset()
    print(f"✓ 数据集大小：{len(dataset)} 条")
    
    # 2. 加载模型和 tokenizer
    print(f"\n🤖 加载模型...")
    print(f"模型：{MODEL_NAME}")
    
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    tokenizer.pad_token = tokenizer.eos_token
    
    # PPO 需要带 Value Head 的模型
    model = AutoModelForCausalLMWithValueHead.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True
    )
    
    # 3. 配置 LoRA
    peft_config = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]
    )
    
    model = get_peft_model(model.pretrained_model, peft_config)
    
    # 重新包装 Value Head
    model = AutoModelForCausalLMWithValueHead(model)
    model.print_trainable_parameters()
    
    # 4. PPO 训练配置
    ppo_config = PPOConfig(
        output_dir=OUTPUT_DIR,
        learning_rate=LEARNING_RATE,
        batch_size=PPO_BATCH_SIZE,
        mini_batch_size=MINI_BATCH_SIZE,
        ppo_epochs=PPO_EPOCHS,
        max_length=MAX_LENGTH,
        max_new_tokens=256,
        num_train_epochs=1,
        logging_steps=10,
        save_steps=50,
        warmup_steps=10,
        gradient_accumulation_steps=2,
        fp16=True,
        report_to="none",
        use_score_scaling=True,
        use_score_norm=True,
    )
    
    # 5. 初始化训练器
    print("\n🔧 初始化训练器...")
    trainer = PPOTrainer(
        config=ppo_config,
        model=model,
        ref_model=None,  # 使用 model 自身作为参考
        tokenizer=tokenizer,
        dataset=dataset,
        data_collator=collator,
    )
    
    # 6. 开始训练
    print("\n🚀 开始训练...")
    print("-" * 60)
    
    generation_kwargs = {
        "min_length": -1,
        "top_k": 0.0,
        "top_p": 1.0,
        "do_sample": True,
        "max_new_tokens": 256,
    }
    
    for epoch in range(1):  # 1 个 epoch
        for batch in trainer.dataloader:
            # 生成响应
            query_tensors = batch["input_ids"]
            response_tensors = trainer.generate(
                *query_tensors,
                **generation_kwargs
            )
            
            # 解码生成结果
            responses = []
            for query, response in zip(query_tensors, response_tensors):
                text = tokenizer.decode(response[query.shape[0]:], skip_special_tokens=True)
                responses.append(text)
            
            # 计算奖励
            rewards = [compute_reward(r) for r in responses]
            
            # PPO 优化
            stats = trainer.step(query_tensors, response_tensors, rewards)
            
            # 记录奖励统计
            stats["reward/mean"] = sum(rewards) / len(rewards)
            stats["reward/std"] = (sum((r - stats["reward/mean"])**2 for r in rewards) / len(rewards)) ** 0.5
            
            # 打印进度
            if trainer.state.global_step % 10 == 0:
                print(f"Step {trainer.state.global_step}: "
                      f"reward={stats['reward/mean']:.3f}, "
                      f"reward_std={stats['reward/std']:.3f}")
    
    print("-" * 60)
    print("✓ 训练完成！")
    
    # 7. 保存模型
    final_dir = os.path.join(OUTPUT_DIR, "final")
    print(f"\n💾 保存模型到 {final_dir}")
    trainer.save_pretrained(final_dir)
    tokenizer.save_pretrained(final_dir)
    
    print("\n" + "=" * 60)
    print("训练完成！")
    print("=" * 60)
    
    # 8. 测试推理
    print("\n🧪 测试推理...")
    test_prompt = """Bug: Fix the addition function

Buggy Code:
```python
def add(a, b):
    return a - b
```

Fix the bug."""
    
    inputs = tokenizer(test_prompt, return_tensors="pt").to(model.device)
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=256,
            temperature=0.7,
            do_sample=True,
            top_p=0.9
        )
    
    result = tokenizer.decode(outputs[0][inputs['input_ids'].shape[1]:], skip_special_tokens=True)
    print("\n生成结果:")
    print(result)
    
    return model, tokenizer


if __name__ == "__main__":
    train_ppo()
