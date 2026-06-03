#!/usr/bin/env python3
"""
代码修复 GRPO 训练脚本（精简版）
基于 TRL 库，适合面试项目演示

数据量：100-500 条（而非 135K）
训练时间：30 分钟 -2 小时
显存需求：单卡 8GB+
"""

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig
from trl import GRPOTrainer, GRPOConfig
import re


# ==================== 配置 ====================

MODEL_NAME = "Qwen/Qwen2.5-Coder-1.5B-Instruct"  # 代码专用小模型
OUTPUT_DIR = "outputs/codefix-grpo"

SYSTEM_PROMPT = """
You are an expert code debugger. Fix the buggy code and explain your reasoning.

Respond in the following format:
<reasoning>
[Analyze the bug, explain what's wrong, describe your fix strategy]
</reasoning>
<fixed_code>
[The corrected code with proper formatting]
</fixed_code>
"""


# ==================== 小型数据集（100 条示例） ====================

def get_small_codefix_dataset():
    """
    创建小型代码修复数据集（100 条）
    实际使用建议收集 200-500 条高质量样本
    """
    
    # 示例数据：可以从你的项目日志、GitHub PR 等收集
    custom_data = {
        'buggy_code': [
            # 基础错误（50 条）
            """def add(a, b):
    return a - b  # Bug: wrong operator""",
            
            """def factorial(n):
    if n == 0:
        return 0  # Bug: should be 1
    return n * factorial(n-1)""",
            
            """def find_max(lst):
    if not lst:
        return None
    max_val = 0  # Bug: should be lst[0]
    for num in lst:
        if num > max_val:
            max_val = num
    return max_val""",
            
            """def reverse_string(s):
    return s[::-2]  # Bug: should be -1""",
            
            """def count_vowels(text):
    vowels = 'aeiou'
    count = 0
    for char in text:
        if char in vowels:
            count += 1
    return count  # Bug: should handle uppercase""",
            
            # 中级错误（30 条）
            """def binary_search(arr, target):
    left, right = 0, len(arr) - 1
    while left < right:  # Bug: should be <=
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1""",
            
            """def merge_dicts(dict1, dict2):
    result = {}
    for key in dict1.keys():  # Bug: should update with dict2
        result[key] = dict1[key]
    return result""",
            
            """def is_palindrome(s):
    s = s.lower()
    s = ''.join(c for c in s if c.isalnum())
    return s == s[::-1]  # Bug: doesn't handle empty string""",
            
            # 高级错误（20 条）
            """class Stack:
    def __init__(self):
        self.items = []
    
    def push(self, item):
        self.items.append(item)
    
    def pop(self):
        return self.items.pop()  # Bug: no empty check
    
    def peek(self):
        return self.items[-1]  # Bug: no empty check""",
            
            """def fibonacci(n):
    if n <= 0:
        return []
    elif n == 1:
        return [0]
    
    fib = [0, 1]
    for i in range(2, n):  # Bug: should be n+1
        fib.append(fib[i-1] + fib[i-2])
    return fib""",
        ],
        
        'fixed_code': [
            """def add(a, b):
    return a + b""",
            
            """def factorial(n):
    if n == 0:
        return 1
    return n * factorial(n-1)""",
            
            """def find_max(lst):
    if not lst:
        return None
    max_val = lst[0]
    for num in lst:
        if num > max_val:
            max_val = num
    return max_val""",
            
            """def reverse_string(s):
    return s[::-1]""",
            
            """def count_vowels(text):
    vowels = 'aeiouAEIOU'
    count = 0
    for char in text:
        if char in vowels:
            count += 1
    return count""",
            
            """def binary_search(arr, target):
    left, right = 0, len(arr) - 1
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1""",
            
            """def merge_dicts(dict1, dict2):
    result = {}
    for key in dict1.keys():
        result[key] = dict1[key]
    result.update(dict2)
    return result""",
            
            """def is_palindrome(s):
    s = s.lower()
    s = ''.join(c for c in s if c.isalnum())
    if not s:
        return True
    return s == s[::-1]""",
            
            """class Stack:
    def __init__(self):
        self.items = []
    
    def push(self, item):
        self.items.append(item)
    
    def pop(self):
        if not self.items:
            raise IndexError("pop from empty stack")
        return self.items.pop()
    
    def peek(self):
        if not self.items:
            raise IndexError("peek from empty stack")
        return self.items[-1]""",
            
            """def fibonacci(n):
    if n <= 0:
        return []
    elif n == 1:
        return [0]
    
    fib = [0, 1]
    for i in range(2, n):
        fib.append(fib[i-1] + fib[i-2])
    return fib""",
        ],
        
        'description': [
            'Fix addition operator',
            'Fix factorial base case',
            'Fix max initialization',
            'Fix string reverse step',
            'Fix vowel counting case',
            'Fix binary search condition',
            'Fix dict merge',
            'Fix palindrome empty check',
            'Fix stack empty check',
            'Fix fibonacci range',
        ]
    }
    
    # 扩展到 100 条（实际使用请收集真实数据）
    # 这里简单重复示例数据用于演示
    while len(custom_data['buggy_code']) < 100:
        idx = len(custom_data['buggy_code']) % 10
        custom_data['buggy_code'].append(custom_data['buggy_code'][idx])
        custom_data['fixed_code'].append(custom_data['fixed_code'][idx])
        custom_data['description'].append(custom_data['description'][idx] + f" (variant {idx})")
    
    data = Dataset.from_dict(custom_data)
    
    def process(x):
        return {
            'prompt': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': f"""
Bug: {x['description']}

Buggy Code:
```python
{x['buggy_code']}
```

Fix the bug and explain your reasoning."""}
            ],
            'fixed_code': x['fixed_code'],
            'description': x['description']
        }
    
    return data.map(process)


# ==================== 奖励函数（精简版 5 个） ====================

def extract_code_block(text: str, tag: str = 'fixed_code') -> str:
    """从 XML 标签或 markdown 代码块提取代码"""
    pattern = f'<{tag}>(.*?)</{tag}>'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    pattern = r'```(?:python)?\n(.*?)\n```'
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""


def format_reward(completions, **kwargs):
    """格式奖励：强制 XML 结构"""
    pattern = r'<reasoning>.*?</reasoning>\s*<fixed_code>.*?</fixed_code>'
    responses = [comp[0]['content'] for comp in completions]
    return [0.5 if re.search(pattern, r, re.DOTALL) else 0.0 for r in responses]


def incremental_format_reward(completions, **kwargs):
    """增量格式奖励：部分得分"""
    responses = [comp[0]['content'] for comp in completions]
    rewards = []
    
    for r in responses:
        score = 0.0
        if '<reasoning>' in r: score += 0.125
        if '</reasoning>' in r: score += 0.125
        if '<fixed_code>' in r: score += 0.125
        if '</fixed_code>' in r: score += 0.125
        rewards.append(score)
    
    return rewards


def syntax_valid_reward(completions, **kwargs):
    """语法正确奖励"""
    responses = [comp[0]['content'] for comp in completions]
    extracted = [extract_code_block(r, 'fixed_code') for r in responses]
    
    rewards = []
    for code in extracted:
        try:
            compile(code, '<string>', 'exec')
            rewards.append(1.0)
        except SyntaxError:
            rewards.append(0.0)
    
    return rewards


def correctness_reward(prompts, completions, fixed_code, **kwargs):
    """正确性奖励：与参考答案匹配"""
    responses = [comp[0]['content'] for comp in completions]
    extracted = [extract_code_block(r, 'fixed_code') for r in responses]
    
    rewards = []
    for pred, ref in zip(extracted, fixed_code):
        if pred.strip() == ref.strip():
            rewards.append(2.0)
        elif normalize_code(pred) == normalize_code(ref):
            rewards.append(1.5)
        else:
            rewards.append(0.0)
    
    return rewards


def code_quality_reward(completions, **kwargs):
    """代码质量奖励"""
    responses = [comp[0]['content'] for comp in completions]
    extracted = [extract_code_block(r, 'fixed_code') for r in responses]
    
    rewards = []
    for code in extracted:
        try:
            compile(code, '<string>', 'exec')
            # 额外奖励：代码简洁、有注释
            if len(code.split('\n')) < 20:
                rewards.append(0.5)
            else:
                rewards.append(0.3)
        except:
            rewards.append(0.0)
    
    return rewards


def normalize_code(code: str) -> str:
    """标准化代码（移除空白和注释）"""
    code = re.sub(r'#.*$', '', code, flags=re.MULTILINE)
    code = re.sub(r'\s+', ' ', code)
    return code.strip()


# ==================== 训练配置 ====================

def get_training_args():
    return GRPOConfig(
        output_dir=OUTPUT_DIR,
        run_name="codefix-grpo-training",
        
        # 学习率
        learning_rate=5e-6,
        warmup_ratio=0.1,
        lr_scheduler_type='cosine',
        
        # Batch 设置
        per_device_train_batch_size=1,
        gradient_accumulation_steps=4,
        
        # GRPO 核心参数
        num_generations=8,        # 每组 8 个修复方案
        max_prompt_length=512,
        max_completion_length=1024,
        
        # 训练时长
        num_train_epochs=1,
        
        # 优化
        bf16=True,
        optim="adamw_8bit",
        max_grad_norm=0.1,
        
        # Logging
        logging_steps=1,
        save_steps=50,
        report_to="none",  # 或 "wandb"
    )


def get_lora_config():
    return LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj"
        ],
        task_type="CAUSAL_LM",
        lora_dropout=0.05,
    )


# ==================== 主训练流程 ====================

def main():
    print("=" * 60)
    print("代码修复 GRPO 训练（精简版）")
    print("=" * 60)
    
    # 1. 加载数据集
    print("\n📚 加载数据集...")
    dataset = get_small_codefix_dataset()
    print(f"✓ 数据集大小：{len(dataset)} 条")
    
    # 2. 加载模型
    print("\n🤖 加载模型...")
    print(f"模型：{MODEL_NAME}")
    
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.bfloat16,
        device_map="auto"
    )
    
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    tokenizer.pad_token = tokenizer.eos_token
    
    print(f"✓ 模型加载完成")
    
    # 3. 训练配置
    print("\n⚙️  训练配置...")
    training_args = get_training_args()
    peft_config = get_lora_config()
    
    print(f"学习率：{training_args.learning_rate}")
    print(f"Generations: {training_args.num_generations}")
    print(f"Epochs: {training_args.num_train_epochs}")
    
    # 4. 初始化训练器
    print("\n🔧 初始化训练器...")
    trainer = GRPOTrainer(
        model=model,
        processing_class=tokenizer,
        reward_funcs=[
            incremental_format_reward,  # 部分得分
            format_reward,              # 完整格式
            syntax_valid_reward,        # 语法正确
            code_quality_reward,        # 代码质量
            correctness_reward,         # 答案正确
        ],
        args=training_args,
        train_dataset=dataset,
        peft_config=peft_config,
    )
    
    print("✓ 训练器初始化完成")
    
    # 5. 开始训练
    print("\n🚀 开始训练...")
    print("-" * 60)
    
    trainer.train()
    
    print("-" * 60)
    print("✓ 训练完成！")
    
    # 6. 保存模型
    print(f"\n💾 保存模型到 {OUTPUT_DIR}/final")
    trainer.save_model(f"{OUTPUT_DIR}/final")
    
    print("\n" + "=" * 60)
    print("训练完成！")
    print("=" * 60)
    
    # 7. 测试推理
    print("\n🧪 测试推理...")
    test_prompt = """
Bug: Fix the addition function

Buggy Code:
```python
def add(a, b):
    return a - b
```

Fix the bug."""
    
    from transformers import pipeline
    pipe = pipeline("text-generation", model=f"{OUTPUT_DIR}/final")
    result = pipe(test_prompt, max_new_tokens=512)
    
    print("\n生成结果:")
    print(result[0]['generated_text'])


if __name__ == "__main__":
    main()
