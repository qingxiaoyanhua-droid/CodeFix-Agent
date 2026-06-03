#!/usr/bin/env python3
"""
代码修复 DPO 训练脚本（对比实验）
基于 TRL 库，与 GRPO 对比

DPO vs GRPO 核心区别：
- DPO: 需要偏好数据（chosen vs rejected）
- GRPO: 只需奖励信号，无需显式偏好标注
"""

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig, get_peft_model
from trl import DPOTrainer, DPOConfig
import re
import os


# ==================== 配置 ====================

MODEL_NAME = "Qwen/Qwen2.5-Coder-1.5B-Instruct"  # 与 GRPO 保持一致
OUTPUT_DIR = "outputs/codefix-dpo"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

# DPO 超参数（与 GRPO 对比）
BETA = 0.1  # DPO 温度参数，控制 KL 散度权重
LEARNING_RATE = 5e-6  # 与 GRPO 一致
BATCH_SIZE = 4  # 与 GRPO 一致
MAX_LENGTH = 512  # 与 GRPO 一致


# ==================== 数据集准备 ====================

def create_dpo_dataset():
    """
    创建 DPO 偏好数据集
    每条数据包含：prompt + chosen_response + rejected_response
    """
    
    # 示例数据：可以从 GRPO 生成历史中收集
    # chosen: 高奖励的生成（reward > 3.0）
    # rejected: 低奖励的生成（reward < 2.0）
    dpo_data = [
        {
            "prompt": """Bug: Fix the addition function

Buggy Code:
```python
def add(a, b):
    return a - b
```

Fix the bug.""",
            "chosen": """<reasoning>
The bug is in the operator used. The function should perform addition, but it's using subtraction.
</reasoning>
<fixed_code>
def add(a, b):
    return a + b
</fixed_code>""",
            "rejected": """<reasoning>
The function has a wrong operator.
</reasoning>
<fixed_code>
def add(a, b):
    return a * b  # Still wrong
</fixed_code>"""
        },
        {
            "prompt": """Bug: Fix the factorial function

Buggy Code:
```python
def factorial(n):
    if n == 0:
        return 0
    return n * factorial(n-1)
```

Fix the bug.""",
            "chosen": """<reasoning>
Base case is wrong. factorial(0) should be 1, not 0.
</reasoning>
<fixed_code>
def factorial(n):
    if n == 0:
        return 1
    return n * factorial(n-1)
</fixed_code>""",
            "rejected": """<reasoning>
The base case needs fixing.
</reasoning>
<fixed_code>
def factorial(n):
    if n == 0:
        return 0  # Still wrong
    return n * factorial(n-1)
</fixed_code>"""
        },
        {
            "prompt": """Bug: Fix the binary search

Buggy Code:
```python
def binary_search(arr, target):
    left, right = 0, len(arr) - 1
    while left < right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1
```

Fix the bug.""",
            "chosen": """<reasoning>
The while condition should be left <= right to handle the case when left == right.
</reasoning>
<fixed_code>
def binary_search(arr, target):
    left, right = 0, len(arr) - 1
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1
</fixed_code>""",
            "rejected": """<reasoning>
There's an issue with the loop condition.
</reasoning>
<fixed_code>
def binary_search(arr, target):
    left, right = 0, len(arr) - 1
    while left < right:  # Still wrong
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1
</fixed_code>"""
        },
    ]
    
    # 扩展数据集（实际使用应收集 100-500 条）
    # 这里简单复制示例用于演示
    while len(dpo_data) < 100:
        dpo_data.extend(dpo_data[:10])
    
    dpo_data = dpo_data[:100]
    
    return Dataset.from_list(dpo_data)


# ==================== 奖励函数（用于生成偏好数据） ====================

def format_reward(text: str) -> float:
    """格式奖励：检查 XML 标签结构"""
    has_reasoning = bool(re.search(r'<reasoning>.*?</reasoning>', text, re.DOTALL))
    has_fixed_code = bool(re.search(r'<fixed_code>.*?</fixed_code>', text, re.DOTALL))
    return 1.0 if (has_reasoning and has_fixed_code) else 0.0


def syntax_reward(text: str) -> float:
    """语法奖励：检查 Python 语法"""
    match = re.search(r'<fixed_code>(.*?)</fixed_code>', text, re.DOTALL)
    if not match:
        return 0.0
    
    code = match.group(1).strip()
    try:
        compile(code, '<string>', 'exec')
        return 1.0
    except SyntaxError:
        return 0.0


def correctness_reward(text: str, reference: str) -> float:
    """正确性奖励：与参考答案匹配度"""
    match = re.search(r'<fixed_code>(.*?)</fixed_code>', text, re.DOTALL)
    if not match:
        return 0.0
    
    generated = match.group(1).strip()
    reference_match = re.search(r'<fixed_code>(.*?)</fixed_code>', reference, re.DOTALL)
    
    if not reference_match:
        return 0.0
    
    reference_code = reference_match.group(1).strip()
    
    # 简单字符串匹配
    if generated == reference_code:
        return 2.0
    elif generated in reference_code or reference_code in generated:
        return 1.0
    else:
        return 0.0


def generate_preference_data(model, tokenizer, prompts, n_generations=8):
    """
    从模型生成中构建偏好数据
    高奖励 = chosen, 低奖励 = rejected
    """
    preference_data = []
    
    for prompt in prompts:
        # 生成多个候选
        candidates = []
        for _ in range(n_generations):
            inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
            with torch.no_grad():
                outputs = model.generate(
                    **inputs,
                    max_new_tokens=256,
                    temperature=0.7,
                    do_sample=True,
                    top_p=0.9
                )
            completion = tokenizer.decode(outputs[0][inputs['input_ids'].shape[1]:], skip_special_tokens=True)
            candidates.append(completion)
        
        # 计算每个候选的奖励
        rewards = []
        for cand in candidates:
            r_format = format_reward(cand)
            r_syntax = syntax_reward(cand)
            r_correct = correctness_reward(cand, candidates[0])  # 用第一个作为参考
            total_reward = r_format + r_syntax + r_correct
            rewards.append(total_reward)
        
        # 选择最高和最低奖励的作为 chosen/rejected
        best_idx = rewards.index(max(rewards))
        worst_idx = rewards.index(min(rewards))
        
        preference_data.append({
            "prompt": prompt,
            "chosen": candidates[best_idx],
            "rejected": candidates[worst_idx]
        })
    
    return Dataset.from_list(preference_data)


# ==================== 训练函数 ====================

def train_dpo():
    """训练 DPO 模型"""
    print("=" * 60)
    print("代码修复 DPO 训练（对比实验）")
    print("=" * 60)
    
    # 1. 加载数据集
    print("\n📚 加载数据集...")
    dataset = create_dpo_dataset()
    print(f"✓ 数据集大小：{len(dataset)} 条")
    
    # 2. 加载模型
    print(f"\n🤖 加载模型...")
    print(f"模型：{MODEL_NAME}")
    
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    tokenizer.pad_token = tokenizer.eos_token
    
    model = AutoModelForCausalLM.from_pretrained(
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
    
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()
    
    # 4. DPO 训练配置
    dpo_config = DPOConfig(
        output_dir=OUTPUT_DIR,
        beta=BETA,
        learning_rate=LEARNING_RATE,
        per_device_train_batch_size=BATCH_SIZE,
        max_length=MAX_LENGTH,
        max_prompt_length=256,
        num_train_epochs=1,
        logging_steps=10,
        save_steps=50,
        warmup_steps=10,
        gradient_accumulation_steps=2,
        fp16=True,
        report_to="none",
    )
    
    # 5. 初始化训练器
    print("\n🔧 初始化训练器...")
    trainer = DPOTrainer(
        model=model,
        ref_model=None,  # 使用 model 自身作为参考
        args=dpo_config,
        beta=BETA,
        train_dataset=dataset,
        tokenizer=tokenizer,
        max_length=MAX_LENGTH,
        max_prompt_length=256,
    )
    
    # 6. 开始训练
    print("\n🚀 开始训练...")
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
    train_dpo()
