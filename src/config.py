#!/usr/bin/env python3
"""
CodeFix-Agent Unified Configuration
===================================
Loads settings from environment variables (.env) with fallback defaults.
All modules should import from this file instead of hardcoding values.

Usage:
    from config import CFG, ModelConfig, ServerConfig
    print(CFG.policy_model)
"""

from __future__ import annotations

import os
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


# ==================== Path Helpers ====================

def _resolve_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        base = Path(__file__).parent
        p = base / path
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ==================== Dataclasses ====================

@dataclass
class ModelConfig:
    """Model-related configuration."""
    policy_model: str = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    reward_model: str = "Qwen/Qwen2.5-Coder-7B-Instruct"
    sft_lora_path: Optional[str] = None
    embedding_model: str = "all-MiniLM-L6-v2"
    ollama_model: str = "qwen2.5-coder:1.5b"
    ollama_base_url: str = "http://localhost:11434"


@dataclass
class ServerConfig:
    """API server configuration."""
    host: str = "0.0.0.0"
    port: int = 8000
    mode: str = "local"  # "local" (Ollama) or "server" (transformers)
    api_key_prefix: str = "cfx_"
    rate_limit_per_minute: int = 30
    rate_limit_per_day: int = 1000
    sandbox_timeout: float = 10.0
    sandbox_max_output: int = 10000
    sandbox_languages: List[str] = field(default_factory=lambda: ["python", "node"])
    disable_auth: bool = False


@dataclass
class MemoryConfig:
    """Memory and persistence configuration."""
    reflection_memory_path: str = "./runs/reflection_memory.json"
    skills_dir: str = "./runs/skills"
    e_pool_path: str = "./runs/e_pool.json"
    x_pool_path: str = "./runs/x_pool.json"
    sessions_dir: str = "./runs/sessions"
    checkpoint_dir: str = "./runs/checkpoints"
    log_dir: str = "logs"
    audit_log_max_bytes: int = 10 * 1024 * 1024  # 10MB per audit log file
    audit_log_max_lines: int = 100000  # rotate if exceeds this many lines


@dataclass
class TrainingConfig:
    """RL training configuration."""
    output_dir: str = "outputs/codefix-grpo-cot-prm"
    max_steps: int = 1000
    gradient_accumulation_steps: int = 4
    learning_rate: float = 1e-5
    warmup_steps: int = 50
    eval_interval: int = 100
    save_interval: int = 200
    num_generations: int = 8  # GRPO group size
    max_tokens_per_sample: int = 2048


@dataclass
class AgentConfig:
    """Agent runtime configuration."""
    max_iterations: int = 3
    max_cot_tokens: int = 1024
    temperature: float = 0.7
    compile_first: bool = True
    use_rag: bool = True
    use_decentmem: bool = True
    compact_threshold_chars: int = 8000
    recovery_timeout_seconds: float = 300.0
    loop_repeat_threshold: int = 4
    loop_history_depth: int = 10


@dataclass
class RAGConfig:
    """RAG pipeline configuration."""
    knowledge_base_path: str = "./runs/bug_fixes.json"
    bm25_weight: float = 0.6
    vector_weight: float = 0.4
    top_k: int = 5
    rerank_top_k: int = 3
    similarity_threshold: float = 0.35
    chunk_size: int = 512


@dataclass
class LLMConfig:
    """LLM Router configuration."""
    failure_threshold: int = 3
    success_threshold: int = 2
    circuit_open_timeout: float = 30.0
    half_open_max_calls: int = 3
    backoff_base_delay: float = 1.0
    backoff_max_delay: float = 60.0
    backoff_exponent: float = 2.0
    backoff_jitter: float = 0.5
    max_retries: int = 3
    token_tracker_path: str = "./runs/token_tracker.json"
    alert_thresholds: str = "0.80,0.95,1.00"


@dataclass
class CostConfig:
    """Cost calculation configuration."""
    token_tracker_persist_path: str = "./runs/token_tracker.json"
    default_quota_per_day: int = 1000
    alert_webhook_url: Optional[str] = None
    alert_email: Optional[str] = None


# ==================== Main Config ====================

class Config:
    """
    Central configuration singleton.
    Loads from environment variables with fallback defaults.
    Priority: env var > .env file > default value
    """

    def __init__(self):
        # Instantiate sub-configs with defaults
        self.model = ModelConfig()
        self.server = ServerConfig()
        self.memory = MemoryConfig()
        self.training = TrainingConfig()
        self.agent = AgentConfig()
        self.rag = RAGConfig()
        self.llm = LLMConfig()
        self.cost = CostConfig()
        self._loaded = False
        self._load()

    def _load(self):
        if self._loaded:
            return
        self._loaded = True

        env_path = Path(".env")
        if env_path.exists():
            self._load_dotenv(env_path)

        self._load_from_env()
        self._ensure_dirs()
        logger.info("[Config] Configuration loaded successfully")

    def _load(self):
        if self._loaded:
            return
        self._loaded = True

        env_path = Path(".env")
        if env_path.exists():
            self._load_dotenv(env_path)

        self._load_from_env()
        self._ensure_dirs()
        logger.info("[Config] Configuration loaded successfully")

    def _load_dotenv(self, path: Path):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

    def _load_from_env(self):
        # Model config
        self.model.policy_model = os.environ.get(
            "POLICY_MODEL", self.model.policy_model)
        self.model.reward_model = os.environ.get(
            "REWARD_MODEL", self.model.reward_model)
        self.model.sft_lora_path = os.environ.get("SFT_LORA_PATH")
        self.model.embedding_model = os.environ.get(
            "EMBEDDING_MODEL", self.model.embedding_model)
        self.model.ollama_model = os.environ.get(
            "OLLAMA_MODEL", self.model.ollama_model)
        self.model.ollama_base_url = os.environ.get(
            "OLLAMA_BASE_URL", self.model.ollama_base_url)

        # Server config
        self.server.host = os.environ.get("SERVER_HOST", self.server.host)
        self.server.port = int(os.environ.get("SERVER_PORT", str(self.server.port)))
        self.server.mode = os.environ.get("SERVER_MODE", self.server.mode)
        self.server.disable_auth = os.environ.get(
            "DISABLE_AUTH", "false").lower() == "true"
        self.server.rate_limit_per_minute = int(os.environ.get(
            "RATE_LIMIT_PER_MINUTE", str(self.server.rate_limit_per_minute)))
        self.server.sandbox_timeout = float(os.environ.get(
            "SANDBOX_TIMEOUT", str(self.server.sandbox_timeout)))

        # Memory config
        self.memory.reflection_memory_path = os.environ.get(
            "REFLECTION_MEMORY_PATH", self.memory.reflection_memory_path)
        self.memory.skills_dir = os.environ.get(
            "SKILLS_DIR", self.memory.skills_dir)
        self.memory.sessions_dir = os.environ.get(
            "SESSIONS_DIR", self.memory.sessions_dir)
        self.memory.log_dir = os.environ.get(
            "LOG_DIR", self.memory.log_dir)
        self.memory.audit_log_max_bytes = int(os.environ.get(
            "AUDIT_LOG_MAX_BYTES", str(self.memory.audit_log_max_bytes)))
        self.memory.audit_log_max_lines = int(os.environ.get(
            "AUDIT_LOG_MAX_LINES", str(self.memory.audit_log_max_lines)))

        # Training config
        self.training.output_dir = os.environ.get(
            "TRAINING_OUTPUT_DIR", self.training.output_dir)

        # RAG config
        self.rag.knowledge_base_path = os.environ.get(
            "KB_PATH", self.rag.knowledge_base_path)

        # LLM Router config
        self.llm.failure_threshold = int(os.environ.get(
            "LLM_CB_FAILURE_THRESHOLD", str(self.llm.failure_threshold)))
        self.llm.success_threshold = int(os.environ.get(
            "LLM_CB_SUCCESS_THRESHOLD", str(self.llm.success_threshold)))
        self.llm.circuit_open_timeout = float(os.environ.get(
            "LLM_CB_OPEN_TIMEOUT", str(self.llm.circuit_open_timeout)))
        self.llm.max_retries = int(os.environ.get(
            "LLM_MAX_RETRIES", str(self.llm.max_retries)))
        self.llm.backoff_base_delay = float(os.environ.get(
            "LLM_BACKOFF_BASE", str(self.llm.backoff_base_delay)))
        self.llm.backoff_max_delay = float(os.environ.get(
            "LLM_BACKOFF_MAX", str(self.llm.backoff_max_delay)))
        self.llm.token_tracker_path = os.environ.get(
            "TOKEN_TRACKER_PATH", self.llm.token_tracker_path)
        self.llm.alert_thresholds = os.environ.get(
            "QUOTA_ALERT_THRESHOLDS", self.llm.alert_thresholds)

        # Cost config
        self.cost.token_tracker_persist_path = os.environ.get(
            "TOKEN_TRACKER_PATH", self.cost.token_tracker_persist_path)
        self.cost.default_quota_per_day = int(os.environ.get(
            "DEFAULT_QUOTA_PER_DAY", str(self.cost.default_quota_per_day)))
        self.cost.alert_webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
        self.cost.alert_email = os.environ.get("ALERT_EMAIL")

        # Agent config
        self.agent.max_iterations = int(os.environ.get(
            "MAX_ITERATIONS", str(self.agent.max_iterations)))

    def _ensure_dirs(self):
        dirs = [
            self.memory.reflection_memory_path,
            self.memory.skills_dir,
            self.memory.sessions_dir,
            self.memory.checkpoint_dir,
            self.memory.log_dir,
            self.rag.knowledge_base_path,
        ]
        for d in dirs:
            p = Path(d)
            p.parent.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model.__dict__,
            "server": self.server.__dict__,
            "memory": self.memory.__dict__,
            "training": self.training.__dict__,
            "agent": self.agent.__dict__,
            "rag": self.rag.__dict__,
            "llm": self.llm.__dict__,
            "cost": self.cost.__dict__,
        }

    def summary(self) -> str:
        return (
            f"[Config] mode={self.server.mode} | "
            f"policy={self.model.policy_model} | "
            f"reward={self.model.reward_model} | "
            f"sessions_dir={self.memory.sessions_dir}"
        )


# ==================== Singleton Instance ====================

CFG = Config()


# ==================== .env Example ====================

ENV_EXAMPLE = """\
# CodeFix-Agent Configuration
# Copy this file to .env and fill in your values

# ---- Server ----
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
SERVER_MODE=local
DISABLE_AUTH=false
RATE_LIMIT_PER_MINUTE=30
SANDBOX_TIMEOUT=10.0

# ---- Models ----
POLICY_MODEL=Qwen/Qwen2.5-Coder-1.5B-Instruct
REWARD_MODEL=Qwen/Qwen2.5-Coder-7B-Instruct
SFT_LORA_PATH=
EMBEDDING_MODEL=all-MiniLM-L6-v2
OLLAMA_MODEL=qwen2.5-coder:1.5b
OLLAMA_BASE_URL=http://localhost:11434

# ---- Memory Paths ----
REFLECTION_MEMORY_PATH=./runs/reflection_memory.json
SKILLS_DIR=./runs/skills
SESSIONS_DIR=./runs/sessions
LOG_DIR=logs

# ---- Audit Log ----
AUDIT_LOG_MAX_BYTES=10485760
AUDIT_LOG_MAX_LINES=100000

# ---- RAG ----
KB_PATH=./runs/bug_fixes.json

# ---- Agent ----
MAX_ITERATIONS=3
"""


def write_env_example(path: str = ".env.example"):
    """Write the .env.example file."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(ENV_EXAMPLE)
    print(f"Wrote {path}")
