#!/usr/bin/env python3
"""
Process Reward Model (PRM) for Code Repair

Motivation: The original GRPO uses 7B model perplexity as reward, which is
a coarse signal — it measures fluency, not correctness.

Compile-First Reward Philosophy:
  Compiler and tests are FREE ground truth signals. Use them FIRST.
  Only call expensive models when ground truth is insufficient.

PRM provides STEP-LEVEL rewards for CoT reasoning, enabling:
  1. Precise credit assignment (which reasoning step caused failure?)
  2. 4x denser training signal for GRPO (one score per step vs one scalar)
  3. Avoids reward hacking (can't game 6 independent dimensions)
  4. Error attribution for targeted data collection

Architecture:
  - Multi-granularity reward: Format + Syntax + AST + Execution + Step-level + Semantic
  - PRM head on top of 7B SFT model for step classification
  - Execution sandbox for ground-truth signal (compile + test cases)
"""

import re
import ast
import sys
import math
import json
import traceback
import logging
from io import StringIO
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field

import torch
import torch.nn as nn
import numpy as np

logger = logging.getLogger(__name__)


# ==================== Multi-Granularity Reward System ====================

@dataclass
class RewardBreakdown:
    """Detailed reward breakdown for interpretability"""
    format_reward: float = 0.0
    syntax_reward: float = 0.0
    ast_reward: float = 0.0
    execution_reward: float = 0.0
    step_rewards: List[float] = field(default_factory=list)
    semantic_reward: float = 0.0
    total_reward: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "format": self.format_reward,
            "syntax": self.syntax_reward,
            "ast": self.ast_reward,
            "execution": self.execution_reward,
            "steps": self.step_rewards,
            "semantic": self.semantic_reward,
            "total": self.total_reward,
        }


class MultiGranularityReward:
    """
    Combines 6 reward dimensions for precise evaluation:
      1. Format  (0.05) - CoT structure completeness
      2. Syntax  (0.10) - Python AST parseable
      3. AST     (0.15) - Structural similarity to reference
      4. Execution (0.30) - Test case pass rate
      5. Process (0.25) - Step-level reasoning quality (PRM)
      6. Semantic (0.15) - Embedding similarity to reference
    """

    WEIGHTS = {
        "format": 0.05,
        "syntax": 0.10,
        "ast": 0.15,
        "execution": 0.30,
        "process": 0.25,
        "semantic": 0.15,
    }

    def __init__(self, prm_model=None, embedding_model=None):
        self.prm_model = prm_model
        self.embedding_model = embedding_model

    def compute(self, prompt: str, completion: str,
                reference_code: str = None,
                test_cases: List[Dict] = None,
                buggy_code: str = None) -> RewardBreakdown:
        """Compute multi-granularity reward"""
        breakdown = RewardBreakdown()

        fixed_code = self._extract_fixed_code(completion)

        breakdown.format_reward = self._format_reward(completion)
        breakdown.syntax_reward = self._syntax_reward(fixed_code)
        breakdown.ast_reward = self._ast_reward(fixed_code, reference_code)
        breakdown.execution_reward = self._execution_reward(fixed_code, test_cases, buggy_code)
        breakdown.step_rewards = self._process_reward(completion, buggy_code)
        breakdown.semantic_reward = self._semantic_reward(fixed_code, reference_code)

        process_score = (
            np.mean(breakdown.step_rewards) if breakdown.step_rewards else 0.0
        )

        breakdown.total_reward = (
            self.WEIGHTS["format"] * breakdown.format_reward +
            self.WEIGHTS["syntax"] * breakdown.syntax_reward +
            self.WEIGHTS["ast"] * breakdown.ast_reward +
            self.WEIGHTS["execution"] * breakdown.execution_reward +
            self.WEIGHTS["process"] * process_score +
            self.WEIGHTS["semantic"] * breakdown.semantic_reward
        )

        return breakdown

    def _extract_fixed_code(self, completion: str) -> str:
        match = re.search(
            r'<fixed_code>\s*```python\s*(.*?)\s*```\s*</fixed_code>',
            completion, re.DOTALL
        )
        if not match:
            match = re.search(
                r'<fixed_code>\s*(.*?)\s*</fixed_code>',
                completion, re.DOTALL
            )
        return match.group(1).strip() if match else ""

    # ----- Dimension 1: Format Reward -----
    def _format_reward(self, completion: str) -> float:
        score = 0.0
        if '<think>' in completion and '</think>' in completion:
            score += 0.25
        for i in range(1, 5):
            if f'[Step {i}:' in completion:
                score += 0.125
        if '<fixed_code>' in completion and '</fixed_code>' in completion:
            score += 0.25
        return min(score, 1.0)

    # ----- Dimension 2: Syntax Reward -----
    def _syntax_reward(self, code: str) -> float:
        if not code:
            return 0.0
        try:
            compile(code, '<string>', 'exec')
            return 1.0
        except SyntaxError:
            return 0.0

    # ----- Dimension 3: AST Structural Similarity -----
    def _ast_reward(self, generated: str, reference: str) -> float:
        if not generated or not reference:
            return 0.0

        try:
            gen_tree = ast.parse(generated)
            ref_tree = ast.parse(reference)
        except SyntaxError:
            return 0.0

        gen_nodes = self._ast_node_types(gen_tree)
        ref_nodes = self._ast_node_types(ref_tree)

        if not ref_nodes:
            return 0.0

        intersection = gen_nodes & ref_nodes
        union = gen_nodes | ref_nodes

        jaccard = len(intersection) / len(union) if union else 0.0

        gen_funcs = self._extract_function_signatures(gen_tree)
        ref_funcs = self._extract_function_signatures(ref_tree)
        sig_match = 1.0 if gen_funcs == ref_funcs else 0.5

        return 0.6 * jaccard + 0.4 * sig_match

    def _ast_node_types(self, tree) -> set:
        """Extract all AST node types as a set"""
        return {type(node).__name__ for node in ast.walk(tree)}

    def _extract_function_signatures(self, tree) -> List[str]:
        """Extract function names and parameter counts"""
        sigs = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                n_args = len(node.args.args)
                sigs.append(f"{node.name}/{n_args}")
        return sorted(sigs)

    # ----- Dimension 4: Execution Reward (ACES-Inspired Weighted) -----
    def _execution_reward(self, code: str, test_cases: List[Dict] = None,
                          buggy_code: str = None) -> float:
        """
        ACES-inspired: weight each test by its discriminative power (δj).

        Traditional: uniform weighting (1/m per test)
        ACES: wj ∝ max(0, δj) where δj = pass_rate_correct - pass_rate_incorrect

        In code repair context, we estimate δj from bug type metadata:
        - Logic/boundary bugs → high discriminative power (1.1-1.2)
        - Style/optimization bugs → low discriminative power (0.5)
        This gives more weight to tests that actually separate good from bad fixes.
        """
        if not code or not test_cases:
            return 0.0

        total_weight = 0.0
        weighted_pass = 0.0

        for tc in test_cases:
            # ACES: get test discriminative power (δj proxy)
            test_weight = get_test_difficulty_weight(tc)
            total_weight += test_weight

            try:
                namespace = {}
                exec(code, namespace)

                func_name = tc.get("function", "")
                args = tc.get("input", [])
                expected = tc.get("output")

                if func_name not in namespace:
                    continue

                old_stdout = sys.stdout
                sys.stdout = StringIO()
                try:
                    if isinstance(args, list):
                        result = namespace[func_name](*args)
                    elif isinstance(args, dict):
                        result = namespace[func_name](**args)
                    else:
                        result = namespace[func_name](args)
                finally:
                    sys.stdout = old_stdout

                if result == expected:
                    weighted_pass += test_weight

            except Exception:
                # ACES: failing to compile/run is a strong negative signal
                # Misleading tests (wrong expected output) get down-weighted via bug type
                continue

        if total_weight <= 0:
            return 0.0

        # Weighted accuracy = discriminative power weighted pass rate
        return weighted_pass / total_weight

    # ----- Dimension 5: Process Reward (Step-Level) -----
    def _process_reward(self, completion: str, buggy_code: str = None) -> List[float]:
        """Score each CoT reasoning step"""
        if self.prm_model:
            return self.prm_model.score_steps_from_text(completion, buggy_code)

        return self._heuristic_step_reward(completion, buggy_code)

    def _heuristic_step_reward(self, completion: str,
                               buggy_code: str = None) -> List[float]:
        """Rule-based step scoring when PRM model is unavailable"""
        scores = []

        steps = self._parse_steps(completion)

        # Step 1: Bug Identification - should mention specific bug type
        s1 = steps.get(1, "")
        score1 = 0.0
        bug_keywords = [
            'off-by-one', 'wrong operator', 'missing check', 'boundary',
            'type error', 'index', 'initialization', 'condition', 'return',
            'null', 'empty', 'overflow', 'underflow', 'logic error'
        ]
        if any(kw in s1.lower() for kw in bug_keywords):
            score1 += 0.5
        if buggy_code and any(tok in s1 for tok in buggy_code.split()[:10]):
            score1 += 0.3
        if len(s1) > 20:
            score1 += 0.2
        scores.append(min(score1, 1.0))

        # Step 2: Root Cause - should explain WHY
        s2 = steps.get(2, "")
        score2 = 0.0
        causal_words = ['because', 'since', 'cause', 'reason', 'when', 'if',
                        'leads to', 'results in', 'triggers', 'produces']
        if any(w in s2.lower() for w in causal_words):
            score2 += 0.4
        if re.search(r'(input|output|return|value|result)\s*(is|should|would|will)',
                      s2, re.I):
            score2 += 0.3
        if len(s2) > 30:
            score2 += 0.3
        scores.append(min(score2, 1.0))

        # Step 3: Fix Strategy - should describe concrete changes
        s3 = steps.get(3, "")
        score3 = 0.0
        action_words = ['change', 'replace', 'add', 'remove', 'modify', 'update',
                        'fix', 'correct', 'swap', 'initialize', 'check']
        if any(w in s3.lower() for w in action_words):
            score3 += 0.5
        if '`' in s3 or 'code' in s3.lower():
            score3 += 0.3
        if len(s3) > 20:
            score3 += 0.2
        scores.append(min(score3, 1.0))

        # Step 4: Edge Cases - should mention specific scenarios
        s4 = steps.get(4, "")
        score4 = 0.0
        edge_keywords = ['empty', 'null', 'none', 'zero', 'negative', 'large',
                         'single', 'boundary', 'edge', 'special', 'corner']
        edge_count = sum(1 for kw in edge_keywords if kw in s4.lower())
        score4 += min(edge_count * 0.25, 0.6)
        if len(s4) > 30:
            score4 += 0.4
        scores.append(min(score4, 1.0))

        return scores

    def _parse_steps(self, completion: str) -> Dict[int, str]:
        steps = {}
        think_match = re.search(r'<think>(.*?)</think>', completion, re.DOTALL)
        if not think_match:
            return steps

        content = think_match.group(1)
        patterns = [
            (1, r'\[Step 1:.*?\]\s*(.*?)(?=\[Step 2:|$)'),
            (2, r'\[Step 2:.*?\]\s*(.*?)(?=\[Step 3:|$)'),
            (3, r'\[Step 3:.*?\]\s*(.*?)(?=\[Step 4:|$)'),
            (4, r'\[Step 4:.*?\]\s*(.*?)$'),
        ]
        for step_id, pattern in patterns:
            match = re.search(pattern, content, re.DOTALL)
            if match:
                steps[step_id] = match.group(1).strip()
        return steps

    # ----- Dimension 6: Semantic Similarity -----
    def _semantic_reward(self, generated: str, reference: str) -> float:
        if not generated or not reference:
            return 0.0

        if self.embedding_model:
            gen_emb = self.embedding_model.encode([generated])[0]
            ref_emb = self.embedding_model.encode([reference])[0]
            cos_sim = np.dot(gen_emb, ref_emb) / (
                np.linalg.norm(gen_emb) * np.linalg.norm(ref_emb) + 1e-8
            )
            return max(0.0, float(cos_sim))

        gen_tokens = set(re.findall(r'\b\w+\b', generated.lower()))
        ref_tokens = set(re.findall(r'\b\w+\b', reference.lower()))
        if not ref_tokens:
            return 0.0
        return len(gen_tokens & ref_tokens) / len(gen_tokens | ref_tokens)


# ==================== ACES-Inspired Test Difficulty Weighting ====================
# ACES核心洞察: 测试的价值不在于它是否正确，而在于能否区分正确代码和错误代码。
# 在代码修复场景：不同bug类型的测试有不同区分能力（discriminative power）。
# - 算术逻辑bug测试: 区分能力强（正确vs错误差异明显）
# - 初始化bug测试: 区分能力强（空值/边界差异明显）
# - 语法修复测试: 区分能力弱（大部分生成代码语法都正确）
# - 风格/优化测试: 区分能力弱（多数正确代码风格一致）

TEST_DISCRIMINATIVE_WEIGHTS = {
    # High discriminative power (ACES: δj > 0, informative)
    "logic_error": 1.2,
    "off_by_one": 1.2,
    "initialization": 1.1,
    "boundary": 1.1,
    "null_pointer": 1.1,
    "wrong_operator": 1.0,
    "missing_check": 1.0,
    "type_error": 0.9,
    # Low discriminative power (ACES: δj ≈ 0, uninformative)
    "syntax_fix": 0.6,
    "style": 0.5,
    "optimization": 0.5,
    "variable_naming": 0.5,
}

def get_test_difficulty_weight(test_case: Dict) -> float:
    """
    Infer test difficulty/discriminative power from test case metadata.
    Falls back to keyword detection if no metadata available.
    ACES principle: even imperfect tests provide ranking signal if δj > 0.
    """
    # Method 1: Explicit metadata
    if "difficulty" in test_case:
        return float(test_case["difficulty"])

    # Method 2: Bug type field (ACES: each test has latent discriminative power δj)
    bug_type = test_case.get("bug_type", "").lower()
    if bug_type in TEST_DISCRIMINATIVE_WEIGHTS:
        return TEST_DISCRIMINATIVE_WEIGHTS[bug_type]

    # Method 3: Function name keyword detection (heuristic)
    func_name = test_case.get("function", "").lower()
    test_input = str(test_case.get("input", "")).lower()

    # Logic/arithmetic tests tend to be more discriminative
    discriminative_keywords = ["sort", "search", "calc", "compute", "fibonacci",
                               "gcd", "prime", "binary", "tree", "graph", "list"]
    uninformative_keywords = ["format", "style", "name", "comment", "docstring"]

    for kw in discriminative_keywords:
        if kw in func_name or kw in test_input:
            return 1.1

    for kw in uninformative_keywords:
        if kw in func_name or kw in test_input:
            return 0.5

    return 1.0  # default


# ==================== PRM Neural Model ====================

class PRMHead(nn.Module):
    """
    Process Reward Model head on top of a language model.

    Takes hidden states at step boundary tokens and classifies
    each step as correct/incorrect with a confidence score.

    Input:  hidden_states at step boundary positions [batch, hidden_dim]
    Output: step scores [batch, num_steps]
    """

    def __init__(self, hidden_dim: int = 4096, num_steps: int = 4):
        super().__init__()
        self.classifier = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(hidden_dim // 2, 1),
            nn.Sigmoid()
        )
        self.num_steps = num_steps

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        """
        Args:
            hidden_states: [batch_size, num_steps, hidden_dim]
        Returns:
            scores: [batch_size, num_steps] in range [0, 1]
        """
        return self.classifier(hidden_states).squeeze(-1)


class ProcessRewardModel:
    """
    Full PRM: 7B base model + PRM head for step-level scoring.

    Training: Binary cross-entropy on step-level labels
    Inference: Score each CoT step independently

    Compared to perplexity-based reward:
      - Perplexity: measures fluency, not correctness (can be gamed)
      - PRM: measures reasoning correctness at each step
      - PRM provides 4x more training signal per sample
    """

    def __init__(self, base_model_path: str = None,
                 prm_head_path: str = None,
                 hidden_dim: int = 4096,
                 device: str = "cuda"):
        self.device = device
        self.hidden_dim = hidden_dim
        self.tokenizer = None
        self.base_model = None
        self.prm_head = PRMHead(hidden_dim=hidden_dim).to(device)

        if base_model_path:
            self._load_model(base_model_path)
        if prm_head_path:
            self.prm_head.load_state_dict(
                torch.load(prm_head_path, map_location=device)
            )

    def _load_model(self, model_path: str):
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(model_path)
        self.base_model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            device_map="auto",
            output_hidden_states=True
        )
        self.base_model.eval()

    def score_steps(self, cot_result, buggy_code: str = None) -> 'CoTResult':
        """Score each step of a CoTResult using the PRM head"""
        if not self.base_model or not cot_result.steps:
            return cot_result

        step_texts = [
            f"[Step {s.step_id}] {s.content}" for s in cot_result.steps
        ]

        full_text = "\n".join(step_texts)
        inputs = self.tokenizer(
            full_text, return_tensors="pt", truncation=True, max_length=1024
        ).to(self.device)

        with torch.no_grad():
            outputs = self.base_model(**inputs)
            hidden_states = outputs.hidden_states[-1]

            step_boundaries = self._find_step_boundaries(
                inputs["input_ids"][0], step_texts
            )

            if len(step_boundaries) == len(cot_result.steps):
                step_hidden = torch.stack([
                    hidden_states[0, pos] for pos in step_boundaries
                ]).unsqueeze(0)

                scores = self.prm_head(step_hidden)[0]

                for step, score in zip(cot_result.steps, scores):
                    step.score = float(score) * 10.0

        return cot_result

    def score_steps_from_text(self, completion: str,
                              buggy_code: str = None) -> List[float]:
        """Score steps directly from raw completion text"""
        if not self.base_model:
            return []

        steps = self._extract_step_texts(completion)
        if not steps:
            return []

        scores = []
        for step_text in steps:
            context = f"Buggy code: {buggy_code}\nReasoning step: {step_text}" if buggy_code else step_text
            inputs = self.tokenizer(
                context, return_tensors="pt", truncation=True, max_length=512
            ).to(self.device)

            with torch.no_grad():
                outputs = self.base_model(**inputs)
                hidden = outputs.hidden_states[-1][0, -1]
                score = self.prm_head.classifier(hidden.unsqueeze(0))
                scores.append(float(score.squeeze()) * 10.0)

        return scores

    def _find_step_boundaries(self, input_ids, step_texts) -> List[int]:
        """Find token positions where each step ends"""
        boundaries = []
        text = self.tokenizer.decode(input_ids)

        for step_text in step_texts:
            pos = text.find(step_text)
            if pos >= 0:
                prefix = text[:pos + len(step_text)]
                token_pos = len(self.tokenizer.encode(prefix)) - 1
                boundaries.append(min(token_pos, len(input_ids) - 1))

        return boundaries

    def _extract_step_texts(self, completion: str) -> List[str]:
        think_match = re.search(r'<think>(.*?)</think>', completion, re.DOTALL)
        if not think_match:
            return []

        content = think_match.group(1)
        steps = []
        for i in range(1, 5):
            next_i = i + 1
            if i < 4:
                pattern = rf'\[Step {i}:.*?\]\s*(.*?)(?=\[Step {next_i}:)'
            else:
                pattern = rf'\[Step {i}:.*?\]\s*(.*?)$'
            match = re.search(pattern, content, re.DOTALL)
            if match:
                steps.append(match.group(1).strip())
        return steps

    def train_prm_head(self, training_data: List[Dict],
                       epochs: int = 5, lr: float = 1e-4):
        """
        Train the PRM head on step-level labeled data.

        training_data format:
        [
            {
                "completion": "...<think>[Step 1:...]...",
                "step_labels": [1, 1, 0, 1],  # 1=correct, 0=incorrect
                "buggy_code": "..."
            },
            ...
        ]
        """
        if not self.base_model:
            raise RuntimeError("Base model not loaded")

        optimizer = torch.optim.AdamW(self.prm_head.parameters(), lr=lr)
        criterion = nn.BCELoss()

        self.prm_head.train()

        for epoch in range(epochs):
            total_loss = 0.0
            n_samples = 0

            for sample in training_data:
                step_texts = self._extract_step_texts(sample["completion"])
                labels = sample["step_labels"]

                if len(step_texts) != len(labels):
                    continue

                hiddens = []
                for step_text in step_texts:
                    context = f"Buggy code: {sample.get('buggy_code', '')}\nStep: {step_text}"
                    inputs = self.tokenizer(
                        context, return_tensors="pt",
                        truncation=True, max_length=512
                    ).to(self.device)

                    with torch.no_grad():
                        outputs = self.base_model(**inputs)
                        hiddens.append(outputs.hidden_states[-1][0, -1])

                if not hiddens:
                    continue

                step_hidden = torch.stack(hiddens).unsqueeze(0)
                label_tensor = torch.tensor(
                    [labels], dtype=torch.float32
                ).to(self.device)

                scores = self.prm_head(step_hidden)
                loss = criterion(scores, label_tensor)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                total_loss += loss.item()
                n_samples += 1

            avg_loss = total_loss / max(n_samples, 1)
            logger.info(f"PRM Epoch {epoch+1}/{epochs}, Loss: {avg_loss:.4f}")

        self.prm_head.eval()

    def save_prm_head(self, path: str):
        torch.save(self.prm_head.state_dict(), path)
        logger.info(f"PRM head saved to {path}")

    def compare_with_perplexity(self, completion: str,
                                buggy_code: str = None) -> Dict:
        """
        Compare PRM scores with perplexity-based scores for analysis.
        Shows why PRM is more informative than perplexity.
        """
        prm_scores = self.score_steps_from_text(completion, buggy_code)

        full_text = completion
        inputs = self.tokenizer(
            full_text, return_tensors="pt", truncation=True, max_length=1024
        ).to(self.device)

        with torch.no_grad():
            outputs = self.base_model(**inputs, labels=inputs["input_ids"])
            perplexity = math.exp(outputs.loss.item())

        return {
            "prm_step_scores": prm_scores,
            "prm_mean": np.mean(prm_scores) if prm_scores else 0.0,
            "perplexity": perplexity,
            "perplexity_reward": -outputs.loss.item(),
            "analysis": (
                "PRM provides 4 independent signals per sample vs "
                "1 scalar from perplexity. PRM catches cases where "
                "fluent text (low perplexity) has incorrect reasoning."
            )
        }


# ==================== GRPO Reward Function Integration ====================

def create_cot_prm_reward_function(
    prm: ProcessRewardModel = None,
    embedding_model=None,
    test_cases_map: Dict[str, List[Dict]] = None
):
    """
    Create a reward function for GRPO training that uses
    multi-granularity rewards + PRM step-level scoring.

    This replaces the simple perplexity-based reward.
    """
    reward_system = MultiGranularityReward(
        prm_model=prm,
        embedding_model=embedding_model
    )

    def reward_fn(completions, prompts=None, **kwargs):
        rewards = []

        for i, completion in enumerate(completions):
            content = completion[0]["content"] if isinstance(completion, list) else completion

            prompt = ""
            buggy_code = ""
            reference = ""
            if prompts and i < len(prompts):
                prompt = prompts[i]
                code_match = re.search(
                    r'```python\s*(.*?)\s*```', prompt, re.DOTALL
                )
                buggy_code = code_match.group(1).strip() if code_match else ""

            tc = None
            if test_cases_map and prompt:
                tc = test_cases_map.get(prompt[:100], None)

            breakdown = reward_system.compute(
                prompt=prompt,
                completion=content,
                reference_code=reference,
                test_cases=tc,
                buggy_code=buggy_code
            )

            rewards.append(breakdown.total_reward * 10.0)

        return rewards

    return reward_fn
