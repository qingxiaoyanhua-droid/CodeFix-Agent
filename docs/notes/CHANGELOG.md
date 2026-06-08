# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added

- **`config.py`** - Unified configuration management.
  - All settings loaded from environment variables with `.env` file support.
  - Priority: env var > `.env` > default value.
  - Six sub-configs: `model`, `server`, `memory`, `training`, `agent`, `rag`.
  - `CFG.to_dict()` for API responses and debugging.
  - `write_env_example()` generates `.env.example`.
  - `GRPO_CODEFIX_TRAIN_COT_PRM.py` now uses `CFG` instead of hardcoded values.

- **`session_manager.py`** - Session lifecycle, persistence, and metrics.
  - `start_session()` / `end_session()` with session tracking.
  - `save_checkpoint()` / `load_checkpoint()` for crash recovery.
  - `list_incomplete_sessions()` to find orphaned checkpoints.
  - Periodic memory snapshots (L2/L3/e-pool/x-pool) every 5 minutes.
  - `BusinessMetrics` dataclass: request counts, pass rate, latency percentiles (p50/p95), bug-type breakdown, memory hit rates.
  - Metrics persisted to `runs/checkpoints/metrics.json`.
  - Last 10 memory snapshots retained, older ones pruned.
  - Thread-safe with `Lock`.

- **`tests/test_infrastructure.py`** - Unit tests covering:
  - `config.py`: env override, `.env` override, CFG singleton, `to_dict()`.
  - `session_manager.py`: lifecycle, checkpoint save/load, incomplete detection, metrics accumulation, metrics persistence, concurrent sessions.
  - `state_manager.py`: step tracking, retry logic, variable lifecycle, loop detection, JSON serialization, context compression.
  - Audit log: write/read, line rotation, size rotation, `summary_all()` aggregation.
  - `APIKeyStore`: add/verify, quota enforcement, revocation, persistence.
  - `SlidingWindowRateLimiter`: under-limit allowance, over-limit blocking, retry-after.

- **`api_server.py`** - Updated with persistence and observability:
  - `APIKeyStore` now backed by `runs/api_keys.json` (persistent across restarts).
  - `AuditLogger` with file rotation: triggered by size (`--audit-max-bytes`) or line count (`--audit-max-lines`), keeps 5 backups.
  - `summary_all()` aggregates across all audit log files.
  - `SessionManager` initialized on startup; stale sessions from previous runs marked as incomplete.
  - `/fix` endpoint now records session start/end and metrics via `SessionManager`.
  - New endpoints: `GET /sessions` (list incomplete sessions), `GET /sessions/{id}` (full detail), `GET /sessions/{id}/resume` (checkpoint recovery), `GET /metrics` (business metrics), `GET /config` (server config).
  - CLI args now read from `CFG` defaults; new flags: `--audit-max-bytes`, `--audit-max-lines`.
  - `lifespan` hook saves metrics on shutdown.

### Changed

- `grpo_codefix_train_cot_prm.py`: Hardcoded `POLICY_MODEL`, `REWARD_MODEL`, `SFT_LORA_PATH`, `OUTPUT_DIR` replaced with `CFG.model.*` and `CFG.training.*`.
- `README.md`: Added "完整持久化与可观测性" feature, updated architecture diagram, added configuration section with `.env` usage, added pytest installation instructions.

### Fixed

- `api_server.py`: `session_store` is now properly initialized and passed to `CoTReActAgent` (was previously `None`).

---

## [1.0.0] - 2026-05-20

### Added

- `cot_react_agent.py`: CoT + ReAct agent with three-layer memory (L1/L2/L3)
- `ast_code_processor.py`: Byte-offset AST-based code replacement
- `process_reward_model.py`: 6-dimensional reward system with PRM
- `enterprise_rag_pipeline.py`: BM25 + vector hybrid search with RRF
- `dual_pool_memory.py`: DECENTMEM E-pool / X-pool implementation
- `online_router.py`: Dynamic pool selection router
- `llm_judge.py`: LLM-as-a-Judge stage evaluation
- `grpo_codefix_train_cot_prm.py`: GRPO training with DAPO-lite improvements
- `api_server.py`: FastAPI server with auth, rate limiting, audit logging
- `eval_benchmark.py`: QuixBugs pass@k evaluation
