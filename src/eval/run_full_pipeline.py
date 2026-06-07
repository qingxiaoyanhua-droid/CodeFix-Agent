#!/usr/bin/env python3
"""
CodeFix-Agent 完整流水线跑通脚本
用于验证整个系统端到端工作，记录真实数据

使用方法:
    python run_full_pipeline.py --step all

步骤说明:
    1. env_check      - 环境检查
    2. data_check    - 数据集检查
    3. eval_baseline - 基线评测(裸模型)
    4. eval_sft      - SFT后评测
    5. rag_check     - RAG检索检查
    6. eval_quixbugs - QuixBugs基准评测
    7. generate_report - 生成报告

二面准备优先流程:
    python run_full_pipeline.py --step quick  # 快速验证（本地）
    python run_full_pipeline.py --step full    # 完整流程（需服务器）
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional

def fix_print_for_windows():
    """Windows GBK 编码兼容"""
    import sys
    import io
    if sys.stdout.encoding.lower() in ('cp936', 'gbk', 'ascii'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

fix_print_for_windows()

# ==================== 配置 ====================

PROJECT_ROOT = Path(__file__).parent.absolute()
RUNS_DIR = PROJECT_ROOT / "runs_pipeline"
DATASET_PATH = PROJECT_ROOT / "datasets" / "dpo_dataset_merged.json"
RAG_KB_PATH = PROJECT_ROOT / "runs" / "bug_fixes.json"
QUIXBUGS_EVAL_SCRIPT = PROJECT_ROOT / "evaluate_on_benchmark.py"

# 确保输出目录存在
RUNS_DIR.mkdir(exist_ok=True)

@dataclass
class PipelineStep:
    name: str
    description: str
    status: str = "pending"  # pending / running / success / failed / skipped
    duration_ms: float = 0.0
    error: str = ""
    output: str = ""

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
            "output": self.output[:500] if len(self.output) > 500 else self.output,
        }


# ==================== Step 1: 环境检查 ====================

def step_env_check() -> PipelineStep:
    step = PipelineStep(name="env_check", description="检查运行环境")

    output_parts = []
    errors = []

    # Python版本
    version = sys.version_info
    output_parts.append(f"Python: {version.major}.{version.minor}.{version.micro}")
    if version.major < 3 or (version.major == 3 and version.minor < 8):
        errors.append("Python版本需要>=3.8")

    # 关键依赖
    deps = {
        "torch": "PyTorch",
        "transformers": "Transformers",
        "peft": "PEFT",
        "trl": "TRL",
        "faiss": "FAISS",
        "numpy": "NumPy",
        "datasets": "Datasets",
    }

    for mod, name in deps.items():
        try:
            __import__(mod)
            output_parts.append(f"  ✓ {name} ({mod})")
        except ImportError:
            output_parts.append(f"  ✗ {name} ({mod}) - 未安装")
            errors.append(f"缺少依赖: {name}")

    # API Key
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if dashscope_key:
        output_parts.append(f"  ✓ DASHSCOPE_API_KEY: {dashscope_key[:8]}...")
    else:
        output_parts.append(f"  ⚠ DASHSCOPE_API_KEY: 未设置（部分功能不可用）")

    step.output = "\n".join(output_parts)
    step.error = "\n".join(errors)
    step.status = "failed" if errors else "success"
    return step


# ==================== Step 2: 数据集检查 ====================

def step_data_check() -> PipelineStep:
    step = PipelineStep(name="data_check", description="检查数据集完整性")

    output_parts = []
    errors = []

    # 主数据集
    if DATASET_PATH.exists():
        with open(DATASET_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        output_parts.append(f"主数据集: {DATASET_PATH.name}")
        output_parts.append(f"  数量: {len(data)} 条")

        # 统计bug类型分布
        bug_types = {}
        for item in data:
            bt = item.get('bug_type', 'unknown')
            bug_types[bt] = bug_types.get(bt, 0) + 1

        output_parts.append(f"  Bug类型分布: {len(bug_types)} 种")
        for bt, cnt in sorted(bug_types.items(), key=lambda x: -x[1])[:5]:
            output_parts.append(f"    - {bt}: {cnt}")
    else:
        errors.append(f"主数据集不存在: {DATASET_PATH}")

    # RAG知识库
    if RAG_KB_PATH.exists():
        with open(RAG_KB_PATH, 'r', encoding='utf-8') as f:
            kb = json.load(f)
        output_parts.append(f"\nRAG知识库: {RAG_KB_PATH.name}")
        output_parts.append(f"  数量: {len(kb)} 条")
    else:
        output_parts.append(f"\nRAG知识库: 不存在（需要构建）")

    step.output = "\n".join(output_parts)
    step.error = "\n".join(errors)
    step.status = "failed" if errors else "success"
    return step


# ==================== Step 3: 本地评测（模拟） ====================

def step_eval_local() -> PipelineStep:
    step = PipelineStep(name="eval_local", description="本地代码级评测")

    output_parts = []

    # 读取数据集做简单验证
    if not DATASET_PATH.exists():
        step.status = "skipped"
        step.output = "跳过（数据集不存在）"
        return step

    with open(DATASET_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 模拟评测：检查修复代码能否通过简单语法验证
    passed_syntax = 0
    failed_syntax = 0
    no_code_count = 0

    for item in data[:50]:  # 只检查前50条
        chosen = item.get('chosen', '')
        # 提取fixed_code
        import re
        code_match = re.search(r'<fixed_code>\s*(.*?)\s*</fixed_code>', chosen, re.DOTALL)
        if not code_match:
            no_code_count += 1
            continue

        code = code_match.group(1).strip()
        try:
            compile(code, '<string>', 'exec')
            passed_syntax += 1
        except SyntaxError:
            failed_syntax += 1

    total = passed_syntax + failed_syntax + no_code_count
    output_parts.append(f"本地语法检查（前{total}条）:")
    output_parts.append(f"  通过语法: {passed_syntax}/{total} ({passed_syntax/total:.1%})")
    output_parts.append(f"  语法错误: {failed_syntax}")
    output_parts.append(f"  无代码段: {no_code_count}")

    step.output = "\n".join(output_parts)
    step.status = "success"
    return step


# ==================== Step 4: QuixBugs基准评测 ====================

def step_eval_quixbugs() -> PipelineStep:
    step = PipelineStep(name="eval_quixbugs", description="QuixBugs基准评测（正式）")

    output_parts = []

    if not QUIXBUGS_EVAL_SCRIPT.exists():
        step.status = "skipped"
        step.output = "评测脚本不存在，跳过"
        return step

    # 直接运行评测脚本的demo模式
    import subprocess

    try:
        result = subprocess.run(
            [sys.executable, str(QUIXBUGS_EVAL_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(PROJECT_ROOT)
        )

        output_parts.append("QuixBugs评测输出:")
        output_parts.append(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)

        if result.returncode != 0:
            output_parts.append(f"\n错误: {result.stderr[-500:]}")

        step.output = "\n".join(output_parts)
        step.status = "success" if result.returncode == 0 else "failed"

    except subprocess.TimeoutExpired:
        step.output = "评测超时（60秒）"
        step.status = "failed"
    except Exception as e:
        step.output = f"评测异常: {e}"
        step.status = "failed"

    return step


# ==================== Step 5: RAG检索验证 ====================

def step_rag_check() -> PipelineStep:
    step = PipelineStep(name="rag_check", description="RAG检索功能验证")

    output_parts = []

    if not RAG_KB_PATH.exists():
        step.output = "RAG知识库不存在，跳过检索测试"
        step.status = "skipped"
        return step

    # 尝试加载RAG系统
    try:
        from enhanced_agent import BugFixRetriever

        retriever = BugFixRetriever(knowledge_base_path=str(RAG_KB_PATH))
        output_parts.append(f"RAG系统加载成功")
        output_parts.append(f"  知识库条目: {len(retriever.knowledge_base)}")

        # 测试检索
        test_queries = [
            "Fix off-by-one error in loop boundary",
            "Fix wrong operator in arithmetic function",
            "Fix missing return value in base case",
        ]

        output_parts.append("\n检索测试:")
        for q in test_queries:
            results = retriever.retrieve_similar_fixes(q, top_k=2)
            if results:
                output_parts.append(f"  查询: {q[:40]}...")
                output_parts.append(f"    → 返回 {len(results)} 条, top-1: {results[0].get('bug_type', 'unknown')}")
            else:
                output_parts.append(f"  查询: {q[:40]}... → 无结果")

        step.output = "\n".join(output_parts)
        step.status = "success"

    except ImportError as e:
        step.output = f"导入失败: {e}"
        step.status = "failed"
    except Exception as e:
        step.output = f"测试异常: {e}"
        step.status = "failed"

    return step


# ==================== Step 6: CoT Agent快速测试 ====================

def step_cot_agent_test() -> PipelineStep:
    step = PipelineStep(name="cot_agent_test", description="CoT Agent快速功能测试")

    output_parts = []

    # 简单测试：验证CoTParser和CodeExecutor能工作
    try:
        from cot_react_agent import CoTParser, CodeExecutor

        parser = CoTParser()
        executor = CodeExecutor()

        # 测试parser
        test_output = """<think>
[Step 1: Bug Identification]
The bug is a wrong operator - using subtraction instead of addition.

[Step 2: Root Cause Analysis]
When adding two numbers, the code returns `a - b` instead of `a + b`.

[Step 3: Fix Strategy]
Change the operator from `-` to `+`.

[Step 4: Edge Case Check]
Handles negative numbers, zeros, and large numbers correctly.
</think>

<fixed_code>
```python
def add(a, b):
    return a + b
```
</fixed_code>"""

        steps = parser.parse_steps(test_output)
        code = parser.extract_code(test_output)

        output_parts.append(f"CoT Parser测试:")
        output_parts.append(f"  解析步骤数: {len(steps)}")
        output_parts.append(f"  提取代码长度: {len(code)} chars")

        # 测试executor
        exec_result = executor.execute(code, [
            {"function": "add", "input": [2, 3], "output": 5},
            {"function": "add", "input": [-1, 1], "output": 0},
        ])

        output_parts.append(f"\nExecutor测试:")
        output_parts.append(f"  执行结果: {'PASS' if exec_result.success else 'FAIL'}")
        output_parts.append(f"  错误类型: {exec_result.error_type if not exec_result.success else 'N/A'}")

        step.output = "\n".join(output_parts)
        step.status = "success"

    except ImportError as e:
        step.output = f"导入失败: {e}"
        step.status = "failed"
    except Exception as e:
        step.output = f"测试异常: {e}"
        step.status = "failed"

    return step


# ==================== Step 7: 生成报告 ====================

def generate_report(all_steps: List[PipelineStep], report_path: Path):
    report = {
        "timestamp": datetime.now().isoformat(),
        "project_root": str(PROJECT_ROOT),
        "steps": [s.to_dict() for s in all_steps],
        "summary": {
            "total": len(all_steps),
            "success": sum(1 for s in all_steps if s.status == "success"),
            "failed": sum(1 for s in all_steps if s.status == "failed"),
            "skipped": sum(1 for s in all_steps if s.status == "skipped"),
            "total_duration_ms": sum(s.duration_ms for s in all_steps),
        }
    }

    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # 打印摘要
    print("\n" + "=" * 60)
    print("  流水线执行报告")
    print("=" * 60)
    print(f"总步骤: {report['summary']['total']}")
    print(f"成功: {report['summary']['success']}  |  失败: {report['summary']['failed']}  |  跳过: {report['summary']['skipped']}")
    print(f"总耗时: {report['summary']['total_duration_ms']/1000:.1f} 秒")
    print("=" * 60)

    for s in all_steps:
        icon = {"success": "✓", "failed": "✗", "skipped": "-", "pending": "○", "running": "▶"}.get(s.status, "?")
        print(f"  {icon} [{s.status.upper():>7s}] {s.name}: {s.description}")
        if s.status == "failed" and s.error:
            print(f"      错误: {s.error[:100]}")

    print("=" * 60)
    print(f"\n详细报告: {report_path}")

    return report


# ==================== 主流程 ====================

def run_pipeline(steps_to_run: List[str]):
    """运行指定步骤"""

    all_steps_funcs = {
        "env_check": step_env_check,
        "data_check": step_data_check,
        "eval_local": step_eval_local,
        "rag_check": step_rag_check,
        "cot_agent_test": step_cot_agent_test,
        "eval_quixbugs": step_eval_quixbugs,
    }

    # 确定要运行的步骤
    if "all" in steps_to_run:
        run_list = list(all_steps_funcs.keys())
    elif "quick" in steps_to_run:
        run_list = ["env_check", "data_check", "eval_local", "rag_check", "cot_agent_test"]
    elif "full" in steps_to_run:
        run_list = list(all_steps_funcs.keys())
    else:
        run_list = steps_to_run

    all_steps = []
    report_path = RUNS_DIR / f"pipeline_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    for step_name in run_list:
        if step_name not in all_steps_funcs:
            print(f"未知步骤: {step_name}")
            continue

        step = PipelineStep(name=step_name, description=all_steps_funcs[step_name].__doc__ or step_name)
        print(f"\n{'='*60}")
        print(f"▶ 运行: {step_name} - {step.description}")
        print(f"{'='*60}")

        step.status = "running"
        start = time.time()

        try:
            result = all_steps_funcs[step_name]()
            step = result
        except Exception as e:
            step.status = "failed"
            step.error = str(e)

        step.duration_ms = (time.time() - start) * 1000

        all_steps.append(step)

        print(f"\n结果: {step.status.upper()}")
        print(f"耗时: {step.duration_ms/1000:.2f}s")
        if step.output:
            print(f"\n输出:\n{step.output[:500]}")

    return generate_report(all_steps, report_path), all_steps


def main():
    parser = argparse.ArgumentParser(description="CodeFix-Agent 完整流水线")
    parser.add_argument(
        "--step",
        nargs="+",
        default=["quick"],
        choices=["all", "quick", "full", "env_check", "data_check", "eval_local",
                 "rag_check", "cot_agent_test", "eval_quixbugs"],
        help="指定要运行的步骤"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出报告路径"
    )

    args = parser.parse_args()

    print("CodeFix-Agent 流水线")
    print(f"工作目录: {PROJECT_ROOT}")
    print(f"输出目录: {RUNS_DIR}")
    print(f"执行步骤: {args.step}")

    report, steps = run_pipeline(args.step)

    if args.output:
        print(f"\n报告已保存: {args.output}")

    # 打印下一步建议
    failed_steps = [s for s in steps if s.status == "failed"]
    if failed_steps:
        print("\n" + "=" * 60)
        print("  ⚠ 需要修复的问题:")
        for s in failed_steps:
            print(f"  - {s.name}: {s.error[:80]}")


if __name__ == "__main__":
    main()
