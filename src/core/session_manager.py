#!/usr/bin/env python3
"""
SessionManager: Session Persistence, Recovery & Metrics
======================================================
Wraps SessionStore with:
  - Periodic checkpointing (every N iterations or time interval)
  - Session recovery (resume after crash/interrupt)
  - Business metrics collection (bug types, memory hit rates, latency)
  - Periodic memory checkpoint (L2/L3/e-pool/x-pool snapshots)

Usage:
    from session_manager import SessionManager

    sm = SessionManager()
    sm.start_session("bug-123", metadata={"bug_type": "off_by_one"})
    sm.save_checkpoint(iteration=1, state={"fixed_code": "..."})
    sm.end_session(success=True)

    # Recovery
    if sm.has_incomplete_session("bug-123"):
        state = sm.load_checkpoint("bug-123")
"""

from __future__ import annotations

import json
import os
import time
import hashlib
import logging
import shutil
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
from threading import Lock
from collections import defaultdict

logger = logging.getLogger(__name__)


# ==================== Metrics ====================

@dataclass
class BusinessMetrics:
    """Collected business-level metrics."""
    total_requests: int = 0
    total_success: int = 0
    total_failure: int = 0
    total_sessions: int = 0
    incomplete_sessions: int = 0

    bug_type_counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    bug_type_success: Dict[str, int] = field(default_factory=lambda: defaultdict(int))

    # Memory effectiveness
    l2_reflection_hits: int = 0
    l3_skill_hits: int = 0
    rag_hits: int = 0
    decentmem_e_pool_hits: int = 0
    decentmem_x_pool_hits: int = 0

    # Latency
    total_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    latency_history: List[float] = field(default_factory=list)

    # Iterations
    avg_iterations: float = 0.0
    iterations_history: List[int] = field(default_factory=list)

    def record_request(self, bug_type: str, success: bool, latency_ms: float,
                      iterations: int, l2_hit: bool, l3_hit: bool, rag_hit: bool,
                      e_pool_hit: bool, x_pool_hit: bool):
        self.total_requests += 1
        self.total_sessions += 1
        if success:
            self.total_success += 1
            self.bug_type_success[bug_type] += 1
        else:
            self.total_failure += 1
        self.bug_type_counts[bug_type] += 1

        if l2_hit:
            self.l2_reflection_hits += 1
        if l3_hit:
            self.l3_skill_hits += 1
        if rag_hit:
            self.rag_hits += 1
        if e_pool_hit:
            self.decentmem_e_pool_hits += 1
        if x_pool_hit:
            self.decentmem_x_pool_hits += 1

        self.total_latency_ms += latency_ms
        self.latency_history.append(latency_ms)
        self.iterations_history.append(iterations)

        self.avg_latency_ms = self.total_latency_ms / max(self.total_requests, 1)
        self.avg_iterations = sum(self.iterations_history) / max(len(self.iterations_history), 1)

        if self.latency_history:
            sorted_lat = sorted(self.latency_history)
            n = len(sorted_lat)
            self.p50_latency_ms = sorted_lat[n // 2]
            self.p95_latency_ms = sorted_lat[int(n * 0.95)] if n >= 20 else sorted_lat[-1]

    def record_incomplete(self):
        self.incomplete_sessions += 1

    def to_dict(self) -> Dict:
        d = asdict(self)
        # Convert defaultdicts for JSON serialization
        d["bug_type_counts"] = dict(d["bug_type_counts"])
        d["bug_type_success"] = dict(d["bug_type_success"])
        return d


# ==================== SessionManager ====================

class SessionManager:
    """
    High-level session lifecycle manager with persistence and metrics.

    Features:
      - start / end / resume session lifecycle
      - Periodic checkpoint during long sessions
      - Recovery of interrupted sessions
      - Business metrics collection
      - Periodic memory snapshots
    """

    def __init__(
        self,
        sessions_dir: str = "./runs/sessions",
        checkpoint_dir: str = "./runs/checkpoints",
        metrics_path: str = "./runs/metrics.json",
        memory_snapshot_dir: str = "./runs/memory_snapshots",
        checkpoint_interval_seconds: float = 60.0,
        memory_snapshot_interval_seconds: float = 300.0,
    ):
        self.sessions_dir = Path(sessions_dir)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.metrics_path = Path(metrics_path)
        self.memory_snapshot_dir = Path(memory_snapshot_dir)
        self.checkpoint_interval = checkpoint_interval_seconds
        self.memory_snapshot_interval = memory_snapshot_interval_seconds

        for d in [self.sessions_dir, self.checkpoint_dir, self.memory_snapshot_dir]:
            d.mkdir(parents=True, exist_ok=True)

        self._lock = Lock()
        self._active_sessions: Dict[str, Dict] = {}
        self._last_checkpoint: Dict[str, float] = {}
        self._last_memory_snapshot: float = 0.0

        self._metrics = BusinessMetrics()
        self._load_metrics()

        logger.info(
            f"[SessionManager] Initialized. "
            f"sessions={sessions_dir}, checkpoints={checkpoint_dir}"
        )

    # ----- Session Lifecycle -----

    def start_session(self, session_id: str, metadata: Dict = None) -> str:
        """Mark a session as active. Returns the session_id."""
        with self._lock:
            self._active_sessions[session_id] = {
                "session_id": session_id,
                "started_at": datetime.now().isoformat(),
                "last_checkpoint_at": time.time(),
                "metadata": metadata or {},
                "checkpoints": 0,
                "state": {},
            }
            self._last_checkpoint[session_id] = time.time()
        logger.info(f"[SessionManager] Session started: {session_id}")
        return session_id

    def save_checkpoint(self, session_id: str, iteration: int,
                       state: Dict, metadata: Dict = None) -> None:
        """
        Save a checkpoint for an active session.
        Called periodically during long agent runs.
        """
        with self._lock:
            if session_id not in self._active_sessions:
                logger.warning(f"[SessionManager] No active session {session_id}, creating")
                self.start_session(session_id, metadata)

            entry = self._active_sessions[session_id]
            now = time.time()

            checkpoint = {
                "session_id": session_id,
                "iteration": iteration,
                "checkpoint_at": datetime.now().isoformat(),
                "elapsed_seconds": now - time.mktime(
                    datetime.fromisoformat(entry["started_at"]).timetuple()
                ),
                "state": state,
                "metadata": metadata or entry.get("metadata", {}),
            }

            # Write checkpoint file (overwritable)
            cp_path = self.checkpoint_dir / f"{session_id}.checkpoint.json"
            with open(cp_path, "w", encoding="utf-8") as f:
                json.dump(checkpoint, f, ensure_ascii=False, indent=2)

            entry["last_checkpoint_at"] = now
            entry["checkpoints"] += 1
            self._last_checkpoint[session_id] = now

            # Periodic full memory snapshot
            if now - self._last_memory_snapshot >= self.memory_snapshot_interval:
                self._snapshot_memory()
                self._last_memory_snapshot = now

    def _snapshot_memory(self):
        """Take a snapshot of all memory layers for recovery."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_dir = self.memory_snapshot_dir / ts
        snap_dir.mkdir(parents=True, exist_ok=True)

        files_to_copy = [
            (Path("./runs/reflection_memory.json"), "reflection_memory.json"),
            (Path("./runs/skills/skills.json"), "skills.json"),
            (Path("./runs/e_pool.json"), "e_pool.json"),
            (Path("./runs/x_pool.json"), "x_pool.json"),
        ]

        count = 0
        for src, name in files_to_copy:
            if src.exists():
                shutil.copy2(src, snap_dir / name)
                count += 1

        # Write snapshot manifest
        manifest = {
            "snapshot_at": datetime.now().isoformat(),
            "files": [name for _, name in files_to_copy],
            "active_sessions": list(self._active_sessions.keys()),
        }
        with open(snap_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False)

        # Keep only last 10 snapshots
        self._prune_snapshots(keep=10)

        logger.info(f"[SessionManager] Memory snapshot saved to {snap_dir} ({count} files)")

    def _prune_snapshots(self, keep: int = 10):
        """Remove old memory snapshots, keeping only the most recent N."""
        if not self.memory_snapshot_dir.exists():
            return
        snaps = sorted(
            self.memory_snapshot_dir.iterdir(),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for old in snaps[keep:]:
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()

    def end_session(self, session_id: str, success: bool,
                    metrics: Dict = None) -> None:
        """
        Mark a session as completed and record metrics.
        Removes the active session entry and persists final state.
        """
        with self._lock:
            if session_id not in self._active_sessions:
                logger.warning(f"[SessionManager] Session {session_id} not found")
                return

            entry = self._active_sessions[session_id]
            duration_s = time.time() - time.mktime(
                datetime.fromisoformat(entry["started_at"]).timetuple()
            )

            # Record metrics
            m = metrics or {}
            self._metrics.record_request(
                bug_type=m.get("bug_type", "unknown"),
                success=success,
                latency_ms=m.get("latency_ms", 0),
                iterations=m.get("iterations", 0),
                l2_hit=m.get("used_past_reflections", 0) > 0,
                l3_hit=m.get("used_skills", []) not in ([], None),
                rag_hit=m.get("rag_hit", False),
                e_pool_hit=m.get("e_pool_hit", False),
                x_pool_hit=m.get("x_pool_hit", False),
            )

            # Clean up
            cp_path = self.checkpoint_dir / f"{session_id}.checkpoint.json"
            if cp_path.exists():
                cp_path.unlink()

            del self._active_sessions[session_id]
            self._last_checkpoint.pop(session_id, None)

            self._save_metrics()

            logger.info(
                f"[SessionManager] Session {session_id} ended "
                f"(success={success}, duration={duration_s:.1f}s)"
            )

    def end_incomplete_sessions(self):
        """Mark any active sessions that weren't properly closed as incomplete."""
        with self._lock:
            count = 0
            for sid in list(self._active_sessions.keys()):
                self._metrics.record_incomplete()
                count += 1
            if count:
                self._save_metrics()
                logger.info(f"[SessionManager] Marked {count} incomplete sessions")
            return count

    # ----- Recovery -----

    def has_incomplete_session(self, session_id: str) -> bool:
        """Check if a checkpoint exists for recovery."""
        cp_path = self.checkpoint_dir / f"{session_id}.checkpoint.json"
        return cp_path.exists()

    def load_checkpoint(self, session_id: str) -> Optional[Dict]:
        """Load the latest checkpoint for a session."""
        cp_path = self.checkpoint_dir / f"{session_id}.checkpoint.json"
        if not cp_path.exists():
            return None
        try:
            with open(cp_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"[SessionManager] Failed to load checkpoint {session_id}: {e}")
            return None

    def list_incomplete_sessions(self) -> List[Dict]:
        """List all sessions with recoverable checkpoints."""
        results = []
        for cp_file in self.checkpoint_dir.glob("*.checkpoint.json"):
            try:
                with open(cp_file, encoding="utf-8") as f:
                    data = json.load(f)
                    results.append({
                        "session_id": data.get("session_id", cp_file.stem.replace(".checkpoint", "")),
                        "iteration": data.get("iteration", -1),
                        "checkpoint_at": data.get("checkpoint_at"),
                        "elapsed_seconds": data.get("elapsed_seconds", 0),
                    })
            except Exception:
                continue
        return sorted(results, key=lambda x: x.get("checkpoint_at", ""), reverse=True)

    # ----- Metrics -----

    def _load_metrics(self):
        if self.metrics_path.exists():
            try:
                with open(self.metrics_path, encoding="utf-8") as f:
                    data = json.load(f)
                self._metrics = BusinessMetrics(
                    total_requests=data.get("total_requests", 0),
                    total_success=data.get("total_success", 0),
                    total_failure=data.get("total_failure", 0),
                    total_sessions=data.get("total_sessions", 0),
                    incomplete_sessions=data.get("incomplete_sessions", 0),
                    bug_type_counts=defaultdict(int, data.get("bug_type_counts", {})),
                    bug_type_success=defaultdict(int, data.get("bug_type_success", {})),
                    l2_reflection_hits=data.get("l2_reflection_hits", 0),
                    l3_skill_hits=data.get("l3_skill_hits", 0),
                    rag_hits=data.get("rag_hits", 0),
                    decentmem_e_pool_hits=data.get("decentmem_e_pool_hits", 0),
                    decentmem_x_pool_hits=data.get("decentmem_x_pool_hits", 0),
                    total_latency_ms=data.get("total_latency_ms", 0.0),
                    avg_latency_ms=data.get("avg_latency_ms", 0.0),
                    p50_latency_ms=data.get("p50_latency_ms", 0.0),
                    p95_latency_ms=data.get("p95_latency_ms", 0.0),
                )
                logger.info(f"[SessionManager] Loaded metrics from {self.metrics_path}")
            except Exception as e:
                logger.warning(f"[SessionManager] Failed to load metrics: {e}")

    def _save_metrics(self):
        self.metrics_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.metrics_path, "w", encoding="utf-8") as f:
                json.dump(self._metrics.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[SessionManager] Failed to save metrics: {e}")

    def get_metrics(self) -> Dict:
        """Return current metrics snapshot."""
        return self._metrics.to_dict()

    def record_bug_type(self, bug_type: str, success: bool):
        """Record a result for a specific bug type."""
        self._metrics.bug_type_counts[bug_type] += 1
        if success:
            self._metrics.bug_type_success[bug_type] += 1

    def should_checkpoint(self, session_id: str) -> bool:
        """Check if enough time has passed since last checkpoint."""
        if session_id not in self._last_checkpoint:
            return True
        return time.time() - self._last_checkpoint[session_id] >= self.checkpoint_interval

    # ----- Session Store Bridge -----

    def get_session_events(self, session_id: str) -> List[Dict]:
        """Load events from the underlying SessionStore (JSONL file)."""
        import sys
        from pathlib import Path as P

        session_file = self.sessions_dir / f"{session_id}.jsonl"
        if not session_file.exists():
            return []

        events = []
        try:
            with open(session_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except IOError as e:
            logger.warning(f"[SessionManager] Failed to load session {session_id}: {e}")
        return events

    def get_session_meta(self, session_id: str) -> Optional[Dict]:
        """Load session metadata."""
        meta_file = self.sessions_dir / f"{session_id}.meta.json"
        if not meta_file.exists():
            return None
        try:
            with open(meta_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
