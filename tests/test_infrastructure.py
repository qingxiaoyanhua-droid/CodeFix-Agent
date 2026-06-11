#!/usr/bin/env python3
"""
Unit tests for CodeFix-Agent infrastructure.

Run with: pytest tests/ -v
Install deps: pip install pytest pytest-asyncio

Tests cover:
  - config.py: env loading, CFG singleton, .env override
  - session_manager.py: session lifecycle, checkpoint, metrics, log rotation
  - state_manager.py: serialization, variable management, loop detection
  - audit_log: rotation, parsing
"""

import os
import sys
import json
import time
import tempfile
import shutil
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ==================== Config Tests ====================

class TestConfig:
    """Tests for config.py."""

    def test_cfG_singleton_loads_defaults(self):
        """CFG should load with default values when no .env exists."""
        # Use a fresh import to test defaults
        with patch.dict(os.environ, {}, clear=True):
            # Remove .env if it exists to test defaults
            with patch("pathlib.Path.exists", return_value=False):
                from config import Config, ModelConfig
                cfg = Config()
                assert cfg.model.policy_model == "Qwen/Qwen2.5-Coder-1.5B-Instruct"
                assert cfg.server.port == 8000
                assert cfg.agent.max_iterations == 3
                assert cfg.memory.audit_log_max_bytes == 10 * 1024 * 1024

    def test_env_override(self):
        """Environment variables should override defaults."""
        with patch.dict(os.environ, {
            "SERVER_PORT": "9000",
            "MAX_ITERATIONS": "5",
            "POLICY_MODEL": "custom/model",
        }, clear=False):
            from config import Config
            cfg = Config()
            assert cfg.server.port == 9000
            assert cfg.agent.max_iterations == 5
            assert cfg.model.policy_model == "custom/model"

    def test_dotenv_override(self):
        """A .env file should override defaults but env vars take precedence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("SERVER_PORT=7000\nMAX_ITERATIONS=7\n")

            with patch.dict(os.environ, {}, clear=True):
                os.environ["SERVER_PORT"] = "8888"  # env var wins
                from config import Config
                cfg = Config()
                # Env var should win over .env
                assert cfg.server.port == 8888

    def test_cfG_to_dict(self):
        """CFG.to_dict() should return all sub-configs."""
        from config import CFG
        d = CFG.to_dict()
        assert "model" in d
        assert "server" in d
        assert "memory" in d
        assert "training" in d
        assert "agent" in d
        assert "rag" in d

    def test_env_example_written(self):
        """write_env_example() should produce a valid .env.example."""
        from config import write_env_example, ENV_EXAMPLE
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env.example", delete=False, dir=PROJECT_ROOT) as f:
            fname = f.name
        try:
            write_env_example(fname)
            content = Path(fname).read_text()
            assert "SERVER_PORT" in content
            assert "POLICY_MODEL" in content
            assert "EMBEDDING_MODEL" in content
        finally:
            Path(fname).unlink(missing_ok=True)


# ==================== SessionManager Tests ====================

class TestSessionManager:
    """Tests for session_manager.py."""

    @pytest.fixture
    def sm(self, tmp_path):
        """Fresh SessionManager with temp directories."""
        sys.path.insert(0, str(PROJECT_ROOT))
        # Reload to get a clean instance
        import importlib
        import session_manager
        importlib.reload(session_manager)

        sm = session_manager.SessionManager(
            sessions_dir=str(tmp_path / "sessions"),
            checkpoint_dir=str(tmp_path / "checkpoints"),
            metrics_path=str(tmp_path / "metrics.json"),
            memory_snapshot_dir=str(tmp_path / "snapshots"),
            checkpoint_interval_seconds=1.0,
        )
        return sm

    def test_start_and_end_session(self, sm):
        """start_session / end_session should track active sessions."""
        sid = sm.start_session("test-001", metadata={"bug_type": "off_by_one"})
        assert sid == "test-001"
        assert "test-001" in sm._active_sessions

        sm.end_session("test-001", success=True, metrics={
            "bug_type": "off_by_one",
            "latency_ms": 500.0,
            "iterations": 2,
        })
        assert "test-001" not in sm._active_sessions

    def test_checkpoint_save_and_load(self, sm):
        """Checkpoints should be saveable and recoverable."""
        sm.start_session("test-002")
        sm.save_checkpoint("test-002", iteration=3, state={
            "fixed_code": "def foo(): return 1",
            "current_iteration": 3,
        })

        assert sm.has_incomplete_session("test-002")
        cp = sm.load_checkpoint("test-002")
        assert cp is not None
        assert cp["iteration"] == 3
        assert cp["state"]["fixed_code"] == "def foo(): return 1"

    def test_incomplete_session_detection(self, sm):
        """list_incomplete_sessions should find orphaned checkpoints."""
        sm.start_session("test-003")
        sm.save_checkpoint("test-003", iteration=1, state={"step": 1})

        # Manually simulate a crash by not calling end_session
        sm._active_sessions.pop("test-003", None)

        incomplete = sm.list_incomplete_sessions()
        assert any(s["session_id"] == "test-003" for s in incomplete)

    def test_metrics_recording(self, sm):
        """Business metrics should accumulate correctly."""
        sm.record_request(
            bug_type="wrong_operator",
            success=True,
            latency_ms=300.0,
            iterations=2,
            l2_hit=True,
            l3_hit=False,
            rag_hit=True,
            e_pool_hit=False,
            x_pool_hit=False,
        )
        sm.record_request(
            bug_type="off_by_one",
            success=False,
            latency_ms=800.0,
            iterations=3,
            l2_hit=False,
            l3_hit=False,
            rag_hit=False,
            e_pool_hit=False,
            x_pool_hit=False,
        )

        m = sm.get_metrics()
        assert m["total_requests"] == 2
        assert m["total_success"] == 1
        assert m["total_failure"] == 1
        assert m["bug_type_counts"]["wrong_operator"] == 1
        assert m["bug_type_counts"]["off_by_one"] == 1
        assert m["bug_type_success"]["wrong_operator"] == 1
        assert m["l2_reflection_hits"] == 1
        assert m["rag_hits"] == 1
        assert m["p50_latency_ms"] > 0

    def test_metrics_persistence(self, sm, tmp_path):
        """Metrics should survive a SessionManager restart."""
        sm.record_request("type_conv", True, 200.0, 1, False, False, False, False, False)
        sm._save_metrics()

        # Reload
        import importlib
        import session_manager
        importlib.reload(session_manager)
        sm2 = session_manager.SessionManager(
            sessions_dir=str(tmp_path / "sessions2"),
            checkpoint_dir=str(tmp_path / "checkpoints2"),
            metrics_path=str(tmp_path / "metrics.json"),
            memory_snapshot_dir=str(tmp_path / "snapshots2"),
        )
        assert sm2.get_metrics()["total_requests"] == 1

    def test_concurrent_sessions(self, sm):
        """Multiple sessions should not interfere."""
        results = {}

        def run_session(sid):
            sm.start_session(sid)
            sm.save_checkpoint(sid, iteration=1, state={"n": sid})
            time.sleep(0.05)
            sm.save_checkpoint(sid, iteration=2, state={"n": sid, "done": True})
            sm.end_session(sid, success=True, metrics={
                "bug_type": sid,
                "latency_ms": 100.0,
                "iterations": 2,
            })
            cp = sm.load_checkpoint(sid)
            results[sid] = cp["state"]["done"]

        threads = [threading.Thread(target=run_session, args=(f"sid-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results.values()), f"Expected all True, got {results}"

    def test_should_checkpoint(self, sm):
        """should_checkpoint should respect interval."""
        sid = sm.start_session("test-checkpoint")
        assert sm.should_checkpoint(sid)  # First call

        # Immediately after, should not need checkpoint
        assert not sm.should_checkpoint(sid)


# ==================== StateManager Tests ====================

class TestStateManager:
    """Tests for state_manager.py."""

    @pytest.fixture
    def sm(self):
        sys.path.insert(0, str(PROJECT_ROOT))
        import importlib
        import state_manager
        importlib.reload(state_manager)
        return state_manager.StateManager(task_id="test-task")

    def test_add_and_complete_step(self, sm):
        """Steps should be trackable with timing."""
        step = sm.add_step("compile", "CodeExecutor", {"code": "x=1"})
        sm.complete_step(step.step_id, "OK", 50.0)

        assert len(sm.steps) == 1
        assert sm.steps[0].status == sm.StepStatus.SUCCESS
        assert sm.steps[0].output.duration_ms == 50.0

    def test_fail_and_retry(self, sm):
        """Failed steps should be retryable up to max_retries."""
        step = sm.add_step("test", "TestRunner", {})
        sm.fail_step(step.step_id, "Test failed", can_retry=True)
        assert sm.steps[0].retry_count == 1
        assert sm.should_retry(step.step_id)

        sm.fail_step(step.step_id, "Test failed again", can_retry=True)
        assert sm.steps[0].retry_count == 2
        assert sm.should_retry(step.step_id)

        sm.fail_step(step.step_id, "Test failed final", can_retry=True)
        assert not sm.should_retry(step.step_id)

    def test_variable_lifecycle(self, sm):
        """Variables should be settable and retrievable."""
        sm.add_step("step1", "tool1", {})
        sm.set_var("result", 42, step_id=1)
        assert sm.get_var("result") is not None
        assert sm.get_var("result").value == 42

        # Update
        sm.set_var("result", 100, step_id=1)
        assert sm.get_var("result").value == 100

    def test_loop_detection(self, sm):
        """LoopDetector should catch repeated tool calls."""
        ld = sm.loop_detector
        ld.record("CodeExecutor", "result-A", 1)
        ld.record("CodeExecutor", "result-B", 2)
        is_loop, _ = ld.check("CodeExecutor")
        assert not is_loop

        # Exceed threshold
        ld.record("CodeExecutor", "result-C", 3)
        ld.record("CodeExecutor", "result-D", 4)
        is_loop, msg = ld.check("CodeExecutor")
        assert is_loop
        assert "CodeExecutor" in msg

    def test_serialization_roundtrip(self, sm):
        """StateManager should survive JSON serialize/deserialize."""
        sm.add_step("step1", "tool1", {"key": "val"})
        sm.set_var("x", 10, step_id=1)
        sm.complete_step(1, "ok", 100.0)

        json_str = sm.to_json()
        restored = sm.from_json(json_str)

        assert restored.task_id == sm.task_id
        assert len(restored.steps) == len(sm.steps)

    def test_context_summary_format(self, sm):
        """build_context_summary should produce readable output."""
        sm.add_step("compile", "CodeExecutor", {"code": "x=1"})
        sm.complete_step(1, "OK", 50.0)

        summary = sm.build_context_summary()
        assert "Task:" in summary
        assert "Step:" in summary
        assert "compile" in summary
        assert "OK" in summary

    def test_context_compression(self, sm):
        """More than MAX_TRACE_LENGTH steps should be compressed."""
        sm.MAX_TRACE_LENGTH = 5
        for i in range(10):
            step = sm.add_step(f"step{i}", "tool", {})
            sm.complete_step(step.step_id, f"result{i}", 0)

        recent = sm._get_recent_steps()
        # Compressed view should have fewer than 10 steps
        assert len(recent) < 10
        # But should include the most recent ones
        assert recent[-1].name == "step9"


# ==================== Audit Log Rotation Tests ====================

class TestAuditLogger:
    """Tests for audit log rotation."""

    @pytest.fixture
    def audit_logger(self, tmp_path):
        sys.path.insert(0, str(PROJECT_ROOT))
        import importlib
        # We test rotation logic directly
        return RotatingAuditLogger(
            log_dir=str(tmp_path / "logs"),
            max_bytes=200,
            max_lines=5,
            backup_count=3,
        )

    def test_write_and_read(self, audit_logger):
        """Entries should be writeable and readable."""
        audit_logger.log(request_id="req1", method="POST", path="/fix",
                         status_code=200, latency_ms=150.0)

        summary = audit_logger.summary()
        assert summary["total"] == 1
        assert summary["errors"] == 0

    def test_line_rotation(self, audit_logger):
        """File should rotate after max_lines."""
        for i in range(8):
            audit_logger.log(request_id=f"req{i}", method="POST", path="/fix",
                             status_code=200, latency_ms=100.0)

        # Should have rotated: original + .1
        files = list(audit_logger.log_dir.glob("audit_*.jsonl"))
        assert len(files) >= 2

        # Original should have recent entries
        summary = audit_logger.summary()
        assert summary["total"] == 8

    def test_size_rotation(self, audit_logger):
        """File should rotate after max_bytes."""
        large_content = "x" * 100
        for i in range(5):
            audit_logger.log(request_id=f"req{i}", method="POST", path="/fix",
                             status_code=200, latency_ms=100.0,
                             extra_field=large_content)

        files = list(audit_logger.log_dir.glob("audit_*.jsonl"))
        assert len(files) >= 2

    def test_summary_all(self, audit_logger):
        """summary_all should aggregate across files."""
        for i in range(3):
            audit_logger.log(request_id=f"req{i}", method="POST", path="/fix",
                             status_code=200, latency_ms=100.0)

        result = audit_logger.summary_all()
        assert result["total_requests"] == 3
        assert result["files_scanned"] >= 1


# ==================== APIKeyStore Tests ====================

class TestAPIKeyStore:
    """Tests for persistent APIKeyStore."""

    @pytest.fixture
    def store(self, tmp_path):
        sys.path.insert(0, str(PROJECT_ROOT))
        import importlib
        import api_server
        importlib.reload(api_server)
        return api_server.APIKeyStore(keys_path=str(tmp_path / "keys.json"))

    def test_add_and_verify(self, store):
        """Keys should be addable and verifiable."""
        key = store.add_key("alice", quota_per_day=10)
        assert key.startswith("cfx_")

        entry = store.verify(key)
        assert entry is not None
        assert entry["owner"] == "alice"

    def test_quota_enforcement(self, store):
        """Quota should be enforced."""
        key = store.add_key("bob", quota_per_day=2)

        store.verify(key)
        store.verify(key)
        # Third attempt should fail (quota exhausted)
        assert store.verify(key) is None

    def test_revocation(self, store):
        """Revoked keys should fail verification."""
        key = store.add_key("carol")
        assert store.verify(key) is not None
        store.revoke(key)
        assert store.verify(key) is None

    def test_persistence(self, store, tmp_path):
        """Keys should survive a store restart."""
        key = store.add_key("dave")
        store._save()

        import importlib
        import api_server
        importlib.reload(api_server)
        store2 = api_server.APIKeyStore(keys_path=str(tmp_path / "keys.json"))
        assert store2.verify(key) is not None


# ==================== Rate Limiter Tests ====================

class TestRateLimiter:
    """Tests for SlidingWindowRateLimiter."""

    @pytest.fixture
    def limiter(self):
        sys.path.insert(0, str(PROJECT_ROOT))
        import importlib
        import api_server
        importlib.reload(api_server)
        return api_server.SlidingWindowRateLimiter(max_requests=3, window_seconds=60)

    def test_allows_under_limit(self, limiter):
        allowed, remaining = limiter.check("user1")
        assert allowed
        assert remaining == 2

    def test_blocks_over_limit(self, limiter):
        """Should block after max_requests."""
        for _ in range(3):
            limiter.check("user2")
        allowed, remaining = limiter.check("user2")
        assert not allowed

    def test_retry_after(self, limiter):
        """get_retry_after should return positive when limited."""
        for _ in range(3):
            limiter.check("user3")
        retry_after = limiter.get_retry_after("user3")
        assert retry_after > 0


# ==================== Helpers ====================

class RotatingAuditLogger:
    """Standalone audit logger for testing rotation without FastAPI."""

    def __init__(self, log_dir, max_bytes=10 * 1024 * 1024, max_lines=100000, backup_count=5):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.backup_count = backup_count

    def _current_path(self):
        return self.log_dir / f"audit_{time.strftime('%Y-%m-%d')}.jsonl"

    def _should_rotate(self, path):
        if not path.exists():
            return False
        if path.stat().st_size >= self.max_bytes:
            return True
        try:
            with open(path, encoding="utf-8") as f:
                if sum(1 for _ in f) >= self.max_lines:
                    return True
        except (OSError, IOError):
            pass
        return False

    def _rotate(self, path):
        if not path.exists():
            return
        oldest = path.parent / f"{path.stem}.{self.backup_count}{path.suffix}"
        if oldest.exists():
            oldest.unlink()
        for i in range(self.backup_count - 1, 0, -1):
            src = path.parent / f"{path.stem}.{i}{path.suffix}"
            dst = path.parent / f"{path.stem}.{i + 1}{path.suffix}"
            if src.exists():
                shutil.move(str(src), str(dst))
        shutil.move(str(path), str(path.parent / f"{path.stem}.1{path.suffix}"))

    def log(self, request_id, method, path, status_code, latency_ms, **extra):
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "request_id": request_id,
            "method": method,
            "path": path,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2),
            **extra,
        }
        log_path = self._current_path()
        if self._should_rotate(log_path):
            self._rotate(log_path)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def summary(self):
        fname = self._current_path()
        if not fname.exists():
            return {}
        total = errors = 0
        latencies = []
        try:
            with open(fname, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    if rec.get("error"):
                        errors += 1
                    lat = rec.get("latency_ms", 0)
                    if lat > 0:
                        latencies.append(lat)
        except Exception:
            pass
        return {"total": total, "errors": errors, "latencies": latencies}

    def summary_all(self):
        total = errors = 0
        files = 0
        for _ in self.log_dir.glob("audit_*.jsonl"):
            files += 1
        return {"total_requests": total, "files_scanned": files}


# ==================== RedisSessionStore Tests ====================

class TestRedisSessionStore:
    """Tests for RedisSessionStore key layout and fallback."""

    def test_key_layout(self):
        """Redis keys should follow expected naming conventions."""
        import tempfile
        from pathlib import Path

        # Patch _get_redis_client so Redis is unavailable, then build store
        with patch("src.core.cot_react_agent._get_redis_client",
                   return_value=(None, "mock unavailable")):
            # Reload so patch takes effect
            from src.core import cot_react_agent
            import importlib
            importlib.reload(cot_react_agent)

            from src.core.cot_react_agent import RedisSessionStore
            with tempfile.TemporaryDirectory() as tmp:
                store = RedisSessionStore(
                    redis_url="redis://localhost:6379/0",
                    namespace="test-ns",
                    fallback_sessions_dir=tmp,
                )

                # Verify key structure
                assert store._ns_prefix() == "session:test-ns"
                assert store._event_key("sid-001") == "session:test-ns:sid-001"
                assert store._meta_key("sid-001") == "session:test-ns:sid-001:meta"
                assert store._state_key("sid-001") == "session:test-ns:sid-001:state"
                assert store._idx_key() == "session:test-ns:idx"

    def test_fallback_to_file_when_redis_unavailable(self):
        """RedisSessionStore should delegate to file store when Redis is unreachable."""
        import tempfile
        from pathlib import Path

        with patch("src.core.cot_react_agent._get_redis_client",
                   return_value=(None, "mock unavailable")):
            from src.core import cot_react_agent
            import importlib
            importlib.reload(cot_react_agent)

            from src.core.cot_react_agent import RedisSessionStore
            with tempfile.TemporaryDirectory() as tmp:
                store = RedisSessionStore(
                    redis_url="redis://invalid-host:9999",
                    namespace="default",
                    fallback_sessions_dir=tmp,
                )

                # Should have fallen back silently
                assert store._redis is None
                assert store._fallback is not None

                # Operations should still work via fallback
                sid = store.create_session("fallback-test", metadata={"note": "via fallback"})
                assert sid == "fallback-test"

                store.append_event("fallback-test", {"type": "ping"})
                events = store.load_session("fallback-test")
                assert len(events) == 1

                sessions = store.list_sessions()
                assert any(s["session_id"] == "fallback-test" for s in sessions)

    def test_redis_client_lazy_init(self):
        """Redis client should not be created until first use."""
        import tempfile

        with patch("src.core.cot_react_agent._get_redis_client",
                   return_value=(None, "mock unavailable")):
            from src.core import cot_react_agent
            import importlib
            importlib.reload(cot_react_agent)

            # Access _redis module-level variable before and after __init__
            prev = cot_react_agent._redis
            cot_react_agent._redis = None  # reset module-level cache

            try:
                with tempfile.TemporaryDirectory() as tmp:
                    # __init__ should NOT connect — _redis stays None
                    from src.core.cot_react_agent import RedisSessionStore
                    _ = RedisSessionStore(
                        redis_url="redis://localhost:6379/0",
                        namespace="default",
                        fallback_sessions_dir=tmp,
                    )
                    assert cot_react_agent._redis is None
            finally:
                cot_react_agent._redis = prev  # restore
