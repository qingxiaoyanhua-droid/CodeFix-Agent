#!/usr/bin/env python3
"""
合并高质量数据和扩充数据
用于 DPO 训练
"""

import json
from pathlib import Path


def load_high_quality_data():
    """加载高质量数据（81 条 LeetCode 等）"""
    hq_path = "datasets/final/high_quality_data.json"
    with open(hq_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def convert_to_dpo_format(hq_data):
    """将高质量数据转换为 DPO 格式"""
    dpo_data = []

    for item in hq_data:
        # 跳过没有 fixed_code 的
        if 'fixed_code' not in item or 'buggy_code' not in item:
            continue

        buggy = item.get('buggy_code', '').replace('\\n', '\n')
        fixed = item.get('fixed_code', '').replace('\\n', '\n')
        desc = item.get('description', 'Fix the bug')
        bug_type = item.get('bug_type', 'unknown')

        # 构造 prompt
        prompt = f"""Bug: {desc}

Buggy Code:
```python
{buggy}
```

Fix the bug."""

        # 构造 chosen（正确修复）
        chosen = f"""<reasoning>
The code has a {bug_type} bug. {desc}
</reasoning>
<fixed_code>
{fixed}
</fixed_code>"""

        # 构造 rejected（使用 buggy code 作为错误答案）
        rejected = f"""<reasoning>
The code looks correct to me.
</reasoning>
<fixed_code>
{buggy}
</fixed_code>"""

        dpo_data.append({
            "prompt": prompt,
            "chosen": chosen,
            "rejected": rejected,
            "source": "high_quality",
            "bug_type": bug_type
        })

    return dpo_data


def load_expanded_data():
    """加载扩充的数据（300 条）"""
    exp_path = "datasets/dpo_dataset.json"
    with open(exp_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def merge_datasets(hq_data, exp_data):
    """合并两个数据集"""
    # 去重（基于 prompt hash）
    seen_prompts = set()
    merged = []

    # 先加高质量数据
    for item in hq_data:
        prompt_hash = hash(item['prompt'])
        if prompt_hash not in seen_prompts:
            seen_prompts.add(prompt_hash)
            merged.append(item)

    # 再加扩充数据
    for item in exp_data:
        prompt_hash = hash(item['prompt'])
        if prompt_hash not in seen_prompts:
            seen_prompts.add(prompt_hash)
            merged.append(item)

    return merged


def main():
    print("=" * 60)
    print("合并高质量数据和扩充数据")
    print("=" * 60)

    # 1. 加载高质量数据
    print("\n📚 加载高质量数据...")
    hq_raw = load_high_quality_data()
    print(f"  原始数据：{len(hq_raw)} 条")

    # 2. 转换为 DPO 格式
    print("\n🔄 转换为 DPO 格式...")
    hq_dpo = convert_to_dpo_format(hq_raw)
    print(f"  转换后：{len(hq_dpo)} 条")

    # 3. 加载扩充数据
    print("\n📚 加载扩充数据...")
    exp_data = load_expanded_data()
    print(f"  扩充数据：{len(exp_data)} 条")

    # 4. 合并
    print("\n🔀 合并数据集...")
    merged = merge_datasets(hq_dpo, exp_data)
    print(f"  合并后：{len(merged)} 条")

    # 5. 保存
    output_path = "datasets/dpo_dataset_merged.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 数据集已保存到 {output_path}")

    # 6. 统计信息
    print("\n📊 数据统计:")
    bug_types = {}
    sources = {}
    for item in merged:
        bt = item.get('bug_type', 'unknown')
        src = item.get('source', 'expanded')
        bug_types[bt] = bug_types.get(bt, 0) + 1
        sources[src] = sources.get(src, 0) + 1

    print("\n  数据来源:")
    for src, count in sorted(sources.items()):
        print(f"    {src}: {count} 条")

    print("\n  错误类型:")
    for bt, count in sorted(bug_types.items(), key=lambda x: x[1], reverse=True):
        print(f"    {bt}: {count} 条")

    # 7. 创建训练用子集（如果需要快速测试）
    if len(merged) > 100:
        subset = merged[:100]
        subset_path = "datasets/dpo_dataset_100.json"
        with open(subset_path, 'w', encoding='utf-8') as f:
            json.dump(subset, f, ensure_ascii=False, indent=2)
        print(f"\n📦 创建了 100 条子集：{subset_path}")

    return merged


if __name__ == "__main__":
    main()
