"""
DECENTMEM-Style Dual-Pool Memory for Code Repair Agent
参考: "Self-Evolving Multi-Agent Systems via Decentralized Memory" (Hao et al., 2026)

核心设计:
- 每个 Agent 维护独立的 E-pool (利用池) 和 X-pool (探索池)
- E-pool: 存储过去成功的代码修复轨迹，检索复用
- X-pool: 当前任务生成的候选修复策略，评估后合并
- 在线路由器: 根据任务相似度动态选择 E-pool 或 X-pool
- LLM-as-a-Judge: stage-level 评估反馈，驱动权重更新
"""

import json
import time
import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple, Any
from collections import defaultdict
import numpy as np

from enterprise_rag_pipeline import CodeRepairRAGPipeline


# ==================== 记忆碎片数据结构 ====================

@dataclass
class MemoryPiece:
    """
    记忆碎片 = (ξ: context prototype, r*: action prototype)
    存储一个代码修复任务的完整轨迹

    对应论文: z = (ξ, r*), 其中:
      - ξ: 任务上下文（bug描述、代码特征、测试用例）
      - r*: 动作原型（CoT推理轨迹 + 修复结果 + 自评注释）
    """
    piece_id: str
    task_type: str  # e.g. "off_by_one", "logic_error"
    bug_signature: str  # 压缩后的 bug 特征（用于快速匹配）
    full_buggy_code: str  # 完整 buggy 代码

    # CoT 推理步骤
    cot_steps: List[Dict]  # [{"step_id": 1, "content": "...", "judge_score": 0.8}, ...]
    final_fix: str  # 最终修复代码
    fix_status: str  # "success" / "partial" / "failed"

    # 质量评估
    judge_scores: Dict[str, float]  # LLM-as-a-judge 各维度评分
    execution_pass_rate: float  # 测试用例通过率

    # 元信息
    source: str  # "successful_fix" / "generated_candidate" / "manual"
    agent_pool: str  # "E-pool" / "X-pool"
    stage_pool: str  # "L1" / "L2" / "L3" / "L4" (对应 CoT 4步)

    # 权重（由路由器维护）
    weight: float = 1.0
    hit_count: int = 0
    last_used: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)

    # 协作轨迹（用于多 Agent 场景）
    cooperation_trace: List[Dict] = field(default_factory=list)  # {"agent": "solver", "action": "..."}
    shared_with: List[str] = field(default_factory=list)  # 分享给哪些 Agent

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "MemoryPiece":
        return cls(**d)

    def relevance_score(self, query: Dict) -> float:
        """
        计算当前记忆碎片与查询任务的相关性
        综合: bug类型匹配 + 代码特征相似 + 时间衰减
        """
        score = 0.0

        # 1. bug 类型匹配 (40%)
        query_type = query.get("task_type", "")
        if query_type == self.task_type:
            score += 0.4
        elif query_type and self.task_type and any(
            t in self.task_type for t in query_type.split()
        ):
            score += 0.2

        # 2. 代码特征匹配 (30%)
        query_sig = query.get("bug_signature", "")
        if query_sig and self.bug_signature:
            # 简单的词重叠匹配
            q_words = set(query_sig.lower().split())
            s_words = set(self.bug_signature.lower().split())
            if q_words and s_words:
                overlap = len(q_words & s_words) / len(q_words | s_words)
                score += 0.3 * overlap

        # 3. 历史命中率 (20%) — 经常被命中的碎片更可信
        if self.hit_count > 0:
            score += 0.2 * min(self.hit_count / 5, 1.0)

        # 4. 时间衰减 (10%) — 半衰期 7 天
        days_elapsed = (time.time() - self.last_used) / 86400
        decay = 0.5 ** (days_elapsed / 7.0)
        score += 0.1 * decay

        return min(score, 1.0)

    def quality_score(self) -> float:
        """
        综合质量分 = LLM-as-a-judge 评分 * 执行通过率
        """
        judge_avg = sum(self.judge_scores.values()) / max(len(self.judge_scores), 1)
        return judge_avg * 0.6 + self.execution_pass_rate * 0.4

    def record_hit(self):
        """记录一次命中"""
        self.hit_count += 1
        self.last_used = time.time()


# ==================== 双池记忆 ====================

class DualPoolMemory:
    """
    去中心化双池记忆
    - E-pool (Exploitation Pool): 成功的代码修复轨迹，检索复用
    - X-pool (Exploration Pool): 当前任务生成的新候选，评估后合并
    - 在线路由器根据权重 w_E / (w_E + w_X) 决定选择哪个池

    对应论文 Section 4.1: Mm = Mm,E-pool ∪ Mm,X-pool
    """

    def __init__(
        self,
        agent_id: str = "codefix_agent",
        e_pool_path: str = "runs/e_pool.json",
        x_pool_path: str = "runs/x_pool.json",
        embedding_model=None,
        similarity_threshold: float = 0.35,
    ):
        self.agent_id = agent_id
        self.e_pool_path = e_pool_path
        self.x_pool_path = x_pool_path
        self.embedding_model = embedding_model
        self.similarity_threshold = similarity_threshold

        # 双池
        self.e_pool: Dict[str, List[MemoryPiece]] = defaultdict(list)  # stage -> pieces
        self.x_pool: Dict[str, List[MemoryPiece]] = defaultdict(list)

        # 路由器权重
        self.w_e = 1.0  # E-pool 权重
        self.w_x = 1.0  # X-pool 权重

        # RAG pipeline（用于 E-pool 的语义检索）
        self.rag_pipeline = None
        if embedding_model:
            self.rag_pipeline = CodeRepairRAGPipeline(embedding_model=embedding_model)

        # 加载已有数据
        self._load_pools()

    # ----- 基础操作 -----

    def _load_pools(self):
        """从磁盘加载记忆池"""
        import os
        os.makedirs("runs", exist_ok=True)

        if os.path.exists(self.e_pool_path):
            with open(self.e_pool_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                for stage, pieces in raw.items():
                    self.e_pool[stage] = [MemoryPiece.from_dict(p) for p in pieces]

        if os.path.exists(self.x_pool_path):
            with open(self.x_pool_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                for stage, pieces in raw.items():
                    self.x_pool[stage] = [MemoryPiece.from_dict(p) for p in pieces]

    def _save_pools(self):
        """持久化记忆池"""
        import os
        os.makedirs("runs", exist_ok=True)

        def pool_to_serializable(pool: Dict) -> Dict:
            return {
                stage: [p.to_dict() for p in pieces]
                for stage, pieces in pool.items()
            }

        with open(self.e_pool_path, "w", encoding="utf-8") as f:
            json.dump(pool_to_serializable(self.e_pool), f, ensure_ascii=False, indent=2)

        with open(self.x_pool_path, "w", encoding="utf-8") as f:
            json.dump(pool_to_serializable(self.x_pool), f, ensure_ascii=False, indent=2)

    # ----- 检索接口 -----

    def retrieve(self, task: Dict, stage: str = "L1",
                 top_k: int = 3, pool_type: str = None) -> List[MemoryPiece]:
        """
        从指定池或路由器自动选择池中检索相关记忆碎片

        Args:
            task: {"buggy_code": "...", "task_type": "...", "bug_signature": "..."}
            stage: CoT 阶段 (L1-L4)
            top_k: 返回前 k 条
            pool_type: 强制指定池 ("E" / "X")，None 则路由器自动选择

        Returns:
            List[MemoryPiece], 按相关性排序
        """
        # 路由器决定用哪个池
        if pool_type is None:
            pool_type = self._router_decide()

        pool = self.e_pool if pool_type == "E" else self.x_pool
        pool_name = "E-pool" if pool_type == "E" else "X-pool"
        pieces = pool.get(stage, [])

        if not pieces:
            return []

        # 计算相关性分数并排序
        scored = [(p, p.relevance_score(task)) for p in pieces]
        scored = [(p, s) for p, s in scored if s >= self.similarity_threshold]
        scored.sort(key=lambda x: x[1], reverse=True)

        results = [p for p, s in scored[:top_k]]
        for p in results:
            p.record_hit()

        return results

    def retrieve_across_stages(
        self, task: Dict, top_k_per_stage: int = 2
    ) -> Dict[str, List[MemoryPiece]]:
        """
        跨阶段检索：从所有阶段获取相关记忆
        用于 LLM-as-a-judge 的协作轨迹评估
        """
        return {
            stage: self.retrieve(task, stage=stage, top_k=top_k_per_stage)
            for stage in ["L1", "L2", "L3", "L4"]
        }

    # ----- 写入接口 -----

    def add_to_e_pool(self, piece: MemoryPiece):
        """向 E-pool 添加成功轨迹"""
        piece.agent_pool = "E-pool"
        piece.weight = 1.0
        stage = piece.stage_pool
        self.e_pool[stage].append(piece)

        # 通过 RAG pipeline 建立向量索引（加速后续检索）
        if self.rag_pipeline:
            self._index_piece_rag(piece)

        self._save_pools()

    def add_to_x_pool(self, piece: MemoryPiece):
        """向 X-pool 添加候选策略"""
        piece.agent_pool = "X-pool"
        piece.weight = 1.0
        stage = piece.stage_pool
        self.x_pool[stage].append(piece)
        self._save_pools()

    def consolidate_x_to_e(self):
        """
        任务完成后：将 X-pool 中高质量碎片合并到 E-pool
        对应论文 Eq.(8): Mm,E-pool ← Mm,E-pool ∪ Mm,X-pool; Mm,X-pool ← ∅

        只有 judge 评分 >= 0.6 的碎片才会被合并
        """
        consolidated_count = 0
        for stage, pieces in list(self.x_pool.items()):
            for p in pieces:
                if p.quality_score() >= 0.6:
                    p.agent_pool = "E-pool"
                    self.e_pool[stage].append(p)
                    consolidated_count += 1

        # 清空 X-pool
        self.x_pool.clear()
        self._save_pools()
        return consolidated_count

    # ----- 路由器 -----

    def _router_decide(self) -> str:
        """
        路由器：根据 E-pool 和 X-pool 权重决定选择哪个池
        对应论文 Eq.(2): α = w_E / (w_E + w_X)

        决策逻辑:
        - E-pool 有高相关记忆 → 优先用 E (exploitation)
        - E-pool 无高相关记忆 → 用 X (exploration)
        - 权重 w 由 LLM-as-a-judge 反馈动态调整
        """
        alpha = self.w_e / (self.w_e + self.w_x)

        # 以概率 alpha 选择 E-pool
        if np.random.random() < alpha:
            return "E"
        else:
            return "X"

    def router_select_pool(self, task: Dict) -> Tuple[str, float]:
        """
        基于任务相似度的智能池选择

        如果 E-pool 有高相关记忆 → 强制用 E
        否则基于权重概率选择
        """
        # 先检查 E-pool 是否有高相关记忆
        e_candidates = self.retrieve(task, stage="L1", top_k=1, pool_type="E")
        if e_candidates and e_candidates[0].relevance_score(task) >= 0.6:
            return "E", 1.0

        # 否则按权重概率
        return self._router_decide(), self.w_e / (self.w_e + self.w_x)

    def update_weights(self, stage: str, used_pool: str, judge_delta: float):
        """
        根据 judge 评估结果更新池权重
        对应论文 Eq.(6)-(7):

        E-pool 被使用:
          - judge 评分提升 (Δ = 1): w_E ← w_E + α
          - judge 评分下降 (Δ = 0): w_E ← max(1.0, β * w_E)

        X-pool 被使用:
          - judge 评分提升 (Δ = 1): w_E ← max(1.0, β * w_E)
          - judge 评分下降 (Δ = 0): w_E ← w_E + α

        其中 α = 0.5, β = 0.5
        """
        alpha = 0.5
        beta = 0.5
        delta = 1.0 if judge_delta > 0 else 0.0

        if used_pool == "E":
            if delta == 1:
                self.w_e = self.w_e + alpha
            else:
                self.w_e = max(1.0, beta * self.w_e)
        else:  # X-pool used
            if delta == 1:
                self.w_e = max(1.0, beta * self.w_e)
            else:
                self.w_e = self.w_e + alpha

        # 打印权重状态
        alpha_pool = self.w_e / (self.w_e + self.w_x)
        print(f"[Router] Pool weights: w_E={self.w_e:.2f}, w_X={self.w_x:.2f}, α={alpha_pool:.3f}")

    # ----- RAG 索引 -----

    def _index_piece_rag(self, piece: MemoryPiece):
        """将碎片通过 RAG pipeline 索引，支持语义检索"""
        if not self.rag_pipeline:
            return

        doc = {
            "content": f"Bug type: {piece.task_type}\n"
                        f"Signature: {piece.bug_signature}\n"
                        f"Code: {piece.full_buggy_code[:200]}\n"
                        f"Fix: {piece.final_fix[:200]}",
            "metadata": {
                "piece_id": piece.piece_id,
                "task_type": piece.task_type,
                "stage": piece.stage_pool,
                "quality": piece.quality_score(),
            }
        }
        self.rag_pipeline.add_documents([doc])

    # ----- 统计信息 -----

    def pool_stats(self) -> Dict:
        """返回双池统计信息"""
        e_total = sum(len(v) for v in self.e_pool.values())
        x_total = sum(len(v) for v in self.x_pool.values())

        e_by_stage = {stage: len(pieces) for stage, pieces in self.e_pool.items()}
        x_by_stage = {stage: len(pieces) for stage, pieces in self.x_pool.items()}

        return {
            "agent_id": self.agent_id,
            "w_E": self.w_e,
            "w_X": self.w_x,
            "alpha": self.w_e / (self.w_e + self.w_x),
            "E_pool_total": e_total,
            "X_pool_total": x_total,
            "E_pool_by_stage": e_by_stage,
            "X_pool_by_stage": x_by_stage,
        }

    def prune_low_quality(self, quality_threshold: float = 0.3):
        """
        定期清理低质量碎片，防止池膨胀
        保留: 命中率 > 0 的 + 高质量碎片
        """
        for stage in list(self.e_pool.keys()):
            original = self.e_pool[stage][:]
            self.e_pool[stage] = [
                p for p in original
                if p.hit_count > 0 or p.quality_score() >= quality_threshold
            ]
            removed = len(original) - len(self.e_pool[stage])
            if removed > 0:
                print(f"[Prune] Removed {removed} low-quality pieces from E-pool/{stage}")

        self._save_pools()

    def import_existing_success(self, existing_fixes: List[Dict]):
        """
        从现有的成功修复历史导入到 E-pool
        用于初始化双池记忆
        """
        count = 0
        for fix_data in existing_fixes:
            piece = MemoryPiece(
                piece_id=hashlib.md5(
                    fix_data.get("buggy_code", "").encode()
                ).hexdigest()[:12],
                task_type=fix_data.get("task_type", "unknown"),
                bug_signature=fix_data.get("bug_signature", ""),
                full_buggy_code=fix_data.get("buggy_code", ""),
                cot_steps=fix_data.get("cot_steps", []),
                final_fix=fix_data.get("fixed_code", ""),
                fix_status="success",
                judge_scores=fix_data.get("judge_scores", {"overall": 0.8}),
                execution_pass_rate=fix_data.get("pass_rate", 1.0),
                source="successful_fix",
                agent_pool="E-pool",
                stage_pool="L1",
                weight=1.0,
            )
            self.add_to_e_pool(piece)
            count += 1

        print(f"[DualPool] Imported {count} existing successful fixes into E-pool")
        return count
