#!/usr/bin/env python3
"""
数据格式转换脚本
将 high_quality_data.json + so_pairs_enhanced.json
转换为 SFT / DPO 训练格式

用法:
    python prepare_training_data.py          # 生成所有格式
    python prepare_training_data.py --sft    # 仅 SFT
    python prepare_training_data.py --dpo    # 仅 DPO
    python prepare_training_data.py --preview # 仅预览前10条
"""

import json
import argparse
import random
import sys
from pathlib import Path
from typing import List, Dict, Any

sys.stdout.reconfigure(encoding='utf-8')

random.seed(42)

# ==================== CoT 格式模板 ====================

COT_TEMPLATE = """<think>
[Step 1: Bug Identification]
{step1}

[Step 2: Root Cause Analysis]
{step2}

[Step 3: Fix Strategy]
{step3}

[Step 4: Edge Case Check]
{step4}
</think>

<fixed_code>
```python
{fixed_code}
```
</fixed_code>"""

COT_STEP_TEMPLATES = {
    "wrong_data_structure": {
        "step1": "The code uses a {wrong} instead of a {correct}. This is a wrong_data_structure bug.",
        "step2": "When using {wrong}, operations like 'in' or index lookups are O(n) because they require linear scanning. For {correct}, the same operations are O(1) using hash-based lookup.",
        "step3": "Replace the {wrong} literal with {correct} literal. Update all access patterns from [{wrong_item}] to [{correct_key}].",
        "step4": "Edge cases: (1) Empty {wrong} → empty {correct}. (2) Single element. (3) Duplicate keys."
    },
    "wrong_argument_order": {
        "step1": "The function arguments are in the wrong order. This causes the operation to produce incorrect results.",
        "step2": "When arguments are swapped, the operation applies to the wrong values. For example, a % b vs b % a produce very different results.",
        "step3": "Swap the argument order to match the correct mathematical/algorithmic definition.",
        "step4": "Edge cases: (1) Zero as first argument. (2) Negative numbers. (3) Equal arguments."
    },
    "off_by_one": {
        "step1": "There is an off-by-one error in a boundary condition. The loop or index goes one step too far or too short.",
        "step2": "The condition uses '>=' when it should use '>', or '<=' when it should use '<'. This causes the loop to execute one extra or one fewer iteration than needed.",
        "step3": "Adjust the boundary condition to the correct operator. In binary search, use '<' for the main loop and handle remaining elements separately.",
        "step4": "Edge cases: (1) Empty input. (2) Single element. (3) Target at first/last position."
    },
    "wrong_expression": {
        "step1": "The expression used in a calculation or condition is incorrect. The wrong operator or value is used.",
        "step2": "Using '{wrong}' instead of '{correct}' changes the mathematical meaning of the expression, leading to incorrect results.",
        "step3": "Replace the '{wrong}' operator/value with '{correct}' in the expression.",
        "step4": "Edge cases: (1) Boundary values. (2) Large/small inputs. (3) Negative values."
    },
    "wrong_operator": {
        "step1": "The wrong bitwise or arithmetic operator is used. '{wrong}' should be '{correct}'.",
        "step2": "The '{wrong}' operator performs a different operation than intended. For bit counting, XOR clears all bits at once instead of one at a time.",
        "step3": "Replace '{wrong}' with '{correct}' at the specific line identified.",
        "step4": "Edge cases: (1) Zero input. (2) Power of 2. (3) All bits set."
    },
    "default": {
        "step1": "There is a bug in the function that causes incorrect output for some inputs.",
        "step2": "The error occurs when the code executes a specific code path with certain input values, producing unexpected results.",
        "step3": "Identify the incorrect operation and replace it with the correct one. Keep the overall structure intact.",
        "step4": "Edge cases: (1) Normal inputs. (2) Boundary values. (3) Edge cases specific to the algorithm."
    }
}

BUGGY_REJECTED_TEMPLATE = """<think>
[Step 1: Bug Identification]
I don't see any bug in this code. It looks correct to me.

[Step 2: Root Cause Analysis]
The code appears to be working as intended based on a quick review.

[Step 3: Fix Strategy]
No fix needed.

[Step 4: Edge Case Check]
The code handles edge cases appropriately.
</think>

<fixed_code>
```python
{buggy_code}
```
</fixed_code>"""


# ==================== 工具函数 ====================

def infer_bug_type(buggy_code: str, fixed_code: str, description: str = "") -> str:
    """从代码中推断 bug 类型"""
    desc_lower = description.lower()

    if any(k in desc_lower for k in ["list", "dict", "hash", "set", "lookup", "O(n²)", "O(n^2)"]):
        return "wrong_data_structure"
    if any(k in desc_lower for k in ["argument", "parameter", "order", "swap"]):
        return "wrong_argument_order"
    if any(k in desc_lower for k in ["off-by-one", "off by one", "boundary", "range", "index"]):
        return "off_by_one"
    if any(k in desc_lower for k in ["expression", "formula", "equation"]):
        return "wrong_expression"
    if any(k in desc_lower for k in ["operator", "bitwise", "xor", "and", "or"]):
        return "wrong_operator"

    # 从代码内容推断
    if "def " not in buggy_code:
        return "default"

    # 检查常见bug模式
    if ("= []" in buggy_code or "= {}" in buggy_code) and ("= {}" in fixed_code or "= set()" in fixed_code):
        return "wrong_data_structure"
    if buggy_code.count("return") != fixed_code.count("return"):
        return "wrong_argument_order"

    return "default"


def extract_function_signature(code: str) -> str:
    """从代码中提取函数签名"""
    import re
    match = re.search(r'def (\w+)\s*\(', code)
    return match.group(1) if match else "function"


def generate_cot(buggy_code: str, fixed_code: str, description: str, bug_type: str = None) -> tuple:
    """为 buggy/fixed 对生成 CoT reasoning"""
    if bug_type is None:
        bug_type = infer_bug_type(buggy_code, fixed_code, description)

    template = COT_STEP_TEMPLATES.get(bug_type, COT_STEP_TEMPLATES["default"])

    func_name = extract_function_signature(buggy_code)

    # 填充模板中的占位符
    def fill(template_str, **kwargs):
        for k, v in kwargs.items():
            template_str = template_str.replace(f"{{{k}}}", v)
        return template_str

    # 针对不同bug类型填充具体内容
    if bug_type == "wrong_data_structure":
        if "[]" in buggy_code and "{}" in fixed_code:
            step1 = fill(template["step1"], wrong="list", correct="dictionary")
            step2 = fill(template["step2"], wrong="list", correct="dictionary",
                         wrong_item="list[-1]", correct_key="dict[key]")
            step3 = fill(template["step3"], wrong="list", correct="dictionary",
                         wrong_item="list.index(x)", correct_key="dict[x]")
            step4 = fill(template["step4"], wrong="list", correct="dictionary")
        else:
            step1 = step2 = step3 = step4 = template["default"].format(bug=func_name)
    elif bug_type == "wrong_argument_order":
        step1 = fill(template["step1"])
        step2 = fill(template["step2"])
        step3 = fill(template["step3"])
        step4 = fill(template["step4"])
    elif bug_type == "off_by_one":
        step1 = fill(template["step1"])
        step2 = fill(template["step2"])
        step3 = fill(template["step3"])
        step4 = fill(template["step4"])
    elif bug_type == "wrong_expression":
        step1 = fill(template["step1"], wrong="X", correct="Y")
        step2 = fill(template["step2"], wrong="X", correct="Y")
        step3 = fill(template["step3"], wrong="X", correct="Y")
        step4 = fill(template["step4"])
    elif bug_type == "wrong_operator":
        step1 = fill(template["step1"], wrong="X", correct="Y")
        step2 = fill(template["step2"], wrong="X", correct="Y")
        step3 = fill(template["step3"], wrong="X", correct="Y")
        step4 = fill(template["step4"])
    else:
        step1 = f"The function `{func_name}` has a logical bug causing incorrect results."
        step2 = f"The bug occurs when the code executes with certain input values, producing unexpected output."
        step3 = f"Identify the incorrect operation and replace with the correct one while keeping the overall structure."
        step4 = "Test with (1) normal inputs, (2) edge cases like empty/single element, (3) boundary values."

    return (
        COT_TEMPLATE.format(
            step1=step1, step2=step2, step3=step3, step4=step4,
            fixed_code=fixed_code.strip()
        ),
        bug_type
    )


# ==================== 格式转换 ====================

def load_data(path: str) -> List[Dict]:
    """加载 JSON 数据文件"""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    return data


def filter_valid_pairs(data: List[Dict]) -> List[Dict]:
    """过滤出有效的 buggy/fixed code pair"""
    valid = []
    for item in data:
        buggy = item.get("buggy_code", "").strip()
        fixed = item.get("fixed_code", "").strip()
        if not buggy or not fixed:
            continue
        # 过滤非代码内容
        if len(buggy) < 20 or len(fixed) < 20:
            continue
        if buggy == fixed:
            continue
        # 过滤太长的代码（不适合小模型）
        if len(buggy) > 2000 or len(fixed) > 2000:
            continue
        valid.append(item)
    return valid


def convert_to_sft(data: List[Dict], bug_type_hints: Dict = None) -> List[Dict]:
    """
    转换为 SFT 格式:
    {
        "messages": [
            {"role": "user", "content": "Bug: ...\n\nBuggy Code:\n..."},
            {"role": "assistant", "content": "<reasoning>...\n<fixed_code>..."}
        ]
    }
    """
    sft_data = []
    bug_type_hints = bug_type_hints or {}

    for item in data:
        buggy = item["buggy_code"].strip()
        fixed = item["fixed_code"].strip()
        desc = item.get("description", "Fix the bug in the code.")
        source = item.get("source", "unknown")

        # 从字段获取 bug_type 或自动推断
        bug_type = item.get("bug_type", "default")
        if bug_type == "unknown":
            bug_type = infer_bug_type(buggy, fixed, desc)

        # 生成 CoT
        cot_content, inferred_type = generate_cot(buggy, fixed, desc, bug_type)
        final_type = bug_type if bug_type != "default" else inferred_type

        sft_item = {
            "messages": [
                {
                    "role": "user",
                    "content": f"Bug: {desc}\n\nBuggy Code:\n```python\n{buggy}\n```\n\nFix the bug. Follow the required format."
                },
                {
                    "role": "assistant",
                    "content": cot_content
                }
            ],
            "bug_type": final_type,
            "source": source
        }
        sft_data.append(sft_item)

    return sft_data


def convert_to_dpo(data: List[Dict]) -> List[Dict]:
    """
    转换为 DPO 格式:
    {
        "prompt": "Bug: ...\n\nBuggy Code:\n...",
        "chosen": "<reasoning>...\n<fixed_code>...",
        "rejected": "<reasoning>...\n<fixed_code>..."
    }
    """
    dpo_data = []

    for item in data:
        buggy = item["buggy_code"].strip()
        fixed = item["fixed_code"].strip()
        desc = item.get("description", "Fix the bug in the code.")
        source = item.get("source", "unknown")
        bug_type = item.get("bug_type", "default")
        if bug_type == "unknown":
            bug_type = infer_bug_type(buggy, fixed, desc)

        # 生成正确修复的 CoT
        chosen_content, inferred_type = generate_cot(buggy, fixed, desc, bug_type)
        final_type = bug_type if bug_type != "default" else inferred_type

        # 生成错误修复的 rejected 内容（使用原始 buggy 代码作为 rejected）
        rejected_content = BUGGY_REJECTED_TEMPLATE.format(buggy_code=buggy)

        dpo_item = {
            "prompt": f"Bug: {desc}\n\nBuggy Code:\n```python\n{buggy}\n```\n\nFix the bug. Follow the required format.",
            "chosen": chosen_content,
            "rejected": rejected_content,
            "bug_type": final_type,
            "source": source
        }
        dpo_data.append(dpo_item)

    return dpo_data


def split_train_eval(data: List[Dict], eval_ratio: float = 0.1) -> tuple:
    """按 bug_type 分层抽样拆分训练集和验证集"""
    by_type = {}
    for item in data:
        bt = item.get("bug_type", "default")
        by_type.setdefault(bt, []).append(item)

    train, eval_set = [], []
    for bt, items in by_type.items():
        random.shuffle(items)
        n_eval = max(1, int(len(items) * eval_ratio))
        eval_set.extend(items[:n_eval])
        train.extend(items[n_eval:])

    random.shuffle(train)
    random.shuffle(eval_set)
    return train, eval_set


# ==================== 主函数 ====================

def main():
    parser = argparse.ArgumentParser(description="数据格式转换")
    parser.add_argument("--sft", action="store_true", help="仅生成 SFT 格式")
    parser.add_argument("--dpo", action="store_true", help="仅生成 DPO 格式")
    parser.add_argument("--preview", action="store_true", help="仅预览前10条")
    parser.add_argument("--output", type=str, default="datasets/training", help="输出目录")
    args = parser.parse_args()

    base_dir = Path(__file__).parent
    output_dir = base_dir / args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    # 加载数据
    sources = [
        ("bushu/datasets/final/high_quality_data.json", 1.0),     # 高质量数据，权重1
        ("bushu/datasets/cleaned/so_pairs_enhanced.json", 0.5),    # SO增强数据，权重0.5（数据量大）
    ]

    all_data = []
    for path_str, weight in sources:
        path = base_dir / path_str
        if not path.exists():
            print(f"[WARN] File not found: {path}, skipping")
            continue
        data = load_data(str(path))
        valid = filter_valid_pairs(data)
        print(f"  [OK] {path_str}: {len(data)} -> {len(valid)} valid (weight={weight})")
        # 乘以权重（重复数据以增加采样概率）
        all_data.extend(valid * (2 if weight >= 1.0 else 1))

    if not all_data:
        print("[FAIL] 没有加载到任何数据！")
        return

    print(f"\n总计: {len(all_data)} 条有效数据")
    print(f"Bug类型分布:")
    type_count = {}
    for item in all_data:
        bt = item.get("bug_type", "default")
        if bt == "unknown":
            bt = infer_bug_type(item["buggy_code"], item["fixed_code"], item.get("description",""))
        type_count[bt] = type_count.get(bt, 0) + 1
    for bt, cnt in sorted(type_count.items(), key=lambda x: -x[1]):
        print(f"  {bt}: {cnt}")

    if args.preview:
        print("\n--- 前10条预览 ---")
        for i, item in enumerate(all_data[:10]):
            print(f"\n[{i+1}] Bug Type: {item.get('bug_type','?')} | Source: {item.get('source','?')}")
            print(f"Description: {item.get('description','')[:80]}")
            print(f"Buggy (first 100 chars): {item['buggy_code'][:100]}...")
            print(f"Fixed (first 100 chars): {item['fixed_code'][:100]}...")
        return

    # 拆分
    train_data, eval_data = split_train_eval(all_data)
    print(f"\n拆分: 训练集 {len(train_data)} 条, 验证集 {len(eval_data)} 条")

    if not args.dpo:  # 默认或 --sft
        print("\n生成 SFT 格式...")
        train_sft = convert_to_sft(train_data)
        eval_sft = convert_to_sft(eval_data)

        train_path = output_dir / "sft_train.json"
        eval_path = output_dir / "sft_eval.json"

        with open(train_path, 'w', encoding='utf-8') as f:
            json.dump(train_sft, f, ensure_ascii=False, indent=2)
        with open(eval_path, 'w', encoding='utf-8') as f:
            json.dump(eval_sft, f, ensure_ascii=False, indent=2)

        print(f"  [OK] SFT 训练集: {train_path} ({len(train_sft)} 条)")
        print(f"  [OK] SFT 验证集: {eval_path} ({len(eval_sft)} 条)")

        # 预览一条 SFT
        print("\n  SFT 示例:")
        ex = train_sft[0]
        print(f"  User: {ex['messages'][0]['content'][:100]}...")
        print(f"  Assistant: {ex['messages'][1]['content'][:200]}...")

    if not args.sft:  # 默认或 --dpo
        print("\n生成 DPO 格式...")
        train_dpo = convert_to_dpo(train_data)
        eval_dpo = convert_to_dpo(eval_data)

        train_path = output_dir / "dpo_train.json"
        eval_path = output_dir / "dpo_eval.json"

        with open(train_path, 'w', encoding='utf-8') as f:
            json.dump(train_dpo, f, ensure_ascii=False, indent=2)
        with open(eval_path, 'w', encoding='utf-8') as f:
            json.dump(eval_dpo, f, ensure_ascii=False, indent=2)

        print(f"  [OK] DPO 训练集: {train_path} ({len(train_dpo)} 条)")
        print(f"  [OK] DPO 验证集: {eval_path} ({len(eval_dpo)} 条)")

    print(f"\n[DONE] 数据准备完成！输出目录: {output_dir}")
    print(f"文件清单:")
    for f in sorted(output_dir.glob("*")):
        print(f"  {f.name}: {f.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
