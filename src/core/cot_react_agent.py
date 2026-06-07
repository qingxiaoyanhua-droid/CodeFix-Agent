#!/usr/bin/env python3
"""
Reflexion-Enhanced ReAct Agent for Code Repair (Compile-First Architecture)

Architecture Evolution: ReAct → Reflexion
  - Base: Compile-First Conditional Escalation (unchanged)
  - New:  After all retries fail, generate a natural-language **reflection**
          summarizing what went wrong and what should be tried differently.
          Store reflection in persistent memory; retrieve relevant reflections
          for future similar tasks → self-evolving agent.

Reflexion loop (outer):
  Task → ReAct loop (inner, max 3 rounds) → Success? → done
                                           → Failure? → Reflect → store in memory
  Next similar task → retrieve past reflections → inject into prompt → ReAct loop

Inner ReAct loop (unchanged):
  Small model CoT → compile/test → pass → done
                                 → fail → large model error analysis → retry

Key benefits over plain ReAct:
  1. Cross-task learning: mistakes on task A help solve task B
  2. Persistent memory: agent gets smarter over time
  3. Structured failure analysis: each reflection captures root cause + lesson
  4. Zero extra training cost: reflection is inference-time self-improvement
"""

from __future__ import annotations

import re
import ast as py_ast
import sys
import json
import time
import logging
import traceback
import hashlib
import uuid

try:
    import tiktoken
    _ENCODER = tiktoken.get_encoding("cl100k_base")
except ImportError:
    _ENCODER = None  # 降级到 char/4 估算
from datetime import datetime
from io import StringIO
from typing import List, Dict, Tuple, Optional, Any
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# 导入AST代码处理器（解决行号偏移问题）
try:
    from ast_code_processor import ASTCodeProcessor, FunctionInfo
    AST_PROCESSOR_AVAILABLE = True
except ImportError:
    AST_PROCESSOR_AVAILABLE = False
    print("⚠️ AST代码处理器未安装，将使用降级方案")

logger = logging.getLogger(__name__)


# ==================== Prompt Templates ====================

COT_SYSTEM_PROMPT = """You are an expert code debugger. Think step-by-step before fixing.

Your response MUST follow this EXACT format:

<think>
[Step 1: Bug Identification]
Identify the bug type and its exact location in the code.

[Step 2: Root Cause Analysis]
Explain WHY this code is incorrect. Trace the execution with a concrete input.

[Step 3: Fix Strategy]
Describe the minimal change needed. Why is this fix correct and complete?

[Step 4: Edge Case Check]
List 2-3 edge cases. Verify the fix handles them.
</think>

<fixed_code>
```python
def fixed_function():
    # corrected implementation
```
</fixed_code>"""

COT_RETRY_SYSTEM_PROMPT = """You are an expert code debugger. Based on reviewer feedback, refine your previous fix.

If the reviewer says CORRECT: output the same code unchanged.
If OPTIMIZATION_ONLY: improve code quality without breaking correctness.
If BUG_DETECTED: fix the specific error identified in your previous attempt.

Follow the same format: <think> with 4 steps + <fixed_code>. """

ERROR_ANALYSIS_PROMPT = """You are a senior code reviewer analyzing a code fix attempt.

**Original Buggy Code:**
```python
{buggy_code}
```

**Junior's Chain-of-Thought Reasoning:**
{cot_reasoning}

**Junior's Attempted Fix:**
```python
{attempted_fix}
```

**Actual Execution Result:**
```
{error_message}
```

Analyze the fix attempt and respond in JSON with these fields:

1. STATUS: Classify the current state:
   - "BUG_DETECTED": Code has a functional bug (wrong output, crash, test failure)
   - "OPTIMIZATION_ONLY": Code runs without errors but could be improved (inefficiency, style, edge cases)
   - "CORRECT": Code is correct and complete

2. first_error_step: If BUG_DETECTED, which reasoning step (1-4) contains the FIRST error? (1-4 or 0 if none)

3. error_in_reasoning: What specifically went wrong?

4. correct_reasoning: What should the correct reasoning be?

5. hint: Actionable hint for the fix (do NOT give the full solution).

6. error_type: One of "logic_error|incomplete_fix|wrong_operator|missing_edge_case|inefficiency|other"

7. optimization_note: If OPTIMIZATION_ONLY, describe what could be improved.

Respond in JSON:
```json
{{
  "status": "BUG_DETECTED|OPTIMIZATION_ONLY|CORRECT",
  "first_error_step": 1-4 or 0,
  "error_in_reasoning": "...",
  "correct_reasoning": "...",
  "hint": "...",
  "error_type": "...",
  "optimization_note": "..."
}}
```"""

REFLECTION_PROMPT = """You are an expert software engineer reflecting on a FAILED bug-fixing attempt.

After {max_iterations} rounds of trying to fix a bug, all attempts failed.

**Original Buggy Code:**
```python
{buggy_code}
```

**Bug Description:** {bug_description}

**Attempt History:**
{attempt_history}

Generate a structured reflection. Be specific and actionable — this reflection will be stored and retrieved to help fix similar bugs in the future.

Respond in JSON:
```json
{{
  "bug_pattern": "A short name for this bug pattern (e.g. 'off-by-one in loop boundary')",
  "root_cause": "Why did all attempts fail? What was fundamentally misunderstood?",
  "failed_strategies": ["strategy 1 that didn't work", "strategy 2 ..."],
  "lesson_learned": "The key insight for fixing this type of bug in the future",
  "suggested_approach": "If encountering a similar bug, try this approach instead"
}}
```"""


# ==================== Reflection Memory ====================

@dataclass
class Reflection:
    """A single reflection entry from a failed repair attempt, with verification tracking."""
    bug_description: str
    buggy_code_hash: str
    bug_pattern: str
    root_cause: str
    failed_strategies: List[str]
    lesson_learned: str
    suggested_approach: str
    timestamp: float = 0.0
    bug_keywords: List[str] = field(default_factory=list)

    # ---- Verification ring fields ----
    verification_count: int = 0      # times this reflection was retrieved
    helpful_count: int = 0          # times it was judged helpful
    last_verified: float = 0.0      # timestamp of last verification
    times_used: int = 0              # total times retrieved for actual use
    source_bug_hash: str = ""       # original bug that generated this reflection

    @property
    def usefulness_score(self) -> float:
        """Jaccard-like score: how often was it helpful when retrieved."""
        if self.verification_count == 0:
            return 0.5  # initial neutral trust
        return self.helpful_count / max(self.verification_count, 1)

    def to_dict(self) -> Dict:
        return {
            "bug_description": self.bug_description,
            "buggy_code_hash": self.buggy_code_hash,
            "bug_pattern": self.bug_pattern,
            "root_cause": self.root_cause,
            "failed_strategies": self.failed_strategies,
            "lesson_learned": self.lesson_learned,
            "suggested_approach": self.suggested_approach,
            "timestamp": self.timestamp,
            "bug_keywords": self.bug_keywords,
            "verification_count": self.verification_count,
            "helpful_count": self.helpful_count,
            "last_verified": self.last_verified,
            "times_used": self.times_used,
            "source_bug_hash": self.source_bug_hash,
        }

    @staticmethod
    def from_dict(d: Dict) -> 'Reflection':
        return Reflection(
            bug_description=d.get("bug_description", ""),
            buggy_code_hash=d.get("buggy_code_hash", ""),
            bug_pattern=d.get("bug_pattern", ""),
            root_cause=d.get("root_cause", ""),
            failed_strategies=d.get("failed_strategies", []),
            lesson_learned=d.get("lesson_learned", ""),
            suggested_approach=d.get("suggested_approach", ""),
            timestamp=d.get("timestamp", 0.0),
            bug_keywords=d.get("bug_keywords", []),
            verification_count=d.get("verification_count", 0),
            helpful_count=d.get("helpful_count", 0),
            last_verified=d.get("last_verified", 0.0),
            times_used=d.get("times_used", 0),
            source_bug_hash=d.get("source_bug_hash", ""),
        )


    @staticmethod
    def is_high_quality(reflection: 'Reflection') -> bool:
        """
        Lightweight quality filter — discard generic or useless reflections.
        Called before storing a newly generated reflection.
        """
        if not reflection.lesson_learned or len(reflection.lesson_learned.strip()) < 20:
            return False  # too terse to be useful

        generic_phrases = [
            "be careful", "pay attention", "try harder", "read the code",
            "double check", "always remember", "never forget", "make sure",
            "be sure to", "don't forget",
        ]
        lesson_lower = reflection.lesson_learned.lower()
        generic_hit_count = sum(1 for p in generic_phrases if p in lesson_lower)

        # More than 2 generic phrases → likely filler text
        if generic_hit_count >= 2:
            return False

        # Bug pattern is too generic
        if len(reflection.bug_pattern.strip()) < 3:
            return False

        # No actual failed strategies listed
        if not reflection.failed_strategies or all(len(s) < 5 for s in reflection.failed_strategies):
            return False

        return True


class ReflectionMemory:
    """
    Persistent memory for storing and retrieving past reflections.

    Retrieval strategy: hybrid scoring combining:
      - Semantic similarity via embedding model (primary)
      - Keyword overlap as a lightweight supplement
      - Usefulness score as a trust/relevance multiplier

    Fallback: keyword-only if embedding model unavailable.
    """

    _embedding_model = None
    _embedding_device = None

    def __init__(self, memory_path: str = "./runs/reflection_memory.json",
                 embedding_model_name: str = "all-MiniLM-L6-v2"):
        self.memory_path = Path(memory_path)
        self.embedding_model_name = embedding_model_name
        self.reflections: List[Reflection] = []
        self._cache_embeddings: Dict[int, List[float]] = {}  # reflection_index -> embedding
        self._embedding_built = False
        self._load()

    @classmethod
    def _get_embedding_model(cls, model_name: str = "all-MiniLM-L6-v2"):
        """Lazy-load the sentence-transformer embedding model (loaded once globally)."""
        if cls._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                cls._embedding_model = SentenceTransformer(model_name)
                logger.info(f"Embedding model '{model_name}' loaded.")
            except ImportError:
                logger.warning(
                    "sentence-transformers not installed. "
                    "Semantic retrieval disabled. Run: pip install sentence-transformers"
                )
                cls._embedding_model = False  # sentinel — mark as unavailable
        return cls._embedding_model

    def _build_embeddings(self):
        """Encode all stored reflections into vectors for fast retrieval."""
        if self._embedding_built or not self.reflections:
            return

        model = self._get_embedding_model(self.embedding_model_name)
        if not model:
            return

        texts = [
            f"{r.bug_pattern} {r.lesson_learned} {r.root_cause} "
            f"{' '.join(r.failed_strategies)}"
            for r in self.reflections
        ]
        vecs = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
        self._cache_embeddings = {i: vecs[i].tolist() for i in range(len(self.reflections))}
        self._embedding_built = True
        logger.info(f"Built embeddings for {len(self.reflections)} reflections.")

    def _invalidate_cache(self):
        """Must be called whenever reflections list changes."""
        self._cache_embeddings.clear()
        self._embedding_built = False

    def _load(self):
        if self.memory_path.exists():
            try:
                with open(self.memory_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.reflections = [Reflection.from_dict(d) for d in data]
                logger.info(f"Loaded {len(self.reflections)} reflections from memory")
                self._invalidate_cache()
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load reflection memory: {e}")
                self.reflections = []

    def save(self):
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.memory_path, 'w', encoding='utf-8') as f:
            json.dump([r.to_dict() for r in self.reflections], f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(self.reflections)} reflections to {self.memory_path}")

    def add(self, reflection: Reflection):
        # Deduplicate by hash
        if reflection.buggy_code_hash in {r.buggy_code_hash for r in self.reflections}:
            logger.info(f"Reflection for bug {reflection.buggy_code_hash} already exists, skipping.")
            return

        # Quality gate: skip low-quality reflections before storing
        if not Reflection.is_high_quality(reflection):
            logger.info(
                f"Reflection for '{reflection.bug_pattern}' failed quality gate, not stored."
            )
            return

        self.reflections.append(reflection)
        self._invalidate_cache()  # embeddings changed
        self.save()

    def retrieve(self, bug_description: str, buggy_code: str,
                 top_k: int = 2) -> List[Reflection]:
        """
        Hybrid retrieval:
          1. Semantic similarity (embedding) — primary signal
          2. Keyword overlap — supplement for exact-code matches
          3. Usefulness score — trust multiplier

        Combines all three into a single fused score.
        """
        if not self.reflections:
            return []

        self._build_embeddings()

        query = bug_description + "\n" + buggy_code
        query_tokens = set(self._extract_keywords(query))

        # Fetch all candidates with their scores
        candidates: List[Tuple[float, Reflection]] = []

        model = self._get_embedding_model(self.embedding_model_name)

        for idx, ref in enumerate(self.reflections):
            total_score = 0.0

            # ---- Semantic score (weight: 0.55) ----
            if model and idx in self._cache_embeddings:
                query_vec = model.encode([query], convert_to_numpy=True)[0]
                ref_vec = np.array(self._cache_embeddings[idx])
                semantic_score = float(np.dot(query_vec, ref_vec) /
                                      (np.linalg.norm(query_vec) * np.linalg.norm(ref_vec) + 1e-8))
            else:
                # Fallback to keyword-only scoring
                semantic_score = 0.0

            # ---- Keyword score (weight: 0.25) ----
            ref_tokens = set(ref.bug_keywords)
            if query_tokens and ref_tokens:
                overlap = len(query_tokens & ref_tokens)
                keyword_score = overlap / (len(query_tokens | ref_tokens) + 1e-8)
            else:
                keyword_score = 0.0

            # ---- Usefulness score (weight: 0.20) ----
            usefulness = ref.usefulness_score

            # ---- Fuse ----
            total_score = (
                0.55 * semantic_score +
                0.25 * keyword_score +
                0.20 * usefulness
            )

            if total_score > 0.05:
                candidates.append((total_score, ref))

        # Sort descending
        candidates.sort(key=lambda x: x[0], reverse=True)

        # Mark verification stats for all returned reflections
        for score, ref in candidates[:top_k]:
            ref.verification_count += 1
            ref.last_verified = time.time()

        # Save updated verification counts
        self.save()

        return [ref for _, ref in candidates[:top_k]]

    def record_helpfulness(self, buggy_code_hash: str, was_helpful: bool):
        """Called by the agent after it uses a reflection to update trust scores."""
        for ref in self.reflections:
            if ref.buggy_code_hash == buggy_code_hash:
                ref.helpful_count += 1 if was_helpful else 0
                ref.times_used += 1
                ref.last_verified = time.time()
                self.save()
                return

    @staticmethod
    def _extract_keywords(text: str) -> List[str]:
        tokens = re.findall(r'\b\w+\b', text.lower())
        raw_tokens = [t for t in tokens if len(t) > 2]
        parts = []
        for t in raw_tokens:
            if '_' in t:
                parts.extend(t.split('_'))
            parts.append(t)
        return list(set(parts))

    @property
    def size(self) -> int:
        return len(self.reflections)


# =============================================================================
# LAYER 3: Skill Memory (Hermes-inspired)
# =============================================================================

@dataclass
class Skill:
    """
    A structured, reusable fix SOP (Standard Operating Procedure) generated from
    a successful repair. Analogous to Hermes' "Skill.md" — a persistent, resumable
    unit of learned capability.

    Unlike Reflection (L2) which captures WHAT NOT TO DO, Skill (L3) captures
    a positive, reusable action pattern that accelerated a successful fix.
    """
    skill_id: str                  # stable UUID-like identifier
    name: str                      # human-readable name, e.g. "Fix-OffByOne-Loop"
    trigger_keywords: List[str]    # keywords that should activate this skill
    description: str               # one-paragraph summary
    applicability: str             # when / in what context this skill applies
    steps: List[str]               # ordered fix steps (the "how")
    verification_hint: str          # how to verify the fix worked
    source_bug_hash: str           # which bug this was first extracted from
    language: str = "python"       # programming language
    complexity_score: int = 0     # 0=trivial, 3=complex — controls when skill is extracted
    version: int = 1               # incremented on each patch/update
    created_at: float = 0.0
    last_used_at: float = 0.0
    last_verified_at: float = 0.0  # when was it last confirmed correct
    success_count: int = 0         # times this skill was retrieved and proved correct
    failure_count: int = 0         # times it was retrieved but the fix still failed
    deprecation_score: float = 0.0  # 0=fresh, 1=completely deprecated
    tags: List[str] = field(default_factory=list)   # extra labels

    @property
    def usefulness(self) -> float:
        """Confidence score: how often did using this skill lead to success."""
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.6  # optimistic default for new skills
        return self.success_count / total

    @property
    def is_stale(self) -> bool:
        """Deprecated if failure rate is too high or too much time has passed."""
        if self.deprecation_score >= 0.7:
            return True
        if self.success_count + self.failure_count >= 5:
            if self.usefulness < 0.4:
                return True
        return False

    def to_dict(self) -> Dict:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "trigger_keywords": self.trigger_keywords,
            "description": self.description,
            "applicability": self.applicability,
            "steps": self.steps,
            "verification_hint": self.verification_hint,
            "source_bug_hash": self.source_bug_hash,
            "language": self.language,
            "complexity_score": self.complexity_score,
            "version": self.version,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "last_verified_at": self.last_verified_at,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "deprecation_score": self.deprecation_score,
            "tags": self.tags,
        }

    @staticmethod
    def from_dict(d: Dict) -> 'Skill':
        return Skill(
            skill_id=d.get("skill_id", ""),
            name=d.get("name", ""),
            trigger_keywords=d.get("trigger_keywords", []),
            description=d.get("description", ""),
            applicability=d.get("applicability", ""),
            steps=d.get("steps", []),
            verification_hint=d.get("verification_hint", ""),
            source_bug_hash=d.get("source_bug_hash", ""),
            language=d.get("language", "python"),
            complexity_score=d.get("complexity_score", 0),
            version=d.get("version", 1),
            created_at=d.get("created_at", 0.0),
            last_used_at=d.get("last_used_at", 0.0),
            last_verified_at=d.get("last_verified_at", 0.0),
            success_count=d.get("success_count", 0),
            failure_count=d.get("failure_count", 0),
            deprecation_score=d.get("deprecation_score", 0.0),
            tags=d.get("tags", []),
        )

    def to_markdown(self) -> str:
        """Hermes-style Markdown format for human readability and LLM prompt injection."""
        lines = [
            f"## Skill: {self.name}",
            "",
            f"**Skill ID:** `{self.skill_id}`  |  **Version:** {self.version}  |  **Language:** {self.language}",
            f"**Usefulness:** {self.usefulness:.0%}  ({self.success_count}✅ / {self.failure_count}❌)  |  **Stale:** {self.is_stale}",
            "",
            f"### Trigger Keywords",
            f"{', '.join(self.trigger_keywords)}",
            "",
            f"### Description",
            f"{self.description}",
            "",
            f"### Applicability",
            f"{self.applicability}",
            "",
            f"### Fix Steps",
        ]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"{i}. {step}")
        lines += [
            "",
            f"### Verification",
            f"{self.verification_hint}",
            "",
            f"---",
            f"_Extracted from bug `{self.source_bug_hash}`, v{self.version}_",
        ]
        return "\n".join(lines)

    @staticmethod
    def is_high_quality(skill: 'Skill') -> bool:
        """Quality gate before storing a new skill."""
        if not skill.name or len(skill.name.strip()) < 4:
            return False
        if not skill.steps or len(skill.steps) < 2:
            return False
        if len(skill.description.strip()) < 15:
            return False
        # Don't store trivial skills (complexity 0 means not worth the overhead)
        if skill.complexity_score < 1:
            return False
        return True


class SkillManager:
    """
    L3 Memory — Hermes-style persistent Skill store.

    Lifecycle:
      - GENERATE:  when a complex fix succeeds, the large model extracts a Skill SOP
      - RETRIEVE:  before a new fix attempt, relevant skills are loaded into context
      - VERIFY:    if a loaded skill led to success, bump success_count
                   if it failed, bump failure_count and optionally PATCH it
      - ARCHIVE:   stale skills are soft-deleted (deprecation_score=1.0)

    Key Hermes innovations:
      - Patching instead of full rewrite (token-efficient self-evolution)
      - Complexity gating (only non-trivial fixes become skills)
      - Staleness detection (deprecated skills auto降权)
    """

    _embedding_model = None
    _embedding_device = None

    def __init__(self, skills_dir: str = "./runs/skills",
                 embedding_model_name: str = "all-MiniLM-L6-v2"):
        self.skills_dir = Path(skills_dir)
        self.embedding_model_name = embedding_model_name
        self.skills: List[Skill] = []
        self._cache_embeddings: Dict[int, List[float]] = {}
        self._embedding_built = False
        self._skills_file = self.skills_dir / "skills.json"
        self._load()

    @classmethod
    def _get_embedding_model(cls, model_name: str = "all-MiniLM-L6-v2"):
        if cls._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                cls._embedding_model = SentenceTransformer(model_name)
                logger.info(f"[SkillManager] Embedding model '{model_name}' loaded.")
            except ImportError:
                logger.warning(
                    "[SkillManager] sentence-transformers not installed. "
                    "Semantic retrieval disabled. Run: pip install sentence-transformers"
                )
                cls._embedding_model = False
        return cls._embedding_model

    def _build_embeddings(self):
        if self._embedding_built or not self.skills:
            return
        model = self._get_embedding_model(self.embedding_model_name)
        if not model:
            return

        texts = [
            f"{s.name} {s.description} {' '.join(s.steps)}"
            for s in self.skills
        ]
        vecs = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
        self._cache_embeddings = {i: vecs[i].tolist() for i in range(len(self.skills))}
        self._embedding_built = True
        logger.info(f"[SkillManager] Built embeddings for {len(self.skills)} skills.")

    def _invalidate_cache(self):
        self._cache_embeddings.clear()
        self._embedding_built = False

    def _load(self):
        if self._skills_file.exists():
            try:
                with open(self._skills_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.skills = [Skill.from_dict(d) for d in data]
                logger.info(f"[SkillManager] Loaded {len(self.skills)} skills from disk.")
                self._invalidate_cache()
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"[SkillManager] Failed to load skills: {e}")
                self.skills = []

    def save(self):
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        with open(self._skills_file, 'w', encoding='utf-8') as f:
            json.dump([s.to_dict() for s in self.skills], f, indent=2, ensure_ascii=False)
        logger.info(f"[SkillManager] Saved {len(self.skills)} skills to {self._skills_file}")

    # ---- GENERATE: extract a Skill from a successful fix ----

    def generate_skill_from_fix(
        self,
        bug_description: str,
        buggy_code: str,
        fixed_code: str,
        successful_steps: List[CoTStep],
        source_bug_hash: str,
        large_model_fn,
        language: str = "python",
        complexity_score: int = 1,
    ) -> Optional[Skill]:
        """
        Ask the large model to extract a reusable Skill SOP from a successful fix.
        Called only when the fix is non-trivial (complexity_score >= 1).

        Returns None if the model declines (not generalizable) or quality gate fails.
        """
        steps_text = "\n".join(
            f"Step {s.step_id} ({s.step_name}): {s.content}"
            for s in successful_steps
        )

        prompt = f"""You are a senior code debugging engineer. A bug was successfully fixed.
Extract a REUSABLE fix SOP (Standard Operating Procedure) from this success.

**Bug Description:** {bug_description}

**Buggy Code:**
```python
{buggy_code}
```

**Fixed Code:**
```python
{fixed_code}
```

**Successful Fix Reasoning:**
{steps_text}

Based on this fix, generate a structured Skill that could be applied to similar bugs.
Respond in JSON:

```json
{{
  "name": "Fix-[BugType]-[BriefContext]",
  "trigger_keywords": ["keyword1", "keyword2", "keyword3"],
  "description": "One paragraph describing what this skill fixes and why it works.",
  "applicability": "When should this skill be used? Be specific about the bug pattern.",
  "steps": ["Step 1: ...", "Step 2: ...", "Step 3: ..."],
  "verification_hint": "How to verify the fix is correct?",
  "tags": ["tag1", "tag2"],
  "complexity_score": 1-3 (how complex was this fix? 1=straightforward, 3=highly complex)
}}
```

Only output JSON. If this fix is too specific (not generalizable), output: {{"decline": true}}.
"""

        try:
            raw = large_model_fn(
                prompt,
                "You are a senior code debugging engineer. Output JSON only."
            )
            json_match = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
            text = json_match.group(1) if json_match else raw.strip()
            data = json.loads(text)

            if data.get("decline"):
                logger.info("[SkillManager] Large model declined to generate skill (too specific).")
                return None

            skill = Skill(
                skill_id=self._generate_id(),
                name=data.get("name", "UnnamedSkill"),
                trigger_keywords=data.get("trigger_keywords", []),
                description=data.get("description", ""),
                applicability=data.get("applicability", ""),
                steps=data.get("steps", []),
                verification_hint=data.get("verification_hint", ""),
                source_bug_hash=source_bug_hash,
                language=language,
                complexity_score=data.get("complexity_score", complexity_score),
                created_at=time.time(),
                tags=data.get("tags", []),
            )

            if not Skill.is_high_quality(skill):
                logger.info(f"[SkillManager] Skill '{skill.name}' failed quality gate, not stored.")
                return None

            self.skills.append(skill)
            self._invalidate_cache()
            self.save()
            logger.info(f"[SkillManager] Skill '{skill.name}' (v{skill.version}) stored.")
            return skill

        except Exception as e:
            logger.warning(f"[SkillManager] Failed to generate skill: {e}")
            return None

    # ---- RETRIEVE: find relevant skills for a new bug ----

    def retrieve(self, bug_description: str, buggy_code: str,
                 top_k: int = 3) -> List[Skill]:
        """
        Hybrid retrieval: semantic similarity + keyword overlap + usefulness score.
        Stale (deprecated) skills are excluded.
        """
        if not self.skills:
            return []

        self._build_embeddings()

        query = bug_description + "\n" + buggy_code
        query_tokens = set(ReflectionMemory._extract_keywords(query))
        model = self._get_embedding_model(self.embedding_model_name)

        candidates: List[Tuple[float, Skill]] = []

        for idx, skill in enumerate(self.skills):
            # Skip stale skills
            if skill.is_stale:
                continue

            total_score = 0.0

            # Semantic score
            if model and idx in self._cache_embeddings:
                query_vec = model.encode([query], convert_to_numpy=True)[0]
                ref_vec = np.array(self._cache_embeddings[idx])
                semantic = float(np.dot(query_vec, ref_vec) /
                               (np.linalg.norm(query_vec) * np.linalg.norm(ref_vec) + 1e-8))
            else:
                semantic = 0.0

            # Keyword score
            skill_tokens = set(skill.trigger_keywords + skill.tags)
            if query_tokens and skill_tokens:
                overlap = len(query_tokens & skill_tokens)
                keyword = overlap / (len(query_tokens | skill_tokens) + 1e-8)
            else:
                keyword = 0.0

            # Usefulness score
            usefulness = skill.usefulness

            total_score = 0.55 * semantic + 0.25 * keyword + 0.20 * usefulness

            if total_score > 0.05:
                candidates.append((total_score, skill))

        candidates.sort(key=lambda x: x[0], reverse=True)
        retrieved = [s for _, s in candidates[:top_k]]

        for s in retrieved:
            s.last_used_at = time.time()

        self.save()
        return retrieved

    # ---- VERIFY: update skill stats after it's been used in a fix attempt ----

    def record_outcome(self, skill_id: str, success: bool):
        """Update skill statistics after it was used in a fix attempt."""
        for skill in self.skills:
            if skill.skill_id == skill_id:
                if success:
                    skill.success_count += 1
                    skill.last_verified_at = time.time()
                    skill.deprecation_score = max(0.0, skill.deprecation_score - 0.1)
                else:
                    skill.failure_count += 1
                    skill.deprecation_score = min(1.0, skill.deprecation_score + 0.2)
                    if skill.deprecation_score >= 0.7:
                        logger.warning(
                            f"[SkillManager] Skill '{skill.name}' is now deprecated "
                            f"(failure_rate={skill.failure_count/(skill.success_count+skill.failure_count):.0%})"
                        )
                self.save()
                return

    # ---- PATCH: incrementally update a failed skill (Hermes-style) ----

    def patch_skill(self, skill_id: str, failed_reason: str,
                    large_model_fn) -> Optional[Skill]:
        """
        Instead of rewriting the entire skill, generate a targeted PATCH.
        This preserves the core logic while fixing the failing part.
        Hermes-style token-efficient self-evolution.
        """
        for skill in self.skills:
            if skill.skill_id == skill_id:
                break
        else:
            return None

        prompt = f"""A Skill (SOP) was used to fix a bug but it FAILED.
Instead of rewriting the whole skill, generate a TARGETED PATCH.

**Existing Skill:**
Name: {skill.name}
Description: {skill.description}
Applicability: {skill.applicability}
Steps:
{chr(10).join(f'{i+1}. {s}' for i, s in enumerate(skill.steps))}

**Why it failed:**
{failed_reason}

Respond in JSON with fields to UPDATE (only include fields that need changing):
```json
{{
  "description": "updated description (if changed)",
  "applicability": "updated applicability (if changed)",
  "steps": ["updated steps if needed"],
  "add_step_note": "any additional guidance to prevent this failure"
}}
```

Or output {{"deprecate": true}} if the skill should be abandoned entirely.
"""

        try:
            raw = large_model_fn(prompt, "You are a senior code engineer. Output JSON only.")
            json_match = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
            text = json_match.group(1) if json_match else raw.strip()
            data = json.loads(text)

            if data.get("deprecate"):
                skill.deprecation_score = 1.0
                self.save()
                logger.info(f"[SkillManager] Skill '{skill.name}' deprecated by patcher.")
                return None

            if "description" in data:
                skill.description = data["description"]
            if "applicability" in data:
                skill.applicability = data["applicability"]
            if "steps" in data:
                skill.steps = data["steps"]

            skill.version += 1
            skill.last_verified_at = time.time()
            self._invalidate_cache()
            self.save()
            logger.info(
                f"[SkillManager] Skill '{skill.name}' patched to v{skill.version}."
            )
            return skill

        except Exception as e:
            logger.warning(f"[SkillManager] Failed to patch skill: {e}")
            return None

    # ---- Helpers ----

    def _generate_id(self) -> str:
        return hashlib.md5(str(time.time()).encode()).hexdigest()[:12]

    def export_markdown(self, output_dir: str = "./runs/skills/markdown"):
        """Export all active skills as human-readable Markdown files."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        for skill in self.skills:
            if skill.deprecation_score < 1.0:
                path = out / f"{skill.skill_id}_{skill.name.replace(' ', '_')}.md"
                path.write_text(skill.to_markdown(), encoding='utf-8')
        logger.info(f"[SkillManager] Exported {len(list(out.glob('*.md')))} skills to Markdown.")

    @property
    def active_count(self) -> int:
        return sum(1 for s in self.skills if not s.is_stale)

    @property
    def archived_count(self) -> int:
        return sum(1 for s in self.skills if s.is_stale)


# =============================================================================
# L2 Memory: Reflection (enhanced with verification ring — already defined above)
# The ReflectionMemory class is defined before this block.
# =============================================================================


# ==================== Data Structures ====================

@dataclass
class CoTStep:
    step_id: int
    step_name: str
    content: str


@dataclass
class ExecutionResult:
    """Result of compiling and running the generated code"""
    success: bool
    error_type: str = ""           # "syntax", "runtime", "test_failure", "timeout"
    error_message: str = ""
    failed_test: Optional[str] = None
    traceback_str: str = ""
    passed_tests: int = 0
    total_tests: int = 0


@dataclass
class LargeModelFeedback:
    """Feedback from the large model after analyzing a failure"""
    status: str = "UNKNOWN"         # BUG_DETECTED / OPTIMIZATION_ONLY / CORRECT
    first_error_step: int = 0
    error_in_reasoning: str = ""
    correct_reasoning: str = ""
    hint: str = ""
    error_type: str = ""
    optimization_note: str = ""
    raw_response: str = ""
    analysis_time_ms: float = 0.0


@dataclass
class IterationRecord:
    """Record of a single ReAct iteration for training data collection"""
    iteration: int
    cot_steps: List[CoTStep]
    fixed_code: str
    execution_result: ExecutionResult
    large_model_feedback: Optional[LargeModelFeedback] = None
    used_large_model: bool = False
    time_ms: float = 0.0


@dataclass
class AgentResult:
    """Final result of the agent's repair attempt"""
    success: bool
    fixed_code: str = ""
    total_iterations: int = 0
    large_model_calls: int = 0
    total_time_ms: float = 0.0
    iterations: List[IterationRecord] = field(default_factory=list)
    reflection: Optional[Reflection] = None
    used_past_reflections: int = 0
    used_skills: List[str] = field(default_factory=list)   # skill IDs used in this fix
    skill_extracted: Optional[str] = None   # skill ID extracted from this fix (if any)

    @property
    def compute_savings(self) -> float:
        if self.total_iterations == 0:
            return 0.0
        return 1.0 - (self.large_model_calls / self.total_iterations)


# ==================== CoT Parser ====================

class CoTParser:

    @staticmethod
    def parse_steps(raw_output: str) -> List[CoTStep]:
        think_match = re.search(r'<think>(.*?)</think>', raw_output, re.DOTALL)
        if not think_match:
            return []

        content = think_match.group(1)
        step_defs = [
            (1, "Bug Identification", r'\[Step 1:.*?\]\s*(.*?)(?=\[Step 2:|$)'),
            (2, "Root Cause Analysis", r'\[Step 2:.*?\]\s*(.*?)(?=\[Step 3:|$)'),
            (3, "Fix Strategy",       r'\[Step 3:.*?\]\s*(.*?)(?=\[Step 4:|$)'),
            (4, "Edge Case Check",    r'\[Step 4:.*?\]\s*(.*?)(?=</think>|$)'),
        ]
        steps = []
        for sid, name, pattern in step_defs:
            match = re.search(pattern, content, re.DOTALL)
            steps.append(CoTStep(
                step_id=sid,
                step_name=name,
                content=match.group(1).strip() if match else ""
            ))
        return steps

    @staticmethod
    def extract_code(raw_output: str) -> str:
        def is_code(s: str) -> bool:
            if not s or len(s) < 10:
                return False
            markers = ['def ', 'return ', 'while ', 'if ', 'for ', 'class ', '=']
            return any(m in s for m in markers)

        patterns = [
            # 1. Standard: <fixed_code> ... ```python ... ``` ... </fixed_code>
            r'<fixed_code>\s*```python\s*(.*?)\s*```\s*</fixed_code>',
            # 2. Missing </fixed_code> — stopped by markdown heading
            r'<fixed_code>\s*```python\s*(.*?)\s*```\s*(?=###|## |<fixed_code>)',
            # 3. No </fixed_code> at all — grab up to next major section
            r'<fixed_code>\s*```python\s*(.*?)\s*```',
            # 4. <fixed_code> without ```python wrapper
            r'<fixed_code>\s*(.*?)\s*</fixed_code>',
            # 5. <fixed_code> with no closing tag — stop at any heading
            r'<fixed_code>\s*(.*?)(?=###|## |</fixed_code>)',
        ]
        for pattern in patterns:
            match = re.search(pattern, raw_output, re.DOTALL)
            if match and is_code(match.group(1).strip()):
                return match.group(1).strip()

        # Fallback: extract the LAST ```python block in the entire response
        all_code_blocks = re.findall(r'```python\s*(.*?)\s*```', raw_output, re.DOTALL)
        for block in reversed(all_code_blocks):
            if is_code(block.strip()):
                return block.strip()

        # Last resort: look for any def ... ( code pattern
        def_blocks = re.findall(r'(def \w+.*?(?=\n(?:def |class |$))', raw_output, re.DOTALL)
        if def_blocks:
            return '\n'.join(def_blocks[:3]).strip()

        return ""

    @staticmethod
    def normalize_function_name(code: str, buggy_code: str) -> str:
        """
        Fix function names in extracted code.
        If model generates 'def fixed_function' but buggy code has 'def bitcount',
        rename the extracted function to match the expected name.
        """
        # Find original function name(s) from buggy code
        orig_funcs = re.findall(r'def (\w+)\s*\(', buggy_code)
        # Find generated function name(s) in fixed code
        gen_funcs = re.findall(r'def (\w+)\s*\(', code)

        if not orig_funcs or not gen_funcs:
            return code

        # Find the first function in buggy code (the main one to fix)
        main_orig = orig_funcs[0]
        main_gen = gen_funcs[0]

        # If the generated name is a variant of "fixed_xxx" or completely different, rename it
        rename_needed = (
            main_gen.startswith('fixed_') or
            main_gen.startswith('corrected_') or
            main_gen.startswith('correct_') or
            main_gen == 'solution' or
            main_gen == 'answer' or
            (main_gen != main_orig and
             main_gen not in buggy_code and
             not any(f in buggy_code for f in [main_gen]))
        )

        if rename_needed:
            code = code.replace(f'def {main_gen}(', f'def {main_orig}(', 1)
            # Also rename all subsequent occurrences in test code
            code = code.replace(f'def {main_gen}(', f'def {main_orig}(')

        return code

    @staticmethod
    def format_steps_as_text(steps: List[CoTStep]) -> str:
        return "\n".join(
            f"[Step {s.step_id}: {s.step_name}]\n{s.content}"
            for s in steps
        )


# ==================== Code Executor (Ground Truth) ====================

class CodeExecutor:
    """
    Execute generated code against test cases.
    This is the GROUND TRUTH signal -- no hallucination, no approximation.
    """

    def __init__(self, timeout_seconds: int = 5):
        self.timeout = timeout_seconds

    def execute(self, code: str, test_cases: List[Dict] = None) -> ExecutionResult:
        """
        Compile + run code + test cases.
        Returns precise error information for large model analysis.
        Uses a background thread with join(timeout) to enforce timeout.
        """
        import threading

        result_holder = [None]  # [ExecutionResult]

        def run():
            result_holder[0] = self._execute_inner(code, test_cases)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=self.timeout)

        if t.is_alive():
            # Timed out - treat as runtime error
            return ExecutionResult(
                success=False,
                error_type="timeout",
                error_message=f"Execution timed out after {self.timeout}s (possible infinite loop or recursion)",
            )
        return result_holder[0]

    def _execute_inner(self, code: str, test_cases: List[Dict] = None) -> ExecutionResult:
        """Actual execution logic (runs in background thread)."""
        # Phase 1: Syntax check (compile)
        try:
            compile(code, '<generated>', 'exec')
        except SyntaxError as e:
            return ExecutionResult(
                success=False,
                error_type="syntax",
                error_message=f"SyntaxError at line {e.lineno}: {e.msg}",
                traceback_str=traceback.format_exc()
            )

        # Phase 2: Basic execution (catches NameError, TypeError, etc.)
        namespace = {}
        try:
            exec(code, namespace)
        except RecursionError as e:
            return ExecutionResult(
                success=False,
                error_type="runtime",
                error_message=f"RecursionError: {str(e)} (possible infinite recursion)",
                traceback_str=traceback.format_exc()
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                error_type="runtime",
                error_message=f"{type(e).__name__}: {str(e)}",
                traceback_str=traceback.format_exc()
            )

        # Phase 3: Test cases (if provided)
        if not test_cases:
            return ExecutionResult(success=True)

        passed = 0
        total = len(test_cases)

        for tc in test_cases:
            func_name = tc.get("function", "")
            args = tc.get("input", [])
            expected = tc.get("output")

            if func_name not in namespace:
                return ExecutionResult(
                    success=False,
                    error_type="runtime",
                    error_message=f"Function '{func_name}' not defined in generated code",
                    passed_tests=passed,
                    total_tests=total
                )

            try:
                old_stdout = sys.stdout
                sys.stdout = StringIO()
                try:
                    import types as _types
                    if isinstance(args, list):
                        if len(args) == 0:
                            # Empty list: pass it as a single argument, not unpacked
                            result = namespace[func_name](args)
                        else:
                            result = namespace[func_name](*args)
                    elif isinstance(args, dict):
                        result = namespace[func_name](**args)
                    else:
                        result = namespace[func_name](args)
                    if isinstance(result, _types.GeneratorType):
                        result = list(result)
                finally:
                    sys.stdout = old_stdout

                if result == expected:
                    passed += 1
                else:
                    return ExecutionResult(
                        success=False,
                        error_type="test_failure",
                        error_message=(
                            f"Test failed for {func_name}({args}): "
                            f"expected {expected}, got {result}"
                        ),
                        failed_test=json.dumps(tc),
                        passed_tests=passed,
                        total_tests=total
                    )

            except RecursionError as e:
                return ExecutionResult(
                    success=False,
                    error_type="runtime",
                    error_message=f"RecursionError: {str(e)} (infinite recursion in {func_name})",
                    traceback_str=traceback.format_exc(),
                    passed_tests=passed,
                    total_tests=total
                )
            except Exception as e:
                return ExecutionResult(
                    success=False,
                    error_type="runtime",
                    error_message=(
                        f"Runtime error in {func_name}({args}): "
                        f"{type(e).__name__}: {str(e)}"
                    ),
                    traceback_str=traceback.format_exc(),
                    passed_tests=passed,
                    total_tests=total
                )

        return ExecutionResult(
            success=True,
            passed_tests=passed,
            total_tests=total
        )


# ==================== Session Persistence (Claude-style Append-Only) ====================

class SessionStore:
    """
    Claude-style append-only session persistence.
    
    设计原则:
    - Append-only: 每次交互只追加，不修改已有记录
    - 按会话ID隔离，每个会话独立存储
    - 支持 resume / fork / rewind 操作
    - Session transcript 存储为 JSONL 格式
    
    存储结构:
        sessions/
        ├── <session_id>.jsonl      # 主会话记录 (append-only)
        ├── <session_id>.meta.json # 会话元数据
        └── <session_id>.state.json # 断点恢复状态 (overwritable)
    """

    def __init__(self, sessions_dir: str = "./runs/sessions"):
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def _meta_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.meta.json"

    def _state_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.state.json"

    def create_session(self, session_id: str, metadata: dict = None) -> str:
        """创建新会话，写入元数据。"""
        meta = {
            "session_id": session_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "metadata": metadata or {},
        }
        with open(self._meta_path(session_id), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        # 初始化空 session 文件
        self._session_path(session_id).touch()
        return session_id

    def append_event(self, session_id: str, event: dict) -> None:
        """
        追加一条事件到会话记录。
        事件格式: {type, timestamp, data, ...}
        """
        event = {
            "timestamp": datetime.now().isoformat(),
            **event,
        }
        with open(self._session_path(session_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        # 更新 meta
        self._update_meta(session_id, updated_at=datetime.now().isoformat())

    def append_user_message(self, session_id: str, content: str, metadata: dict = None) -> None:
        self.append_event(session_id, {
            "type": "user_message",
            "content": content,
            "metadata": metadata or {},
        })

    def append_model_output(self, session_id: str, content: str,
                            tool_uses: list = None,
                            metadata: dict = None) -> None:
        self.append_event(session_id, {
            "type": "model_output",
            "content": content,
            "tool_uses": tool_uses or [],
            "metadata": metadata or {},
        })

    def append_tool_result(self, session_id: str, tool_name: str,
                           result: dict, success: bool,
                           metadata: dict = None) -> None:
        self.append_event(session_id, {
            "type": "tool_result",
            "tool_name": tool_name,
            "result": result,
            "success": success,
            "metadata": metadata or {},
        })

    def append_compact_boundary(self, session_id: str, summary: str,
                                tokens_freed: int,
                                metadata: dict = None) -> None:
        self.append_event(session_id, {
            "type": "compact_boundary",
            "summary": summary,
            "tokens_freed": tokens_freed,
            "metadata": metadata or {},
        })

    def append_agent_result(self, session_id: str, agent_result: AgentResult) -> None:
        # 序列化 AgentResult (排除不可JSON序列化的部分)
        result_dict = {
            "success": agent_result.success,
            "fixed_code": agent_result.fixed_code,
            "total_iterations": agent_result.total_iterations,
            "large_model_calls": agent_result.large_model_calls,
            "total_time_ms": agent_result.total_time_ms,
            "used_past_reflections": agent_result.used_past_reflections,
            "used_skills": agent_result.used_skills,
            "skill_extracted": agent_result.skill_extracted,
            "reflection": None,
        }
        if agent_result.reflection:
            r = agent_result.reflection
            result_dict["reflection"] = {
                "bug_pattern": r.bug_pattern,
                "lesson_learned": r.lesson_learned,
                "what_not_to_do": r.what_not_to_do,
                "root_cause": r.root_cause,
            }
        self.append_event(session_id, {
            "type": "agent_result",
            "result": result_dict,
        })

    def save_checkpoint(self, session_id: str, state: dict) -> None:
        """保存断点状态 (可覆盖，用于 resume/fork)。"""
        state["checkpoint_at"] = datetime.now().isoformat()
        with open(self._state_path(session_id), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def load_checkpoint(self, session_id: str) -> Optional[dict]:
        """加载断点状态，用于恢复会话。"""
        p = self._state_path(session_id)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return None

    def load_session(self, session_id: str) -> List[dict]:
        """加载完整会话记录。"""
        events = []
        p = self._session_path(session_id)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        return events

    def fork_session(self, parent_id: str, fork_id: str) -> str:
        """Fork 一个新会话，继承父会话的历史。"""
        self.create_session(fork_id, metadata={"forked_from": parent_id})
        # 追加父会话所有事件（作为历史）
        for event in self.load_session(parent_id):
            self.append_event(fork_id, event)
        return fork_id

    def list_sessions(self) -> List[dict]:
        """列出所有会话。"""
        sessions = []
        for p in self.sessions_dir.glob("*.meta.json"):
            with open(p, encoding="utf-8") as f:
                sessions.append(json.load(f))
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    def _update_meta(self, session_id: str, **kwargs) -> None:
        p = self._meta_path(session_id)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                meta = json.load(f)
        else:
            meta = {"session_id": session_id}
        meta.update(kwargs)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)


# ==================== Compaction Pipeline (Claude-style 5-Layer Context Management) ====================

class CompactionPipeline:
    """
    Claude-style 5-layer context compaction pipeline.

    设计原则:
    - 5层按序执行，前面的层更轻量、代价更低
    - 只有当前面所有层都不足以缓解压力时，才触发后面的层
    - 每层只做一件事，避免职责混乱
    - 所有压缩记录都写入 session，方便后续回溯和调试

    Layer1: Budget Reduction — 单个工具输出截断
    Layer2: Snip            — 历史片段级别裁剪
    Layer3: Microcompact     — 缓存感知微压缩
    Layer4: Context Collapse — 时间投影（用summary替换历史）
    Layer5: Auto-compact     — 最后手段：让模型生成语义摘要

    压缩触发阈值:
    - BUDGET_LIMIT: 单个工具输出最大字符数 (默认 8000)
    - HISTORY_SNIP_THRESHOLD: 历史超过多少字符时触发snip (默认 30000)
    - MICROCOMPACT_THRESHOLD: 触发微压缩的token估计 (默认 40000)
    - CONTEXT_COLLAPSE_THRESHOLD: 触发上下文折叠的token估计 (默认 60000)
    - AUTO_COMPACT_THRESHOLD: 触发auto-compact的token估计 (默认 80000)
    """

    def __init__(
        self,
        session_store: SessionStore = None,
        max_tool_result_chars: int = 8000,
        history_snip_threshold: int = 30000,
        microcompact_threshold: int = 40000,
        context_collapse_threshold: int = 60000,
        auto_compact_threshold: int = 80000,
        model_for_summary=None,  # 可选：用于生成摘要的模型
    ):
        self.session_store = session_store
        self.max_tool_result_chars = max_tool_result_chars
        self.history_snip_threshold = history_snip_threshold
        self.microcompact_threshold = microcompact_threshold
        self.context_collapse_threshold = context_collapse_threshold
        self.auto_compact_threshold = auto_compact_threshold
        self.model_for_summary = model_for_summary

        # 压缩统计
        self._stats = {
            "budget_reductions": 0,
            "snip_count": 0,
            "microcompact_count": 0,
            "context_collapse_count": 0,
            "auto_compact_count": 0,
            "total_tokens_freed": 0,
        }

    def compact(self, context_messages: list, session_id: str = None,
                iteration: int = 0) -> tuple[list, int]:
        """
        执行5层压缩管道。

        Args:
            context_messages: 当前消息列表 (list of dict with keys: role, content, type, ...)
            session_id: 会话ID，用于记录压缩事件
            iteration: 当前迭代轮次

        Returns:
            (compacted_messages, total_tokens_freed)
        """
        messages = list(context_messages)  # 不修改原列表
        total_freed = 0
        session_id = session_id or "unknown"

        # Layer 1: Budget Reduction
        messages, freed = self._layer1_budget_reduction(messages)
        total_freed += freed
        if freed > 0:
            self._stats["budget_reductions"] += 1
            self._log_compact_event(session_id, "budget_reduction", freed, messages, iteration)

        # Layer 2: Snip
        messages, freed = self._layer2_snip(messages)
        total_freed += freed
        if freed > 0:
            self._stats["snip_count"] += 1
            self._log_compact_event(session_id, "snip", freed, messages, iteration)

        # Layer 3: Microcompact
        messages, freed = self._layer3_microcompact(messages)
        total_freed += freed
        if freed > 0:
            self._stats["microcompact_count"] += 1
            self._log_compact_event(session_id, "microcompact", freed, messages, iteration)

        # Layer 4: Context Collapse
        messages, freed = self._layer4_context_collapse(messages)
        total_freed += freed
        if freed > 0:
            self._stats["context_collapse_count"] += 1
            self._log_compact_event(session_id, "context_collapse", freed, messages, iteration)

        # Layer 5: Auto-compact (最后手段)
        messages, freed = self._layer5_auto_compact(messages)
        total_freed += freed
        if freed > 0:
            self._stats["auto_compact_count"] += 1
            self._log_compact_event(session_id, "auto_compact", freed, messages, iteration)

        self._stats["total_tokens_freed"] += total_freed
        return messages, total_freed

    # ---- Layer 1: Budget Reduction ----
    def _layer1_budget_reduction(self, messages: list) -> tuple[list, int]:
        """
        对单个工具输出进行截断。
        如果某个tool result超过max_tool_result_chars，用摘要引用替换。
        """
        before = sum(len(str(m.get("content", ""))) for m in messages)
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if msg.get("role") == "tool" and isinstance(content, str) and len(content) > self.max_tool_result_chars:
                # 截断并添加引用标记
                truncated = content[:self.max_tool_result_chars]
                truncated += f"\n\n[... output truncated. Total length: {len(content)} chars ...]"
                new_msg = dict(msg)
                new_msg["content"] = truncated
                new_msg["_truncated"] = True
                new_msg["_original_length"] = len(content)
                result.append(new_msg)
            else:
                result.append(msg)
        after = sum(len(str(m.get("content", ""))) for m in result)
        return result, before - after

    # ---- Layer 2: Snip ----
    def _layer2_snip(self, messages: list) -> tuple[list, int]:
        """
        如果对话历史超过threshold，裁剪最老的非关键消息。
        保留: system prompt, 第一个user message, 最近N条消息。
        """
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if total_chars <= self.history_snip_threshold:
            return messages, 0

        # 识别关键消息：system, 第一个user, assistant, tool
        # 裁剪策略：保留首尾，压缩中间
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if len(non_system) <= 6:
            return messages, 0  # 对话太短，不裁剪

        # 保留最近4条消息（通常是关键上下文）
        keep_last = 4
        keep_first_user = 1  # 第一个user message包含bug描述，保留

        first_user_idx = 0
        for i, m in enumerate(non_system):
            if m.get("role") == "user":
                first_user_idx = i
                break

        # 构建裁剪后的消息
        result = system_msgs[:]
        # 第一个user message（bug描述）
        if first_user_idx < len(non_system):
            result.append(non_system[first_user_idx])
        # 中间消息 → 压缩成一个摘要消息
        middle = non_system[first_user_idx + 1:-keep_last] if first_user_idx + 1 < len(non_system) - keep_last else []
        if middle:
            summary_content = self._summarize_messages(middle)
            result.append({
                "role": "system",
                "content": f"[Earlier conversation ({len(middle)} messages) summarized]\n{summary_content}",
                "_is_compacted": True,
                "_original_count": len(middle),
            })
        # 最近keep_last条消息
        result.extend(non_system[-keep_last:])

        before = sum(len(str(m.get("content", ""))) for m in messages)
        after = sum(len(str(m.get("content", ""))) for m in result)
        return result, before - after

    def _summarize_messages(self, messages: list) -> str:
        """
        将一批消息压缩成一行摘要（轻量级，不需要LLM）。
        """
        roles = [m.get("role", "?") for m in messages]
        tool_results = sum(1 for m in messages if m.get("role") == "tool")
        assistant_msgs = sum(1 for m in messages if m.get("role") == "assistant")
        return (
            f"({len(messages)} messages: "
            f"assistant={assistant_msgs}, tool_results={tool_results})"
        )

    # ---- Layer 3: Microcompact ----
    def _layer3_microcompact(self, messages: list) -> tuple[list, int]:
        """
        缓存感知微压缩。
        识别重复模式，用引用替换重复内容。
        当前实现: 合并连续的tool_result消息（如果超过3条）。
        """
        if len(messages) < 8:
            return messages, 0

        before = sum(len(str(m.get("content", ""))) for m in messages)
        result = []
        i = 0
        tool_result_buffer = []

        while i < len(messages):
            msg = messages[i]
            # 收集连续的tool_result
            if msg.get("role") == "tool" and not msg.get("_is_compacted"):
                tool_result_buffer.append(msg)
                i += 1
                continue

            # 吐出buffer
            if tool_result_buffer:
                if len(tool_result_buffer) > 3:
                    # 超过3条连续tool_result，合并成一个摘要
                    summary_lines = []
                    for tr in tool_result_buffer:
                        c = tr.get("content", "")
                        if len(c) > 100:
                            c = c[:100] + "..."
                        tool_name = tr.get("name", "unknown")
                        success = tr.get("success", "?")
                        summary_lines.append(f"[{tool_name}: {'OK' if success else 'FAIL'}] {c}")
                    result.append({
                        "role": "system",
                        "content": f"[{len(tool_result_buffer)} tool results collapsed]\n" + "\n".join(summary_lines),
                        "_is_compacted": True,
                        "_original_count": len(tool_result_buffer),
                    })
                else:
                    result.extend(tool_result_buffer)
                tool_result_buffer = []

            result.append(msg)
            i += 1

        # 处理尾部buffer
        if tool_result_buffer:
            if len(tool_result_buffer) > 3:
                summary_lines = []
                for tr in tool_result_buffer:
                    c = tr.get("content", "")
                    if len(c) > 100:
                        c = c[:100] + "..."
                    tool_name = tr.get("name", "unknown")
                    success = tr.get("success", "?")
                    summary_lines.append(f"[{tool_name}: {'OK' if success else 'FAIL'}] {c}")
                result.append({
                    "role": "system",
                    "content": f"[{len(tool_result_buffer)} tool results collapsed]\n" + "\n".join(summary_lines),
                    "_is_compacted": True,
                    "_original_count": len(tool_result_buffer),
                })
            else:
                result.extend(tool_result_buffer)

        after = sum(len(str(m.get("content", ""))) for m in result)
        return result, before - after

    # ---- Layer 4: Context Collapse ----
    def _layer4_context_collapse(self, messages: list) -> tuple[list, int]:
        """
        上下文折叠：将非首尾的历史替换为摘要消息。
        与Layer2的区别：Layer2只裁剪最老的，Layer4替换中间段为语义摘要。
        当前实现: 简单的角色统计摘要，不依赖LLM。
        """
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if total_chars <= self.context_collapse_threshold:
            return messages, 0

        if len(messages) <= 8:
            return messages, 0

        system_msgs = [m for m in messages if m.get("role") == "system" and not m.get("_is_compacted")]
        keep_first = 2  # 保留前2条消息
        keep_last = 4    # 保留最近4条消息

        # 识别可折叠的中间部分
        if len(messages) <= keep_first + keep_last:
            return messages, 0

        result = messages[:keep_first]
        middle = messages[keep_first:-keep_last] if keep_first < len(messages) - keep_last else []

        if middle:
            # 生成折叠摘要
            collapse_summary = self._generate_collapse_summary(messages[keep_first:-keep_last])
            result.append({
                "role": "system",
                "content": collapse_summary,
                "_is_compacted": True,
                "_is_collapse": True,
                "_original_count": len(middle),
            })
        result.extend(messages[-keep_last:])

        before = sum(len(str(m.get("content", ""))) for m in messages)
        after = sum(len(str(m.get("content", ""))) for m in result)
        return result, before - after

    def _generate_collapse_summary(self, middle_messages: list) -> str:
        """生成折叠区域的摘要（轻量级统计方法）。"""
        assistant_count = sum(1 for m in middle_messages if m.get("role") == "assistant")
        tool_count = sum(1 for m in middle_messages if m.get("role") == "tool")
        user_count = sum(1 for m in middle_messages if m.get("role") == "user")

        # 提取关键主题词（从assistant消息中取前50字符）
        key_snippets = []
        for m in middle_messages:
            if m.get("role") == "assistant" and not m.get("_is_compacted"):
                c = str(m.get("content", ""))[:80]
                if c:
                    key_snippets.append(c[:60] + "...")

        summary = f"[Context collapsed: {len(middle_messages)} messages — {assistant_count}assistant/{tool_count}tool/{user_count}user]"
        if key_snippets:
            summary += "\n\nKey exchanges:\n" + "\n".join(f"- {s}" for s in key_snippets[:3])
        return summary

    # ---- Layer 5: Auto-compact (最后手段) ----
    def _layer5_auto_compact(self, messages: list) -> tuple[list, int]:
        """
        最后手段：使用LLM生成语义摘要。
        需要model_for_summary。如果未配置，跳过。
        当前实现: 简单的按比例压缩。
        """
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if total_chars <= self.auto_compact_threshold:
            return messages, 0

        if self.model_for_summary is None:
            # 没有LLM，降级为按比例裁剪
            return self._fallback_ratio_compact(messages, ratio=0.5)

        # 有LLM时的语义摘要（未来扩展）
        # 当前先用降级方案
        return self._fallback_ratio_compact(messages, ratio=0.5)

    def _fallback_ratio_compact(self, messages: list, ratio: float = 0.5) -> tuple[list, int]:
        """按比例压缩：保留ratio比例的内容。"""
        if ratio >= 1.0 or ratio <= 0:
            return messages, 0

        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if not non_system:
            return messages, 0

        keep_count = max(2, int(len(non_system) * ratio))
        result = system_msgs + non_system[-keep_count:]

        before = sum(len(str(m.get("content", ""))) for m in messages)
        after = sum(len(str(m.get("content", ""))) for m in result)
        return result, before - after

    # ---- Utility ----
    def _log_compact_event(self, session_id: str, layer: str,
                          chars_freed: int, messages: list, iteration: int) -> None:
        """记录压缩事件到session。"""
        if self.session_store:
            summary = f"[Compaction] Layer={layer}, freed={chars_freed}chars, remaining_msgs={len(messages)}"
            self.session_store.append_compact_boundary(
                session_id,
                summary=summary,
                tokens_freed=chars_freed,
                metadata={"layer": layer, "iteration": iteration}
            )

    def get_stats(self) -> dict:
        """返回压缩统计。"""
        return dict(self._stats)

    def reset_stats(self) -> None:
        """重置统计。"""
        for k in self._stats:
            self._stats[k] = 0


# ==================== I/O Logger (Claude-style Input/Output Tracing) ====================

class IOLogger:
    """
    Claude-style 完整I/O追踪器。

    设计原则:
    - 每次模型调用同时记录输入(prompt)和输出(response)
    - 输入输出成对记录，方便对比分析
    - 所有日志写入 SessionStore
    - 支持生成可读的I/O摘要

    记录的事件类型:
    - model_input: 模型输入(prompt)
    - model_output: 模型输出(response)
    - tool_execute: 工具执行
    - tool_result: 工具结果
    """

    def __init__(self, session_store: SessionStore = None):
        self.session_store = session_store

    def log_model_input(
        self,
        session_id: str,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        metadata: dict = None,
    ) -> str:
        """
        记录模型输入。
        返回 io_pair_id 用于关联后续的 model_output。
        """
        io_pair_id = str(uuid.uuid4())[:12]

        full_input = f"[System Prompt]\n{system_prompt}\n\n[User Prompt]\n{user_prompt}"

        self.session_store.append_event(session_id, {
            "type": "model_input",
            "io_pair_id": io_pair_id,
            "model_name": model_name,
            "system_prompt": system_prompt[:2000] if system_prompt else None,
            "user_prompt": user_prompt[:5000] if user_prompt else None,
            "input_length": len(full_input),
            "metadata": metadata or {},
        })

        logger.info(
            f"[IOLogger] model_input logged | model={model_name} | "
            f"input_len={len(full_input)} | io_pair_id={io_pair_id}"
        )
        return io_pair_id

    def log_model_output(
        self,
        session_id: str,
        io_pair_id: str,
        model_name: str,
        raw_output: str,
        metadata: dict = None,
    ) -> None:
        """记录模型输出，与之前的 model_input 配对。"""
        self.session_store.append_event(session_id, {
            "type": "model_output",
            "io_pair_id": io_pair_id,
            "model_name": model_name,
            "raw_output": raw_output[:10000] if raw_output else None,
            "output_length": len(raw_output) if raw_output else 0,
            "metadata": metadata or {},
        })

        logger.info(
            f"[IOLogger] model_output logged | model={model_name} | "
            f"output_len={len(raw_output) if raw_output else 0} | io_pair_id={io_pair_id}"
        )

    def log_model_call(
        self,
        session_id: str,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        raw_output: str,
        extra_metadata: dict = None,
    ) -> str:
        """
        一次性记录一次完整的模型调用(输入+输出)。
        返回 io_pair_id。
        """
        io_pair_id = str(uuid.uuid4())[:12]

        full_input = f"[System Prompt]\n{system_prompt}\n\n[User Prompt]\n{user_prompt}"

        self.session_store.append_event(session_id, {
            "type": "model_input",
            "io_pair_id": io_pair_id,
            "model_name": model_name,
            "system_prompt": system_prompt[:2000] if system_prompt else None,
            "user_prompt": user_prompt[:5000] if user_prompt else None,
            "input_length": len(full_input),
            "metadata": extra_metadata or {},
        })

        self.session_store.append_event(session_id, {
            "type": "model_output",
            "io_pair_id": io_pair_id,
            "model_name": model_name,
            "raw_output": raw_output[:10000] if raw_output else None,
            "output_length": len(raw_output) if raw_output else 0,
            "metadata": extra_metadata or {},
        })

        return io_pair_id

    def generate_io_summary(self, session_id: str, max_pairs: int = 10) -> str:
        """
        为指定session生成I/O摘要报告。
        用于分析、调试和Meta-Harness风格的harness优化。
        """
        events = self.session_store.load_session(session_id)
        io_pairs = []
        current_pair = {}

        for evt in events:
            if evt.get("type") == "model_input":
                current_pair = {
                    "io_pair_id": evt.get("io_pair_id"),
                    "model_name": evt.get("model_name"),
                    "iteration": evt.get("metadata", {}).get("iteration"),
                    "input_length": evt.get("input_length", 0),
                    "user_prompt_preview": evt.get("user_prompt", "")[:300],
                    "system_prompt_preview": evt.get("system_prompt", "")[:200],
                }
            elif evt.get("type") == "model_output" and current_pair.get("io_pair_id") == evt.get("io_pair_id"):
                current_pair["output_length"] = evt.get("output_length", 0)
                current_pair["output_preview"] = evt.get("raw_output", "")[:300]
                io_pairs.append(current_pair)
                current_pair = {}

        lines = [f"I/O Summary for session {session_id}", "=" * 60]
        lines.append(f"Total I/O pairs: {len(io_pairs)}")
        lines.append("")

        for i, pair in enumerate(io_pairs[:max_pairs]):
            lines.append(
                f"[Pair {i+1}] {pair.get('model_name')} | "
                f"iter={pair.get('iteration')} | "
                f"in={pair.get('input_length')}chars | "
                f"out={pair.get('output_length')}chars"
            )
            lines.append(f"  Prompt: {pair.get('user_prompt_preview', '')[:150]}...")
            lines.append(f"  Output: {pair.get('output_preview', '')[:150]}...")
            lines.append("")

        if len(io_pairs) > max_pairs:
            lines.append(f"... ({len(io_pairs) - max_pairs} more pairs)")

        return "\n".join(lines)


# ==================== Permission Gate (Claude-style Deny-First Access Control) ====================

class PermissionGate:
    """
    Claude-style Deny-First 权限控制层。

    设计原则:
    - 默认拒绝：未经明确允许的操作一律禁止
    - 白名单制：每个操作类别需要显式授权
    - 可配置：运行时可动态调整权限策略
    - 可审核：所有操作决策记录到session

    操作类型:
      - code_execution: 允许执行用户提交的代码
      - file_write: 允许写入文件系统
      - file_read: 允许读取文件系统
      - network_request: 允许发起网络请求
      - subprocess: 允许创建子进程
      - env_modify: 允许修改环境变量
    """

    class PermissionDeniedError(Exception):
        """权限被拒绝时抛出"""
        def __init__(self, operation: str, reason: str, suggested_action: str = ""):
            self.operation = operation
            self.reason = reason
            self.suggested_action = suggested_action
            super().__init__(f"Permission denied for '{operation}': {reason}")

    def __init__(
        self,
        session_store: SessionStore = None,
        # 默认权限：code_execution开启，其他默认关闭
        allowed_operations: set = None,
        denied_operations: set = None,
        max_execution_time: float = 10.0,  # 秒
        max_file_size_kb: int = 1024,        # KB
        allowed_file_extensions: set = None,
    ):
        self.session_store = session_store
        self._current_session_id: Optional[str] = None

        # 默认开启code_execution，其他全部deny
        self._default_allowed = {"code_execution"}
        self._allowed = allowed_operations or self._default_allowed
        self._denied = denied_operations or set()

        # 限制参数
        self.max_execution_time = max_execution_time
        self.max_file_size_kb = max_file_size_kb
        self.allowed_file_extensions = allowed_file_extensions or {
            ".py", ".txt", ".md", ".json", ".yaml", ".yml",
            ".js", ".ts", ".java", ".cpp", ".c", ".go", ".rs"
        }

        # 操作计数器
        self._op_counts = {op: 0 for op in self._allowed}

    def set_session(self, session_id: str) -> None:
        """设置当前session ID，用于权限记录。"""
        self._current_session_id = session_id

    def check(self, operation: str, context: dict = None) -> bool:
        """
        检查操作是否被允许。
        deny-first：未明确允许的 → 拒绝。

        Args:
            operation: 操作类型
            context: 额外上下文信息(dict)

        Returns:
            True if allowed

        Raises:
            PermissionDeniedError: 如果操作被拒绝
        """
        context = context or {}

        # Layer 1: 明确拒绝列表优先
        if operation in self._denied:
            self._log_decision(operation, "denied", "explicit_deny_list", context)
            raise self.PermissionDeniedError(
                operation,
                f"Operation '{operation}' is explicitly denied by policy.",
                suggested_action="Contact administrator to request permission."
            )

        # Layer 2: 白名单检查
        if operation not in self._allowed:
            self._log_decision(operation, "denied", "not_in_allowlist", context)
            raise self.PermissionDeniedError(
                operation,
                f"Operation '{operation}' is not in the allowlist.",
                suggested_action=f"Grant permission explicitly: agent.grant_permission('{operation}')"
            )

        # Layer 3: 操作特定的安全检查
        safety_check = self._safety_checks.get(operation)
        if safety_check and not safety_check(self, context):
            self._log_decision(operation, "denied", "safety_check_failed", context)
            raise self.PermissionDeniedError(
                operation,
                f"Operation '{operation}' failed safety check.",
                suggested_action="Review context and try a safer variant."
            )

        # 允许
        self._op_counts[operation] = self._op_counts.get(operation, 0) + 1
        self._log_decision(operation, "allowed", "policy_passed", context)
        return True

    def check_and_execute(self, operation: str, fn, *args, context: dict = None, **kwargs):
        """
        权限检查通过后执行函数。
        用法: gate.check_and_execute("code_execution", self.executor.execute, code, tests)
        """
        self.check(operation, context)
        return fn(*args, **kwargs)

    def grant(self, operation: str) -> None:
        """授予操作权限。"""
        self._denied.discard(operation)
        self._allowed.add(operation)
        self._log_decision(operation, "granted", "runtime_change", {})

    def revoke(self, operation: str) -> None:
        """撤销操作权限。"""
        self._allowed.discard(operation)
        self._denied.add(operation)
        self._log_decision(operation, "revoked", "runtime_change", {})

    def is_allowed(self, operation: str) -> bool:
        """检查权限但不抛出异常。"""
        try:
            self.check(operation)
            return True
        except self.PermissionDeniedError:
            return False

    def get_op_counts(self) -> dict:
        """返回各操作的使用计数。"""
        return dict(self._op_counts)

    def _log_decision(self, operation: str, decision: str,
                     reason: str, context: dict) -> None:
        """记录权限决策到session。"""
        if self.session_store and self._current_session_id:
            self.session_store.append_event(self._current_session_id, {
                "type": "permission_decision",
                "operation": operation,
                "decision": decision,
                "reason": reason,
                "context": context,
            })

    def _check_file_extension(self, filepath: str) -> bool:
        """检查文件扩展名是否在白名单中。"""
        import os
        _, ext = os.path.splitext(filepath)
        return ext.lower() in self.allowed_file_extensions

    def _check_file_size(self, filepath: str) -> bool:
        """检查文件大小是否超限。"""
        import os
        try:
            size_kb = os.path.getsize(filepath) / 1024
            return size_kb <= self.max_file_size_kb
        except OSError:
            return False

    _safety_checks = {}  # 注册到这里的函数接收(self, context)参数


# ---- 为PermissionGate注册安全检查函数 ----
def _gate_check_file_write(gate: PermissionGate, context: dict) -> bool:
    filepath = context.get("filepath", "")
    if not filepath:
        return True  # 无文件名时跳过
    return gate._check_file_extension(filepath)

def _gate_check_code_execution(gate: PermissionGate, context: dict) -> bool:
    code = context.get("code", "")
    if not code:
        return True
    # 禁止危险操作
    dangerous = ["rm -rf", "os.system(", "subprocess.Popen", "eval(",
                 "__import__", "open(", "sys.exit", "os._exit"]
    for pattern in dangerous:
        if pattern in code:
            return False
    return True

PermissionGate._safety_checks = {
    "file_write": _gate_check_file_write,
    "code_execution": _gate_check_code_execution,
}


# ==================== Recovery Manager (Claude-style Checkpoint + Resume) ====================

class RecoveryManager:
    """
    Claude-style 恢复管理器。

    设计原则:
    - 定期保存checkpoint：每轮迭代后自动保存agent状态
    - 可恢复：从checkpoint恢复继续执行
    - 超时检测：防止无限循环
    - 失败隔离：单次失败不影响主流程

    恢复点保存内容:
      - agent状态（迭代计数器、feedback等）
      - 用户输入
      - 当前执行的代码
      - session_id
    """

    def __init__(
        self,
        session_store: SessionStore = None,
        checkpoint_interval: int = 1,  # 每N轮迭代保存一次
        timeout_seconds: float = 300.0,  # 5分钟超时
        max_retries: int = 2,
    ):
        self.session_store = session_store
        self.checkpoint_interval = checkpoint_interval
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

        self._start_time: Optional[float] = None
        self._retry_count = 0

    def start_session(self, session_id: str, metadata: dict = None) -> None:
        """标记一次会话的开始。"""
        self._start_time = time.time()
        self._retry_count = 0
        if self.session_store:
            self.session_store.append_event(session_id, {
                "type": "recovery_session_start",
                "metadata": metadata or {},
            })

    def save_checkpoint(
        self,
        session_id: str,
        agent_state: dict,
        metadata: dict = None,
    ) -> None:
        """
        保存checkpoint。

        agent_state 应包含:
          - iteration: 当前迭代号
          - feedback: 当前feedback状态
          - previous_cot_text: 上次推理
          - previous_code: 上次代码
          - previous_error: 上次错误
          - buggy_code: 原始bug代码
          - bug_description: bug描述
        """
        checkpoint = {
            "session_id": session_id,
            "agent_state": agent_state,
            "saved_at": datetime.now().isoformat(),
            "elapsed_seconds": (time.time() - self._start_time) if self._start_time else 0,
            "metadata": metadata or {},
        }

        if self.session_store:
            self.session_store.save_checkpoint(session_id, checkpoint)
            self.session_store.append_event(session_id, {
                "type": "checkpoint_saved",
                "iteration": agent_state.get("iteration", 0),
                "elapsed_seconds": checkpoint["elapsed_seconds"],
            })

        logger.info(
            f"[Recovery] checkpoint saved at iteration={agent_state.get('iteration', 0)}, "
            f"elapsed={checkpoint['elapsed_seconds']:.1f}s"
        )

    def load_checkpoint(self, session_id: str) -> Optional[dict]:
        """从checkpoint加载状态。"""
        if not self.session_store:
            return None

        checkpoint = self.session_store.load_checkpoint(session_id)
        if checkpoint:
            self.session_store.append_event(session_id, {
                "type": "checkpoint_loaded",
                "iteration": checkpoint.get("agent_state", {}).get("iteration", 0),
            })
            logger.info(f"[Recovery] checkpoint loaded from iteration={checkpoint.get('agent_state', {}).get('iteration', 0)}")
        return checkpoint

    def check_timeout(self) -> bool:
        """检查是否超时。"""
        if self._start_time is None:
            return False
        elapsed = time.time() - self._start_time
        if elapsed > self.timeout_seconds:
            logger.warning(f"[Recovery] timeout: {elapsed:.1f}s > {self.timeout_seconds}s")
            return True
        return False

    def should_checkpoint(self, iteration: int) -> bool:
        """判断当前迭代是否应该保存checkpoint。"""
        return iteration > 0 and iteration % self.checkpoint_interval == 0

    def handle_failure(
        self,
        session_id: str,
        error: Exception,
        agent_state: dict,
    ) -> Optional[dict]:
        """
        处理失败：保存checkpoint并返回恢复信息。

        Returns:
            None: 达到最大重试次数，不恢复
            dict: 恢复信息，包含重新执行的参数
        """
        self._retry_count += 1

        if self._retry_count > self.max_retries:
            logger.warning(f"[Recovery] max retries ({self.max_retries}) reached, giving up")
            return None

        if self.session_store:
            self.session_store.append_event(session_id, {
                "type": "recovery_failure",
                "retry_count": self._retry_count,
                "error_type": type(error).__name__,
                "error_message": str(error)[:500],
                "iteration": agent_state.get("iteration", 0),
            })

        # 保存失败的checkpoint
        self.save_checkpoint(
            session_id,
            agent_state,
            metadata={
                "failure_point": "retry_exhausted",
                "retry_count": self._retry_count,
            }
        )

        return {
            "retry": True,
            "retry_count": self._retry_count,
            "agent_state": agent_state,
        }

    def get_session_stats(self, session_id: str) -> dict:
        """获取session的统计信息。"""
        if not self.session_store:
            return {}

        events = self.session_store.load_session(session_id)
        checkpoints = [e for e in events if e.get("type") == "checkpoint_saved"]
        failures = [e for e in events if e.get("type") == "recovery_failure"]
        completions = [e for e in events if e.get("type") == "agent_result"]

        return {
            "total_events": len(events),
            "checkpoints_saved": len(checkpoints),
            "failures_handled": len(failures),
            "completed": len(completions) > 0,
            "last_checkpoint": checkpoints[-1].get("elapsed_seconds", 0) if checkpoints else 0,
        }


# ==================== Main Agent ====================

class CoTReActAgent:
    """
    Three-Layer Memory Enhanced CoT ReAct Agent.

    Memory layers (Hermes-inspired):
      L1 (Working): current bug context — the immediate working memory
      L2 (Reflection): past failures — "what not to do" (Reflexion)
      L3 (Skill): past successes — "how to do it right" (Hermes)

    Typical flow for easy bugs:
      Small model CoT → compile → pass → done (no L2/L3 needed)

    Flow for hard bugs:
      [L2 retrieve reflections] + [L3 retrieve skills]
      → Small model CoT (with lessons injected) → compile → fail
      → Large model error analysis → retry → ... → pass or exhaust
      → If all retries fail: generate reflection → store in L2
      → If complex fix succeeded: extract skill SOP → store in L3
    
    AST Integration (解决行号偏移问题):
      - 使用Tree-sitter进行精确的函数定位和替换
      - 基于字节偏移而非行号进行代码替换
      - 支持Python/Java/JavaScript多语言
    """

    def __init__(self, small_model_fn, large_model_fn,
                 rag_retriever=None, max_iterations: int = 3,
                 reflection_memory: ReflectionMemory = None,
                 skill_manager: SkillManager = None,
                 language: str = "python",
                 session_store: SessionStore = None,
                 event_callback: callable = None,
                 reflection_model: str = "large",
                 max_reflection_tokens: int = 2048):
        """
        Args:
            event_callback: 可选回调函数，签名为
                callback(event_type: str, data: dict, iteration: int)
                用于流式推送（如 SSE）。事件类型：
                  phase, cot_generated, code_extracted, compile_result,
                  large_model_feedback, ruff_check, iteration_start, iteration_end,
                  memory_hit, done, error
            reflection_model: 反思使用的模型，"large"（大模型，默认）或 "small"
            max_reflection_tokens: 反思 prompt 的最大 token 数估算，超过则截断
        """
        self.small_model = small_model_fn
        self.large_model = large_model_fn
        self.rag_retriever = rag_retriever
        self.max_iterations = max_iterations
        self.executor = CodeExecutor()
        self.parser = CoTParser()
        self.reflection_memory = reflection_memory or ReflectionMemory()
        self.skill_manager = skill_manager or SkillManager()
        self._used_skills_this_fix: List[Skill] = []  # full Skill objects for injection
        self.event_callback = event_callback  # 流式事件回调
        self.reflection_model = reflection_model  # "large" or "small"
        self.max_reflection_tokens = max_reflection_tokens

    def _emit(self, event_type: str, data: dict, iteration: int = 0):
        """触发事件回调（线程安全）"""
        if self.event_callback:
            try:
                self.event_callback(event_type, data, iteration)
            except Exception:
                pass  # 回调错误不中断主流程
        
        # Session Persistence (Claude-style, append-only)
        self.session_store = session_store or SessionStore()
        self._current_session_id: Optional[str] = None

        # I/O Logger (Claude-style 完整输入输出追踪)
        self.io_logger = IOLogger(session_store=self.session_store)

        # Compaction Pipeline (Claude-style 5层上下文压缩)
        self.compaction_pipeline = CompactionPipeline(
            session_store=self.session_store,
        )

        # 历史上下文管理（CompactionPipeline配套）
        # 累积迭代过程中的消息，用于压缩
        self._iteration_history: List[dict] = []

        # Permission Gate (Claude-style deny-first权限控制)
        self.permission_gate = PermissionGate(
            session_store=self.session_store,
        )

        # Recovery Manager (Claude-style checkpoint + resume)
        self.recovery_manager = RecoveryManager(
            session_store=self.session_store,
            timeout_seconds=300.0,
        )

        
        # AST代码处理器（解决行号偏移问题）
        self.language = language
        self.ast_processor = None
        if AST_PROCESSOR_AVAILABLE:
            try:
                self.ast_processor = ASTCodeProcessor(language)
                logger.info(f"✓ AST代码处理器已初始化 ({language})")
            except Exception as e:
                logger.warning(f"⚠️ AST代码处理器初始化失败: {e}")

    def fix_bug(self, bug_description: str, buggy_code: str,
                test_cases: List[Dict] = None,
                session_id: str = None) -> AgentResult:
        """
        Main entry point — Reflexion-enhanced ReAct loop.
        
        Args:
            session_id: Optional. If provided, all events are logged to this session.
                        If None, a new session ID is auto-generated.
        """
        agent_result = AgentResult(success=False)
        start_time = time.time()
        self._used_skills_this_fix = []  # reset

        # ---- Session Persistence: create or resume session ----
        if session_id:
            self._current_session_id = session_id
        else:
            self._current_session_id = str(uuid.uuid4())[:8]
        bug_hash = hashlib.md5((buggy_code + bug_description).encode()).hexdigest()[:8]
        self.session_store.create_session(
            self._current_session_id,
            metadata={
                "bug_description": bug_description[:200],
                "bug_hash": bug_hash,
                "max_iterations": self.max_iterations,
            }
        )
        # Log the incoming user message
        self.session_store.append_user_message(
            self._current_session_id,
            content=f"Bug: {bug_description}\n\nBuggy Code:\n{buggy_code}",
            metadata={"bug_hash": bug_hash}
        )

        # ---- Recovery Manager: 初始化会话 ----
        self.recovery_manager.start_session(
            self._current_session_id,
            metadata={"bug_hash": bug_hash, "max_iterations": self.max_iterations}
        )
        self.permission_gate.set_session(self._current_session_id)

        # ---- Compaction Pipeline: 重置迭代历史 ----
        self._iteration_history = []

        # ---- L2 Retrieval: relevant past reflection (what NOT to do) ----
        past_reflections = self.reflection_memory.retrieve(
            bug_description, buggy_code, top_k=2
        )
        self._emit("memory_hit", {
            "pool": "L2_reflection",
            "count": len(past_reflections),
            "items": [{"pattern": r.bug_pattern, "lesson": r.lesson_learned[:80]}
                      for r in (past_reflections or [])],
        })
        if past_reflections:
            logger.info(
                f"[L2] Retrieved {len(past_reflections)} relevant reflections from memory"
            )
            agent_result.used_past_reflections = len(past_reflections)

        # ---- L3 Retrieval: relevant past skills (how TO do it right) ----
        past_skills = self.skill_manager.retrieve(
            bug_description, buggy_code, top_k=3
        )
        self._emit("memory_hit", {
            "pool": "L3_skill",
            "count": len(past_skills),
            "items": [{"name": s.name, "usefulness": s.usefulness}
                      for s in (past_skills or [])],
        })
        if past_skills:
            logger.info(f"[L3] Retrieved {len(past_skills)} relevant skills")
            self._used_skills_this_fix = past_skills  # full objects for injection
            agent_result.used_skills = [s.skill_id for s in past_skills]

        feedback: Optional[LargeModelFeedback] = None
        previous_cot_text = ""
        previous_code = ""
        previous_error = ""

        for iteration in range(1, self.max_iterations + 1):
            iter_start = time.time()
            logger.info(f"--- Iteration {iteration}/{self.max_iterations} ---")
            self._emit("iteration_start", {
                "iteration": iteration,
                "max_iterations": self.max_iterations,
                "elapsed_ms": (time.time() - start_time) * 1000,
            })

            # ---- Recovery Manager: 超时检测 ----
            if self.recovery_manager.check_timeout():
                logger.warning(f"[Recovery] Session {self._current_session_id} timed out at iteration {iteration}")
                self.session_store.append_event(self._current_session_id, {
                    "type": "session_timeout",
                    "iteration": iteration,
                })
                agent_result.total_iterations = iteration
                agent_result.total_time_ms = (time.time() - start_time) * 1000
                self.session_store.append_agent_result(self._current_session_id, agent_result)
                return agent_result

            # ---- Compaction Pipeline: 在模型调用前执行5层压缩 ----
            compact_stats = {}
            if self._iteration_history:
                compacted_history, chars_freed = self.compaction_pipeline.compact(
                    self._iteration_history,
                    session_id=self._current_session_id,
                    iteration=iteration
                )
                compact_stats = self.compaction_pipeline.get_stats()
                if chars_freed > 0:
                    logger.info(f"[Compaction] freed {chars_freed} chars, layers: {compact_stats}")
            else:
                compacted_history = []

            record = IterationRecord(
                iteration=iteration,
                cot_steps=[],
                fixed_code="",
                execution_result=ExecutionResult(success=False),
            )

            # ---- Step 1: Small model generates CoT + fix ----
            if feedback is None:
                raw_output = self._generate_first_attempt(
                    bug_description, buggy_code, past_reflections, self._used_skills_this_fix,
                    compacted_history=compacted_history
                )
                print(f"[SmallModel raw output]\n{raw_output}\n")
            else:
                raw_output = self._generate_retry(
                    bug_description, buggy_code,
                    previous_cot_text, previous_code,
                    previous_error, feedback,
                    past_reflections, self._used_skills_this_fix,
                    compacted_history=compacted_history
                )
                print(f"[SmallModel raw output]\n{raw_output}\n")

            # ---- Session Persistence: log small model output ----
            self.session_store.append_model_output(
                self._current_session_id,
                content=raw_output,
                metadata={
                    "iteration": iteration,
                    "model": "small",
                    "feedback_provided": feedback.hint if feedback else None,
                }
            )

            steps = self.parser.parse_steps(raw_output)
            fixed_code = self.parser.extract_code(raw_output)
            fixed_code = self.parser.normalize_function_name(fixed_code, buggy_code)
            record.cot_steps = steps
            record.fixed_code = fixed_code

            # 流式事件：小模型推理完成
            self._emit("cot_generated", {
                "iteration": iteration,
                "cot_text": raw_output[:2000],  # 截断避免过大
                "model": "small",
            })
            self._emit("code_extracted", {
                "iteration": iteration,
                "fixed_code": fixed_code[:500] if fixed_code else "",
                "step_count": len(steps),
                "has_code": bool(fixed_code),
            })

            if not fixed_code:
                logger.warning("Small model produced no code, skipping to next iteration")
                previous_error = "No valid code was generated in the response."
                agent_result.iterations.append(record)
                continue

            logger.info("Compiling and testing...")
            # ---- Permission Gate: 检查代码执行权限 ----
            try:
                self.permission_gate.check(
                    "code_execution",
                    context={"code": fixed_code, "iteration": iteration}
                )
            except PermissionGate.PermissionDeniedError as e:
                logger.warning(f"Code execution blocked: {e}")
                exec_result = ExecutionResult(
                    success=False,
                    error_type="permission_denied",
                    error_message=f"Permission denied: {e.reason}",
                )
                record.execution_result = exec_result
            else:
                exec_result = self.executor.execute(fixed_code, test_cases)
                record.execution_result = exec_result

                # 流式事件：编译+测试结果
                self._emit("compile_result", {
                    "iteration": iteration,
                    "success": exec_result.success,
                    "error_type": exec_result.error_type or "",
                    "error_message": (exec_result.error_message or "")[:500],
                    "passed_tests": exec_result.passed_tests,
                    "total_tests": exec_result.total_tests,
                    "passed": exec_result.passed_tests == exec_result.total_tests
                        if exec_result.total_tests > 0 else exec_result.success,
                })

            # ---- Session Persistence: log tool result ----
            self.session_store.append_tool_result(
                self._current_session_id,
                tool_name="CodeExecutor",
                result={
                    "success": exec_result.success,
                    "error_type": exec_result.error_type,
                    "error_message": exec_result.error_message[:500] if exec_result.error_message else None,
                    "test_results": [
                        {"name": tr.get("name"), "passed": tr.get("passed")}
                        for tr in (test_cases or [])
                    ],
                },
                success=exec_result.success,
                metadata={"iteration": iteration, "fixed_code": fixed_code[:500]},
            )

            if exec_result.success:
                # ---- OPTIMIZATION_ONLY check via local ruff (no LLM, no hallucination) ----
                # 使用分数阈值机制：问题数超过阈值时触发优化
                has_quality_issues, quality_issues, issue_count = self._check_code_quality(
                    fixed_code, threshold=0  # 阈值为0，表示有任何问题就触发优化
                )

                if has_quality_issues:
                    # Ruff found style/readability issues → trigger one-shot optimization
                    logger.info(
                        f"Ruff found {len(quality_issues)} issue(s), "
                        f"triggering OPTIMIZATION_ONLY"
                    )
                    record.used_large_model = False
                    agent_result.total_iterations = iteration
                    agent_result.total_time_ms = (time.time() - start_time) * 1000

                    opt_record = self._optimization_pass(
                        fixed_code, quality_issues, iteration, test_cases
                    )
                    if opt_record:
                        agent_result.iterations.append(record)
                        agent_result.iterations.append(opt_record)
                        if opt_record.execution_result and opt_record.execution_result.success:
                            # L2: mark reflections as helpful
                            for ref in past_reflections:
                                self.reflection_memory.record_helpfulness(ref.buggy_code_hash, was_helpful=True)
                            # L3: record skill outcomes
                            for s in self._used_skills_this_fix:
                                self.skill_manager.record_outcome(s.skill_id, success=True)
                            # L3: try to extract a new skill from this fix
                            self._maybe_extract_skill(
                                bug_description, buggy_code, opt_record.fixed_code,
                                opt_record.cot_steps, agent_result, iteration
                            )
                            agent_result.success = True
                            agent_result.fixed_code = opt_record.fixed_code
                            agent_result.total_time_ms = (time.time() - start_time) * 1000
                            self.session_store.append_agent_result(self._current_session_id, agent_result)
                            return agent_result
                        else:
                            # Optimization didn't improve — return what we had
                            for ref in past_reflections:
                                self.reflection_memory.record_helpfulness(ref.buggy_code_hash, was_helpful=True)
                            agent_result.success = True
                            agent_result.fixed_code = fixed_code
                            self.session_store.append_agent_result(self._current_session_id, agent_result)
                            return agent_result
                    else:
                        agent_result.success = True
                        agent_result.fixed_code = fixed_code
                        self.session_store.append_agent_result(self._current_session_id, agent_result)
                        return agent_result
                else:
                    # Ruff clean → CORRECT, no further optimization needed
                    logger.info("CORRECT: code passes compiler + tests + ruff clean")
                    record.used_large_model = False
                    record.time_ms = (time.time() - iter_start) * 1000
                    agent_result.iterations.append(record)

                    # L2: mark reflections as helpful
                    for ref in past_reflections:
                        self.reflection_memory.record_helpfulness(ref.buggy_code_hash, was_helpful=True)

                    # L3: record skill outcomes and extract new skill
                    for s in self._used_skills_this_fix:
                        self.skill_manager.record_outcome(s.skill_id, success=True)
                    self._maybe_extract_skill(
                        bug_description, buggy_code, fixed_code,
                        record.cot_steps, agent_result, iteration
                    )

                    agent_result.success = True
                    agent_result.fixed_code = fixed_code
                    agent_result.total_iterations = iteration
                    agent_result.total_time_ms = (time.time() - start_time) * 1000
                    self.session_store.append_agent_result(self._current_session_id, agent_result)
                    return agent_result

            # ---- FAIL ----
            logger.info(
                f"FAIL ({exec_result.error_type}): {exec_result.error_message[:100]}"
            )

            previous_cot_text = self.parser.format_steps_as_text(steps)
            previous_code = fixed_code
            previous_error = self._format_error(exec_result)

            # ---- Step 3: Large model error analysis ----
            if iteration < self.max_iterations:
                logger.info("Calling large model for error analysis...")
                feedback = self._analyze_error(
                    buggy_code, previous_cot_text,
                    fixed_code, previous_error
                )
                record.large_model_feedback = feedback
                record.used_large_model = True
                agent_result.large_model_calls += 1

                # 流式事件：大模型反馈
                self._emit("large_model_feedback", {
                    "iteration": iteration,
                    "status": feedback.status,
                    "first_error_step": feedback.first_error_step,
                    "error_type": feedback.error_type,
                    "hint": feedback.hint,
                })

                # ---- Session Persistence: log large model feedback ----
                self.session_store.append_model_output(
                    self._current_session_id,
                    content=f"[Large Model Feedback] status={feedback.status}, "
                            f"hint={feedback.hint}, error_type={feedback.error_type}",
                    metadata={
                        "iteration": iteration,
                        "model": "large",
                        "feedback": {
                            "status": feedback.status,
                            "hint": feedback.hint,
                            "error_type": feedback.error_type,
                        }
                    }
                )

                logger.info(
                    f"Large model says: error at Step {feedback.first_error_step}, "
                    f"hint: {feedback.hint[:80]}"
                )

            record.time_ms = (time.time() - iter_start) * 1000
            agent_result.iterations.append(record)

            # 流式事件：迭代结束
            self._emit("iteration_end", {
                "iteration": iteration,
                "success": record.execution_result.success,
                "used_large_model": record.used_large_model,
                "time_ms": record.time_ms,
            })

            # ---- Compaction Pipeline: 累积本次迭代消息到历史 ----
            self._iteration_history.extend([
                {"role": "user", "content": f"[Iteration {iteration}] Bug: {bug_description}", "iteration": iteration},
                {"role": "assistant", "content": raw_output, "iteration": iteration},
                {
                    "role": "tool",
                    "content": (
                        f"success={exec_result.success}, "
                        f"error_type={exec_result.error_type}, "
                        f"msg={exec_result.error_message[:200] if exec_result.error_message else ''}"
                    ),
                    "iteration": iteration,
                    "success": exec_result.success,
                    "name": "CodeExecutor",
                },
            ])
            if feedback:
                self._iteration_history.append({
                    "role": "assistant",
                    "content": f"[LargeModelFeedback] status={feedback.status}, hint={feedback.hint}",
                    "iteration": iteration,
                    "model": "large",
                })

            # ---- Recovery Manager: 迭代结束保存checkpoint ----
            if self.recovery_manager.should_checkpoint(iteration):
                agent_state = {
                    "iteration": iteration,
                    "feedback_status": feedback.status if feedback else None,
                    "feedback_hint": feedback.hint if feedback else None,
                    "previous_code": fixed_code,
                    "previous_error": previous_error,
                    "previous_cot_text": previous_cot_text,
                    "buggy_code": buggy_code,
                    "bug_description": bug_description,
                    "exec_success": exec_result.success,
                }
                self.recovery_manager.save_checkpoint(
                    self._current_session_id,
                    agent_state,
                    metadata={"method": "iteration_end_checkpoint"}
                )

        # ---- All retries exhausted → L2: generate and store reflection ----
        agent_result.total_iterations = self.max_iterations
        agent_result.total_time_ms = (time.time() - start_time) * 1000

        # L3: record skill failures and trigger Hermes-style patch
        for s in self._used_skills_this_fix:
            self.skill_manager.record_outcome(s.skill_id, success=False)
            failed_reason = (
                f"All {self.max_iterations} fix attempts failed. "
                "The loaded skill did not prevent failure — it may need refinement."
            )
            self.skill_manager.patch_skill(s.skill_id, failed_reason, self.large_model)

        logger.info("All retries exhausted. Generating reflection...")
        reflection = self._generate_reflection(
            bug_description, buggy_code, agent_result
        )
        if reflection:
            self.reflection_memory.add(reflection)
            agent_result.reflection = reflection
            logger.info(
                f"Reflection stored: pattern='{reflection.bug_pattern}', "
                f"lesson='{reflection.lesson_learned[:80]}'"
            )

        # ---- Session Persistence: log final result ----
        self.session_store.append_agent_result(self._current_session_id, agent_result)
        self.session_store.save_checkpoint(
            self._current_session_id,
            state={
                "status": "completed",
                "success": agent_result.success,
                "session_id": self._current_session_id,
            }
        )

        # 流式事件：任务完成
        self._emit("done", {
            "success": agent_result.success,
            "fixed_code": agent_result.fixed_code,
            "total_iterations": agent_result.total_iterations,
            "large_model_calls": agent_result.large_model_calls,
            "total_time_ms": agent_result.total_time_ms,
            "used_reflections": agent_result.used_past_reflections,
            "used_skills": agent_result.used_skills or [],
            "skill_extracted": getattr(agent_result, "skill_extracted", None),
        })

        return agent_result

    # ---- AST-based Code Operations (解决行号偏移问题) ----
    def _extract_function_by_name(self, code: str, function_name: str) -> Optional[str]:
        """
        使用AST从完整代码中提取指定函数
        解决：当模型返回完整文件代码时，需要提取特定函数进行验证
        """
        if not self.ast_processor:
            # 降级方案：正则匹配
            pattern = rf'(def {function_name}\s*\([^)]*\).*?(?=\ndef |\nclass |\n$))'
            match = re.search(pattern, code, re.DOTALL)
            return match.group(1) if match else None
        
        func_info = self.ast_processor.find_function_by_name(code, function_name)
        if func_info:
            return code[func_info.start_byte:func_info.end_byte]
        return None
    
    def _replace_function_in_code(self, original_code: str, function_name: str,
                                   new_function_code: str) -> Optional[str]:
        """
        使用AST精确替换指定函数（基于字节偏移，解决行号偏移问题）
        
        Args:
            original_code: 原始完整代码
            function_name: 要替换的函数名
            new_function_code: 新的函数代码
        
        Returns:
            替换后的完整代码，失败返回None
        """
        if not self.ast_processor:
            # 降级方案：正则替换
            pattern = rf'(def {function_name}\s*\([^)]*\).*?(?=\ndef |\nclass |\n$))'
            match = re.search(pattern, original_code, re.DOTALL)
            if match:
                return original_code[:match.start()] + new_function_code + original_code[match.end():]
            return None
        
        return self.ast_processor.replace_function_safe(original_code, function_name, new_function_code)
    
    def _find_function_by_error_line(self, code: str, error_line: int) -> Optional[str]:
        """
        根据错误行号定位包含该行的函数
        解决：编译器报错只给出行号，需要找到对应的函数进行修复
        """
        if not self.ast_processor:
            # 降级方案：简单的行号匹配
            lines = code.split('\n')
            func_name = None
            for i, line in enumerate(lines[:error_line]):
                if line.strip().startswith('def '):
                    func_name = re.match(r'def (\w+)\s*\(', line).group(1)
            return func_name
        
        func_info = self.ast_processor.find_function_by_line_number(code, error_line)
        return func_info.name if func_info else None
    
    def _fix_imports_in_code(self, code: str, missing_imports: List[str]) -> str:
        """
        自动修复缺失的导入语句
        """
        if not self.ast_processor:
            # 降级方案：简单的导入处理
            for imp in missing_imports:
                if imp not in code:
                    code = imp + '\n' + code
            return code
        
        return self.ast_processor.fix_imports(code, missing_imports)
    
    def _analyze_function_dependencies(self, code: str, function_name: str) -> Dict[str, List[str]]:
        """
        分析函数的依赖关系（被调用的函数、导入等）
        用于：在修复前了解函数的上下文依赖
        """
        if not self.ast_processor:
            return {"calls": [], "imports": [], "variables": []}
        
        return self.ast_processor.get_function_dependencies(code, function_name)
    
    # ---- L3: Attempt to extract a Skill from a successful fix ----
    def _maybe_extract_skill(self, bug_desc: str, buggy_code: str,
                             fixed_code: str, cot_steps: List[CoTStep],
                             agent_result: AgentResult, iteration: int):
        """
        Trigger skill extraction only when the fix is non-trivial.
        Complexity gating: only extract from multi-iteration or complex fixes.
        """
        # Trigger conditions (Hermes complexity threshold):
        #   - Fix took more than 1 iteration, OR
        #   - Fix involved large model calls (hard bug), OR
        #   - Code diff is non-trivial (more than 3 lines changed)
        complexity = self._estimate_complexity(buggy_code, fixed_code, iteration,
                                               agent_result.large_model_calls)

        if complexity < 1:
            logger.info(
                f"[L3] Fix was too trivial (complexity={complexity}), "
                "not extracting skill to avoid noise."
            )
            return

        code_hash = hashlib.md5(buggy_code.encode()).hexdigest()[:12]

        skill = self.skill_manager.generate_skill_from_fix(
            bug_description=bug_desc,
            buggy_code=buggy_code,
            fixed_code=fixed_code,
            successful_steps=cot_steps,
            source_bug_hash=code_hash,
            large_model_fn=self.large_model,
            complexity_score=complexity,
        )

        if skill:
            agent_result.skill_extracted = skill.skill_id
            logger.info(
                f"[L3] Skill extracted: '{skill.name}' "
                f"(complexity={complexity}, steps={len(skill.steps)})"
            )

    @staticmethod
    def _estimate_complexity(buggy_code: str, fixed_code: str,
                             iterations: int, large_model_calls: int) -> int:
        """
        Estimate fix complexity to gate skill extraction.
        0 = trivial (single-iteration, no LLM), not worth storing
        1 = moderate (multi-iteration or LLM involved)
        2 = complex (multiple LLM calls)
        3 = highly complex (many iterations + many LLM calls + large diff)
        """
        lines_diff = abs(len(fixed_code.splitlines()) - len(buggy_code.splitlines()))

        score = 0
        if iterations > 1:
            score += 1
        if large_model_calls > 0:
            score += 1
        if lines_diff > 3:
            score += 1
        if iterations > 2:
            score += 1

        return min(score, 3)

    # ---- Small Model: First Attempt (with L2 Reflection + L3 Skill context) ----
    def _generate_first_attempt(self, bug_desc: str, buggy_code: str,
                                past_reflections: List[Reflection] = None,
                                past_skills: List[Skill] = None,
                                compacted_history: List[dict] = None) -> str:
        user_prompt = f"Bug: {bug_desc}\n\nBuggy Code:\n```python\n{buggy_code}\n```\n"

        # Inject past reflections (L2: what NOT to do)
        if past_reflections:
            user_prompt += "\n--- LESSONS FROM PAST SIMILAR BUGS ---\n"
            for i, ref in enumerate(past_reflections, 1):
                user_prompt += (
                    f"\nLesson {i} (pattern: {ref.bug_pattern}):\n"
                    f"- What failed before: {'; '.join(ref.failed_strategies[:2])}\n"
                    f"- Key lesson: {ref.lesson_learned}\n"
                    f"- Suggested approach: {ref.suggested_approach}\n"
                )
            user_prompt += "\nApply these lessons to avoid repeating the same mistakes.\n"

        # Inject past skills (L3: how TO do it right — Hermes-style)
        if past_skills:
            user_prompt += "\n--- RELEVANT FIX SOPs FROM PAST SUCCESSES ---\n"
            for i, skill in enumerate(past_skills, 1):
                user_prompt += (
                    f"\nSkill {i}: {skill.name}\n"
                    f"Usefulness: {skill.usefulness:.0%} ({skill.success_count}✅ / {skill.failure_count}❌)\n"
                    f"Trigger: {', '.join(skill.trigger_keywords[:5])}\n"
                    f"Applicability: {skill.applicability}\n"
                    f"Steps:\n"
                )
                for j, step in enumerate(skill.steps, 1):
                    user_prompt += f"  {j}. {step}\n"
                user_prompt += f"Verification: {skill.verification_hint}\n"
            user_prompt += "\nFollow these SOPs where applicable. Adapt steps to this specific bug.\n"

        # ---- Compaction Pipeline: 注入历史压缩摘要 ----
        if compacted_history:
            history_summary = self._format_compacted_history(compacted_history)
            user_prompt += f"\n--- PRIOR ITERATION HISTORY (compacted) ---\n{history_summary}\n"

        # RAG retrieval
        if self.rag_retriever:
            similar = self.rag_retriever.retrieve_similar_fixes(
                f"{bug_desc}\n{buggy_code}", top_k=2
            )
            if similar:
                user_prompt += "\nSimilar bug fixes from knowledge base:\n"
                for i, fix in enumerate(similar, 1):
                    user_prompt += (
                        f"\nExample {i} ({fix.get('bug_type', '')}):\n"
                        f"Original: {fix.get('original_code', '')[:150]}\n"
                        f"Fixed: {fix.get('fixed_code', '')[:150]}\n"
                    )

        user_prompt += "\nFix the bug. Follow the required format."
        # ---- IOLogger: 记录小模型输入+输出 ----
        io_pair_id = self.io_logger.log_model_input(
            self._current_session_id,
            model_name="small",
            system_prompt=COT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            metadata={"method": "_generate_first_attempt"},
        )
        raw_output = self.small_model(user_prompt, COT_SYSTEM_PROMPT)
        self.io_logger.log_model_output(
            self._current_session_id,
            io_pair_id=io_pair_id,
            model_name="small",
            raw_output=raw_output,
            metadata={"method": "_generate_first_attempt"},
        )
        return raw_output

    # ---- Small Model: Retry with Feedback + L2 Reflection + L3 Skill ----
    def _generate_retry(self, bug_desc: str, buggy_code: str,
                        prev_cot: str, prev_code: str,
                        prev_error: str,
                        feedback: LargeModelFeedback,
                        past_reflections: List[Reflection] = None,
                        past_skills: List[Skill] = None,
                        compacted_history: List[dict] = None) -> str:
        status = feedback.status

        user_prompt = (
            f"Bug: {bug_desc}\n\n"
            f"Buggy Code:\n```python\n{buggy_code}\n```\n\n"
            f"--- YOUR PREVIOUS ATTEMPT ---\n\n"
            f"Your previous reasoning:\n{prev_cot}\n\n"
            f"Your previous fix:\n```python\n{prev_code}\n```\n\n"
            f"Execution result:\n```\n{prev_error}\n```\n\n"
            f"--- SENIOR REVIEWER FEEDBACK ---\n\n"
            f"STATUS: {status}\n"
        )

        if status == "CORRECT":
            user_prompt += (
                "The reviewer confirms your fix is CORRECT. No changes needed.\n"
            )
        elif status == "OPTIMIZATION_ONLY":
            user_prompt += (
                f"The reviewer says your code runs but could be improved.\n"
                f"Optimization suggestion: {feedback.optimization_note}\n"
                f"Improve the code quality while maintaining correctness.\n"
            )
        else:
            # BUG_DETECTED
            user_prompt += (
                f"Error is in your Step {feedback.first_error_step}.\n"
                f"Problem: {feedback.error_in_reasoning}\n"
                f"Hint: {feedback.hint}\n\n"
                f"Fix the bug. Pay special attention to Step {feedback.first_error_step}."
            )

        # ---- Inject past reflections (reinforce on retry rounds) ----
        if past_reflections:
            user_prompt += (
                "\n--- PAST SIMILAR BUG LESSONS (remember these) ---\n"
                "The following lessons were learned from fixing similar bugs. "
                "Apply them to avoid repeating the same mistakes:\n"
            )
            for i, ref in enumerate(past_reflections, 1):
                user_prompt += (
                    f"\nLesson {i} (pattern: {ref.bug_pattern}):\n"
                    f"  Root cause: {ref.root_cause}\n"
                    f"  What failed: {'; '.join(ref.failed_strategies[:2])}\n"
                    f"  Key insight: {ref.lesson_learned}\n"
                    f"  Suggested approach: {ref.suggested_approach}\n"
                )
            user_prompt += "\nUse these lessons to guide your next fix.\n"

        # ---- Compaction Pipeline: 注入历史压缩摘要 (retry) ----
        if compacted_history:
            history_summary = self._format_compacted_history(compacted_history)
            user_prompt += f"\n--- PRIOR ITERATION HISTORY (compacted) ---\n{history_summary}\n"

        # ---- Inject past skills (reinforce on retry rounds) ----
        if past_skills:
            user_prompt += (
                "\n--- RELEVANT FIX SOPs (remember these) ---\n"
                "Apply these proven fix steps where relevant to this bug:\n"
            )
            for i, skill in enumerate(past_skills, 1):
                user_prompt += (
                    f"\nSkill {i}: {skill.name}\n"
                    f"  Trigger: {', '.join(skill.trigger_keywords[:5])}\n"
                    f"  Steps:\n"
                )
                for j, step in enumerate(skill.steps, 1):
                    user_prompt += f"    {j}. {step}\n"
                    user_prompt += f"  Verification: {skill.verification_hint}\n"

        # ---- IOLogger: 记录小模型输入+输出 (retry) ----
        io_pair_id = self.io_logger.log_model_input(
            self._current_session_id,
            model_name="small",
            system_prompt=COT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            metadata={"method": "_generate_retry"},
        )
        raw_output = self.small_model(user_prompt, COT_SYSTEM_PROMPT)
        self.io_logger.log_model_output(
            self._current_session_id,
            io_pair_id=io_pair_id,
            model_name="small",
            raw_output=raw_output,
            metadata={"method": "_generate_retry"},
        )
        return raw_output

    # ---- Large Model: Error Analysis ----
    def _analyze_error(self, buggy_code: str, cot_text: str,
                       attempted_fix: str, error_msg: str) -> LargeModelFeedback:
        start = time.time()

        prompt = ERROR_ANALYSIS_PROMPT.format(
            buggy_code=buggy_code,
            cot_reasoning=cot_text,
            attempted_fix=attempted_fix,
            error_message=error_msg
        )

        # ---- IOLogger: 记录大模型输入+输出 ----
        io_pair_id = self.io_logger.log_model_input(
            self._current_session_id,
            model_name="large",
            system_prompt="You are a senior code reviewer. Respond in JSON only.",
            user_prompt=prompt,
            metadata={"method": "_analyze_error"},
        )
        raw = self.large_model(prompt, "You are a senior code reviewer. Respond in JSON only.")
        elapsed = (time.time() - start) * 1000
        self.io_logger.log_model_output(
            self._current_session_id,
            io_pair_id=io_pair_id,
            model_name="large",
            raw_output=raw,
            metadata={"method": "_analyze_error", "elapsed_ms": elapsed},
        )

        return self._parse_feedback(raw, elapsed)

    def _parse_feedback(self, raw: str, elapsed_ms: float) -> LargeModelFeedback:
        fb = LargeModelFeedback(raw_response=raw, analysis_time_ms=elapsed_ms)

        json_match = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
        text = json_match.group(1) if json_match else raw.strip()

        try:
            data = json.loads(text)
            fb.status = data.get("status", "BUG_DETECTED").upper()
            fb.first_error_step = data.get("first_error_step", 0)
            fb.error_in_reasoning = data.get("error_in_reasoning", "")
            fb.correct_reasoning = data.get("correct_reasoning", "")
            fb.hint = data.get("hint", "")
            fb.error_type = data.get("error_type", "other")
            fb.optimization_note = data.get("optimization_note", "")
        except (json.JSONDecodeError, KeyError):
            fb.hint = "Re-analyze the bug carefully, your previous reasoning was incorrect."

        return fb

    def _check_code_quality(self, code: str, threshold: int = 0) -> Tuple[bool, List[str], int]:
        """
        Run ruff locally to detect code quality issues (style, readability, etc.).
        
        核心特性：
        - 零幻觉风险：纯规则匹配，无LLM参与
        - 毫秒级延迟：本地工具调用
        - 分数阈值机制：低于阈值时触发优化或重新生成
        
        Args:
            code: 待检查的代码
            threshold: 问题数量阈值，超过此值触发优化（默认0，即有任何问题就触发）
        
        Returns:
            (needs_optimization, list_of_issue_messages, issue_count)
            - needs_optimization: 是否需要优化（问题数 > threshold）
            - list_of_issue_messages: 问题列表
            - issue_count: 问题总数
        """
        import subprocess, tempfile, os, shutil

        ruff_path = shutil.which("ruff")
        if not ruff_path:
            logger.warning("ruff not found in PATH, skipping quality check")
            return False, [], 0

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(code)
            tmp = f.name

        try:
            # 使用更全面的规则集进行检查
            # E: Error (语法错误)
            # F: Flake8 (代码风格)
            # W: Warning (警告)
            # C90: 圈复杂度
            # N: Naming (命名规范)
            # Q: Pycodestyle (Python代码风格)
            # UP: pyupgrade
            result = subprocess.run(
                [ruff_path, 'check', tmp,
                 '--select=E,F,W,C90,N,Q,UP',
                 '--ignore=F401,E501,E741',  # 忽略未使用的导入、行长度、模糊命名
                 '--max-line-length=120'],
                capture_output=True, text=True, timeout=10
            )
            
            issues = []
            if result.returncode != 0:
                for line in result.stdout.split('\n'):
                    line = line.strip()
                    if line and ':' in line:
                        # 解析ruff输出格式：filename:line:col: code: message
                        parts = line.split(':', 3)
                        if len(parts) >= 4:
                            issues.append(line)
            
            issue_count = len(issues)
            needs_optimization = issue_count > threshold
            
            if needs_optimization:
                logger.info(
                    f"Ruff检测到 {issue_count} 个问题 (阈值={threshold}), "
                    f"触发OPTIMIZATION_ONLY优化"
                )
            else:
                logger.info(f"Ruff检查通过: {issue_count} 个问题 (阈值={threshold})")
            
            return needs_optimization, issues, issue_count
        except Exception as e:
            logger.warning(f"Ruff quality check failed: {e}")
            return False, [], 0
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _check_code_quality_detailed(self, code: str) -> Dict[str, Any]:
        """
        详细的代码质量检查，返回分类统计信息
        
        Returns:
            {
                "total_issues": int,
                "needs_optimization": bool,
                "issues": List[str],
                "category_counts": {
                    "error": int,      # E: 语法错误
                    "style": int,      # F, Q: 代码风格
                    "warning": int,    # W: 警告
                    "complexity": int, # C90: 圈复杂度
                    "naming": int      # N: 命名规范
                },
                "score": float  # 质量分数 (0-100，越高越好)
            }
        """
        import subprocess, tempfile, os, shutil

        ruff_path = shutil.which("ruff")
        if not ruff_path:
            return {
                "total_issues": 0,
                "needs_optimization": False,
                "issues": [],
                "category_counts": {"error": 0, "style": 0, "warning": 0, "complexity": 0, "naming": 0},
                "score": 100.0
            }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(code)
            tmp = f.name

        try:
            result = subprocess.run(
                [ruff_path, 'check', tmp, '--select=E,F,W,C90,N,Q,UP', '--format=json'],
                capture_output=True, text=True, timeout=10
            )
            
            issues = []
            category_counts = {"error": 0, "style": 0, "warning": 0, "complexity": 0, "naming": 0}
            
            if result.returncode != 0:
                try:
                    import json as json_lib
                    data = json_lib.loads(result.stdout)
                    for issue in data:
                        issues.append(f"{issue['filename']}:{issue['line']}:{issue['column']}: {issue['code']}: {issue['message']}")
                        code_prefix = issue['code'][0]
                        if code_prefix == 'E':
                            category_counts["error"] += 1
                        elif code_prefix in ['F', 'Q']:
                            category_counts["style"] += 1
                        elif code_prefix == 'W':
                            category_counts["warning"] += 1
                        elif code_prefix == 'C':
                            category_counts["complexity"] += 1
                        elif code_prefix == 'N':
                            category_counts["naming"] += 1
                except (json_lib.JSONDecodeError, KeyError):
                    # 回退到文本解析
                    for line in result.stdout.split('\n'):
                        line = line.strip()
                        if line and ':' in line:
                            issues.append(line)
            
            total_issues = len(issues)
            # 计算质量分数：基于问题数量和严重程度
            # 错误权重：error=5, complexity=3, style=1, warning=1, naming=1
            penalty = (
                category_counts["error"] * 5 +
                category_counts["complexity"] * 3 +
                category_counts["style"] +
                category_counts["warning"] +
                category_counts["naming"]
            )
            # 分数范围：0-100，基础分100，每单位penalty扣1分，最低0分
            score = max(0.0, 100.0 - penalty)
            
            return {
                "total_issues": total_issues,
                "needs_optimization": total_issues > 0,
                "issues": issues,
                "category_counts": category_counts,
                "score": score
            }
        except Exception as e:
            logger.warning(f"Ruff detailed check failed: {e}")
            return {
                "total_issues": 0,
                "needs_optimization": False,
                "issues": [],
                "category_counts": {"error": 0, "style": 0, "warning": 0, "complexity": 0, "naming": 0},
                "score": 100.0
            }
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _optimization_pass(self, current_code: str,
                           quality_issues: List[str],
                           iteration: int,
                           test_cases: List[Dict] = None) -> Optional['IterationRecord']:
        """
        One-shot optimization pass triggered by OPTIMIZATION_ONLY status.
        Uses a lightweight prompt to fix ruff-reported issues without
        changing functionality — this is NOT a retry, just a style pass.
        """
        issue_summary = '\n'.join(f"  - {issue}" for issue in quality_issues[:10])

        opt_prompt = (
            f"The following code passes all tests but has style/quality issues:\n"
            f"```python\n{current_code}\n```\n\n"
            f"Ruff reported these issues:\n{issue_summary}\n\n"
            f"Rewrite the code to fix ONLY the issues above. "
            f"Do NOT change any functionality — all tests must still pass. "
            f"Return ONLY the corrected Python code, no explanation."
        )

        opt_start = time.time()
        try:
            # ---- IOLogger: 记录优化pass的输入+输出 ----
            io_pair_id = self.io_logger.log_model_input(
                self._current_session_id,
                model_name="small",
                system_prompt="(optimization pass, no system prompt)",
                user_prompt=opt_prompt,
                metadata={"method": "_optimization_pass", "quality_issues_count": len(quality_issues)},
            )
            raw_output = self.small_model(opt_prompt)
            self.io_logger.log_model_output(
                self._current_session_id,
                io_pair_id=io_pair_id,
                model_name="small",
                raw_output=raw_output,
                metadata={"method": "_optimization_pass"},
            )
            print(f"[SmallModel opt output]\n{raw_output}\n")
        except Exception as e:
            logger.warning(f"Optimization pass failed: {e}")
            return None

        opt_steps = self.parser.parse_steps(raw_output)
        opt_code = self.parser.extract_code(raw_output)

        if not opt_code:
            logger.warning("Optimization pass produced no code")
            return None

        opt_exec = self.executor.execute(opt_code, test_cases)
        opt_record = IterationRecord(
            iteration=iteration + 1,
            cot_steps=opt_steps,
            fixed_code=opt_code,
            execution_result=opt_exec,
            used_large_model=False,
            time_ms=(time.time() - opt_start) * 1000,
        )
        return opt_record

    # ---- Compaction Pipeline: 格式化压缩历史摘要 ----
    def _format_compacted_history(self, compacted_history: List[dict]) -> str:
        """
        将压缩后的历史消息列表格式化为可读的摘要字符串，
        注入到小模型的prompt中。
        """
        if not compacted_history:
            return "(no prior iteration history)"

        lines = []
        for msg in compacted_history:
            role = msg.get("role", "?")
            content = str(msg.get("content", ""))

            if msg.get("_is_compacted"):
                # 压缩块：显示摘要
                count = msg.get("_original_count", 0)
                layer_type = "collapsed" if msg.get("_is_collapse") else "summarized"
                lines.append(f"[{count} messages {layer_type}]")
                if content:
                    lines.append(content[:200] + ("..." if len(content) > 200 else ""))
            elif msg.get("_truncated"):
                # 被截断的工具输出
                orig_len = msg.get("_original_length", len(content))
                lines.append(f"[tool output truncated: {orig_len} -> {len(content)} chars]")
                lines.append(content[:100] + "...")
            elif role == "assistant":
                # 只显示前100字符
                lines.append(f"[assistant]: {content[:120]}{'...' if len(content) > 120 else ''}")
            elif role == "tool":
                tool_name = msg.get("name", "unknown")
                success = msg.get("success", "?")
                lines.append(f"[{tool_name}: {'OK' if success else 'FAIL'}]: {content[:80]}{'...' if len(content) > 80 else ''}")
            elif role == "user":
                lines.append(f"[user]: {content[:100]}{'...' if len(content) > 100 else ''}")
            else:
                lines.append(f"[{role}]: {content[:80]}{'...' if len(content) > 80 else ''}")

        return "\n".join(lines)

    def _format_error(self, result: ExecutionResult) -> str:
        parts = [f"Error Type: {result.error_type}"]
        parts.append(f"Message: {result.error_message}")
        if result.traceback_str:
            lines = result.traceback_str.strip().split('\n')
            parts.append(f"Traceback (last 5 lines):\n" + '\n'.join(lines[-5:]))
        if result.total_tests > 0:
            parts.append(f"Tests: {result.passed_tests}/{result.total_tests} passed")
        return '\n'.join(parts)

    # ---- Reflexion: Generate Reflection from Failed Attempt ----
    def _generate_reflection(self, bug_desc: str, buggy_code: str,
                             agent_result: AgentResult) -> Optional[Reflection]:
        """
        生成反思。

        上下文管理策略：
        - 先估算总 token 数，超过 max_reflection_tokens 则截断
        - 截断顺序：按迭代轮次从后往前截（最新迭代优先保留）
        - 代码片段最多 300 字符，错误信息最多 150 字符
        - 使用大模型或小模型取决于 reflection_model 配置
        """
        attempt_history = self._build_attempt_history(agent_result)

        # ---- 上下文长度截断 ----
        reflection_prompt = REFLECTION_PROMPT.format(
            max_iterations=self.max_iterations,
            buggy_code=buggy_code,
            bug_description=bug_desc,
            attempt_history=attempt_history,
        )

        estimated_tokens = self._estimate_tokens(reflection_prompt)
        if estimated_tokens > self.max_reflection_tokens:
            logger.warning(
                f"[Reflexion] Prompt too long: ~{estimated_tokens} tokens "
                f"(limit: {self.max_reflection_tokens}). Truncating..."
            )
            reflection_prompt = self._truncate_reflection_prompt(
                reflection_prompt, self.max_reflection_tokens
            )

        # ---- 选择模型 ----
        model_fn = self.large_model if self.reflection_model == "large" else self.small_model
        model_name = "large" if self.reflection_model == "large" else "small"
        logger.info(f"[Reflexion] Generating reflection with {model_name} model")

        try:
            raw = model_fn(
                reflection_prompt,
                "You are an expert reflecting on failed attempts. Respond in JSON only.",
            )

            json_match = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
            text = json_match.group(1) if json_match else raw.strip()
            data = json.loads(text)

            code_hash = hashlib.md5(buggy_code.encode()).hexdigest()[:12]
            keywords = ReflectionMemory._extract_keywords(bug_desc + "\n" + buggy_code)

            return Reflection(
                bug_description=bug_desc[:200],
                buggy_code_hash=code_hash,
                bug_pattern=data.get("bug_pattern", "unknown"),
                root_cause=data.get("root_cause", ""),
                failed_strategies=data.get("failed_strategies", []),
                lesson_learned=data.get("lesson_learned", ""),
                suggested_approach=data.get("suggested_approach", ""),
                timestamp=time.time(),
                bug_keywords=keywords,
                source_bug_hash=code_hash,
            )
        except json.JSONDecodeError as e:
            logger.warning(f"[Reflexion] Failed to parse JSON from reflection: {e}")
            return None
        except Exception as e:
            logger.warning(f"[Reflexion] Failed to generate reflection: {e}")
            return None

    def _build_attempt_history(self, agent_result: AgentResult) -> str:
        """
        构建 attempt history 字符串。

        每节长度限制（宽松到合理范围）：
        - CoT 推理：每步最多 400 字符（足够看清推理步骤）
        - 修复代码：最多 800 字符（一个中等函数的行数）
        - 执行结果：最多 300 字符（完整错误信息）
        - 大模型反馈：最多 200 字符（hint）
        """
        CODEREF_MAX = 800   # 修复代码上限（char）
        COT_MAX = 400       # 单步 CoT 上限（char）
        ERROR_MAX = 300     # 错误信息上限（char）
        HINT_MAX = 200      # hint 上限（char）

        parts = []
        for rec in agent_result.iterations:
            # CoT 分步截断
            cot_steps = self.parser.format_steps_as_text(rec.cot_steps)
            truncated_steps = "\n".join(
                line[:COT_MAX] for line in cot_steps.splitlines()
            )

            entry = (
                f"\n== Attempt {rec.iteration} ==\n"
                f"Reasoning:\n{truncated_steps}\n"
                f"Code:\n```python\n{rec.fixed_code[:CODEREF_MAX]}\n```\n"
                f"Result: {rec.execution_result.error_type} - "
                f"{rec.execution_result.error_message[:ERROR_MAX]}\n"
            )
            if rec.large_model_feedback:
                fb = rec.large_model_feedback
                entry += (
                    f"Reviewer STATUS: {fb.status}, "
                    f"error at Step {fb.first_error_step}, "
                    f"hint: {fb.hint[:HINT_MAX]}\n"
                )
            parts.append(entry)
        return "\n".join(parts)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """
        估算 token 数。

        优先级：
        1. tiktoken cl100k_base（GPT-4/3.5 同款，最准）
        2. 降级：char / 3.5（代码密集文本的平均值，比 /4 更紧）
        """
        if _ENCODER is not None:
            return len(_ENCODER.encode(text))
        return max(1, int(len(text) / 3.5))

    def _truncate_reflection_prompt(self, prompt: str, max_tokens: int) -> str:
        """
        基于 token 数精确截断反思 prompt。

        优先级保留（从高到低）：
        1. 模板系统指令（固定 ~400 tokens，无法裁剪）
        2. Bug description（~200 tokens）
        3. 迭代历史（按轮次从后往前删，直到总 token 达标）
        4. Buggy code（截断到前 N 字符）
        """
        HEADER_OVERHEAD = 500  # 模板 + system prompt 的 token 数（估算）
        available = max_tokens - HEADER_OVERHEAD

        if available <= 0:
            return prompt[:max_tokens * 3]

        # 估算当前 attempt_history 的 token 数
        if "Attempt History:" in prompt:
            header = prompt.split("Attempt History:", 1)[0] + "Attempt History:"
            history_text = prompt.split("Attempt History:", 1)[1]

            # buggy_code 部分（如果有）
            buggy_code_section = ""
            if "Buggy Code:" in history_text:
                parts = history_text.split("Buggy Code:", 1)
                history_text = parts[0]
                buggy_code_section = "Buggy Code:" + parts[1]

            history_tokens = self._estimate_tokens(history_text)
            buggy_tokens = self._estimate_tokens(buggy_code_section)
            total_content_tokens = history_tokens + buggy_tokens

            if total_content_tokens <= available:
                return prompt  # 不需要截断

            # 按比例分配可用 token
            if total_content_tokens > 0:
                history_budget = int(available * history_tokens / total_content_tokens)
                buggy_budget = available - history_budget
            else:
                history_budget = available
                buggy_budget = 0

            # 按比例截断 history（先从最后一代往前删）
            # 每节 = "== Attempt N ==" 约 20 tokens
            SECTION_HEADER_TOKENS = 20
            sections = history_text.strip().split("\n== Attempt")
            budget_per_section = max(SECTION_HEADER_TOKENS, history_budget // max(len(sections), 1))

            kept_sections = []
            used_tokens = 0
            # 从最新的迭代开始，尽量保留更多
            for i in range(len(sections) - 1, -1, -1):
                section = sections[i]
                if not section.strip():
                    continue
                section_tokens = self._estimate_tokens(section)
                if used_tokens + section_tokens <= history_budget:
                    kept_sections.insert(0, section)
                    used_tokens += section_tokens
                # 即使超预算也保留最新的一节（至少有一个 attempt）
                if i == len(sections) - 1 and not kept_sections:
                    truncated_section = self._token_truncate_text(
                        "\n== Attempt" + section, history_budget
                    )
                    kept_sections.insert(0, truncated_section.replace("\n== Attempt", ""))
                    break

            truncated_history = "\n== Attempt".join(kept_sections)

            # buggy_code 按字符数截断（1 token ≈ 3.5 chars）
            if buggy_code_section and buggy_budget > 50:
                buggy_chars = int(buggy_budget * 3.5)
                buggy_code_section = buggy_code_section[:buggy_chars]

            return header + truncated_history + buggy_code_section
        else:
            # 没有 history，直接按字符截断
            return self._token_truncate_text(prompt, max_tokens)

    @staticmethod
    def _token_truncate_text(text: str, max_tokens: int) -> str:
        """按 token 数截断文本"""
        if _ENCODER is not None:
            tokens = _ENCODER.encode(text)
            if len(tokens) <= max_tokens:
                return text
            return _ENCODER.decode(tokens[:max_tokens])
        # 降级
        return text[:max_tokens * 3]


# ==================== Error Attribution Tracker ====================

class ErrorAttributor:
    """
    Track which reasoning steps fail most often across many samples.
    Useful for:
      1. Targeted data collection (collect more data for weak steps)
      2. Curriculum learning (train on weak-step samples first)
      3. Interview: shows systematic analysis capability
    """

    def __init__(self):
        self.step_failures = {1: 0, 2: 0, 3: 0, 4: 0}
        self.error_type_counts = {}
        self.total_failures = 0
        self.first_pass_successes = 0
        self.total_attempts = 0

    def record(self, result: AgentResult):
        self.total_attempts += 1
        if result.success and result.large_model_calls == 0:
            self.first_pass_successes += 1
            return

        for it_record in result.iterations:
            if it_record.large_model_feedback:
                fb = it_record.large_model_feedback
                self.total_failures += 1
                step = fb.first_error_step
                if 1 <= step <= 4:
                    self.step_failures[step] += 1
                et = fb.error_type
                self.error_type_counts[et] = self.error_type_counts.get(et, 0) + 1

    def report(self) -> Dict:
        step_names = {
            1: "Bug Identification",
            2: "Root Cause Analysis",
            3: "Fix Strategy",
            4: "Edge Case Check"
        }
        step_dist = {}
        if self.total_failures > 0:
            step_dist = {
                step_names[k]: f"{v}/{self.total_failures} ({v/self.total_failures:.1%})"
                for k, v in self.step_failures.items()
            }

        first_pass_rate = (
            self.first_pass_successes / self.total_attempts
            if self.total_attempts > 0 else 0.0
        )

        return {
            "total_attempts": self.total_attempts,
            "first_pass_rate": f"{first_pass_rate:.1%}",
            "large_model_calls_saved": f"{first_pass_rate:.1%} of cases needed no large model",
            "step_failure_distribution": step_dist,
            "error_types": self.error_type_counts,
            "weakest_step": step_names.get(
                max(self.step_failures, key=self.step_failures.get), "N/A"
            ) if self.total_failures > 0 else "N/A"
        }


# ==================== Training Data Exporter ====================

def export_grpo_training_data(results: List[AgentResult]) -> List[Dict]:
    """
    Export agent results as GRPO training data with step-level rewards.

    For each iteration:
      - Successful first-pass → all step rewards = 1.0 (reinforce this CoT)
      - Failed iteration → step rewards from large model feedback
        (e.g., Steps 1-2 correct=1.0, Step 3 wrong=0.0, Step 4 skipped=0.0)

    This gives GRPO 4x denser training signal than perplexity.
    """
    training_data = []

    for result in results:
        for record in result.iterations:
            step_rewards = [0.5] * 4  # default: uncertain

            if record.execution_result.success:
                step_rewards = [1.0, 1.0, 1.0, 1.0]
            elif record.large_model_feedback:
                err_step = record.large_model_feedback.first_error_step
                for i in range(4):
                    if i + 1 < err_step:
                        step_rewards[i] = 1.0   # steps before error are correct
                    elif i + 1 == err_step:
                        step_rewards[i] = 0.0   # the failing step
                    else:
                        step_rewards[i] = 0.0   # steps after are tainted

            cot_text = CoTParser.format_steps_as_text(record.cot_steps)
            total_reward = sum(step_rewards) / 4.0

            training_data.append({
                "cot_reasoning": cot_text,
                "fixed_code": record.fixed_code,
                "step_rewards": step_rewards,
                "total_reward": total_reward,
                "success": record.execution_result.success,
                "error_type": record.execution_result.error_type,
                "reviewer_status": (
                    record.large_model_feedback.status
                    if record.large_model_feedback else "NONE"
                ),
                "used_large_model": record.used_large_model,
            })

    return training_data
