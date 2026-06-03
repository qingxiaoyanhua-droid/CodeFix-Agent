"""
DECENTMEM 集成示例：如何在现有 Agent 中接入双池记忆

使用方法:
    python decentmem_integration.py

这个脚本展示:
1. 如何用双池记忆替换原有的静态 reflection_memory.json
2. 如何用 LLM-as-a-Judge 替换启发式评分
3. 如何用路由器动态决定检索策略
4. 如何在 GRPO 训练中接入路由器 reward
"""

import json
import time
import numpy as np
from dual_pool_memory import DualPoolMemory, MemoryPiece
from llm_judge import LLMJudge
from online_router import OnlineRouter, DECENTMEMCodeFixPipeline

# ----- 示例1: 初始化双池记忆 -----

def demo_dual_pool_init():
    print("\n" + "=" * 60)
    print("示例1: 初始化双池记忆")
    print("=" * 60)

    # 创建双池记忆
    memory = DualPoolMemory(
        agent_id="codefix_agent",
        e_pool_path="runs/e_pool.json",
        x_pool_path="runs/x_pool.json",
        similarity_threshold=0.35,
    )

    # 从现有成功案例初始化 E-pool
    # 这里用示例数据，实际应该从你的 evaluation 结果中导入
    existing_fixes = [
        {
            "task_type": "off_by_one",
            "bug_signature": "binary_search/mid",
            "buggy_code": "def binary_search(arr, target):\n    lo, hi = 0, len(arr)\n    while lo < hi:\n        mid = (lo + hi) // 2\n        if arr[mid] < target:\n            lo = mid + 1\n        else:\n            hi = mid - 1\n    return lo",
            "fixed_code": "def binary_search(arr, target):\n    lo, hi = 0, len(arr)\n    while lo < hi:\n        mid = (lo + hi) // 2\n        if arr[mid] < target:\n            lo = mid + 1\n        else:\n            hi = mid\n    return lo",
            "cot_steps": [
                {"step_id": 1, "content": "Bug type: off-by-one in boundary condition"},
                {"step_id": 2, "content": "Root cause: hi should be mid, not mid-1"},
                {"step_id": 3, "content": "Fix: change hi = mid - 1 to hi = mid"},
                {"step_id": 4, "content": "Verify with edge cases"},
            ],
            "judge_scores": {"overall": 8.5, "L1": 8.0, "L2": 9.0, "L3": 9.0, "L4": 8.0},
            "pass_rate": 1.0,
        },
        {
            "task_type": "logic_error",
            "bug_signature": "is_prime/n",
            "buggy_code": "def is_prime(n):\n    if n < 2:\n        return False\n    for i in range(2, n):\n        if n % i == 0:\n            return True\n    return False",
            "fixed_code": "def is_prime(n):\n    if n < 2:\n        return False\n    for i in range(2, int(n**0.5) + 1):\n        if n % i == 0:\n            return False\n    return True",
            "cot_steps": [
                {"step_id": 1, "content": "Bug type: logic error, return value inverted"},
                {"step_id": 2, "content": "Root cause: returns True when divisor found, should return False"},
                {"step_id": 3, "content": "Fix: invert return values + add sqrt optimization"},
                {"step_id": 4, "content": "Verify with primes and composites"},
            ],
            "judge_scores": {"overall": 9.0, "L1": 9.0, "L2": 9.0, "L3": 9.0, "L4": 9.0},
            "pass_rate": 1.0,
        },
    ]

    count = memory.import_existing_success(existing_fixes)
    print(f"导入 {count} 条成功经验到 E-pool")

    # 查看统计
    stats = memory.pool_stats()
    print(f"双池状态: {stats}")

    return memory


# ----- 示例2: 在线路由器决策 -----

def demo_router_decisions():
    print("\n" + "=" * 60)
    print("示例2: 在线路由器动态决策")
    print("=" * 60)

    router = OnlineRouter(
        alpha=0.5,
        beta=0.5,
        force_e_threshold=0.6,
    )

    # 模拟任务流
    tasks = [
        {"task_id": "task_1", "task_type": "off_by_one", "bug_signature": "binary_search/mid"},
        {"task_id": "task_2", "task_type": "logic_error", "bug_signature": "is_prime/n"},
        {"task_id": "task_3", "task_type": "initialization", "bug_signature": "new_func/x"},
        {"task_id": "task_4", "task_type": "off_by_one", "bug_signature": "binary_search/mid"},
        {"task_id": "task_5", "task_type": "syntax_fix", "bug_signature": "any/func"},
    ]

    # 模拟 E-pool 初始有高相关记忆
    e_pool_relevances = [0.8, 0.3, 0.1, 0.7, 0.2]

    print("\n初始状态:")
    router.print_state()

    print("\n决策序列:")
    for i, (task, e_rel) in enumerate(zip(tasks, e_pool_relevances)):
        pool = router.decide(task, e_rel, task["task_type"])

        # 模拟 judge 反馈
        # E-pool 被选且相关性高 → 成功率更高
        if pool == "E" and e_rel >= 0.6:
            judge_delta = 1.0
        elif pool == "X":
            judge_delta = 0.5
        else:
            judge_delta = 0.0

        router.update(pool, judge_delta)

        print(f"  Task {i+1}: type={task['task_type']}, "
              f"E_rel={e_rel:.1f} → Pool={pool}, "
              f"Δ={judge_delta:.1f} → "
              f"α={router.state.alpha:.3f}")

    print("\n最终状态:")
    router.print_state()

    # 收敛报告
    report = router.convergence_report()
    print(f"\n收敛分析: {report['interpretation']}")

    return router


# ----- 示例3: 路由器权重演化 -----

def demo_router_simulation():
    print("\n" + "=" * 60)
    print("示例3: 路由器权重演化模拟")
    print("=" * 60)

    router = OnlineRouter()

    # 场景1: E-pool 70% 的情况更好
    print("\n[场景1] E-pool 真正更好的概率 = 70%")
    router.simulate(n_tasks=50, ground_truth_e_better_prob=0.7)

    # 重置
    router = OnlineRouter()

    # 场景2: X-pool 60% 的情况更好（探索更重要）
    print("\n[场景2] E-pool 真正更好的概率 = 40%")
    router.simulate(n_tasks=50, ground_truth_e_better_prob=0.4)

    return router


# ----- 示例4: LLM-as-a-Judge 评估 -----

def demo_llm_judge():
    print("\n" + "=" * 60)
    print("示例4: LLM-as-a-Judge 评估")
    print("=" * 60)

    # 创建 judge（会自动降级到启发式评估）
    judge = LLMJudge(
        model_path=None,  # 不加载模型，使用启发式
        fallback_to_heuristic=True,
    )

    buggy_code = """def binary_search(arr, target):
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1  # BUG: should be hi = mid
    return lo"""

    good_cot = """<think>
[Step 1: Bug Identification] This is an off-by-one error in the boundary condition. The variable `hi` is being decremented incorrectly when the target is found at or before mid.

[Step 2: Root Cause Analysis] When `arr[mid] >= target`, we should keep mid in the search range. Setting `hi = mid - 1` excludes mid itself from the search range, which causes the algorithm to skip the correct position and may lead to an index out of bounds when `mid = lo`.

[Step 3: Fix Strategy] Change `hi = mid - 1` to `hi = mid`. This ensures that the search range correctly shrinks to include the potential target position.

[Step 4: Verification] Test with edge cases: empty array, single element, target at first/last position, target not in array.
</think>

<fixed_code>
def binary_search(arr, target):
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo
</fixed_code>"""

    bad_cot = """<think>
[Step 1: Bug Identification] The code might have an issue.

[Step 2: Root Cause Analysis] Something is wrong with the loop.

[Step 3: Fix Strategy] Try changing the operator.

[Step 4: Verification] Check if it works.
</think>

<fixed_code>
def binary_search(arr, target):
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] <= target:
            lo = mid + 1
        else:
            hi = mid - 1
    return lo
</fixed_code>"""

    execution_good = {"pass_rate": 1.0, "tests_passed": 10, "tests_total": 10}
    execution_bad = {"pass_rate": 0.4, "tests_passed": 4, "tests_total": 10}

    # 评估好的 CoT
    print("\n[好 CoT 评估]")
    eval_good = judge.evaluate_task(buggy_code, good_cot, "test_1", execution_good)
    print(f"  总体分: {eval_good.overall_score:.1f}/10, 质量: {eval_good.quality}")
    for stage in eval_good.stage_evals:
        print(f"  Step {stage.stage_id} ({stage.stage_name}): {stage.score:.1f}/10")

    # 评估坏的 CoT
    print("\n[坏 CoT 评估]")
    eval_bad = judge.evaluate_task(buggy_code, bad_cot, "test_2", execution_bad)
    print(f"  总体分: {eval_bad.overall_score:.1f}/10, 质量: {eval_bad.quality}")
    for stage in eval_bad.stage_evals:
        print(f"  Step {stage.stage_id} ({stage.stage_name}): {stage.score:.1f}/10")

    # 对比 perplexity
    print("\n[Judge vs Perplexity 对比]")
    comparison = judge.compare_with_perplexity(buggy_code, good_cot)
    print(f"  Judge 总体分: {comparison['judge_overall_score']:.1f}")
    print(f"  Perplexity: {comparison['perplexity']:.2f}")
    print(f"  分析: {comparison['judge_vs_perplexity']}")

    return judge


# ----- 示例5: 在 GRPO 训练中使用路由器 -----

def demo_grpo_integration():
    print("\n" + "=" * 60)
    print("示例5: 在 GRPO 训练中使用路由器 reward")
    print("=" * 60)

    """
    原有 GRPO reward 权重是固定的:
        accuracy: 0.30, format: 0.05, ast: 0.15,
        semantic: 0.15, process: 0.25, length: 0.05

    改造后: reward 权重由路由器动态调整

    新的 reward 计算流程:
        1. 路由器根据任务相似度决定用 E-pool 还是 X-pool
        2. E-pool 被选中 → 提高 format/process 权重（利用已有推理模式）
        3. X-pool 被选中 → 提高 semantic/reward 权重（鼓励探索新推理路径）
        4. judge 评估 → 动态调整下一轮的池权重
    """

    router = OnlineRouter()

    def decentmem_reward_fn(completions, prompts=None, **kwargs):
        """
        DECENTMEM 风格的 reward 函数
        替代原有的固定权重 reward
        """
        rewards = []

        for i, completion in enumerate(completions):
            content = completion[0]["content"] if isinstance(completion, list) else completion

            # 解析 buggy code
            buggy_code = ""
            if prompts and i < len(prompts):
                match = kwargs.get("re", __import__("re")).search(
                    r'```python\s*(.*?)\s*```', prompts[i], __import__("re").DOTALL
                )
                if match:
                    buggy_code = match.group(1).strip()

            # 路由器决定池
            task = {"buggy_code": buggy_code, "task_type": kwargs.get("task_type", "unknown")}
            selected_pool = router.decide(task, e_pool_relevance=0.5)

            # judge 评估
            judge = kwargs.get("judge")
            if judge:
                eval_result = judge.evaluate_task(buggy_code, content)
                judge_score = eval_result.overall_score / 10.0
            else:
                judge_score = 0.5

            # 根据选中的池调整 reward 权重
            # E-pool: 偏向格式和过程（利用已有模式）
            # X-pool: 偏向语义和探索（鼓励新路径）
            if selected_pool == "E":
                base_reward = judge_score
            else:
                # X-pool 探索策略：增加探索奖励
                base_reward = judge_score * 1.1  # 轻微提升探索激励

            rewards.append(base_reward)

            # 模拟 judge 更新路由器
            router.update(selected_pool, 1.0 if judge_score > 0.6 else 0.0)

        return rewards

    print("DECENTMEM reward 函数已创建")
    print("接入方式: 在 GRPO 训练时，将 reward_fn 替换为 decentmem_reward_fn")
    print("\n关键区别:")
    print("  旧: 六维 reward → 固定权重 → 每轮相同")
    print("  新: 路由器选择池 → judge 评分 → 动态权重 → 自适应探索/利用平衡")

    return decentmem_reward_fn


# ----- 示例6: 端到端流程演示 -----

def demo_end_to_end():
    print("\n" + "=" * 60)
    print("示例6: 端到端流程演示")
    print("=" * 60)

    # 初始化组件
    memory = DualPoolMemory(agent_id="codefix_demo")
    judge = LLMJudge(fallback_to_heuristic=True)
    router = OnlineRouter()

    # 导入示例数据
    demo_dual_pool_init()

    print("\n开始处理 5 个代码修复任务...")

    tasks = [
        {
            "buggy_code": "def find_first(arr, x):\n    for i in range(len(arr)):\n        if arr[i] == x:\n            return i + 1\n    return -1",
            "task_type": "off_by_one",
            "task_signature": "find_first/i",
            "execution_pass_rate": 0.0,  # 初始失败
        },
        {
            "buggy_code": "def gcd(a, b):\n    while b:\n        a = b\n        b = a % b\n    return a",
            "task_type": "logic_error",
            "task_signature": "gcd/a,b",
            "execution_pass_rate": 0.0,
        },
        {
            "buggy_code": "def fibonacci(n):\n    a, b = 0, 1\n    for i in range(n):\n        a = b\n        b = a + b\n    return a",
            "task_type": "logic_error",
            "task_signature": "fibonacci/n",
            "execution_pass_rate": 0.0,
        },
    ]

    for i, task in enumerate(tasks):
        print(f"\n--- Task {i+1}: {task['task_type']} ---")

        # 1. 路由器决策
        e_rel = memory._check_e_pool_relevance(task) if hasattr(memory, '_check_e_pool_relevance') else 0.3
        selected_pool = router.decide(task, e_rel, task["task_type"])
        print(f"  路由器: 选中 {selected_pool}-pool (α={router.state.alpha:.3f})")

        # 2. 检索记忆
        memories = memory.retrieve(task, stage="L1", top_k=2, pool_type=selected_pool)
        print(f"  检索到 {len(memories)} 条相关记忆")

        # 3. 模拟 CoT 生成（这里用占位）
        # 实际使用时应该调用你的 CoTReActAgent
        print(f"  生成 CoT 推理...")

        # 4. 模拟执行结果（假设后续任务成功了）
        success = (i + 1) % 2 == 0
        pass_rate = 1.0 if success else 0.5

        # 5. 评估
        fake_cot = f"[Step 1] Bug type: {task['task_type']}\n[Step 2] Root cause\n[Step 3] Fix strategy\n[Step 4] Verification"
        eval_result = judge.evaluate_task(task["buggy_code"], fake_cot, task_id=str(i))

        print(f"  Judge 评分: {eval_result.overall_score:.1f}/10, 质量: {eval_result.quality}")

        # 6. 更新路由器
        judge_delta = 1.0 if success else 0.0
        router.update(selected_pool, judge_delta)

        # 7. 记录到记忆
        if success:
            from dual_pool_memory import MemoryPiece
            import hashlib
            piece = MemoryPiece(
                piece_id=hashlib.md5(task["buggy_code"].encode()).hexdigest()[:12],
                task_type=task["task_type"],
                bug_signature=task["task_signature"],
                full_buggy_code=task["buggy_code"],
                cot_steps=[{"step_id": 1, "content": "example"}],
                final_fix=task["buggy_code"],
                fix_status="success",
                judge_scores={"overall": eval_result.overall_score},
                execution_pass_rate=1.0,
                source="demo",
                agent_pool=f"{selected_pool}-pool",
                stage_pool="L1",
            )
            memory.add_to_e_pool(piece)
            print(f"  成功经验已记录到 E-pool")
        else:
            print(f"  失败尝试已记录到 X-pool (后续 judge 评分 >= 0.6 时合并)")

    # 8. X-pool 合并
    consolidated = memory.consolidate_x_to_e()
    print(f"\n  X-pool 合并: {consolidated} 条高质量碎片合并到 E-pool")

    # 最终统计
    print(f"\n最终双池状态:")
    stats = memory.pool_stats()
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print(f"\n最终路由器状态:")
    router.print_state()


# ----- 运行所有示例 -----

if __name__ == "__main__":
    print("=" * 60)
    print("DECENTMEM 双池记忆集成示例")
    print("=" * 60)

    demo_dual_pool_init()
    demo_router_decisions()
    demo_router_simulation()
    demo_llm_judge()
    demo_grpo_integration()
    demo_end_to_end()

    print("\n" + "=" * 60)
    print("所有示例运行完成!")
    print("=" * 60)
    print("\n下一步:")
    print("  1. 将 dual_pool_memory.py 的导入添加到 cot_react_agent.py")
    print("  2. 用 llm_judge.py 替换启发式评分")
    print("  3. 用 online_router.py 替换固定 RAG 检索")
    print("  4. 在 GRPO 训练中使用路由器 reward 函数")
