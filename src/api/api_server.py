#!/usr/bin/env python3
"""
CodeFix-Agent API Server
FastAPI wrapper around CoTReActAgent for HTTP access.

Usage:
    # Local (Ollama)
    python api_server.py --mode local

    # Server (HuggingFace transformers)
    python api_server.py --mode server --model /data/wbt333/models/Qwen/Qwen2.5-Coder-7B-Instruct

Endpoints:
    POST /fix          - Fix a single bug
    POST /eval         - Run batch evaluation
    GET  /sessions/{id} - Get session history
    GET  /memory/stats - Get L2/L3 memory stats
    GET  /health       - Health check
    GET  /knowledge/stats - Get knowledge base stats
"""

import os
import io
import sys
import json
import time
import uuid
import secrets
import traceback
import argparse
import logging
import shutil
from pathlib import Path
from collections import defaultdict, deque
from threading import Lock
from typing import List, Dict, Optional, Any
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

import requests

# Windows encoding fix
if sys.platform == "win32":
    import io
    if sys.stdout.encoding.lower() in ("cp936", "gbk", "ascii"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

from config import CFG
from session_manager import SessionManager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Depends, Header, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from shell_sandbox import ShellSandbox, TestRunner
from cot_react_agent import (
    CoTReActAgent, ReflectionMemory, SkillManager,
    Reflection, Skill, SessionStore,
)
from enhanced_agent import BugFixRetriever

# ==================== Logging ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
)
logger = logging.getLogger("api")


# ==================== API Key Auth (Persistent) ====================

class APIKeyStore:
    """
    Persistent API key registry backed by JSON file.
    Loaded on startup, saved on every write.

    Key format: cfx_<32 random hex chars>
    """

    def __init__(self, keys_path: str = "./runs/api_keys.json"):
        self.keys_path = Path(keys_path)
        self.keys_path.parent.mkdir(parents=True, exist_ok=True)
        self._keys: Dict[str, dict] = {}
        self._lock = Lock()
        self._load()

    def _load(self):
        if self.keys_path.exists():
            try:
                with open(self.keys_path, encoding="utf-8") as f:
                    raw = json.load(f)
                self._keys = {k: v for k, v in raw.items()}
                logger.info(f"[APIKeyStore] Loaded {len(self._keys)} keys from {self.keys_path}")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"[APIKeyStore] Failed to load keys: {e}, starting fresh")
                self._keys = {}

    def _save(self):
        try:
            with open(self.keys_path, "w", encoding="utf-8") as f:
                json.dump(self._keys, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"[APIKeyStore] Failed to save keys: {e}")

    def _reset(self):
        now = time.time()
        self._reset_at = now
        for entry in self._keys.values():
            entry["used_today"] = 0
            entry["reset_at"] = now

    def _check_reset(self):
        if time.time() - self._reset_at > 86400:
            self._reset()

    def add_key(self, owner: str, is_admin: bool = False,
                quota_per_day: int = 1000) -> str:
        raw = secrets.token_hex(16)
        key = f"cfx_{raw}"
        now = time.time()
        with self._lock:
            self._keys[key] = {
                "owner": owner,
                "is_admin": is_admin,
                "quota_per_day": quota_per_day,
                "used_today": 0,
                "reset_at": now,
                "created_at": now,
            }
            self._save()
        return key

    def verify(self, key: str) -> Optional[dict]:
        if not key:
            return None
        with self._lock:
            self._check_reset()
            entry = self._keys.get(key)
            if entry is None:
                return None
            if entry["used_today"] >= entry["quota_per_day"]:
                return None
            entry["used_today"] += 1
            entry["_raw_key"] = key
            return dict(entry)

    def revoke(self, key: str):
        with self._lock:
            if key in self._keys:
                del self._keys[key]
                self._save()

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_keys": len(self._keys),
                "admins": sum(1 for e in self._keys.values() if e.get("is_admin")),
                "total_used_today": sum(e.get("used_today", 0) for e in self._keys.values()),
            }


# Global key store (populate from env or CLI in main)
api_keys = APIKeyStore(keys_path="./runs/api_keys.json")


def mask_key(key: str) -> str:
    """Mask key for logging: cfx_12345678***."""
    if not key or len(key) < 12:
        return "***"
    return key[:10] + "***"


# FastAPI dependency: require a valid API key in X-API-Key header.
def require_api_key(x_api_key: str = Header(None)) -> dict:
    """
    Dependency for routes that require authentication.
    Reads X-API-Key from HTTP header.
    Usage: def my_route(..., key_info: dict = Depends(require_api_key))
    """
    entry = api_keys.verify(x_api_key or "")
    if entry is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key. Provide a valid X-API-Key header.",
        )
    return entry


def require_admin(x_api_key: str = Header(None)) -> dict:
    """Dependency: require an admin-level API key."""
    entry = api_keys.verify(x_api_key or "")
    if entry is None or not entry.get("is_admin"):
        raise HTTPException(
            status_code=403,
            detail="Admin access required.",
        )
    return entry


# ==================== Rate Limiting (Sliding Window) ====================

class SlidingWindowRateLimiter:
    """
    Per-key sliding window rate limiter.

    Uses a deque of timestamps per key. On each request, evicts timestamps
    older than `window_seconds` and checks count. O(1) amortized.
    """

    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_requests + 1)
        )
        self._lock = Lock()
        logger.info(f"RateLimiter: {max_requests} req / {window_seconds}s per key")

    def _evict_old(self, bucket: deque) -> int:
        """Remove timestamps older than window. Returns count of remaining."""
        now = time.time()
        cutoff = now - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket)

    def check(self, key: str) -> tuple[bool, int]:
        """
        Check if request is allowed.
        Returns (allowed, remaining_requests).
        """
        with self._lock:
            bucket = self._buckets[key]
            self._evict_old(bucket)
            if len(bucket) >= self.max_requests:
                return False, 0
            bucket.append(time.time())
            return True, self.max_requests - len(bucket)

    def get_retry_after(self, key: str) -> int:
        """Return seconds until a slot frees up."""
        with self._lock:
            bucket = self._buckets[key]
            self._evict_old(bucket)
            if len(bucket) < self.max_requests:
                return 0
            oldest = bucket[0]
            return max(1, int(oldest + self.window_seconds - time.time()))


# Global rate limiter
rate_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=60)


def rate_check(x_api_key: str = Header(None)) -> dict:
    """
    Combined dependency: auth + rate-limit check.
    Validates API key, then enforces sliding-window rate limit.
    """
    entry = api_keys.verify(x_api_key or "")
    if entry is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key.",
        )

    raw_key = entry.get("_raw_key", "")
    allowed, _ = rate_limiter.check(raw_key)
    if not allowed:
        retry_after = rate_limiter.get_retry_after(raw_key)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Retry after {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
    return entry


# ==================== Audit Logger (Rotating) ====================

class AuditLogger:
    """
    Append-only JSONL audit logger with file rotation.

    Rotation triggers:
      - Daily: new file per day (logs/audit_YYYY-MM-DD.jsonl)
      - Size-based: rotate when file exceeds max_bytes
      - Line-based: rotate when file exceeds max_lines

    Backups are kept as .1, .2, ..., .N (newest to oldest).
    """

    def __init__(
        self,
        log_dir: str = "logs",
        max_bytes: int = 10 * 1024 * 1024,
        max_lines: int = 100000,
        backup_count: int = 5,
    ):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.backup_count = backup_count

    def _current_path(self) -> Path:
        date_str = time.strftime("%Y-%m-%d")
        return self.log_dir / f"audit_{date_str}.jsonl"

    def _should_rotate(self, path: Path) -> bool:
        """Check if rotation is needed based on size or line count."""
        if not path.exists():
            return False
        try:
            size = path.stat().st_size
            if size >= self.max_bytes:
                return True
            # Line count check
            with open(path, encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
            if line_count >= self.max_lines:
                return True
        except (OSError, IOError):
            pass
        return False

    def _rotate(self, path: Path):
        """Rotate the log file: current -> .1 -> .2 -> ..."""
        if not path.exists():
            return

        # Delete the oldest backup
        oldest = path.parent / f"{path.stem}.{self.backup_count}{path.suffix}"
        if oldest.exists():
            oldest.unlink()

        # Shift existing backups
        for i in range(self.backup_count - 1, 0, -1):
            src = path.parent / f"{path.stem}.{i}{path.suffix}"
            dst = path.parent / f"{path.stem}.{i + 1}{path.suffix}"
            if src.exists():
                shutil.move(str(src), str(dst))

        # Rename current to .1
        dst = path.parent / f"{path.stem}.1{path.suffix}"
        shutil.move(str(path), str(dst))
        logger.info(f"[AuditLogger] Rotated {path.name} -> {dst.name}")

    def log(self, request_id: str, method: str, path: str,
            status_code: int, latency_ms: float,
            api_key: Optional[str] = None,
            request_body_hash: Optional[str] = None,
            error: Optional[str] = None,
            **extra):
        import hashlib

        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "request_id": request_id,
            "method": method,
            "path": path,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2),
            "api_key": mask_key(api_key) if api_key else None,
            "error": error,
            **extra,
        }
        try:
            log_path = self._current_path()

            # Rotate if needed
            if self._should_rotate(log_path):
                self._rotate(log_path)

            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # Never let audit logging crash a request

    def summary(self, date: Optional[str] = None) -> Dict:
        """Return summary stats for a given date (YYYY-MM-DD) or today."""
        fname = self.log_dir / f"audit_{date or time.strftime('%Y-%m-%d')}.jsonl"
        return self._parse_summary(fname)

    def summary_all(self) -> Dict:
        """Aggregate summary across all audit files."""
        total = 0
        errors = 0
        by_path: Dict[str, int] = defaultdict(int)
        by_status: Dict[str, int] = defaultdict(int)
        latencies: List[float] = []

        for fpath in sorted(self.log_dir.glob("audit_*.jsonl")):
            result = self._parse_summary(fpath)
            total += result.get("total", 0)
            errors += result.get("errors", 0)
            for k, v in result.get("by_path", {}).items():
                by_path[k] += v
            for k, v in result.get("by_status", {}).items():
                by_status[k] += v
            lat = result.get("latency_avg_ms", 0)
            if lat > 0:
                latencies.append(lat)

        return {
            "total_requests": total,
            "total_errors": errors,
            "error_rate": round(errors / total, 4) if total else 0,
            "by_path": dict(by_path),
            "by_status": dict(by_status),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0,
            "files_scanned": len(list(self.log_dir.glob("audit_*.jsonl"))),
        }

    def _parse_summary(self, fname: Path) -> Dict:
        if not fname.exists():
            return {}
        total = 0
        errors = 0
        by_path: Dict[str, int] = defaultdict(int)
        by_status: Dict[str, int] = defaultdict(int)
        latencies: List[float] = []

        try:
            with open(fname, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    if rec.get("error"):
                        errors += 1
                    by_path[rec.get("path", "")] += 1
                    by_status[str(rec.get("status_code", ""))] += 1
                    lat = rec.get("latency_ms", 0)
                    if lat > 0:
                        latencies.append(lat)
        except Exception:
            pass

        if not latencies:
            return {"total": total, "errors": errors,
                    "by_path": dict(by_path), "by_status": dict(by_status)}

        latencies.sort()
        return {
            "total": total,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0,
            "by_path": dict(by_path),
            "by_status": dict(by_status),
            "latency_p50_ms": round(latencies[len(latencies) // 2], 2),
            "latency_p99_ms": round(latencies[int(len(latencies) * 0.99)], 2),
            "latency_avg_ms": round(sum(latencies) / len(latencies), 2),
        }


audit_logger = AuditLogger(log_dir=CFG.memory.log_dir)


# ==================== Pydantic Models ====================

class TestCase(BaseModel):
    function: str
    input: List[Any] = Field(default_factory=list)
    output: Any


class FixRequest(BaseModel):
    bug_description: str = Field(..., min_length=1, description="Bug description text")
    buggy_code: str = Field(..., min_length=1, description="Buggy source code")
    test_cases: List[TestCase] = Field(default_factory=list, description="Test cases")
    session_id: Optional[str] = Field(None, description="Session ID for continuity (optional)")
    max_iterations: int = Field(3, ge=1, le=10, description="Max agent iterations")
    language: str = Field("python", description="Programming language")
    use_rag: bool = Field(True, description="Enable knowledge base RAG retrieval")
    memory_namespace: Optional[str] = Field(
        None,
        description=(
            "Memory namespace for user isolation. When set, all persistent memory "
            "(reflection, skills, e_pool, x_pool) is stored in runs/{namespace}/ "
            "instead of runs/. When None, uses 'default' namespace for backward compat."
        ),
    )


class FixResponse(BaseModel):
    session_id: str
    success: bool
    fixed_code: str
    total_iterations: int
    large_model_calls: int
    total_time_ms: float
    used_reflections: int
    used_skills: List[str]
    skill_extracted: Optional[str]
    compute_savings: float
    iterations: List[Dict]
    memory_namespace: str = "default"
    error: Optional[str] = None


class EvalItem(BaseModel):
    name: str
    buggy_code: str
    fixed_code: Optional[str] = None
    bug_type: Optional[str] = None
    description: Optional[str] = None
    test_cases: List[TestCase] = Field(default_factory=list)
    language: str = Field("python", description="Programming language")


class EvalRequest(BaseModel):
    items: List[EvalItem] = Field(..., min_length=1, max_length=100)
    max_iterations: int = Field(3, ge=1, le=10)
    language: str = Field("python")
    use_rag: bool = Field(True)
    reset_memory: bool = Field(False, description="Clear L2/L3 memory before eval")


class EvalProgress(BaseModel):
    total: int
    completed: int
    passed: int
    failed: int
    in_progress: str
    pass_rate: float


class EvalResult(BaseModel):
    eval_id: str
    total: int
    passed: int
    pass_rate: float
    total_time_s: float
    results: List[Dict]
    memory_effectiveness: Dict


class SessionInfo(BaseModel):
    session_id: str
    bug_description: str
    bug_hash: str
    iterations: int
    success: bool
    events: List[Dict]


class HealthResponse(BaseModel):
    status: str
    mode: str
    model_loaded: bool
    memory_l2: int
    memory_l3: int
    knowledge_base_size: int


# ==================== Model Wrappers ====================

class OllamaModel:
    """Wrapper around Ollama REST API."""

    def __init__(self, model_name: str = "qwen2.5-coder:1.5b",
                 base_url: str = "http://localhost:11434",
                 timeout: int = 120):
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._connected = False
        self._verify()

    def _verify(self):
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            self._connected = self.model_name in models
            if not self._connected:
                logger.warning(f"Model '{self.model_name}' not in Ollama. Available: {models}")
        except Exception as e:
            logger.error(f"Ollama unreachable: {e}")
            self._connected = False

    @property
    def available(self) -> bool:
        return self._connected

    def __call__(self, prompt: str, system_prompt: str = "",
                 max_new_tokens: int = 512, temperature: float = 0.7) -> str:
        payload = {
            "model": self.model_name,
            "messages": [
                *([{"role": "system", "content": system_prompt}] if system_prompt else []),
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                "top_p": 0.9,
                "num_predict": max_new_tokens,
            }
        }
        try:
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]
        except Exception as e:
            logger.error(f"Ollama API error: {e}")
            return ""


class ServerModel:
    """Loads Qwen2.5-Coder via transformers on GPU."""

    def __init__(self, model_path: str, lora_path: str = None,
                 device: str = "cuda", use_quantization: bool = False):
        self.model_path = model_path
        self.lora_path = lora_path
        self.device = device
        self.use_quantization = use_quantization
        self.model = None
        self.tokenizer = None
        self._loaded = False
        self._load()

    def _load(self):
        logger.info(f"Loading model from {self.model_path} ...")
        t0 = time.time()
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from peft import PeftModel

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_path, trust_remote_code=True, local_files_only=True
        )
        self.tokenizer.pad_token = self.tokenizer.eos_token

        dtype = torch.float16
        bnb_config = None
        if self.use_quantization:
            from transformers import BitsAndBytesConfig
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=dtype,
            )

        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            torch_dtype=dtype,
            quantization_config=bnb_config,
            trust_remote_code=True,
            local_files_only=True,
            device_map="auto",
        )

        if self.lora_path and Path(self.lora_path).exists():
            logger.info(f"Loading LoRA adapter from {self.lora_path} ...")
            self.model = PeftModel.from_pretrained(self.model, self.lora_path)

        self.model.eval()
        self._loaded = True
        logger.info(f"Model loaded in {time.time() - t0:.1f}s")

    @property
    def available(self) -> bool:
        return self._loaded

    def __call__(self, prompt: str, system_prompt: str = "",
                 max_new_tokens: int = 512, temperature: float = 0.7) -> str:
        from transformers import GenerationConfig
        import torch

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        text = self.tokenizer.apply_chat_template(messages, tokenize=False)
        inputs = self.tokenizer(text, return_tensors="pt").to(self.device)

        with torch.no_grad():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=temperature > 0,
                temperature=temperature if temperature > 0 else None,
                pad_token_id=self.tokenizer.eos_token_id,
            )

        result = self.tokenizer.decode(output_ids[0], skip_special_tokens=True)
        return result[len(text):].strip()


# ==================== Shared Application State ====================

class AppState:
    """Global singleton shared across requests."""

    def __init__(self):
        self.agent: Optional[CoTReActAgent] = None
        self.mode: str = "local"
        self.ollama_model: Optional[OllamaModel] = None
        self.server_model: Optional[ServerModel] = None
        self.rag_retriever: Optional[BugFixRetriever] = None

        # In-memory tracking for background eval jobs
        self.eval_jobs: Dict[str, Dict] = {}
        self.executor = ThreadPoolExecutor(max_workers=4)

        # Shell sandbox for code execution
        self.sandbox: Optional[ShellSandbox] = None
        self.test_runner: Optional[TestRunner] = None

        # Per-namespace memory components cache.
        # Key: namespace string, Value: dict with reflection_memory, skill_manager, session_store
        self._namespace_memories: Dict[str, Dict] = {}
        self._embedding_model_name: str = "all-MiniLM-L6-v2"

    def _get_or_create_memory_for_namespace(
        self,
        namespace: str,
    ) -> Dict:
        """
        Get or create memory components (ReflectionMemory, SkillManager, SessionStore)
        for the given namespace. All components share the same runs/{namespace}/ dir.
        """
        ns = _sanitize_namespace(namespace or "default")
        if ns in self._namespace_memories:
            return self._namespace_memories[ns]

        mem_dir = _get_memory_dir(ns)
        reflection_path = str(mem_dir / "reflection_memory.json")
        skills_dir = str(mem_dir / "skills")
        e_pool_path = str(mem_dir / "e_pool.json")
        x_pool_path = str(mem_dir / "x_pool.json")

        reflection_memory = ReflectionMemory(
            memory_path=reflection_path,
            embedding_model_name=self._embedding_model_name,
        )
        skill_manager = SkillManager(
            skills_dir=skills_dir,
            embedding_model_name=self._embedding_model_name,
        )

        # Redis session store when enabled; falls back to file store automatically
        sessions_dir = str(mem_dir / "sessions")
        if CFG.memory.session_redis_enabled and CFG.memory.redis_url:
            session_store = SessionStore.create_redis_store(
                redis_url=CFG.memory.redis_url,
                namespace=ns,
                fallback_sessions_dir=sessions_dir,
            )
            logger.info(f"[AppState] Redis session store enabled for namespace '{ns}'")
        else:
            session_store = SessionStore(sessions_dir=sessions_dir)

        entry = {
            "reflection_memory": reflection_memory,
            "skill_manager": skill_manager,
            "session_store": session_store,
            "e_pool_path": e_pool_path,
            "x_pool_path": x_pool_path,
        }
        self._namespace_memories[ns] = entry
        logger.info(
            f"[AppState] Created namespace '{ns}' memory components at {mem_dir}/"
        )
        return entry

    def build_agent_for_namespace(self, mode: str, namespace: str = "default", **kwargs):
        """
        Build a CoTReActAgent for the given namespace.
        Uses (or creates) namespace-isolated memory components.
        """
        self.mode = mode
        mem = self._get_or_create_memory_for_namespace(namespace)
        reflection_memory = mem["reflection_memory"]
        skill_manager = mem["skill_manager"]
        session_store = mem["session_store"]

        # Build model wrappers
        if mode == "local":
            self.ollama_model = OllamaModel(**kwargs)
            small = self.ollama_model
            large = self.ollama_model
        else:
            self.server_model = ServerModel(**kwargs)
            small = self.server_model
            large = self.server_model

        self.agent = CoTReActAgent(
            small_model_fn=small,
            large_model_fn=large,
            rag_retriever=self.rag_retriever,
            max_iterations=CFG.agent.max_iterations,
            reflection_memory=reflection_memory,
            skill_manager=skill_manager,
            session_store=session_store,
            memory_namespace=namespace,
        )
        logger.info(f"[AppState] Agent built in {mode} mode for namespace '{namespace}'")

    def init_sandbox(
        self,
        timeout: float = 10.0,
        max_output_chars: int = 10000,
        allowed_languages: Optional[List[str]] = None,
    ):
        """Initialize the shell sandbox."""
        self.sandbox = ShellSandbox(
            timeout=timeout,
            max_output_chars=max_output_chars,
            allowed_languages=allowed_languages,
        )
        self.test_runner = TestRunner(self.sandbox)
        logger.info(f"Sandbox initialized: timeout={timeout}s, langs={allowed_languages}")

    def init_session_manager(self, checkpoint_interval_seconds: float = 60.0):
        """Initialize SessionManager with persistence and metrics."""
        self.session_manager = SessionManager(
            sessions_dir=str(Path(CFG.memory.sessions_dir)),
            checkpoint_dir=str(Path(CFG.memory.checkpoint_dir)),
            metrics_path=str(Path(CFG.memory.checkpoint_dir) / "metrics.json"),
            memory_snapshot_dir=str(Path(CFG.memory.checkpoint_dir) / "memory_snapshots"),
            checkpoint_interval_seconds=checkpoint_interval_seconds,
        )
        # Mark any stale sessions from previous runs as incomplete
        stale = self.session_manager.end_incomplete_sessions()
        logger.info(f"[AppState] SessionManager ready. Marked {stale} incomplete sessions")

    def build_agent(self, mode: str, **kwargs):
        """Build or rebuild the CoTReActAgent."""
        self.mode = mode

        if mode == "local":
            self.ollama_model = OllamaModel(**kwargs)
            small = self.ollama_model
            large = self.ollama_model  # same model in local mode
        else:
            self.server_model = ServerModel(**kwargs)
            small = self.server_model
            large = self.server_model

        # SessionStore for append-only session logs
        self.session_store = SessionStore(sessions_dir=str(Path(CFG.memory.sessions_dir)))

        self.agent = CoTReActAgent(
            small_model_fn=small,
            large_model_fn=large,
            rag_retriever=self.rag_retriever,
            max_iterations=CFG.agent.max_iterations,
            reflection_memory=self.reflection_memory,
            skill_manager=self.skill_manager,
            session_store=self.session_store,
        )
        logger.info(f"Agent built in {mode} mode")

    def add_api_key(self, owner: str, is_admin: bool = False,
                    quota_per_day: int = 1000) -> str:
        """Register a new API key. Returns the raw key (show only once!)."""
        return api_keys.add_key(owner, is_admin, quota_per_day)

    def revoke_api_key(self, key: str):
        api_keys.revoke(key)

    def get_key_stats(self) -> dict:
        return api_keys.stats()


# Global state
state = AppState()


# ==================== Namespace Path Helper ====================

def _sanitize_namespace(ns: str) -> str:
    """Sanitize namespace string for use in path components."""
    import re
    # Allow alphanum, dash, underscore; collapse others to underscore
    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', ns)
    # Collapse multiple underscores
    safe = re.sub(r'_+', '_', safe)
    # Strip leading/trailing underscores
    safe = safe.strip('_')
    # Max length 64 chars
    return safe[:64] or "default"


def _get_memory_dir(namespace: str = "default") -> Path:
    """
    Resolve the base memory directory for a given namespace.
    All persistent memory files (reflection, skills, e_pool, x_pool)
    live under runs/{namespace}/ when namespace is non-default.
    """
    ns = _sanitize_namespace(namespace or "default")
    base = Path(CFG.memory.reflection_memory_path).parent  # ./runs
    return base / ns


# ==================== Helper: Code Execution (Sandbox) ====================

def execute_and_test(
    code: str,
    test_cases: List[TestCase],
    sandbox: Optional[ShellSandbox] = None,
    language: str = "python",
) -> tuple[bool, str]:
    """
    Execute code in sandbox and run test cases.

    Falls back to in-process exec() if sandbox is not available.
    """
    if sandbox is None:
        # Fallback: in-process exec (for backward compatibility)
        return _execute_inline(code, test_cases)

    runner = TestRunner(sandbox)
    test_dicts = [tc.model_dump() for tc in test_cases]
    passed, message = runner.run_tests(code, language, test_dicts)
    return passed, message


def _execute_inline(code: str, test_cases: List[TestCase]) -> tuple[bool, str]:
    """Fallback inline executor using exec()."""
    try:
        ns = {}
        exec(code, ns)
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"
    except Exception as e:
        return False, f"CompileError: {e}"

    for tc in test_cases:
        fname = tc.function
        args = tc.input
        expected = tc.output
        if fname not in ns:
            return False, f"Function '{fname}' not defined"
        try:
            result = ns[fname](*args) if isinstance(args, list) else ns[fname](args)
            if result != expected:
                return False, f"{fname}({args}) = {result}, expected {expected}"
        except Exception as e:
            return False, f"RuntimeError: {e}"
    return True, ""


# ==================== Lifespan ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialize SessionManager. Shutdown: save metrics."""
    logger.info("Starting CodeFix API Server ...")
    # Initialize SessionManager on startup
    state.init_session_manager(checkpoint_interval_seconds=60.0)
    yield
    logger.info("Shutting down ...")
    # Save metrics before shutdown
    if state.session_manager:
        state.session_manager._save_metrics()
    state.executor.shutdown(wait=True)


# ==================== FastAPI App ====================

app = FastAPI(
    title="CodeFix Agent API",
    description="HTTP API for the Compile-First ReAct code repair agent",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== Audit Middleware ====================

@app.middleware("http")
async def audit_middleware(request, call_next):
    """Log every HTTP request to JSONL."""
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())[:8])
    t0 = time.perf_counter()

    # Exclude the docs/Swagger endpoints from audit
    path = request.url.path
    if path.startswith("/docs") or path.startswith("/openapi.json") or path == "/redoc":
        return await call_next(request)

    response = await call_next(request)
    latency_ms = (time.perf_counter() - t0) * 1000

    # Get the API key (if present) for audit
    api_key = request.headers.get("x-api-key", "")

    audit_logger.log(
        request_id=request_id,
        method=request.method,
        path=path,
        status_code=response.status_code,
        latency_ms=latency_ms,
        api_key=api_key,
        user_agent=request.headers.get("user-agent", ""),
        client_ip=request.client.host if request.client else "",
    )

    # Attach request_id to response headers
    response.headers["x-request-id"] = request_id
    return response


# ==================== Endpoints ====================

@app.get("/health", response_model=HealthResponse)
def health():
    """Liveness probe."""
    return HealthResponse(
        status="ok",
        mode=state.mode,
        model_loaded=(state.ollama_model.available if state.mode == "local"
                      else (state.server_model.available if state.server_model else False)),
        memory_l2=sum(
            m["reflection_memory"].size
            for m in state._namespace_memories.values()
        ),
        memory_l3=sum(
            m["skill_manager"].active_count
            for m in state._namespace_memories.values()
        ),
        knowledge_base_size=(
            len(state.rag_retriever.knowledge_base)
            if state.rag_retriever and state.rag_retriever.knowledge_base else 0
        ),
    )


@app.post("/fix", response_model=FixResponse)
def fix_bug(req: FixRequest, key_info: dict = Depends(rate_check)):
    """
    Fix a single bug with the CoTReActAgent.

    The agent uses compile-first verification: small model generates fix,
    if tests fail, large model provides targeted feedback for retry.
    L2 (reflection) + L3 (skill) + Knowledge Base RAG are all used automatically.

    All sessions are recorded to disk and metrics are collected via SessionManager.

    Memory isolation: when memory_namespace is set, all persistent memory
    (reflection, skills, e_pool, x_pool) is stored under runs/{namespace}/.
    This allows different API key owners to have completely isolated memories.

    Requires: X-API-Key header.
    """
    if not state.agent:
        raise HTTPException(status_code=503, detail="Agent not initialized. Set mode via --mode on startup.")

    try:
        # Resolve namespace: explicit > key owner > default
        namespace = req.memory_namespace
        if not namespace and key_info:
            owner = key_info.get("owner", "")
            if owner:
                namespace = _sanitize_namespace(owner)

        effective_ns = namespace or "default"

        # Ensure agent matches the namespace
        current_ns = getattr(state.agent, '_memory_namespace', None)
        if current_ns != effective_ns:
            state.build_agent_for_namespace(
                state.mode,
                namespace=effective_ns,
                model_name=(
                    getattr(state.ollama_model, 'model_name', None)
                    if state.mode == "local" else None
                ),
                base_url=(
                    getattr(state.ollama_model, 'base_url', None)
                    if state.mode == "local" else None
                ),
            ) if state.mode == "local" else state.build_agent_for_namespace(
                state.mode,
                namespace=effective_ns,
                model_path=getattr(state.server_model, 'model_path', None),
                lora_path=getattr(state.server_model, 'lora_path', None),
            )

        test_dicts = [tc.model_dump() for tc in req.test_cases]

        # Record session start in SessionManager
        session_id = req.session_id or str(uuid.uuid4())[:8]
        if state.session_manager:
            state.session_manager.start_session(session_id, metadata={
                "bug_type": req.bug_description[:50],
                "language": req.language,
                "memory_namespace": effective_ns,
            })

        result = state.agent.fix_bug(
            bug_description=req.bug_description,
            buggy_code=req.buggy_code,
            test_cases=test_dicts,
            session_id=session_id,
        )

        # Also verify externally
        if result.fixed_code and req.test_cases:
            verified, err = execute_and_test(
                result.fixed_code, req.test_cases,
                sandbox=state.sandbox,
                language=req.language,
            )
        else:
            verified, err = result.success, None

        # Record session end and metrics
        if state.session_manager:
            state.session_manager.end_session(
                session_id,
                success=verified,
                metrics={
                    "bug_type": req.bug_description[:50],
                    "latency_ms": result.total_time_ms,
                    "iterations": result.total_iterations,
                    "used_past_reflections": result.used_past_reflections,
                    "used_skills": result.used_skills or [],
                    "rag_hit": False,
                    "e_pool_hit": False,
                    "x_pool_hit": False,
                    "memory_namespace": effective_ns,
                }
            )

        return FixResponse(
            session_id=state.agent._current_session_id,
            success=result.success,
            fixed_code=result.fixed_code,
            total_iterations=result.total_iterations,
            large_model_calls=result.large_model_calls,
            total_time_ms=result.total_time_ms,
            used_reflections=result.used_past_reflections,
            used_skills=result.used_skills or [],
            skill_extracted=result.skill_extracted,
            compute_savings=getattr(result, 'compute_savings', 0.0),
            iterations=[
                {
                    "iteration": it.iteration,
                    "cot_steps": [s.model_dump() for s in it.cot_steps],
                    "fixed_code": it.fixed_code,
                    "execution_result": it.execution_result.model_dump() if it.execution_result else {},
                }
                for it in result.iterations
            ],
            memory_namespace=effective_ns,
            error=err if not verified else None,
        )

    except Exception as e:
        logger.exception(f"fix_bug failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/eval", response_model=Dict)
def start_eval(req: EvalRequest, background_tasks: BackgroundTasks,
               key_info: dict = Depends(rate_check)):
    """
    Start a batch evaluation job in the background.

    Returns an eval_id immediately. Poll GET /eval/{eval_id} for progress.

    Requires: X-API-Key header.
    """
    eval_id = str(uuid.uuid4())[:8]

    # Reset memory if requested
    if req.reset_memory:
        # Reset only the "default" namespace for backward compat in eval context
        mem = state._get_or_create_memory_for_namespace("default")
        mem["reflection_memory"].reflections.clear()
        mem["reflection_memory"].save()
        mem["skill_manager"].skills.clear()
        mem["skill_manager"].save()
        if state.agent:
            state.agent.reflection_memory = mem["reflection_memory"]
            state.agent.skill_manager = mem["skill_manager"]

    state.eval_jobs[eval_id] = {
        "status": "pending",
        "total": len(req.items),
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "results": [],
        "start_time": time.time(),
    }

    def run_eval():
        job = state.eval_jobs[eval_id]
        job["status"] = "running"

        for i, item in enumerate(req.items):
            job["in_progress"] = item.name
            try:
                test_dicts = [tc.model_dump() for tc in item.test_cases]
                result = state.agent.fix_bug(
                    bug_description=f"Fix the bug in {item.name}. {item.description or ''}",
                    buggy_code=item.buggy_code,
                    test_cases=test_dicts,
                    max_iterations=req.max_iterations,
                )

                if result.fixed_code and item.test_cases:
                    verified, err = execute_and_test(
                        result.fixed_code, item.test_cases,
                        sandbox=state.sandbox,
                        language=item.language,
                    )
                else:
                    verified, err = result.success, None

                job["results"].append({
                    "name": item.name,
                    "bug_type": item.bug_type,
                    "agent_success": result.success,
                    "test_passed": verified,
                    "error": err,
                    "iterations": result.total_iterations,
                    "llm_calls": result.large_model_calls,
                    "used_reflections": result.used_past_reflections,
                    "used_skills": result.used_skills,
                    "skill_extracted": result.skill_extracted,
                })

                if verified:
                    job["passed"] += 1
                else:
                    job["failed"] += 1

            except Exception as e:
                job["results"].append({
                    "name": item.name,
                    "error": str(e),
                })
                job["failed"] += 1

            job["completed"] = i + 1

        job["status"] = "done"
        job["total_time_s"] = time.time() - job["start_time"]

    background_tasks.add_task(run_eval)

    return {"eval_id": eval_id, "message": "Evaluation started in background"}


@app.get("/eval/{eval_id}", response_model=EvalResult)
def get_eval_result(eval_id: str, key_info: dict = Depends(rate_check)):
    """Get the result of a background evaluation job."""
    job = state.eval_jobs.get(eval_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Eval job '{eval_id}' not found")

    if job["status"] != "done":
        return JSONResponse({
            "eval_id": eval_id,
            "status": job["status"],
            "total": job["total"],
            "completed": job["completed"],
            "passed": job["passed"],
            "failed": job["failed"],
            "in_progress": job.get("in_progress", ""),
            "pass_rate": job["passed"] / max(job["completed"], 1),
            "results": job["results"],
        })

    # Return full result
    return EvalResult(
        eval_id=eval_id,
        total=job["total"],
        passed=job["passed"],
        pass_rate=job["passed"] / job["total"] if job["total"] > 0 else 0.0,
        total_time_s=job["total_time_s"],
        results=job["results"],
        memory_effectiveness={
            "reflections_used": sum(1 for r in job["results"] if r.get("used_reflections", 0) > 0),
            "skills_used": sum(1 for r in job["results"] if r.get("used_skills")),
            "skills_extracted": sum(1 for r in job["results"] if r.get("skill_extracted")),
        },
    )


@app.get("/sessions/{session_id}", response_model=SessionInfo)
def get_session(session_id: str, key_info: dict = Depends(rate_check)):
    """Get the full history of a session."""
    if not state.agent or not state.agent.session_store:
        raise HTTPException(status_code=503, detail="Session store not available")

    session = state.agent.session_store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    return SessionInfo(
        session_id=session_id,
        bug_description=session.get("metadata", {}).get("bug_description", ""),
        bug_hash=session.get("metadata", {}).get("bug_hash", ""),
        iterations=session.get("metadata", {}).get("iterations_used", 0),
        success=session.get("metadata", {}).get("success", False),
        events=session.get("events", []),
    )


@app.get("/memory/stats")
def memory_stats(key_info: dict = Depends(require_admin)):
    """Get L2 and L3 memory statistics across all namespaces."""
    stats_by_ns = {}
    for ns, mem in state._namespace_memories.items():
        stats_by_ns[ns] = {
            "l2_reflection_count": mem["reflection_memory"].size,
            "l3_skill_count": mem["skill_manager"].active_count,
        }

    # Aggregate for backward-compatible top-level keys
    total_reflections = sum(m["reflection_memory"].size for m in state._namespace_memories.values())
    total_skills = sum(m["skill_manager"].active_count for m in state._namespace_memories.values())

    def _build_reflection_entries(mem):
        return [
            {
                "pattern": r.bug_pattern,
                "usefulness": round(r.usefulness_score, 3),
                "verified": r.verification_count,
                "helpful": r.helpful_count,
            }
            for r in (mem["reflection_memory"].reflections or [])
        ]

    def _build_skill_entries(mem):
        return [
            {
                "name": s.name,
                "usefulness": round(s.usefulness, 3),
                "success": s.success_count,
                "failure": s.failure_count,
                "is_stale": s.is_stale,
            }
            for s in (mem["skill_manager"].skills or [])
        ]

    # Default namespace stats for backward compat
    default_mem = state._namespace_memories.get("default", {})
    return {
        "by_namespace": stats_by_ns,
        "total_reflections": total_reflections,
        "total_skills": total_skills,
        "l2_reflection_memory": {
            "total_entries": default_mem.get("reflection_memory", type("X", (), {"size": 0})()).size,
            "entries": _build_reflection_entries(default_mem),
        },
        "l3_skill_manager": {
            "active_count": default_mem.get("skill_manager", type("X", (), {"active_count": 0})()).active_count,
            "archived_count": default_mem.get("skill_manager", type("X", (), {"archived_count": 0})()).archived_count,
            "skills": _build_skill_entries(default_mem),
        },
    }


@app.get("/knowledge/stats")
def knowledge_stats():
    """Get knowledge base statistics."""
    if not state.rag_retriever:
        return {"size": 0, "types": {}}

    kb = state.rag_retriever.knowledge_base
    type_counts: Dict[str, int] = {}
    for entry in kb:
        bt = entry.get("bug_type", "unknown")
        type_counts[bt] = type_counts.get(bt, 0) + 1

    return {
        "size": len(kb),
        "types": type_counts,
    }


# ==================== Session & Metrics Endpoints ====================

@app.get("/health")
def health_check():
    """
    Health check endpoint for frontend connectivity test.
    NOTE: /health only checks API server is up. For full system readiness,
    use /ready which verifies agent, model, and memory are all initialized.
    """
    return {"status": "ok", "version": "1.0.0"}

@app.get("/sessions")
def list_sessions(key_info: dict = Depends(rate_check)):
    """
    List all past sessions (sidebar view). Requires API key.
    """
    if not state.session_store:
        return {"sessions": [], "total": 0}

    sessions = []
    if state.session_store.sessions_dir.exists():
        for f in state.session_store.sessions_dir.glob("*.meta.json"):
            try:
                import json as _json
                with open(f, encoding="utf-8") as fp:
                    meta = _json.load(fp)
                    sessions.append(meta)
            except Exception:
                continue

    sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return {"sessions": sessions, "total": len(sessions)}


@app.get("/sessions/admin/incomplete")
def list_incomplete_sessions(key_info: dict = Depends(require_admin)):
    """
    List sessions with recoverable checkpoints (admin only).
    """
    if not state.session_manager:
        return {"sessions": [], "total": 0}
    incomplete = state.session_manager.list_incomplete_sessions()
    return {"sessions": incomplete, "total": len(incomplete)}


@app.get("/sessions/{session_id}")
def get_session_detail(session_id: str, key_info: dict = Depends(rate_check)):
    """
    Get full session detail: metadata, events, and checkpoint state.
    """
    if not state.session_manager:
        raise HTTPException(status_code=503, detail="SessionManager not initialized")

    meta = state.session_manager.get_session_meta(session_id)
    events = state.session_manager.get_session_events(session_id)
    checkpoint = state.session_manager.load_checkpoint(session_id)

    if not meta and not events:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    return {
        "session_id": session_id,
        "meta": meta,
        "event_count": len(events),
        "has_checkpoint": checkpoint is not None,
        "checkpoint": checkpoint,
    }


@app.get("/sessions/{session_id}/resume")
def resume_session(session_id: str, key_info: dict = Depends(rate_check)):
    """
    Attempt to resume an incomplete session from its latest checkpoint.
    Returns the checkpoint state so the client can retry from where it left off.
    """
    if not state.session_manager:
        raise HTTPException(status_code=503, detail="SessionManager not initialized")

    checkpoint = state.session_manager.load_checkpoint(session_id)
    if not checkpoint:
        raise HTTPException(status_code=404, detail=f"No checkpoint found for session '{session_id}'")

    meta = state.session_manager.get_session_meta(session_id)
    return {
        "session_id": session_id,
        "resumable": True,
        "checkpoint": checkpoint,
        "meta": meta,
        "message": "Session is resumable. Retry your request with session_id to continue."
    }


@app.get("/metrics")
def get_metrics(key_info: dict = Depends(require_admin)):
    """
    Get aggregated business metrics from SessionManager.
    Includes: request counts, success/failure rates, latency percentiles,
    bug-type breakdown, memory hit rates.

    Admin only.
    """
    if not state.session_manager:
        return {"error": "SessionManager not initialized"}
    metrics = state.session_manager.get_metrics()
    return {
        "total_requests": metrics.get("total_requests", 0),
        "total_success": metrics.get("total_success", 0),
        "total_failure": metrics.get("total_failure", 0),
        "pass_rate": round(
            metrics.get("total_success", 0) / max(metrics.get("total_requests", 1), 1), 4
        ),
        "incomplete_sessions": metrics.get("incomplete_sessions", 0),
        "avg_latency_ms": round(metrics.get("avg_latency_ms", 0), 2),
        "p50_latency_ms": round(metrics.get("p50_latency_ms", 0), 2),
        "p95_latency_ms": round(metrics.get("p95_latency_ms", 0), 2),
        "avg_iterations": round(metrics.get("avg_iterations", 0), 2),
        "bug_type_counts": dict(metrics.get("bug_type_counts", {})),
        "bug_type_success": dict(metrics.get("bug_type_success", {})),
        "memory_hits": {
            "l2_reflection": metrics.get("l2_reflection_hits", 0),
            "l3_skill": metrics.get("l3_skill_hits", 0),
            "rag": metrics.get("rag_hits", 0),
            "e_pool": metrics.get("decentmem_e_pool_hits", 0),
            "x_pool": metrics.get("decentmem_x_pool_hits", 0),
        },
    }


@app.get("/config")
def get_config(key_info: dict = Depends(require_admin)):
    """
    Get current server configuration (safe subset, no secrets).
    Admin only.
    """
    return CFG.to_dict()


# ==================== Admin Endpoints ====================

class CreateKeyRequest(BaseModel):
    owner: str
    is_admin: bool = False
    quota_per_day: int = Field(default=1000, ge=1, le=100000)


class CreateKeyResponse(BaseModel):
    api_key: str
    owner: str
    is_admin: bool
    quota_per_day: int
    warning: str = "Store this key securely. It will not be shown again."


@app.post("/admin/keys", response_model=CreateKeyResponse)
def create_api_key(req: CreateKeyRequest, _: dict = Depends(require_admin)):
    """
    Create a new API key. Admin only.

    The returned api_key is shown only once — the caller must store it.
    """
    key = state.add_api_key(
        owner=req.owner,
        is_admin=req.is_admin,
        quota_per_day=req.quota_per_day,
    )
    return CreateKeyResponse(
        api_key=key,
        owner=req.owner,
        is_admin=req.is_admin,
        quota_per_day=req.quota_per_day,
    )


class RevokeKeyRequest(BaseModel):
    api_key: str = Field(..., description="Full API key to revoke")


@app.delete("/admin/keys")
def revoke_api_key(req: RevokeKeyRequest, _: dict = Depends(require_admin)):
    """Revoke an API key. Admin only."""
    state.revoke_api_key(req.api_key)
    return {"status": "ok", "message": "Key revoked."}


@app.get("/admin/keys/stats")
def api_key_stats(_: dict = Depends(require_admin)):
    """Get API key usage statistics. Admin only."""
    return state.get_key_stats()


@app.get("/audit/stats")
def audit_stats(
    date: Optional[str] = Query(None, description="Date in YYYY-MM-DD format, defaults to today"),
    _key_info: dict = Depends(require_admin),
):
    """Get audit log summary. Admin only."""
    return audit_logger.summary(date)


# ==================== CLI Entry Point ====================

def parse_args():
    parser = argparse.ArgumentParser(description="CodeFix Agent API Server")

    # Load CFG defaults first
    cfg_defaults = {
        "host": CFG.server.host,
        "port": CFG.server.port,
        "mode": CFG.server.mode,
        "rate_limit": CFG.server.rate_limit_per_minute,
        "sandbox_timeout": CFG.server.sandbox_timeout,
        "sandbox_max_output": CFG.server.sandbox_max_output,
        "sandbox_languages": ",".join(CFG.server.sandbox_languages),
        "embedding_model": CFG.model.embedding_model,
        "memory_path": CFG.memory.reflection_memory_path,
        "skills_dir": CFG.memory.skills_dir,
        "rag_path": CFG.rag.knowledge_base_path,
        "ollama_url": CFG.model.ollama_base_url,
        "ollama_model": CFG.model.ollama_model,
        "model": CFG.model.policy_model,
    }

    parser.add_argument("--host", default=cfg_defaults["host"], help="Bind host")
    parser.add_argument("--port", type=int, default=cfg_defaults["port"], help="Bind port")
    parser.add_argument("--mode", choices=["local", "server"], default=cfg_defaults["mode"],
                        help="local=Ollama (no GPU), server=transformers (GPU)")
    parser.add_argument("--ollama-url", default=cfg_defaults["ollama_url"],
                        help="Ollama base URL (local mode)")
    parser.add_argument("--ollama-model", default=cfg_defaults["ollama_model"],
                        help="Ollama model name")
    parser.add_argument("--model", default=cfg_defaults["model"],
                        help="Model path (server mode)")
    parser.add_argument("--lora", default=None, help="LoRA adapter path (server mode)")
    parser.add_argument("--quantize", action="store_true", help="Use 4-bit quantization (server mode)")
    parser.add_argument("--embedding-model", default=cfg_defaults["embedding_model"],
                        help="Sentence-transformers model for embedding")
    parser.add_argument("--memory-path", default=cfg_defaults["memory_path"])
    parser.add_argument("--skills-dir", default=cfg_defaults["skills_dir"])
    parser.add_argument("--rag-path", default=cfg_defaults["rag_path"])
    parser.add_argument("--reload", action="store_true",
                        help="Reset L2/L3 memory on startup")
    parser.add_argument("--rate-limit", type=int, default=cfg_defaults["rate_limit"],
                        help="Max requests per key per minute (default from CFG)")
    parser.add_argument("--admin-key", action="append", dest="admin_keys",
                        default=[],
                        help="Pre-register an admin API key (can be repeated). "
                             "Pass the raw key value. Use only for trusted environments.")
    parser.add_argument("--no-auth", action="store_true",
                        help="Disable auth (dev only! exposes all endpoints publicly)")
    parser.add_argument("--sandbox-timeout", type=float, default=cfg_defaults["sandbox_timeout"],
                        help="Shell sandbox timeout in seconds (default from CFG)")
    parser.add_argument("--sandbox-max-output", type=int, default=cfg_defaults["sandbox_max_output"],
                        help="Max chars of sandbox output (default from CFG)")
    parser.add_argument("--sandbox-languages", type=str, default=cfg_defaults["sandbox_languages"],
                        help="Comma-separated allowed languages")
    parser.add_argument("--audit-max-bytes", type=int, default=CFG.memory.audit_log_max_bytes,
                        help="Max audit log file size before rotation (default from CFG)")
    parser.add_argument("--audit-max-lines", type=int, default=CFG.memory.audit_log_max_lines,
                        help="Max audit log lines before rotation (default from CFG)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Rate limit config
    global rate_limiter
    rate_limiter = SlidingWindowRateLimiter(
        max_requests=args.rate_limit,
        window_seconds=60,
    )

    # Initialize shell sandbox
    allowed_langs = [l.strip() for l in args.sandbox_languages.split(",")]
    state.init_sandbox(
        timeout=args.sandbox_timeout,
        max_output_chars=args.sandbox_max_output,
        allowed_languages=allowed_langs,
    )

    # Pre-register admin keys from CLI
    if args.admin_keys:
        for raw_key in args.admin_keys:
            key = raw_key if raw_key.startswith("cfx_") else f"cfx_{raw_key}"
            # Directly insert into the key store (bypass the add_key random generator)
            now = time.time()
            with api_keys._lock:
                api_keys._keys[key] = {
                    "owner": "cli-admin",
                    "is_admin": True,
                    "quota_per_day": 999999,
                    "used_today": 0,
                    "reset_at": now,
                }
            logger.info(f"Pre-registered admin key: {mask_key(key)}")

    if args.no_auth:
        # Dev mode: inject a passthrough dependency
        import fastapi
        global require_api_key, require_admin, rate_check
        def require_api_key(_: str = Header(None)) -> dict:
            return {"owner": "dev", "is_admin": True}
        def require_admin(_: str = Header(None)) -> dict:
            return {"owner": "dev", "is_admin": True}
        def rate_check(_: str = Header(None)) -> dict:
            return {"owner": "dev", "is_admin": True}
        logger.warning("!!! Auth DISABLED (--no-auth) — do not use in production !!!")

    # Initialize memory components for "default" namespace (lazy - only create when needed)
    state._embedding_model_name = args.embedding_model
    # Pre-warm default namespace memory so startup state is consistent
    default_mem = state._get_or_create_memory_for_namespace("default")
    default_mem["reflection_memory"]  # access to ensure created
    default_mem["skill_manager"]
    state.rag_retriever = BugFixRetriever(knowledge_base_path=args.rag_path)

    if args.reload:
        state.reflection_memory.reflections.clear()
        state.reflection_memory.save()
        state.skill_manager.skills.clear()
        state.skill_manager.save_all()
        logger.info("Memory reset.")

    # Build agent for "default" namespace (backward compat)
    # Override default memory paths from CLI args if provided
    if args.memory_path != CFG.memory.reflection_memory_path:
        default_mem_dir = _get_memory_dir("default")
        # When CLI overrides memory path, point the default namespace to that dir
        state._namespace_memories["default"] = {
            "reflection_memory": ReflectionMemory(
                memory_path=args.memory_path,
                embedding_model_name=args.embedding_model,
            ),
            "skill_manager": SkillManager(
                skills_dir=args.skills_dir,
                embedding_model_name=args.embedding_model,
            ),
            "session_store": SessionStore(sessions_dir=str(default_mem_dir / "sessions")),
            "e_pool_path": str(default_mem_dir / "e_pool.json"),
            "x_pool_path": str(default_mem_dir / "x_pool.json"),
        }
    if args.mode == "local":
        state.build_agent_for_namespace(
            "local",
            namespace="default",
            model_name=args.ollama_model,
            base_url=args.ollama_url,
        )
    else:
        state.build_agent_for_namespace(
            "server",
            namespace="default",
            model_path=args.model,
            lora_path=args.lora,
            use_quantization=args.quantize,
        )

    # Update audit logger with rotation config
    global audit_logger
    audit_logger = AuditLogger(
        log_dir=CFG.memory.log_dir,
        max_bytes=args.audit_max_bytes,
        max_lines=args.audit_max_lines,
        backup_count=5,
    )
    logger.info(f"Audit log rotation: max_bytes={args.audit_max_bytes}, max_lines={args.audit_max_lines}")

    logger.info(f"Starting server on http://{args.host}:{args.port}")
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )
