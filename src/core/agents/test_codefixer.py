#!/usr/bin/env python3
"""
CodeFixer 使用示例和验证脚本
================================
演示如何独立使用 CodeFixer 和集成到 MultiAgentOrchestrator。
"""

from __future__ import annotations

import sys
import os

# 确保 src 在路径中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.core.agents.codefixer import CodeFixer, FixResult, FixStatus, create_codefixer_agent
from src.core.agents.base_agent import SharedContext
from src.core.multi_agent_orchestrator import MultiAgentOrchestrator


# ==================== Mock 模型函数 ====================

def mock_small_model(prompt: str, system_prompt: str) -> str:
    """模拟小模型：简单返回修复代码"""
    if "return 'bug'" in prompt:
        return """[Step 1: Bug Identification]
The function returns the wrong string.

[Step 2: Root Cause Analysis]
The code returns 'bug' instead of 'fixed'.

[Step 3: Fix Strategy]
Change the return value to 'fixed'.

[Step 4: Edge Case Check]
No edge cases needed for this simple function.

<fixed_code>
```python
def foo():
    return 'fixed'
```
</fixed_code>"""
    return """[Step 1: Bug Identification]
Found an error in the code.

[Step 2: Root Cause Analysis]
The logic is incorrect.

[Step 3: Fix Strategy]
Fix the logic.

[Step 4: Edge Case Check]
Checked.

<fixed_code>
```python
def foo():
    return 'fixed'
```
</fixed_code>"""


def mock_large_model(prompt: str, system_prompt: str) -> str:
    """模拟大模型：返回 hint"""
    return '{"hint": "Check the return value and ensure it matches expected output.", "status": "BUG_DETECTED"}'


# ==================== 测试用例 ====================

def test_codefixer_direct():
    """测试 1: 直接使用 CodeFixer"""
    print("\n" + "=" * 60)
    print("测试 1: 直接使用 CodeFixer")
    print("=" * 60)

    fixer = CodeFixer(
        small_model_fn=mock_small_model,
        large_model_fn=mock_large_model,
    )

    result = fixer.fix(
        buggy_code="def foo(): return 'bug'",
        bug_description="函数返回错误的结果",
    )

    print(f"\n结果:")
    print(f"  success:       {result.success}")
    print(f"  status:        {result.status.value}")
    print(f"  final_code:    {result.final_code.strip()}")
    print(f"  quality_score: {result.quality_score:.2f}")
    print(f"  total_rounds:  {result.total_rounds}")
    print(f"  small_calls:   {result.small_model_calls}")
    print(f"  large_calls:   {result.large_model_calls}")
    print(f"  sandbox_runs:  {result.sandbox_runs}")
    print(f"  took_over:     {result.large_model_took_over}")

    assert result.success, f"Expected success, got {result.status}"
    assert "fixed" in result.final_code, "Expected 'fixed' in code"
    print("\n✅ 测试 1 通过")


def test_codefixer_baseagent_interface():
    """测试 2: CodeFixer 作为 BaseAgent 使用"""
    print("\n" + "=" * 60)
    print("测试 2: CodeFixer 作为 BaseAgent 使用")
    print("=" * 60)

    fixer = CodeFixer(
        small_model_fn=mock_small_model,
        large_model_fn=mock_large_model,
    )

    context = SharedContext(
        task_id="test_task_001",
        buggy_code="def foo(): return 'bug'",
        bug_description="返回错误",
    )

    result = fixer.run(context)

    print(f"\n结果:")
    print(f"  status:         {result.get('status')}")
    print(f"  success:        {result.get('success')}")
    print(f"  final_code:     {result.get('final_code', '').strip()}")
    print(f"  quality_score:  {result.get('quality_score', 0.0):.2f}")
    print(f"  small_calls:    {result.get('small_model_calls')}")
    print(f"  large_calls:    {result.get('large_model_calls')}")

    assert result.get("status") == "ok", f"Expected 'ok', got {result.get('status')}"
    assert result.get("success") == True, "Expected success"
    assert context.solver_output is not None, "solver_output should be set"
    print("\n✅ 测试 2 通过")


def test_codefixer_with_orchestrator():
    """测试 3: 集成到 MultiAgentOrchestrator"""
    print("\n" + "=" * 60)
    print("测试 3: 集成到 MultiAgentOrchestrator")
    print("=" * 60)

    codefixer = create_codefixer_agent(
        agent_id="codefixer",
        small_model_fn=mock_small_model,
        large_model_fn=mock_large_model,
    )

    orchestrator = MultiAgentOrchestrator(
        codefixer_agent=codefixer,
        memory_agent=None,  # 不需要记忆
    )

    result = orchestrator.run(
        buggy_code="def foo(): return 'bug'",
        bug_description="返回错误",
        task_id="test_task_002",
    )

    print(f"\n结果:")
    print(f"  success:            {result.success}")
    print(f"  phase:              {result.phase}")
    print(f"  final_code:         {result.final_code.strip()}")
    print(f"  quality_score:      {result.quality_score:.2f}")
    print(f"  codefixer_calls:    {result.codefixer_calls}")
    print(f"  small_model_calls:  {result.small_model_calls}")
    print(f"  large_model_calls:  {result.large_model_calls}")
    print(f"  large_took_over:    {result.large_model_took_over}")

    assert result.success, f"Expected success, got phase={result.phase}"
    assert result.codefixer_calls == 1, "Should have 1 codefixer call"
    print("\n✅ 测试 3 通过")


def test_codefixer_fallback_to_large():
    """测试 4: 小模型失败后大模型 fallback"""
    print("\n" + "=" * 60)
    print("测试 4: 小模型失败后大模型 fallback")
    print("=" * 60)

    call_count = {"small": 0}

    def failing_small(prompt, system):
        call_count["small"] += 1
        return "<fixed_code>\n```python\ndef foo(): return 'still bug'\n```\n</fixed_code>"

    def always_fixed_large(prompt, system):
        return """[Step 1: Fix]
Fixing the return value.

[Step 2: Done]

[Step 3: Done]

[Step 4: Done]

<fixed_code>
```python
def foo():
    return 'large_model_fixed'
```
</fixed_code>"""

    fixer = CodeFixer(
        small_model_fn=failing_small,
        large_model_fn=always_fixed_large,
        max_small_model_rounds=2,
    )

    result = fixer.fix(
        buggy_code="def foo(): return 'bug'",
        bug_description="需要大模型亲自修复",
    )

    print(f"\n结果:")
    print(f"  success:            {result.success}")
    print(f"  status:             {result.status.value}")
    print(f"  small_calls:        {result.small_model_calls}")
    print(f"  large_calls:        {result.large_model_calls}")
    print(f"  large_took_over:    {result.large_model_took_over}")
    print(f"  total_rounds:       {result.total_rounds}")

    assert result.success, "Should eventually succeed via large model"
    assert result.large_model_took_over == True, "Large model should have taken over"
    print("\n✅ 测试 4 通过")


def main():
    print("\n" + "#" * 60)
    print("# CodeFixer 验证测试")
    print("#" * 60)

    try:
        test_codefixer_direct()
        test_codefixer_baseagent_interface()
        test_codefixer_with_orchestrator()
        test_codefixer_fallback_to_large()

        print("\n" + "#" * 60)
        print("# ✅ 所有测试通过")
        print("#" * 60)
        print("\nCodeFixer 实现验证成功！可以正常使用。")

    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
