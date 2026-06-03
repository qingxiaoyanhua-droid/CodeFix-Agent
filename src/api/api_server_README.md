# CodeFix Agent API Server

HTTP API wrapper around the CoTReActAgent code repair system.

## Install

```bash
pip install -r requirements.txt
```

## Quick Start

### Local (Ollama, no GPU)

```bash
# Pull model first
ollama pull qwen2.5-coder:1.5b

# Start server with default settings (no auth for local dev)
python api_server.py --mode local --port 8000 --no-auth
```

### Server (HuggingFace transformers, GPU required)

```bash
python api_server.py \
    --mode server \
    --model /data/wbt333/models/Qwen/Qwen2.5-Coder-7B-Instruct \
    --port 8000 \
    --admin-key YOUR_ADMIN_KEY
```

## Authentication

The server uses **API Key** authentication. All keys have the prefix `cfx_`.

### Key Types

| Type | Access | Creation |
|------|--------|----------|
| Normal key | `/fix`, `/eval`, `/eval/{id}`, `/sessions/{id}` | Via admin only |
| Admin key | All endpoints + admin routes | Via admin or `--admin-key` CLI |

### Creating Keys

1. Start the server with `--admin-key` (or use `--no-auth` for local dev)
2. Call the admin endpoint:

```bash
# Create a normal key
curl -X POST http://localhost:8000/admin/keys \
  -H "X-API-Key: YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"owner": "alice", "is_admin": false, "quota_per_day": 500}'

# Response: {"api_key": "cfx_abc123...", "warning": "Store this key securely. It will not be shown again."}
```

3. Use the returned key in the `X-API-Key` header for all subsequent requests.

### Key Usage

```bash
curl -X POST http://localhost:8000/fix \
  -H "X-API-Key: cfx_abc123..." \
  -H "Content-Type: application/json" \
  -d '{...}'
```

## Rate Limiting

Each API key is limited to **30 requests per minute** (configurable via `--rate-limit`).
When exceeded, the server returns HTTP 429 with a `Retry-After` header.

## Endpoints

### Public (no auth required)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/knowledge/stats` | Knowledge base size + bug type distribution |
| GET | `/docs` | Auto-generated Swagger UI |

### Authenticated (X-API-Key required)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/fix` | Fix a single bug |
| POST | `/eval` | Start batch evaluation (async) |
| GET | `/eval/{id}` | Get evaluation result or progress |
| GET | `/sessions/{id}` | Get session history |

### Admin only (admin X-API-Key required)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/admin/keys` | Create a new API key |
| DELETE | `/admin/keys` | Revoke an API key |
| GET | `/admin/keys/stats` | API key usage statistics |
| GET | `/audit/stats` | Audit log summary (today or ?date=YYYY-MM-DD) |
| GET | `/memory/stats` | L2 (ReflectionMemory) and L3 (SkillManager) statistics |

## Example: Fix a Bug

```bash
curl -X POST http://localhost:8000/fix \
  -H "X-API-Key: cfx_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "bug_description": "Fix the bitcount function. It uses XOR instead of AND.",
    "buggy_code": "def bitcount(n):\n    count = 0\n    while n:\n        n ^= n - 1\n        count += 1\n    return count",
    "test_cases": [
      {"function": "bitcount", "input": [127], "output": 7},
      {"function": "bitcount", "input": [0], "output": 0}
    ]
  }'
```

Response:

```json
{
  "session_id": "a1b2c3d4",
  "success": true,
  "fixed_code": "def bitcount(n):\n    count = 0\n    while n:\n        n &= n - 1\n        count += 1\n    return count",
  "total_iterations": 1,
  "large_model_calls": 0,
  "total_time_ms": 1234.5,
  "compute_savings": 1.0
}
```

## Example: Batch Evaluation

```bash
# Start eval
curl -X POST http://localhost:8000/eval \
  -H "X-API-Key: cfx_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [{"name": "bitcount", "buggy_code": "...", "bug_type": "wrong_operator", "test_cases": [...] }],
    "max_iterations": 3,
    "use_rag": true,
    "reset_memory": false
  }'

# Response: {"eval_id": "x9y2z1", "message": "Evaluation started in background"}

# Poll for result
curl http://localhost:8000/eval/x9y2z1 -H "X-API-Key: cfx_YOUR_KEY"
```

## Architecture

```
Client
  |
  v
FastAPI (api_server.py)
  |-- AppState (singleton: agent + memory + KB)
  |     |-- CoTReActAgent
  |     |     |-- PermissionGate
  |     |     |-- ReflectionMemory (L2)
  |     |     |-- SkillManager (L3)
  |     |     |-- BugFixRetriever (Knowledge Base RAG)
  |     |     |-- CompactionPipeline
  |     |     |-- RecoveryManager
  |     |
  |     +-- OllamaModel / ServerModel
  |
  +-- APIKeyStore (in-memory key registry)
  +-- SlidingWindowRateLimiter (per-key rate limit)
  +-- AuditLogger (JSONL, one file per day)
  +-- ShellSandbox (subprocess isolation, multi-language)
  v
Client
```

## Audit Logs

Every request is logged to `logs/audit_YYYY-MM-DD.jsonl`.

Example line:

```json
{
  "timestamp": "2026-05-16T13:45:00.000Z",
  "request_id": "a1b2c3d4",
  "method": "POST",
  "path": "/fix",
  "status_code": 200,
  "latency_ms": 2341.5,
  "api_key": "cfx_abc123***",
  "error": null
}
```

## Code Execution Sandbox

Every code execution goes through `ShellSandbox` — a 6-layer security model:

| Layer | Mechanism |
|-------|-----------|
| 1 | Language whitelist (python, node, c, cpp, javascript) |
| 2 | Danger pattern detection (shell + Python builtins) |
| 3 | Subprocess isolation (separate PID, clean env) |
| 4 | Temp directory with auto-cleanup |
| 5 | Resource limits: timeout + output size |
| 6 | Exit code verification |

Test cases are automatically injected into the code as assertions.

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8000 | HTTP port |
| `--mode` | local | `local` (Ollama) or `server` (transformers) |
| `--ollama-model` | qwen2.5-coder:1.5b | Ollama model name |
| `--model` | ... | Model path (server mode) |
| `--lora` | None | LoRA adapter path |
| `--quantize` | False | 4-bit quantization (server mode) |
| `--reload` | False | Reset L2/L3 memory on startup |
| `--rate-limit` | 30 | Max requests per key per minute |
| `--admin-key` | — | Pre-register an admin key (can repeat) |
| `--no-auth` | False | Disable auth (dev only!) |
| `--sandbox-timeout` | 10.0 | Sandbox execution timeout in seconds |
| `--sandbox-max-output` | 10000 | Max output characters |
| `--sandbox-languages` | python,node | Comma-separated allowed languages |
