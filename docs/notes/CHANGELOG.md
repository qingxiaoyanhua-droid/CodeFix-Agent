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
  - **New**: `llm` sub-config (LLM Router settings: circuit breaker thresholds, backoff params, token tracker path).
  - **New**: `cost` sub-config (cost tracking settings: default quota, alert webhook/email).

- **`api/llm_router.py`** - LLM Router with circuit breaker, exponential backoff, and model degradation chain. **P0 核心特性**.
  - **Per-model circuit breaker**: CLOSED → OPEN → HALF_OPEN state machine.
    - `failure_threshold` (default 3): consecutive failures to trip the breaker.
    - `success_threshold` (default 2): successes in half-open to recover.
    - `open_timeout` (default 30s): time before transitioning OPEN → HALF_OPEN.
  - **Exponential backoff with jitter**: base 1s × 2^attempt, cap 60s, ±50% jitter.
  - **Model degradation chain**: `register_fallback_chain(["gpt-4o", "gpt-4o-mini"])`. Tries models in order; skips open circuits.
  - **Retryable error detection**: auto-identifies rate limits (429), server errors (5xx), timeouts, connection issues.
  - **Token usage tracking**: per-model `prompt_tokens`, `completion_tokens`, `total_cost_usd`.
  - **Sync + async**: both `call()` and `acall()` supported.
  - Helper wrappers: `wrap_ollama_for_router()`, `wrap_openai_for_router()`.
  - Admin endpoints: `GET /router/stats`, `GET /router/circuit/{model}`, `POST /router/circuit/{model}/reset`.

- **`api/token_tracker.py`** - Token usage tracker with per-API-key stats, cost calculation, and quota alerts. **P0 核心特性**.
  - **Per-API-key tracking**: `prompt_tokens`, `completion_tokens`, `total_tokens`, `total_requests`, `total_cost_usd`.
  - **Per-model breakdown**: each key tracks usage per model independently.
  - **Cost calculation**: register model pricing (`cost_per_1k_input`, `cost_per_1k_output`); auto-compute USD per call.
  - **Quota alerts**: three thresholds — 80% (WARNING), 95% (CRITICAL), 100% (EXCEEDED).
  - **Alert callbacks**: webhook, email hook, or custom function; only fires on level escalation (not every request).
  - **Persistence**: JSON file at `./runs/token_tracker.json`; survives process restarts.
  - **Top consumers**: rank API keys by cost / tokens / request count.
  - Admin endpoints: `GET /tokens/usage`, `GET /tokens/quota/{api_key}`, `GET /tokens/top-consumers`, `GET /tokens/alerts`, `PATCH /tokens/quota/{api_key}`.

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
- `config.py`: Fixed `Config` class — added explicit `__init__` with `super().__init__()` call so dataclass fields (`model`, `server`, `memory`, `training`, `agent`, `rag`, `llm`, `cost`) are properly instantiated as objects, not raw `Field` descriptors. Also removed redundant `CFG._load()` call at module bottom.

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
