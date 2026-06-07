#!/usr/bin/env python3
"""
Bad Case 分析脚本 - 用于提取和分析 GRPO 训练中的失败案例

使用方法：
1. 在公司服务器上运行（加载训练好的模型）
2. 或者手动整理训练日志中的生成结果

输出：bad_cases_analysis.json 和 bad_cases_summary.md
"""

import json
import os
from pathlib import Path
from typing import List, Dict
import re


# ==================== 奖励函数（复现训练时的逻辑） ====================

def format_reward(text: str) -> float:
    """格式奖励：检查 XML 标签结构"""
    has_reasoning = bool(re.search(r'<reasoning>.*?</reasoning>', text, re.DOTALL))
    has_fixed_code = bool(re.search(r'<fixed_code>.*?</fixed_code>', text, re.DOTALL))
    if has_reasoning and has_fixed_code:
        return 0.5
    elif has_fixed_code:
        return 0.25
    return 0.0


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


def code_quality_reward(text: str) -> float:
    """代码质量奖励：简洁性"""
    match = re.search(r'<fixed_code>(.*?)</fixed_code>', text, re.DOTALL)
    if not match:
        return 0.0
    
    code = match.group(1).strip()
    lines = code.split('\n')
    
    if len(lines) <= 10:
        return 0.5
    elif len(lines) <= 20:
        return 0.25
    return 0.0


def correctness_reward(text: str, reference: str) -> float:
    """正确性奖励：与参考答案匹配度"""
    match = re.search(r'<fixed_code>(.*?)</fixed_code>', text, re.DOTALL)
    if not match:
        return 0.0
    
    generated = match.group(1).strip()
    
    # 简单字符串匹配
    if generated == reference:
        return 2.0
    elif reference in generated or generated in reference:
        return 1.0
    else:
        return 0.0


def compute_total_reward(text: str, reference: str) -> Dict:
    """计算总奖励和各分项奖励"""
    r_format = format_reward(text)
    r_syntax = syntax_reward(text)
    r_quality = code_quality_reward(text)
    r_correct = correctness_reward(text, reference)
    
    total = r_format + r_syntax + r_quality + r_correct
    
    return {
        'total': total,
        'format': r_format,
        'syntax': r_syntax,
        'quality': r_quality,
        'correctness': r_correct
    }


# ==================== Bad Case 分类 ====================

ERROR_TYPES = {
    'syntax_error': '语法错误（Python 编译失败）',
    'logic_error': '逻辑错误（代码能跑但结果不对）',
    'format_error': '格式错误（缺少 XML 标签）',
    'no_change': '未修复（输出与输入相同）',
    'over_fix': '过度修复（修改了不该改的地方）',
    'hallucination': '幻觉（生成了无关内容）',
    'truncated': '截断（生成不完整）',
}


def classify_error(generated: str, reference: str, prompt: str) -> str:
    """自动分类错误类型"""
    
    # 检查格式
    if not re.search(r'<fixed_code>.*?</fixed_code>', generated, re.DOTALL):
        return 'format_error'
    
    # 检查语法
    syntax_score = syntax_reward(generated)
    if syntax_score == 0:
        return 'syntax_error'
    
    # 检查是否正确
    correct_score = correctness_reward(generated, reference)
    if correct_score == 2.0:
        return None  # 正确案例
    
    # 检查是否未修改
    generated_code = re.search(r'<fixed_code>(.*?)</fixed_code>', generated, re.DOTALL).group(1).strip()
    reference_code = reference.strip()
    
    if generated_code == reference_code:
        return None  # 正确
    
    # 检查是否完全没改（从 prompt 中提取原始代码）
    prompt_match = re.search(r'```python\s*(.*?)\s*```', prompt, re.DOTALL)
    if prompt_match:
        original_code = prompt_match.group(1).strip()
        if generated_code == original_code:
            return 'no_change'
    
    # 检查是否过度修复（编辑距离过大）
    edit_distance = len(generated_code) - len(reference_code)
    if abs(edit_distance) > len(reference_code) * 0.5:
        return 'over_fix'
    
    # 默认逻辑错误
    return 'logic_error'


# ==================== 分析主流程 ====================

def analyze_bad_cases_from_logs(log_file: str, output_dir: str = "./bad_cases"):
    """
    从训练日志中提取并分析 Bad Case
    
    日志格式示例（从 training.log 中提取）：
    Step 50: prompt="...", generated="...", reference="...", reward=1.5
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    bad_cases = []
    total_cases = 0
    
    # 读取日志（需要根据实际日志格式调整解析逻辑）
    with open(log_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 解析日志（示例格式，需根据实际情况调整）
    # 这里假设日志包含生成的文本和奖励
    pattern = r'Step (\d+):.*?prompt="(.*?)".*?generated="(.*?)".*?reference="(.*?)".*?reward=([\d.]+)'
    
    for match in re.finditer(pattern, content, re.DOTALL):
        step = int(match.group(1))
        prompt = match.group(2)
        generated = match.group(3)
        reference = match.group(4)
        reward = float(match.group(5))
        
        total_cases += 1
        
        # 只分析低奖励案例（Bad Case）
        if reward < 3.0:  # 阈值可调
            error_type = classify_error(generated, reference, prompt)
            
            if error_type:
                rewards = compute_total_reward(generated, reference)
                
                bad_cases.append({
                    'step': step,
                    'prompt': prompt,
                    'generated': generated,
                    'reference': reference,
                    'total_reward': reward,
                    'rewards': rewards,
                    'error_type': error_type,
                    'error_description': ERROR_TYPES.get(error_type, '未知错误'),
                    'analysis': '待人工分析'
                })
    
    # 保存 Bad Case
    with open(output_path / 'bad_cases.json', 'w', encoding='utf-8') as f:
        json.dump(bad_cases, f, ensure_ascii=False, indent=2)
    
    # 生成统计报告
    generate_summary_report(bad_cases, total_cases, output_path)
    
    print(f"✓ 分析完成！")
    print(f"  总案例数：{total_cases}")
    print(f"  Bad Case 数：{len(bad_cases)}")
    print(f"  保存位置：{output_path}/bad_cases.json")
    print(f"  报告位置：{output_path}/bad_cases_summary.md")
    
    return bad_cases


def generate_summary_report(bad_cases: List[Dict], total_cases: int, output_path: Path):
    """生成 Bad Case 统计报告"""
    
    # 统计错误类型分布
    error_distribution = {}
    for case in bad_cases:
        error_type = case['error_type']
        if error_type not in error_distribution:
            error_distribution[error_type] = 0
        error_distribution[error_type] += 1
    
    # 生成 Markdown 报告
    report = []
    report.append("# Bad Case 分析报告\n")
    report.append(f"## 总体统计\n")
    report.append(f"- 总分析案例：{total_cases}")
    report.append(f"- Bad Case 数量：{len(bad_cases)}")
    report.append(f"- Bad Case 率：{len(bad_cases)/total_cases*100:.1f}%\n")
    
    report.append("## 错误类型分布\n")
    report.append("| 错误类型 | 数量 | 占比 |")
    report.append("|----------|------|------|")
    
    for error_type, count in sorted(error_distribution.items(), key=lambda x: x[1], reverse=True):
        desc = ERROR_TYPES.get(error_type, error_type)
        ratio = count / len(bad_cases) * 100 if bad_cases else 0
        report.append(f"| {desc} | {count} | {ratio:.1f}% |")
    
    report.append("\n## Top 10 典型 Bad Case\n")
    
    for i, case in enumerate(bad_cases[:10], 1):
        report.append(f"### Case {i}: {case['error_description']}\n")
        report.append(f"**Step**: {case['step']}")
        report.append(f"**总奖励**: {case['total_reward']:.2f}")
        report.append(f"  - 格式：{case['rewards']['format']:.2f}")
        report.append(f"  - 语法：{case['rewards']['syntax']:.2f}")
        report.append(f"  - 质量：{case['rewards']['quality']:.2f}")
        report.append(f"  - 正确性：{case['rewards']['correctness']:.2f}\n")
        
        report.append(f"**Prompt**:\n```\n{case['prompt'][:200]}...\n```\n")
        report.append(f"**Generated**:\n```\n{case['generated'][:300]}...\n```\n")
        report.append(f"**Reference**:\n```\n{case['reference'][:200]}...\n```\n")
        report.append(f"**人工分析**: （待填写）\n\n")
    
    report.append("## 改进建议\n")
    report.append("根据以上分析，主要问题集中在：\n")
    
    if error_distribution.get('syntax_error', 0) > 0:
        report.append("1. **语法错误**: 需要加强语法约束，可在奖励函数中增加语法惩罚权重\n")
    if error_distribution.get('logic_error', 0) > 0:
        report.append("2. **逻辑错误**: 需要改进正确性奖励，或增加思维链监督\n")
    if error_distribution.get('over_fix', 0) > 0:
        report.append("3. **过度修复**: 需要增加编辑距离惩罚，约束模型只修改必要部分\n")
    
    with open(output_path / 'bad_cases_summary.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(report))


# ==================== 手动整理模板 ====================

def create_manual_analysis_template():
    """创建手动分析模板（如果没有自动日志）"""
    
    template = """# Bad Case 手动分析模板

## 说明
如果训练日志中没有保存完整的生成文本，需要手动整理典型失败案例。

## 使用方法
1. 打开训练日志（training.log）
2. 找到 reward 较低的步骤
3. 记录对应的 prompt 和生成结果
4. 填写下表

## Bad Case 分析表

| ID | Step | 输入 Bug | 模型输出 | 错误类型 | 原因分析 | 改进思路 |
|----|------|----------|----------|----------|----------|----------|
| 1  |      |          |          |          |          |          |
| 2  |      |          |          |          |          |          |
| 3  |      |          |          |          |          |          |

## 错误类型参考
- syntax_error: 语法错误
- logic_error: 逻辑错误
- format_error: 格式错误
- no_change: 未修复
- over_fix: 过度修复
- hallucination: 幻觉

## 面试回答模板

"我对训练过程中的所有 Bad Case 进行了全量分析，发现主要错误集中在三类：
1. 语法错误（占比 X%）：模型生成的代码有语法问题
2. 逻辑错误（占比 X%）：代码能跑但修复逻辑不对
3. 过度修复（占比 X%）：修改了不该改的地方

针对这些问题，我做了以下改进：
1. ...
2. ...
3. ...
"
"""
    
    with open('bad_cases_manual_template.md', 'w', encoding='utf-8') as f:
        f.write(template)
    
    print("✓ 手动分析模板已创建：bad_cases_manual_template.md")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Bad Case 分析脚本')
    parser.add_argument('--log', type=str, help='训练日志文件路径')
    parser.add_argument('--output', type=str, default='./bad_cases', help='输出目录')
    parser.add_argument('--template', action='store_true', help='只创建手动分析模板')
    
    args = parser.parse_args()
    
    if args.template:
        create_manual_analysis_template()
    elif args.log:
        analyze_bad_cases_from_logs(args.log, args.output)
    else:
        # 默认：创建模板
        create_manual_analysis_template()
        print("\n使用方法:")
        print("  python analyze_bad_cases.py --template  # 创建手动分析模板")
        print("  python analyze_bad_cases.py --log training.log --output ./bad_cases")
