#!/usr/bin/env python3
"""
Memory Agent: 三层记忆 + RAG 的统一调度层
========================================
职责：
  - 管理 L1/L2/L3 记忆读写
  - 调度 DualPoolMemory（E-pool / X-pool）
  - 与 RAG pipeline 协作提供上下文检索
  - 维护记忆质量（写入门槛、定期清理）

对外接口（通过 SharedContext）：
  - 输入: buggy_code, bug_description, bug_type
  - 输出: retrieved_memories (L1/L2/L3 + RAG), write_piece (待写入)
"""

from __future__ import annotations

import json
import time
import hashlib
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

from base_agent import BaseAgent, AgentProfile, AgentCapability, SharedContext
from dual_pool_memory import DualPoolMemory, MemoryPiece
from rag.enterprise_rag_pipeline import CodeRepairRAGPipeline


# ==================== L1/L2/L3 记忆操作 ====================

@dataclass
class MemoryQuery:
    """记忆检索请求"""
    query_text: str
    bug_type: str = ""
    bug_signature: str = ""
    keywords: List[str] = field(default_factory=list)
    stage: str = "L1"    # L1/L2/L3/L4 对应 CoT 4 步
    top_k: int = 3


@dataclass
class MemoryResult:
    """记忆检索结果"""
    l1_memories: List[Dict] = field(default_factory=list)   # 当前会话上下文
    l2_memories: List[Dict] = field(default_factory=list)   # 失败教训
    l3_memories: List[Dict] = field(default_factory=list)   # 成功 SOP
    e_pool_memories: List[Dict] = field(default_factory=list)  # 成功轨迹
    x_pool_memories: List[Dict] = field(default_factory=list)  # 候选策略
    rag_results: List[Dict] = field(default_factory=list)    # RAG 案例
    retrieval_time_ms: float = 0.0


# ==================== Memory Agent ====================

class MemoryAgent(BaseAgent):
    """
    记忆管理 Agent（去中心化记忆系统的调度中枢）

    职责拆分：
      1. L1 上下文压缩：当前任务上下文，token 级别压缩
      2. L2 失败教训：失败经验结构化，按 bug 类型组织
      3. L3 技能 SOP：成功修复标准操作流程，按 bug 类型组织
      4. E-pool / X-pool：DECENTMEM 双池（成功轨迹 / 候选策略）
      5. RAG 混合检索：BM25 + FAISS + Cross-Encoder 重排

    与现有代码的关系：
      - 复用 dual_pool_memory.DualPoolMemory
      - 复用 enterprise_rag_pipeline.CodeRepairRAGPipeline
    """

    def __init__(
        self,
        agent_id: str = "memory_agent",
        embedding_model=None,
        rag_pipeline: Optional[CodeRepairRAGPipeline] = None,
        dual_pool: Optional[DualPoolMemory] = None,
    ):
        profile = AgentProfile(
            name=agent_id,
            capabilities={AgentCapability.MEMORY_READ, AgentCapability.MEMORY_WRITE},
            description="三层记忆 + RAG + 双池记忆的统一调度层",
            timeout_seconds=10.0,
        )
        super().__init__(agent_id, profile)

        self.embedding_model = embedding_model

        # 双池记忆
        self.dual_pool = dual_pool or DualPoolMemory(
            agent_id=agent_id,
            embedding_model=embedding_model,
        )

        # RAG pipeline（可选，不强制依赖）
        self.rag_pipeline = rag_pipeline
        if rag_pipeline is None:
            try:
                self.rag_pipeline = CodeRepairRAGPipeline()
            except Exception:
                self.rag_pipeline = None

        # 三层记忆（会话级，不持久化）
        self.l1_context: List[Dict] = []  # 当前会话的上下文碎片
        self.l2_failures: List[Dict] = []  # 失败教训
        self.l3_skills: List[Dict] = []    # 成功 SOP

        # 写入质量门槛
        self.write_quality_threshold = 0.5

    # ==================== 核心：检索 ====================

    def run(self, context: SharedContext) -> Dict[str, Any]:
        """检索相关记忆，供 Solver/Reviewer 使用"""
        t0 = time.time()

        # 构造检索请求
        query = MemoryQuery(
            query_text=f"{context.bug_description} {context.buggy_code[:200]}",
            bug_type=self._infer_bug_type(context),
            bug_signature=self._extract_signature(context.buggy_code),
            keywords=self._extract_keywords(context.buggy_code),
            stage="L1",
            top_k=3,
        )

        result = MemoryResult()

        # L1: 当前会话上下文（已经在内存中）
        result.l1_memories = self._retrieve_l1(query)

        # L2: 失败教训（从 DualPool X-pool + 本地 L2）
        result.l2_memories = self._retrieve_l2(query)
        result.x_pool_memories = self._retrieve_x_pool(query)

        # L3: 成功 SOP（从 DualPool E-pool + 本地 L3）
        result.l3_memories = self._retrieve_l3(query)
        result.e_pool_memories = self._retrieve_e_pool(query)

        # RAG: 混合检索（596 条知识库）
        result.rag_results = self._retrieve_rag(query)

        result.retrieval_time_ms = (time.time() - t0) * 1000

        # 写入 SharedContext
        context.memory_output = {
            "retrieval_result": self._to_dict(result),
            "query": {
                "bug_type": query.bug_type,
                "keywords": query.keywords,
            },
        }

        # 更新 L1 上下文
        self._update_l1_context(context)

        return {
            "status": "ok",
            "retrieval_result": result,
            "retrieval_time_ms": result.retrieval_time_ms,
        }

    # ==================== L1 上下文 ====================

    def _retrieve_l1(self, query: MemoryQuery) -> List[Dict]:
        """L1: 当前会话的上下文记忆"""
        if not self.l1_context:
            return []

        # 基于关键词过滤 + 相似度排序
        scored = []
        for piece in self.l1_context:
            score = 0.0
            for kw in query.keywords:
                if kw.lower() in piece.get("content", "").lower():
                    score += 1
            if score > 0 or not query.keywords:
                scored.append((piece, score / max(len(query.keywords), 1)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [p for p, _ in scored[:query.top_k]]

    def _update_l1_context(self, context: SharedContext):
        """每次任务结束后，将成功/失败经验追加到 L1"""
        if context.status == "done" and context.reviewer_output:
            outcome = context.reviewer_output.get("outcome", "unknown")
            self.l1_context.append({
                "task_id": context.task_id,
                "bug_type": self._infer_bug_type(context),
                "content": context.buggy_code[:300],
                "outcome": outcome,
                "timestamp": time.time(),
            })
            # 限制 L1 大小，防止无限增长
            if len(self.l1_context) > 50:
                self.l1_context = self.l1_context[-30:]

    # ==================== L2 失败教训 ====================

    def _retrieve_l2(self, query: MemoryQuery) -> List[Dict]:
        """L2: 从本地失败教训 + X-pool 中检索"""
        results = []

        # 本地 L2 失败教训
        for piece in self.l2_failures:
            if query.bug_type in piece.get("bug_type", ""):
                results.append(piece)

        return results[:query.top_k]

    def record_failure(self, context: SharedContext, failure_reason: str):
        """记录一次失败经验到 L2"""
        piece = {
            "task_id": context.task_id,
            "bug_type": self._infer_bug_type(context),
            "buggy_code": context.buggy_code[:200],
            "failure_reason": failure_reason,
            "round": context.current_round,
            "timestamp": time.time(),
        }
        self.l2_failures.append(piece)
        # 限制大小
        if len(self.l2_failures) > 100:
            self.l2_failures = self.l2_failures[-50:]

    # ==================== L3 成功 SOP ====================

    def _retrieve_l3(self, query: MemoryQuery) -> List[Dict]:
        """L3: 从本地技能 SOP + E-pool 中检索"""
        results = []

        for piece in self.l3_skills:
            if query.bug_type in piece.get("bug_type", ""):
                results.append(piece)

        return results[:query.top_k]

    def extract_skill(self, context: SharedContext) -> Optional[Dict]:
        """从成功案例中提取 SOP 到 L3"""
        if context.reviewer_output and context.reviewer_output.get("outcome") == "success":
            solver_out = context.solver_output or {}
            return {
                "task_id": context.task_id,
                "bug_type": self._infer_bug_type(context),
                "sop_steps": solver_out.get("cot_steps", []),
                "fixed_code_snippet": solver_out.get("fixed_code", "")[:200],
                "quality_score": context.reviewer_output.get("quality_score", 0.0),
                "timestamp": time.time(),
            }
        return None

    # ==================== 双池记忆 ====================

    def _retrieve_e_pool(self, query: MemoryQuery) -> List[Dict]:
        """从 E-pool 检索成功轨迹"""
        pieces = self.dual_pool.retrieve(
            task={
                "task_type": query.bug_type,
                "bug_signature": query.bug_signature,
            },
            stage=query.stage,
            top_k=query.top_k,
            pool_type="E",
        )
        return [self._piece_to_dict(p) for p in pieces]

    def _retrieve_x_pool(self, query: MemoryQuery) -> List[Dict]:
        """从 X-pool 检索候选策略"""
        pieces = self.dual_pool.retrieve(
            task={
                "task_type": query.bug_type,
                "bug_signature": query.bug_signature,
            },
            stage=query.stage,
            top_k=query.top_k,
            pool_type="X",
        )
        return [self._piece_to_dict(p) for p in pieces]

    def write_to_pool(self, context: SharedContext, quality_score: float):
        """
        任务结束后，将轨迹写入双池。

        - 成功 → E-pool
        - 失败但有参考价值 → X-pool
        - 高质量 X-pool → 合并到 E-pool
        """
        if quality_score >= self.write_quality_threshold:
            # 写入 E-pool
            piece = self._build_memory_piece(context, quality_score)
            self.dual_pool.add_to_e_pool(piece)
        elif quality_score >= 0.3:
            # 写入 X-pool（候选策略）
            piece = self._build_memory_piece(context, quality_score)
            self.dual_pool.add_to_x_pool(piece)

    def consolidate_pools(self) -> int:
        """将 X-pool 中高质量碎片合并到 E-pool"""
        return self.dual_pool.consolidate_x_to_e()

    # ==================== RAG ====================

    def _retrieve_rag(self, query: MemoryQuery) -> List[Dict]:
        """从 RAG 知识库检索相似案例"""
        if not self.rag_pipeline:
            return []

        try:
            rag_results = self.rag_pipeline.search(
                query=query.query_text,
                top_k=query.top_k,
                use_rerank=True,
            )
            return [
                {
                    "content": r.chunk.content[:300],
                    "score": r.score,
                    "relevance": r.relevance,
                    "source": r.source,
                }
                for r in rag_results
            ]
        except Exception:
            return []

    # ==================== 辅助方法 ====================

    def _infer_bug_type(self, context: SharedContext) -> str:
        """从代码和描述中推断 bug 类型"""
        code = context.buggy_code
        desc = context.bug_description

        combined = f"{desc} {code}".lower()

        bug_type_map = {
            "off-by-one": ["off-by-one", "off by one", "boundary", "range", "len()"],
            "null-pointer": ["null", "None", "NoneType", "attribute error"],
            "infinite-loop": ["infinite", "loop", "死循环", "timeout", "hang"],
            "logic-error": ["logic", "逻辑", "wrong", "incorrect", "result"],
            "type-error": ["type error", "类型错误", "cannot concat"],
            "index-error": ["index error", "索引错误", "list index", "out of range"],
            "syntax-error": ["syntax", "语法", "expected", "invalid syntax"],
        }

        for bug_type, keywords in bug_type_map.items():
            if any(kw in combined for kw in keywords):
                return bug_type

        return "general_bug"

    def _extract_signature(self, code: str) -> str:
        """提取代码特征签名（用于快速匹配）"""
        lines = [l.strip() for l in code.split("\n") if l.strip() and not l.strip().startswith("#")]
        sig_parts = []
        for line in lines[:5]:
            sig_parts.append(line[:50])
        return " | ".join(sig_parts)

    def _extract_keywords(self, code: str) -> List[str]:
        """提取关键词"""
        import re
        funcs = re.findall(r'def\s+(\w+)', code)
        funcs += re.findall(r'(\w+)\s*\(', code)
        keywords = [f for f in funcs if len(f) > 2 and f not in
                    {"def", "for", "while", "if", "else", "return", "import"}]
        return list(set(keywords))[:10]

    def _piece_to_dict(self, piece: MemoryPiece) -> Dict:
        return {
            "piece_id": piece.piece_id,
            "task_type": piece.task_type,
            "bug_signature": piece.bug_signature,
            "cot_steps": piece.cot_steps,
            "final_fix": piece.final_fix,
            "fix_status": piece.fix_status,
            "judge_scores": piece.judge_scores,
            "execution_pass_rate": piece.execution_pass_rate,
            "quality_score": piece.quality_score(),
        }

    def _build_memory_piece(self, context: SharedContext, quality_score: float) -> MemoryPiece:
        """从 SharedContext 构建 MemoryPiece"""
        solver_out = context.solver_output or {}
        reviewer_out = context.reviewer_output or {}

        return MemoryPiece(
            piece_id=hashlib.md5(context.task_id.encode()).hexdigest()[:12],
            task_type=self._infer_bug_type(context),
            bug_signature=self._extract_signature(context.buggy_code),
            full_buggy_code=context.buggy_code,
            cot_steps=solver_out.get("cot_steps", []),
            final_fix=solver_out.get("fixed_code", ""),
            fix_status=reviewer_out.get("outcome", "unknown"),
            judge_scores=reviewer_out.get("judge_scores", {}),
            execution_pass_rate=reviewer_out.get("test_pass_rate", 0.0),
            source="memory_agent",
            agent_pool="E-pool",
            stage_pool="L1",
        )

    def _to_dict(self, result: MemoryResult) -> Dict:
        return {
            "l1_memories": result.l1_memories,
            "l2_memories": result.l2_memories,
            "l3_memories": result.l3_memories,
            "e_pool_memories": result.e_pool_memories,
            "x_pool_memories": result.x_pool_memories,
            "rag_results": result.rag_results,
            "retrieval_time_ms": result.retrieval_time_ms,
        }

    def pool_stats(self) -> Dict:
        """返回双池统计"""
        return self.dual_pool.pool_stats()
