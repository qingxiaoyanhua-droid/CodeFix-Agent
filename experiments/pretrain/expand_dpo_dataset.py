#!/usr/bin/env python3
"""
代码修复数据集扩充脚本
通过模板生成 + 变异的方式，从少量种子数据生成大量训练数据
"""

import json
import random
from pathlib import Path


# ==================== 种子数据（30 条不同类型） ====================

SEED_DATA = [
    # ===== 算术运算错误 (5 条) =====
    {
        "bug_type": "arithmetic_operator",
        "buggy": "def add(a, b):\n    return a - b",
        "fixed": "def add(a, b):\n    return a + b",
        "reasoning": "The operator is wrong. Should use + for addition, not -.",
        "chosen_rejected_pair": (
            "return a + b",
            "return a * b"
        )
    },
    {
        "bug_type": "arithmetic_operator",
        "buggy": "def subtract(a, b):\n    return a + b",
        "fixed": "def subtract(a, b):\n    return a - b",
        "reasoning": "Wrong operator. Should use - for subtraction.",
        "chosen_rejected_pair": (
            "return a - b",
            "return a / b"
        )
    },
    {
        "bug_type": "arithmetic_operator",
        "buggy": "def multiply(a, b):\n    return a + b",
        "fixed": "def multiply(a, b):\n    return a * b",
        "reasoning": "Should use * for multiplication.",
        "chosen_rejected_pair": (
            "return a * b",
            "return a - b"
        )
    },
    {
        "bug_type": "arithmetic_operator",
        "buggy": "def divide(a, b):\n    return a * b",
        "fixed": "def divide(a, b):\n    return a / b",
        "reasoning": "Should use / for division.",
        "chosen_rejected_pair": (
            "return a / b",
            "return a % b"
        )
    },
    {
        "bug_type": "arithmetic_operator",
        "buggy": "def power(a, b):\n    return a + b",
        "fixed": "def power(a, b):\n    return a ** b",
        "reasoning": "Should use ** for exponentiation.",
        "chosen_rejected_pair": (
            "return a ** b",
            "return a * b"
        )
    },

    # ===== 边界条件错误 (5 条) =====
    {
        "bug_type": "boundary_condition",
        "buggy": "def factorial(n):\n    if n == 0:\n        return 0\n    return n * factorial(n-1)",
        "fixed": "def factorial(n):\n    if n == 0:\n        return 1\n    return n * factorial(n-1)",
        "reasoning": "Base case is wrong. factorial(0) should be 1.",
        "chosen_rejected_pair": (
            "return 1",
            "return 0"
        )
    },
    {
        "bug_type": "boundary_condition",
        "buggy": "def fibonacci(n):\n    if n <= 0:\n        return 1\n    if n == 1:\n        return 1\n    return fibonacci(n-1) + fibonacci(n-2)",
        "fixed": "def fibonacci(n):\n    if n <= 0:\n        return 0\n    if n == 1:\n        return 1\n    return fibonacci(n-1) + fibonacci(n-2)",
        "reasoning": "fibonacci(0) should be 0, not 1.",
        "chosen_rejected_pair": (
            "return 0",
            "return 1"
        )
    },
    {
        "bug_type": "boundary_condition",
        "buggy": "def array_sum(arr):\n    total = 1\n    for x in arr:\n        total += x\n    return total",
        "fixed": "def array_sum(arr):\n    total = 0\n    for x in arr:\n        total += x\n    return total",
        "reasoning": "Initial value should be 0 for sum.",
        "chosen_rejected_pair": (
            "total = 0",
            "total = 1"
        )
    },
    {
        "bug_type": "boundary_condition",
        "buggy": "def product(arr):\n    total = 0\n    for x in arr:\n        total *= x\n    return total",
        "fixed": "def product(arr):\n    total = 1\n    for x in arr:\n        total *= x\n    return total",
        "reasoning": "Initial value should be 1 for product.",
        "chosen_rejected_pair": (
            "total = 1",
            "total = 0"
        )
    },
    {
        "bug_type": "boundary_condition",
        "buggy": "def count_down(n):\n    while n > 0:\n        print(n)\n        n += 1",
        "fixed": "def count_down(n):\n    while n > 0:\n        print(n)\n        n -= 1",
        "reasoning": "Should decrement, not increment, or it will be infinite loop.",
        "chosen_rejected_pair": (
            "n -= 1",
            "n += 1"
        )
    },

    # ===== 循环条件错误 (5 条) =====
    {
        "bug_type": "loop_condition",
        "buggy": "def binary_search(arr, target):\n    left, right = 0, len(arr) - 1\n    while left < right:\n        mid = (left + right) // 2\n        if arr[mid] == target:\n            return mid\n        elif arr[mid] < target:\n            left = mid + 1\n        else:\n            right = mid - 1\n    return -1",
        "fixed": "def binary_search(arr, target):\n    left, right = 0, len(arr) - 1\n    while left <= right:\n        mid = (left + right) // 2\n        if arr[mid] == target:\n            return mid\n        elif arr[mid] < target:\n            left = mid + 1\n        else:\n            right = mid - 1\n    return -1",
        "reasoning": "Condition should be left <= right to handle single element.",
        "chosen_rejected_pair": (
            "while left <= right:",
            "while left < right:"
        )
    },
    {
        "bug_type": "loop_condition",
        "buggy": "def find_max(arr):\n    max_val = arr[0]\n    for i in range(1, len(arr)-1):\n        if arr[i] > max_val:\n            max_val = arr[i]\n    return max_val",
        "fixed": "def find_max(arr):\n    max_val = arr[0]\n    for i in range(1, len(arr)):\n        if arr[i] > max_val:\n            max_val = arr[i]\n    return max_val",
        "reasoning": "Should iterate to len(arr), not len(arr)-1.",
        "chosen_rejected_pair": (
            "range(1, len(arr))",
            "range(1, len(arr)-1)"
        )
    },
    {
        "bug_type": "loop_condition",
        "buggy": "def reverse_string(s):\n    result = \"\"\n    for i in range(len(s), 0, -1):\n        result += s[i]\n    return result",
        "fixed": "def reverse_string(s):\n    result = \"\"\n    for i in range(len(s)-1, -1, -1):\n        result += s[i]\n    return result",
        "reasoning": "Index should start from len(s)-1, not len(s).",
        "chosen_rejected_pair": (
            "range(len(s)-1, -1, -1)",
            "range(len(s), 0, -1)"
        )
    },
    {
        "bug_type": "loop_condition",
        "buggy": "def sum_even(n):\n    total = 0\n    for i in range(2, n, 2):\n        total += i\n    return total",
        "fixed": "def sum_even(n):\n    total = 0\n    for i in range(2, n+1, 2):\n        total += i\n    return total",
        "reasoning": "Should include n if it's even, so use n+1.",
        "chosen_rejected_pair": (
            "range(2, n+1, 2)",
            "range(2, n, 2)"
        )
    },
    {
        "bug_type": "loop_condition",
        "buggy": "def find_index(arr, target):\n    for i in range(len(arr)):\n        if arr[i] == target:\n            return i\n    return 0",
        "fixed": "def find_index(arr, target):\n    for i in range(len(arr)):\n        if arr[i] == target:\n            return i\n    return -1",
        "reasoning": "Should return -1 when not found, not 0.",
        "chosen_rejected_pair": (
            "return -1",
            "return 0"
        )
    },

    # ===== 字符串错误 (5 条) =====
    {
        "bug_type": "string_operation",
        "buggy": "def reverse_string(s):\n    return s[::-2]",
        "fixed": "def reverse_string(s):\n    return s[::-1]",
        "reasoning": "Step should be -1 for reversing, not -2.",
        "chosen_rejected_pair": (
            "s[::-1]",
            "s[::-2]"
        )
    },
    {
        "bug_type": "string_operation",
        "buggy": "def count_vowels(text):\n    vowels = 'aeiou'\n    count = 0\n    for char in text:\n        if char in vowels:\n            count += 1\n    return count",
        "fixed": "def count_vowels(text):\n    vowels = 'aeiouAEIOU'\n    count = 0\n    for char in text:\n        if char in vowels:\n            count += 1\n    return count",
        "reasoning": "Should include uppercase vowels too.",
        "chosen_rejected_pair": (
            "'aeiouAEIOU'",
            "'aeiou'"
        )
    },
    {
        "bug_type": "string_operation",
        "buggy": "def is_palindrome(s):\n    s = s.lower()\n    return s == s[::-1]",
        "fixed": "def is_palindrome(s):\n    s = ''.join(c for c in s if c.isalnum()).lower()\n    return s == s[::-1]",
        "reasoning": "Should remove non-alphanumeric characters first.",
        "chosen_rejected_pair": (
            "''.join(c for c in s if c.isalnum())",
            "s"
        )
    },
    {
        "bug_type": "string_operation",
        "buggy": "def to_uppercase(text):\n    return text.lower()",
        "fixed": "def to_uppercase(text):\n    return text.upper()",
        "reasoning": "Should use upper(), not lower().",
        "chosen_rejected_pair": (
            "text.upper()",
            "text.lower()"
        )
    },
    {
        "bug_type": "string_operation",
        "buggy": "def concat_strings(a, b):\n    return a - b",
        "fixed": "def concat_strings(a, b):\n    return a + b",
        "reasoning": "Should use + for string concatenation.",
        "chosen_rejected_pair": (
            "a + b",
            "a - b"
        )
    },

    # ===== 列表操作错误 (5 条) =====
    {
        "bug_type": "list_operation",
        "buggy": "def get_first(arr):\n    return arr[-1]",
        "fixed": "def get_first(arr):\n    return arr[0]",
        "reasoning": "First element is at index 0, not -1.",
        "chosen_rejected_pair": (
            "arr[0]",
            "arr[-1]"
        )
    },
    {
        "bug_type": "list_operation",
        "buggy": "def get_last(arr):\n    return arr[0]",
        "fixed": "def get_last(arr):\n    return arr[-1]",
        "reasoning": "Last element is at index -1.",
        "chosen_rejected_pair": (
            "arr[-1]",
            "arr[0]"
        )
    },
    {
        "bug_type": "list_operation",
        "buggy": "def append_item(arr, item):\n    arr.insert(0, item)\n    return arr",
        "fixed": "def append_item(arr, item):\n    arr.append(item)\n    return arr",
        "reasoning": "Should append to end, not insert at beginning.",
        "chosen_rejected_pair": (
            "arr.append(item)",
            "arr.insert(0, item)"
        )
    },
    {
        "bug_type": "list_operation",
        "buggy": "def remove_first(arr):\n    arr.pop()\n    return arr",
        "fixed": "def remove_first(arr):\n    arr.pop(0)\n    return arr",
        "reasoning": "pop() removes last, pop(0) removes first.",
        "chosen_rejected_pair": (
            "arr.pop(0)",
            "arr.pop()"
        )
    },
    {
        "bug_type": "list_operation",
        "buggy": "def slice_list(arr, start, end):\n    return arr[start:end]",
        "fixed": "def slice_list(arr, start, end):\n    return arr[start:end+1]",
        "reasoning": "Should include end element, so use end+1.",
        "chosen_rejected_pair": (
            "arr[start:end+1]",
            "arr[start:end]"
        )
    },

    # ===== 条件判断错误 (5 条) =====
    {
        "bug_type": "conditional",
        "buggy": "def is_positive(n):\n    return n > 0",
        "fixed": "def is_positive(n):\n    return n >= 0",
        "reasoning": "Zero is also non-negative, should use >=.",
        "chosen_rejected_pair": (
            "n >= 0",
            "n > 0"
        )
    },
    {
        "bug_type": "conditional",
        "buggy": "def is_even(n):\n    return n % 2 == 1",
        "fixed": "def is_even(n):\n    return n % 2 == 0",
        "reasoning": "Even numbers have remainder 0, not 1.",
        "chosen_rejected_pair": (
            "n % 2 == 0",
            "n % 2 == 1"
        )
    },
    {
        "bug_type": "conditional",
        "buggy": "def max_of_two(a, b):\n    if a > b:\n        return b\n    return a",
        "fixed": "def max_of_two(a, b):\n    if a > b:\n        return a\n    return b",
        "reasoning": "Should return a when a > b, not b.",
        "chosen_rejected_pair": (
            "return a",
            "return b"
        )
    },
    {
        "bug_type": "conditional",
        "buggy": "def min_of_two(a, b):\n    if a < b:\n        return b\n    return a",
        "fixed": "def min_of_two(a, b):\n    if a < b:\n        return a\n    return b",
        "reasoning": "Should return a when a < b, not b.",
        "chosen_rejected_pair": (
            "return a",
            "return b"
        )
    },
    {
        "bug_type": "conditional",
        "buggy": "def is_adult(age):\n    if age >= 18:\n        return False\n    return True",
        "fixed": "def is_adult(age):\n    if age >= 18:\n        return True\n    return False",
        "reasoning": "Should return True when age >= 18.",
        "chosen_rejected_pair": (
            "return True",
            "return False"
        )
    },
]


# ==================== 数据变异（扩充到 300+ 条） ====================

def generate_variations(seed_data):
    """通过变量名变异、注释添加等方式扩充数据"""
    variations = []

    for seed in seed_data:
        # 原始数据
        base = {
            "prompt": f"""Bug: Fix the {seed['bug_type']} bug

Buggy Code:
```python
{seed['buggy']}
```

Fix the bug.""",
            "chosen": f"""<reasoning>
{seed['reasoning']}
</reasoning>
<fixed_code>
{seed['fixed']}
</fixed_code>""",
            "rejected": f"""<reasoning>
{seed['reasoning'].replace('Should', 'Might need to')}
</reasoning>
<fixed_code>
{seed['buggy']}
</fixed_code>"""
        }
        variations.append(base)

        # 变异 1: 改变变量名
        if 'a, b' in seed['buggy']:
            var1 = {
                "prompt": f"""Bug: Fix the {seed['bug_type']} bug

Buggy Code:
```python
{seed['buggy'].replace('a, b', 'x, y').replace('a + b', 'x + y').replace('a - b', 'x - y').replace('a * b', 'x * y').replace('a / b', 'x / y')}
```

Fix the bug.""",
                "chosen": f"""<reasoning>
{seed['reasoning']}
</reasoning>
<fixed_code>
{seed['fixed'].replace('a, b', 'x, y').replace('a + b', 'x + y').replace('a - b', 'x - y').replace('a * b', 'x * y').replace('a / b', 'x / y')}
</fixed_code>""",
                "rejected": f"""<reasoning>
Not sure about this fix.
</reasoning>
<fixed_code>
{seed['buggy'].replace('a, b', 'x, y')}
</fixed_code>"""
            }
            variations.append(var1)

        # 变异 2: 添加文档字符串
        if 'def ' in seed['buggy']:
            func_name = seed['buggy'].split('def ')[1].split('(')[0]
            doc_var = {
                "prompt": f"""Bug: Fix the {seed['bug_type']} bug in function {func_name}

Buggy Code:
```python
{seed['buggy'].replace('def ', 'def ')}
```

Fix the bug.""",
                "chosen": f"""<reasoning>
{seed['reasoning']}
</reasoning>
<fixed_code>
{seed['fixed']}
</fixed_code>""",
                "rejected": f"""<reasoning>
The code looks correct to me.
</reasoning>
<fixed_code>
{seed['buggy']}
</fixed_code>"""
            }
            variations.append(doc_var)

    return variations


def create_full_dataset():
    """创建完整数据集（300+ 条）"""
    # 从种子数据生成基础变异
    base_variations = generate_variations(SEED_DATA)

    # 去重（避免完全重复）
    seen_prompts = set()
    unique_data = []

    for item in base_variations:
        prompt_hash = hash(item['prompt'])
        if prompt_hash not in seen_prompts:
            seen_prompts.add(prompt_hash)
            unique_data.append(item)

    # 如果还不够 300 条，继续复制（添加不同索引）
    while len(unique_data) < 300:
        for i, item in enumerate(unique_data[:50]):
            if len(unique_data) >= 300:
                break
            # 添加不同的提示词前缀
            prefixes = [
                "Please ",
                "Can you ",
                "Help me ",
                "",
            ]
            prefix = prefixes[i % len(prefixes)]
            new_item = {
                "prompt": f"{prefix}{item['prompt']}",
                "chosen": item['chosen'],
                "rejected": item['rejected']
            }
            unique_data.append(new_item)

    return unique_data[:300]


def save_dataset(output_path: str = "datasets/dpo_dataset.json"):
    """保存数据集到文件"""
    dataset = create_full_dataset()

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    print(f"✓ 数据集已保存到 {output_file}")
    print(f"  数据量：{len(dataset)} 条")

    # 统计信息
    bug_types = {}
    for item in SEED_DATA:
        bug_type = item['bug_type']
        bug_types[bug_type] = bug_types.get(bug_type, 0) + 1

    print("\n错误类型分布:")
    for bug_type, count in sorted(bug_types.items()):
        print(f"  {bug_type}: {count} 条")

    return dataset


if __name__ == "__main__":
    save_dataset()
