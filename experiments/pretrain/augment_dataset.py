#!/usr/bin/env python3
"""
Dataset Augmentation via Mutation (半天扩增 5x)

Strategy: take 596 seed samples, apply 5 types of mutations to generate variants.
No crawling needed, no API calls needed for basic mutations.

596 seeds × 5 mutations = ~3000 samples

Mutation types:
  1. Variable Renaming: change variable names (semantics preserved)
  2. Bug Type Transfer: inject different bug types into same code
  3. Complexity Scaling: add parameters / edge cases to existing functions
  4. Style Variation: list comprehension ↔ for loop, ternary ↔ if-else
  5. Docstring/Comment Variation: add/change descriptions
"""

import json
import re
import random
import copy
import os
from typing import List, Dict


# ==================== Mutation Functions ====================

VARIABLE_POOLS = {
    "list_names": ["arr", "nums", "data", "items", "values", "elements", "lst", "seq"],
    "index_names": ["i", "j", "k", "idx", "pos", "index", "ptr"],
    "result_names": ["result", "res", "ans", "output", "ret", "out"],
    "temp_names": ["tmp", "temp", "val", "cur", "current", "x"],
    "count_names": ["count", "cnt", "total", "n", "num", "freq"],
}


def mutate_variable_rename(code: str) -> str:
    """Mutation 1: Rename variables (semantics preserved)"""
    # Find single-letter or common variable names and swap them
    renames = {}

    for category, pool in VARIABLE_POOLS.items():
        for var in pool:
            pattern = rf'\b{re.escape(var)}\b'
            if re.search(pattern, code) and var not in ('i', 'j', 'k', 'n'):
                candidates = [v for v in pool if v != var and v not in code]
                if candidates:
                    new_name = random.choice(candidates)
                    renames[var] = new_name

    # Apply at most 2 renames to keep code readable
    for old, new in list(renames.items())[:2]:
        code = re.sub(rf'\b{re.escape(old)}\b', new, code)

    return code


BUG_INJECTIONS = [
    # (pattern, replacement, bug_type, description)
    (r'return (\w+) \+ (\w+)', r'return \1 - \2', 'wrong_operator', 'Changed + to -'),
    (r'return (\w+) - (\w+)', r'return \1 + \2', 'wrong_operator', 'Changed - to +'),
    (r'<= ', '< ', 'off_by_one', 'Changed <= to <'),
    (r'< ', '<= ', 'off_by_one', 'Changed < to <='),
    (r'>= ', '> ', 'off_by_one', 'Changed >= to >'),
    (r'== 0', '== 1', 'wrong_constant', 'Changed 0 to 1'),
    (r'== 1', '== 0', 'wrong_constant', 'Changed 1 to 0'),
    (r'return 0\b', 'return 1', 'wrong_return', 'Changed return 0 to return 1'),
    (r'return 1\b', 'return 0', 'wrong_return', 'Changed return 1 to return 0'),
    (r'\[0\]', '[-1]', 'wrong_index', 'Changed [0] to [-1]'),
    (r'\[-1\]', '[0]', 'wrong_index', 'Changed [-1] to [0]'),
    (r'\[::-1\]', '[::-2]', 'wrong_slice', 'Changed [::-1] to [::-2]'),
    (r'append\(', 'extend([', 'wrong_method', 'Changed append to extend'),
]


def mutate_bug_transfer(code: str, original_bug_type: str) -> tuple:
    """Mutation 2: Inject a different bug type into the same code"""
    # Try each injection, pick one that's different from original
    random.shuffle(BUG_INJECTIONS)

    for pattern, replacement, bug_type, desc in BUG_INJECTIONS:
        if bug_type == original_bug_type:
            continue
        if re.search(pattern, code):
            buggy = re.sub(pattern, replacement, code, count=1)
            if buggy != code:
                return buggy, bug_type, desc

    return None, None, None


STYLE_TRANSFORMS = [
    # for loop → list comprehension (simple cases)
    (
        r'(\w+) = \[\]\s*\n\s*for (\w+) in (.+?):\s*\n\s*\1\.append\((.+?)\)',
        lambda m: f'{m.group(1)} = [{m.group(4)} for {m.group(2)} in {m.group(3)}]',
        'Convert for-append to list comprehension'
    ),
]


def mutate_style(code: str) -> str:
    """Mutation 3: Style variation (for loop ↔ comprehension, etc.)"""
    for pattern, replacement, _ in STYLE_TRANSFORMS:
        if re.search(pattern, code):
            if callable(replacement):
                code = re.sub(pattern, replacement, code, count=1)
            else:
                code = re.sub(pattern, replacement, code, count=1)
            break
    return code


def mutate_add_docstring(code: str, bug_desc: str) -> str:
    """Mutation 4: Add or change docstring"""
    docstrings = [
        f'    """Fix: {bug_desc}"""',
        f'    # {bug_desc}',
        f'    """{bug_desc}\n    \n    Returns:\n        The corrected result.\n    """',
    ]
    ds = random.choice(docstrings)

    # Insert after first def line
    lines = code.split('\n')
    for i, line in enumerate(lines):
        if line.strip().startswith('def '):
            lines.insert(i + 1, ds)
            break

    return '\n'.join(lines)


def mutate_parameter_name(code: str) -> str:
    """Mutation 5: Rename function parameters"""
    param_renames = {
        'a': 'x', 'b': 'y', 'n': 'num', 's': 'text',
        'lst': 'array', 'arr': 'numbers', 'target': 'goal',
    }
    for old, new in param_renames.items():
        pattern = rf'\b{old}\b'
        if re.search(pattern, code):
            code = re.sub(pattern, new, code)
            break
    return code


# ==================== Main Augmentation Pipeline ====================

def augment_dataset(input_path: str, output_path: str, target_multiplier: int = 5):
    """
    Augment dataset by applying mutations.

    Args:
        input_path: path to original dataset (JSON)
        output_path: path to save augmented dataset
        target_multiplier: target size = original × multiplier
    """
    if not os.path.exists(input_path):
        print(f"Input file not found: {input_path}")
        return

    with open(input_path, 'r', encoding='utf-8') as f:
        original_data = json.load(f)

    print(f"Original dataset: {len(original_data)} samples")
    print(f"Target: ~{len(original_data) * target_multiplier} samples")

    augmented = list(original_data)  # keep all originals
    seen_codes = set()

    for item in original_data:
        seen_codes.add(item.get('chosen', '')[:200])

    mutation_stats = {
        'variable_rename': 0,
        'bug_transfer': 0,
        'style_variation': 0,
        'docstring_added': 0,
        'param_rename': 0,
    }

    for item in original_data:
        prompt = item.get('prompt', '')
        chosen = item.get('chosen', '')
        bug_type = item.get('bug_type', 'unknown')

        # Extract code from chosen
        code_match = re.search(r'<fixed_code>\s*(.*?)\s*</fixed_code>', chosen, re.DOTALL)
        if not code_match:
            continue
        fixed_code = code_match.group(1).strip()

        buggy_match = re.search(r'```python\s*(.*?)\s*```', prompt, re.DOTALL)
        buggy_code = buggy_match.group(1).strip() if buggy_match else ""

        mutations = [
            ('variable_rename', lambda c: mutate_variable_rename(c)),
            ('param_rename', lambda c: mutate_parameter_name(c)),
            ('docstring_added', lambda c: mutate_add_docstring(c, bug_type)),
            ('style_variation', lambda c: mutate_style(c)),
        ]

        for mut_name, mut_fn in mutations:
            try:
                new_fixed = mut_fn(fixed_code)
                if new_fixed == fixed_code:
                    continue
                if new_fixed[:200] in seen_codes:
                    continue

                new_buggy = mut_fn(buggy_code) if buggy_code else buggy_code

                new_item = copy.deepcopy(item)
                new_item['chosen'] = chosen.replace(fixed_code, new_fixed)
                if buggy_code and new_buggy != buggy_code:
                    new_item['prompt'] = prompt.replace(buggy_code, new_buggy)
                new_item['source'] = f"augmented_{mut_name}"
                new_item['original_source'] = item.get('source', 'unknown')

                augmented.append(new_item)
                seen_codes.add(new_fixed[:200])
                mutation_stats[mut_name] += 1

            except Exception:
                continue

        # Bug transfer mutation (generates new bug type from same code)
        if buggy_code:
            new_buggy, new_bug_type, desc = mutate_bug_transfer(
                fixed_code, bug_type
            )
            if new_buggy and new_buggy[:200] not in seen_codes:
                new_item = copy.deepcopy(item)
                new_prompt = prompt.replace(buggy_code, new_buggy)
                bug_desc_match = re.search(r'Bug:\s*(.*?)\n', prompt)
                if bug_desc_match:
                    new_prompt = new_prompt.replace(
                        bug_desc_match.group(1), desc
                    )
                new_item['prompt'] = new_prompt
                new_item['bug_type'] = new_bug_type
                new_item['source'] = 'augmented_bug_transfer'
                augmented.append(new_item)
                seen_codes.add(new_buggy[:200])
                mutation_stats['bug_transfer'] += 1

    # Deduplicate
    final = []
    final_codes = set()
    for item in augmented:
        key = item.get('chosen', '')[:300]
        if key not in final_codes:
            final_codes.add(key)
            final.append(item)

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(final, f, ensure_ascii=False, indent=2)

    print(f"\nAugmentation complete:")
    print(f"  Original: {len(original_data)}")
    print(f"  Augmented: {len(final)} ({len(final)/len(original_data):.1f}x)")
    print(f"\nMutation breakdown:")
    for k, v in mutation_stats.items():
        print(f"  {k}: +{v}")

    # Bug type distribution
    type_counts = {}
    for item in final:
        bt = item.get('bug_type', 'unknown')
        type_counts[bt] = type_counts.get(bt, 0) + 1

    print(f"\nBug type distribution:")
    for bt, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {bt}: {count}")


if __name__ == "__main__":
    augment_dataset(
        input_path="datasets/dpo_dataset_merged.json",
        output_path="datasets/augmented_dataset.json",
        target_multiplier=5
    )
