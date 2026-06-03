"""
LLM-as-a-Judge: Stage-Level Quality Evaluation
参考: DECENTMEM Section 3 & 4.3

核心设计:
- 对 CoT 推理的每个阶段做 stage-level 评估（而非仅评估最终答案）
- 评估维度: 正确性、分配质量、中间一致性、最终整合
- 输出结构化反馈，驱动在线路由器权重更新

对比你现有的启发式评分:
  旧: 基于关键词规则的 heuristic_step_reward()
  新: 基于 LLM 推理的真正理解式评分
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


# ==================== 评估数据结构 ====================

@dataclass
class StageEvaluation:
    """单阶段评估结果"""
    stage_id: int  # 1-4 对应 CoT 四步
    stage_name: str  # "Bug Identification" / "Root Cause" / "Strategy" / "Verification"
    score: float  # 0-10 分
    quality: str  # "poor" / "fair" / "good" / "excellent"

    # 详细反馈
    reasoning: str  # 评估理由
    solution_quality: float  # 解质量
    llm_answer_quality: float  # 推理质量
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)
    agent_coordination: str = "N/A"  # 单 Agent 场景固定为 N/A

    # 用于路由器更新
    improvement_delta: float = 0.0  # 相对上一阶段的分数变化


@dataclass
class FullTaskEvaluation:
    """完整任务评估结果"""
    task_id: str
    overall_score: float  # 加权平均分
    stage_evals: List[StageEvaluation]  # 各阶段评估

    # 路由器关心的一维信号
    success: bool  # 是否成功修复
    total_reward: float  # 归一化 reward

    # 协作轨迹（DECENTMEM 特有）
    cooperation_trace: List[Dict] = field(default_factory=list)

    # 元信息
    judge_model: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "overall_score": self.overall_score,
            "stage_evals": [
                {
                    "stage_id": s.stage_id,
                    "stage_name": s.stage_name,
                    "score": s.score,
                    "quality": s.quality,
                    "reasoning": s.reasoning,
                    "improvement_delta": s.improvement_delta,
                }
                for s in self.stage_evals
            ],
            "success": self.success,
            "total_reward": self.total_reward,
            "judge_model": self.judge_model,
            "timestamp": self.timestamp,
        }


# ==================== 阶段配置 ====================

STAGE_CONFIG = {
    1: {
        "name": "Bug Identification",
        "description": "识别 bug 类型和特征",
        "criteria": [
            "是否准确定位了 bug 所在的代码位置",
            "是否识别出了具体的 bug 类型（off-by-one, logic error 等）",
            "识别描述是否有代码证据支持",
        ],
    },
    2: {
        "name": "Root Cause Analysis",
        "description": "分析 bug 根因",
        "criteria": [
            "是否解释了 bug 产生的具体原因",
            "根因分析是否有逻辑支撑",
            "是否关联了输入输出行为异常",
        ],
    },
    3: {
        "name": "Fix Strategy",
        "description": "制定修复策略",
        "criteria": [
            "修复策略是否针对根因",
            "是否描述了具体的代码改动",
            "策略是否考虑了边界情况",
        ],
    },
    4: {
        "name": "Verification",
        "description": "验证修复结果",
        "criteria": [
            "是否描述了测试验证方案",
            "是否考虑了边界 case",
            "是否说明了预期的修复效果",
        ],
    },
}


# ==================== LLM Judge ====================

class LLMJudge:
    """
    LLM-as-a-Judge 评估器

    核心能力:
    1. Stage-level 评分（每个 CoT 步独立评估）
    2. 对比两阶段分数，计算 improvement_delta
    3. 输出结构化 JSON，方便后续路由器解析
    4. 支持降级到启发式评估（无 GPU 时）

    对应论文 Section 4.3:
      - 评估每个 stage 的正确性、分配质量、中间一致性、最终整合
      - q_prev 和 q_curr 是连续两阶段的分数
      - Δt = I[q_curr > q_prev] 用于路由器更新
    """

    STAGE_PROMPT = """你是一位专业的代码修复评估专家。你的任务是对 AI Agent 的 CoT 推理过程进行严格评估。

## 当前任务
Buggy Code:
```python
{buggy_code}
```

## Agent 的推理过程
```
{reasoning_content}
```

## 评估阶段: {stage_name} (Step {stage_id}/4)

请评估这第 {stage_id} 步推理的质量。

## 评分标准
1. **问题理解**: Agent 是否正确理解了问题？
2. **推理质量**: 推理过程是否有逻辑性？
3. **具体性**: 是否给出了具体的分析/策略/代码？
4. **与代码的关联**: 是否引用了 buggy code 的具体位置？

## 输出要求
请严格按以下 JSON 格式输出，不要有任何额外文字：

{{
    "stage_id": {stage_id},
    "stage_name": "{stage_name}",
    "score": <0到10的分数>,
    "quality": "<poor/fair/good/excellent之一>",
    "reasoning": "<详细的评估理由，1-2句话>",
    "solution_quality": <0到10的分数>,
    "llm_answer_quality": <0到10的分数>,
    "strengths": ["<优点1>", "<优点2>"],
    "weaknesses": ["<缺点1>", "<缺点2>"]
}}

注意: score 是整数或一位小数，在 0-10 之间。"""

    # ----- 初始化 -----

    def __init__(
        self,
        model_path: str = "Qwen/Qwen2.5-7B-Instruct",
        device: str = "cuda",
        fallback_to_heuristic: bool = True,
    ):
        self.device = device if torch.cuda.is_available() else "cpu"
        self.fallback_to_heuristic = fallback_to_heuristic
        self.model = None
        self.tokenizer = None
        self.model_path = model_path

        if model_path:
            try:
                print(f"[Judge] Loading judge model: {model_path}")
                self.tokenizer = AutoTokenizer.from_pretrained(
                    model_path, trust_remote_code=True
                )
                self.model = AutoModelForCausalLM.from_pretrained(
                    model_path,
                    torch_dtype=torch.float16,
                    device_map="auto",
                    trust_remote_code=True,
                )
                self.model.eval()
                print(f"[Judge] Model loaded successfully")
            except Exception as e:
                print(f"[Judge] Failed to load model: {e}, will use heuristic fallback")
                self.model = None
                self.tokenizer = None

    # ----- 核心评估接口 -----

    def evaluate_task(
        self,
        buggy_code: str,
        cot_result: str,
        task_id: str = None,
        execution_result: Dict = None,
    ) -> FullTaskEvaluation:
        """
        评估一个完整的代码修复任务

        Args:
            buggy_code: 原始 buggy 代码
            cot_result: Agent 的完整 CoT 推理结果
            task_id: 任务 ID
            execution_result: 执行结果 {"pass_rate": float, "tests_passed": int, "tests_total": int}

        Returns:
            FullTaskEvaluation: 完整评估结果
        """
        task_id = task_id or hash(buggy_code) % 1000000
        steps = self._parse_cot_steps(cot_result)

        if not steps:
            # 无法解析 CoT，返回失败评估
            return self._create_empty_eval(task_id, buggy_code, cot_result)

        stage_evals = []

        for step_id, step_content in steps.items():
            eval_result = self._evaluate_stage(
                buggy_code=buggy_code,
                step_id=step_id,
                step_content=step_content,
            )
            stage_evals.append(eval_result)

        # 计算阶段间的 improvement_delta
        for i in range(1, len(stage_evals)):
            delta = stage_evals[i].score - stage_evals[i - 1].score
            stage_evals[i].improvement_delta = delta

        # 计算总体分
        stage_weights = [0.20, 0.25, 0.30, 0.25]  # Step 3 (策略) 权重最高
        overall = sum(
            e.score * w for e, w in zip(stage_evals, stage_weights)
        )

        # 如果有执行结果，融合进来
        if execution_result:
            pass_rate = execution_result.get("pass_rate", 0.0)
            # 执行通过率 * 10 映射到 0-10 分
            exec_score = pass_rate * 10.0
            overall = overall * 0.5 + exec_score * 0.5

        quality_map = {
            (9, 11): "excellent",
            (7, 9): "good",
            (4, 7): "fair",
            (0, 4): "poor",
        }
        quality = next(
            (v for (lo, hi), v in quality_map.items() if lo <= overall < hi),
            "poor",
        )

        success = execution_result.get("pass_rate", 0.0) >= 1.0 if execution_result else False

        eval_ = FullTaskEvaluation(
            task_id=str(task_id),
            overall_score=overall,
            stage_evals=stage_evals,
            success=success,
            total_reward=overall / 10.0,
            judge_model=self.model_path or "heuristic",
        )

        return eval_

    def evaluate_stage_stream(
        self,
        buggy_code: str,
        step_id: int,
        step_content: str,
        previous_score: float = None,
    ) -> StageEvaluation:
        """
        评估单个阶段（流式接口，用于在线路由器实时反馈）

        Args:
            buggy_code: buggy 代码
            step_id: 阶段 ID (1-4)
            step_content: 该阶段的推理内容
            previous_score: 上一阶段的分数（用于计算 improvement_delta）

        Returns:
            StageEvaluation: 阶段评估结果
        """
        return self._evaluate_stage(buggy_code, step_id, step_content, previous_score)

    # ----- 内部方法 -----

    def _evaluate_stage(
        self,
        buggy_code: str,
        step_id: int,
        step_content: str,
        previous_score: float = None,
    ) -> StageEvaluation:
        """评估单个 CoT 阶段"""
        if self.model and self.tokenizer:
            return self._llm_evaluate(buggy_code, step_id, step_content)
        elif self.fallback_to_heuristic:
            return self._heuristic_evaluate(step_id, step_content, buggy_code)
        else:
            raise RuntimeError("No judge model available and heuristic fallback disabled")

    def _llm_evaluate(
        self, buggy_code: str, step_id: int, step_content: str
    ) -> StageEvaluation:
        """用 LLM 做 stage-level 评估"""
        config = STAGE_CONFIG.get(step_id, STAGE_CONFIG[1])

        prompt = self.STAGE_PROMPT.format(
            buggy_code=buggy_code[:500],  # 截断避免 token 过长
            reasoning_content=step_content[:800],
            stage_id=step_id,
            stage_name=config["name"],
        )

        messages = [
            {"role": "system", "content": "你是一个严格的代码修复评估专家。只输出JSON格式。"},
            {"role": "user", "content": prompt},
        ]

        try:
            inputs = self.tokenizer.apply_chat_template(
                messages, return_tensors="pt"
            ).to(self.device)

            with torch.no_grad():
                outputs = self.model.generate(
                    inputs,
                    max_new_tokens=200,
                    temperature=0.1,
                    do_sample=False,
                )

            response = self.tokenizer.decode(
                outputs[0][len(inputs[0]):], skip_special_tokens=True
            ).strip()

            # 解析 JSON
            eval_data = self._parse_judge_response(response)

            stage_eval = StageEvaluation(
                stage_id=eval_data.get("stage_id", step_id),
                stage_name=eval_data.get("stage_name", config["name"]),
                score=eval_data.get("score", 5.0),
                quality=eval_data.get("quality", "fair"),
                reasoning=eval_data.get("reasoning", ""),
                solution_quality=eval_data.get("solution_quality", 5.0),
                llm_answer_quality=eval_data.get("llm_answer_quality", 5.0),
                strengths=eval_data.get("strengths", []),
                weaknesses=eval_data.get("weaknesses", []),
            )

            return stage_eval

        except Exception as e:
            print(f"[Judge] LLM evaluation failed: {e}, falling back to heuristic")
            return self._heuristic_evaluate(step_id, step_content, buggy_code)

    def _parse_judge_response(self, response: str) -> Dict:
        """从 LLM 输出中解析 JSON 评估结果"""
        # 尝试提取 JSON 块
        json_match = re.search(r"\{[\s\S]*\}", response)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        # 降级：解析关键字段
        score_match = re.search(r'"score":\s*([0-9.]+)', response)
        quality_match = re.search(r'"quality":\s*"([^"]+)"', response)

        return {
            "score": float(score_match.group(1)) if score_match else 5.0,
            "quality": quality_match.group(1) if quality_match else "fair",
            "stage_id": 1,
            "stage_name": "unknown",
            "reasoning": response[:200],
            "solution_quality": 5.0,
            "llm_answer_quality": 5.0,
            "strengths": [],
            "weaknesses": [],
        }

    def _heuristic_evaluate(
        self, step_id: int, step_content: str, buggy_code: str
    ) -> StageEvaluation:
        """
        启发式评估（当 LLM 不可用时的降级方案）
        复用你原有的 heuristic_step_reward 逻辑
        """
        config = STAGE_CONFIG.get(step_id, STAGE_CONFIG[1])
        score = 5.0  # 默认分
        strengths = []
        weaknesses = []

        # 长度检查
        if len(step_content) < 20:
            score = 3.0
            weaknesses.append("推理内容过短，缺乏分析深度")
        elif len(step_content) > 500:
            score = 7.0
            strengths.append("推理详细具体")

        # 阶段特定检查
        if step_id == 1:  # Bug Identification
            bug_keywords = ['off-by-one', 'wrong operator', 'missing check', 'boundary',
                           'type error', 'index', 'initialization', 'condition', 'logic error']
            if any(kw in step_content.lower() for kw in bug_keywords):
                score = max(score, 7.0)
                strengths.append("准确识别了 bug 类型")
            if buggy_code and any(tok in step_content for tok in buggy_code.split()[:10]):
                score = max(score, 8.0)
                strengths.append("引用了 buggy code 的具体代码")

        elif step_id == 2:  # Root Cause
            causal_words = ['because', 'since', 'cause', 'reason', 'when', 'if',
                          'leads to', 'results in', 'triggers']
            if any(w in step_content.lower() for w in causal_words):
                score = max(score, 7.0)
                strengths.append("给出了因果分析")
            if re.search(r'(input|output|return|value|result)\s*(is|should|would)',
                         step_content, re.I):
                score = max(score, 8.0)
                strengths.append("关联了输入输出行为")

        elif step_id == 3:  # Fix Strategy
            action_words = ['change', 'replace', 'add', 'remove', 'modify', 'update',
                          'fix', 'correct', 'swap', 'initialize', 'check']
            if any(w in step_content.lower() for w in action_words):
                score = max(score, 7.0)
                strengths.append("给出了具体的修复动作")
            if '`' in step_content or 'code' in step_content.lower():
                score = max(score, 8.0)
                strengths.append("包含代码片段")

        elif step_id == 4:  # Verification
            edge_keywords = ['empty', 'null', 'none', 'zero', 'negative', 'large',
                           'single', 'boundary', 'special', 'corner']
            edge_count = sum(1 for kw in edge_keywords if kw in step_content.lower())
            if edge_count >= 2:
                score = max(score, 7.0)
                strengths.append("考虑了多个边界情况")

        score = min(score, 10.0)

        quality_map = {
            (9, 11): "excellent",
            (7, 9): "good",
            (4, 7): "fair",
            (0, 4): "poor",
        }
        quality = next(
            (v for (lo, hi), v in quality_map.items() if lo <= score < hi),
            "fair",
        )

        return StageEvaluation(
            stage_id=step_id,
            stage_name=config["name"],
            score=score,
            quality=quality,
            reasoning=f"Heuristic evaluation (no LLM): {', '.join(strengths) or 'standard analysis'}",
            solution_quality=score,
            llm_answer_quality=score,
            strengths=strengths,
            weaknesses=weaknesses,
        )

    def _parse_cot_steps(self, cot_result: str) -> Dict[int, str]:
        """从 CoT 结果中解析出各步骤内容"""
        steps = {}

        think_match = re.search(r'<think>(.*?)
</think>', cot_result, re.DOTALL)
        if not think_match:
            return steps

        content = think_match.group(1)
        patterns = [
            (1, r'\[Step 1:.*?\]\s*(.*?)(?=\[Step 2:|$)', "Bug Identification"),
            (2, r'\[Step 2:.*?\]\s*(.*?)(?=\[Step 3:|$)', "Root Cause Analysis"),
            (3, r'\[Step 3:.*?\]\s*(.*?)(?=\[Step 4:|$)', "Fix Strategy"),
            (4, r'\[Step 4:.*?\]\s*(.*?)$', "Verification"),
        ]

        for step_id, pattern, _ in patterns:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                steps[step_id] = match.group(1).strip()

        return steps

    def _create_empty_eval(self, task_id: str, buggy_code: str, cot_result: str):
        """创建空评估（无法解析 CoT 时）"""
        return FullTaskEvaluation(
            task_id=str(task_id),
            overall_score=0.0,
            stage_evals=[],
            success=False,
            total_reward=0.0,
            judge_model=self.model_path or "heuristic",
        )

    # ----- 路由器辅助 -----

    def get_router_delta(self, eval_result: FullTaskEvaluation) -> Tuple[float, str]:
        """
        从评估结果提取路由器更新所需信号

        Returns:
            (Δt, used_pool_hint)
            Δt = 1 if overall score improved relative to last stage, else 0
            used_pool_hint: "E" or "X" based on score trajectory
        """
        if not eval_result.stage_evals or len(eval_result.stage_evals) < 2:
            return 0.0, "X"

        # 计算 Δt：最后阶段的分数是否高于第一阶段
        first_score = eval_result.stage_evals[0].score
        last_score = eval_result.stage_evals[-1].score
        delta = 1.0 if last_score > first_score else 0.0

        # 如果最终分数 >= 7，认为 E-pool 策略有效
        pool_hint = "E" if last_score >= 7.0 else "X"

        return delta, pool_hint

    # ----- 对比分析（实验用）----

    def compare_with_perplexity(self, buggy_code: str, cot_result: str) -> Dict:
        """
        对比 LLM-as-a-Judge 与 Perplexity 两种评估方式
        说明为什么 judge 优于 perplexity
        """
        import math

        # 用 judge 评分
        judge_eval = self.evaluate_task(buggy_code, cot_result)

        # 用 perplexity（复用你原有的 semantic reward 逻辑）
        tokens = cot_result.split()
        if len(tokens) > 1:
            # 简化的 perplexity 估计
            log_prob = -len(tokens) * 0.5  # 假设每个 token log_prob = -0.5
            perplexity = math.exp(-log_prob / len(tokens))
        else:
            perplexity = 1.0

        return {
            "judge_overall_score": judge_eval.overall_score,
            "judge_stage_scores": [e.score for e in judge_eval.stage_evals],
            "perplexity": perplexity,
            "perplexity_reward": -math.log(perplexity),
            "judge_vs_perplexity": (
                "Judge 提供每步独立的 4 个信号，而 perplexity 只提供 1 个标量。"
                "Judge 能捕捉推理过程中某一步的错误，perplexity 只能衡量语言流畅度。"
                "这正是 DECENTMEM 中 LLM-as-a-Judge 相比朴素 reward 的优势。"
            ),
        }
