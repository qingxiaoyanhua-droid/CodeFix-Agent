#!/usr/bin/env python3
"""
代码修复 DPO 训练脚本（使用扩充数据集）
基于 TRL 库，与 GRPO 对比
"""

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig, get_peft_model
from trl import DPOTrainer, DPOConfig
import re
import os
import json


# ==================== 配置 ====================

# 使用与 GRPO 相同的模型路径（服务器上的路径）
MODEL_NAME = "/data/models/model_cache/Qwen/Qwen2.5-Coder-7B-Instruct"
OUTPUT_DIR = "outputs/codefix-dpo"

# DPO 超参数（与 GRPO 对比）
BETA = 0.1  # DPO 温度参数
LEARNING_RATE = 5e-6
BATCH_SIZE = 4
MAX_LENGTH = 512
MAX_PROMPT_LENGTH = 256
NUM_TRAIN_EPOCHS = 1
SAVE_STEPS = 50
LOGGING_STEPS = 1


# ==================== 数据集准备 ====================

def create_dpo_dataset():
    """
    创建 DPO 偏好数据集
    先从本地加载扩充的数据集，如果没有则使用内置种子数据
    """
    # 尝试从文件加载扩充的数据集
    dataset_path = "datasets/dpo_dataset.json"
    if os.path.exists(dataset_path):
        with open(dataset_path, 'r', encoding='utf-8') as f:
            dpo_data = json.load(f)
        print(f"✓ 从文件加载数据集：{len(dpo_data)} 条")
    else:
        # 使用内置种子数据（30 条基础）
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
    return a * b
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
        return 0
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
    while left < right:
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
        print(f"⚠️ 使用内置种子数据：{len(dpo_data)} 条")
        print(f"   运行 python expand_dpo_dataset.py 生成更多数据")

    return Dataset.from_list(dpo_data)


# ==================== 训练函数 ====================

def train_dpo():
    """训练 DPO 模型"""
    print("=" * 60)
    print("代码修复 DPO 训练（扩充数据集版）")
    print("=" * 60)

    # 1. 加载数据集
    print("\n📚 加载数据集...")
    dataset = create_dpo_dataset()
    print(f"✓ 数据集大小：{len(dataset)} 条")

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

    # 4. DPO 训练配置
    dpo_config = DPOConfig(
        output_dir=OUTPUT_DIR,
        beta=BETA,
        learning_rate=LEARNING_RATE,
        per_device_train_batch_size=BATCH_SIZE,
        max_length=MAX_LENGTH,
        max_prompt_length=MAX_PROMPT_LENGTH,
        num_train_epochs=NUM_TRAIN_EPOCHS,
        logging_steps=LOGGING_STEPS,
        save_steps=SAVE_STEPS,
        warmup_steps=10,
        gradient_accumulation_steps=2,
        fp16=True,
        report_to="none",
        use_cache=False,
    )

    # 5. 初始化训练器
    print("\n🔧 初始化训练器...")
    trainer = DPOTrainer(
        model=model,
        ref_model=None,
        args=dpo_config,
        beta=BETA,
        train_dataset=dataset,
        tokenizer=tokenizer,
        max_length=MAX_LENGTH,
        max_prompt_length=MAX_PROMPT_LENGTH,
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
            top_p=0.9,
            pad_token_id=tokenizer.pad_token_id
        )

    result = tokenizer.decode(outputs[0][inputs['input_ids'].shape[1]:], skip_special_tokens=True)
    print("\n生成结果:")
    print(result)

    return model, tokenizer


if __name__ == "__main__":
    train_dpo()
