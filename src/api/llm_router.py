"""
LLM Router — Circuit Breaker + Exponential Backoff Retry + Model Degradation Chain
================================================================================

Architecture:
  - Per-model circuit breakers (closed → open → half-open)
  - Exponential backoff with jitter on retry
  - Model degradation chain: try primary → fallback1 → fallback2 → ... → last resort
  - Token usage tracking per model + per API key
  - Async-compatible with both Ollama and OpenAI-compatible APIs

Usage:
    router = LLMRouter()
    router.register_model("gpt-4o", model_fn, cost_per_1k_input=0.005, cost_per_1k_output=0.015)
    router.register_model("gpt-4o-mini", model_fn_mini, cost_per_1k_input=0.00015, cost_per_1k_output=0.0006)
    router.register_fallback_chain(["gpt-4o", "gpt-4o-mini"])

    response = await router.acall("Explain this code", model="gpt-4o")
"""

from __future__ import annotations

import time
import random
import logging
import asyncio
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Any, Dict, List, Tuple
from enum import Enum
from collections import defaultdict

logger = logging.getLogger("llm_router")

# ==================== Exceptions ====================


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open."""
    def __init__(self, model: str, retry_after: float):
        self.model = model
        self.retry_after = retry_after
        super().__init__(f"Circuit open for '{model}', retry after {retry_after:.1f}s")


class AllModelsExhaustedError(Exception):
    """Raised when all models in the fallback chain have failed."""
    def __init__(self, tried: List[str]):
        self.tried = tried
        super().__init__(f"All models exhausted: {tried}")


class RetryableError(Exception):
    """Error that should trigger a retry with backoff."""
    def __init__(self, message: str, is_retryable: bool = True):
        self.message = message
        self.is_retryable = is_retryable
        super().__init__(message)


# ==================== Circuit Breaker State ====================


class CircuitState(Enum):
    CLOSED = "closed"   # Normal operation
    OPEN = "open"        # Failing, reject immediately
    HALF_OPEN = "half_open"  # Testing if it recovered


# ==================== Circuit Breaker ====================


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    success_threshold: int = 2
    open_timeout: float = 30.0
    half_open_max_calls: int = 3


@dataclass
class CircuitBreaker:
    """
    Per-model circuit breaker.

    State machine:
      CLOSED → (failure_threshold failures) → OPEN
      OPEN → (open_timeout elapsed) → HALF_OPEN
      HALF_OPEN → (success_threshold successes) → CLOSED
      HALF_OPEN → (any failure) → OPEN
    """
    model_name: str
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0.0
    half_open_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_success(self):
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.success_threshold:
                    logger.info(f"[CircuitBreaker] {self.model_name}: CLOSED (recovered)")
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.success_count = 0
                    self.half_open_calls = 0
            elif self.state == CircuitState.CLOSED:
                self.failure_count = max(0, self.failure_count - 1)

    def record_failure(self):
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                logger.warning(f"[CircuitBreaker] {self.model_name}: HALF_OPEN → OPEN (failure)")
                self.state = CircuitState.OPEN
                self.last_failure_time = time.time()
                self.success_count = 0
                self.half_open_calls = 0
            elif self.state == CircuitState.CLOSED:
                self.failure_count += 1
                if self.failure_count >= self.config.failure_threshold:
                    logger.warning(f"[CircuitBreaker] {self.model_name}: CLOSED → OPEN ({self.failure_count} failures)")
                    self.state = CircuitState.OPEN
                    self.last_failure_time = time.time()

    def can_execute(self) -> Tuple[bool, float]:
        """Returns (can_execute, retry_after_seconds)."""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True, 0.0
            if self.state == CircuitState.OPEN:
                elapsed = time.time() - self.last_failure_time
                if elapsed >= self.config.open_timeout:
                    logger.info(f"[CircuitBreaker] {self.model_name}: OPEN → HALF_OPEN")
                    self.state = CircuitState.HALF_OPEN
                    self.half_open_calls = 0
                    self.success_count = 0
                    return True, 0.0
                return False, self.config.open_timeout - elapsed
            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls < self.config.half_open_max_calls:
                    self.half_open_calls += 1
                    return True, 0.0
                return False, self.config.open_timeout
            return True, 0.0

    def get_state(self) -> Dict:
        with self._lock:
            return {
                "model": self.model_name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
            }


# ==================== Exponential Backoff ====================


@dataclass
class BackoffConfig:
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponent: float = 2.0
    jitter: float = 0.5
    max_retries: int = 3


def compute_backoff(attempt: int, config: BackoffConfig) -> float:
    """Compute delay with exponential growth + jitter."""
    delay = min(config.base_delay * (config.exponent ** attempt), config.max_delay)
    jitter_range = delay * config.jitter
    delay += random.uniform(-jitter_range, jitter_range)
    return max(0.1, delay)


# ==================== Model Definition ====================


@dataclass
class ModelInfo:
    name: str
    call_fn: Callable  # sync or async function: (prompt, system_prompt, max_tokens, temperature) -> str
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    max_tokens: int = 4096
    is_async: bool = False


# ==================== Token Counter ====================


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0


# ==================== LLM Router ====================


class LLMRouter:
    """
    Multi-model LLM router with circuit breakers, exponential backoff, and fallback chains.

    Key features:
    - Per-model circuit breakers: fast-fail when a model is unhealthy
    - Exponential backoff with jitter on retryable errors
    - Model degradation chain: fall back to cheaper/smaller models on failure
    - Token usage tracking per model
    - Thread-safe for sync use, asyncio-compatible for async use

    Example fallback chain:
        router.register_model("gpt-4o", gpt4o_fn, cost_per_1k_input=0.005, cost_per_1k_output=0.015)
        router.register_model("gpt-4o-mini", gpt4o_mini_fn, cost_per_1k_input=0.00015, cost_per_1k_output=0.0006)
        router.register_model("claude-3-haiku", claude_fn, cost_per_1k_input=0.00025, cost_per_1k_output=0.00125)
        router.register_fallback_chain(["gpt-4o", "gpt-4o-mini", "claude-3-haiku"])
    """

    def __init__(
        self,
        backoff_config: Optional[BackoffConfig] = None,
        circuit_config: Optional[CircuitBreakerConfig] = None,
    ):
        self.backoff = backoff_config or BackoffConfig()
        self.circuit_cfg = circuit_config or CircuitBreakerConfig()

        self._models: Dict[str, ModelInfo] = {}
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._chains: List[List[str]] = []
        self._default_chain: List[str] = []
        self._lock = threading.Lock()

        self._token_usage: Dict[str, TokenUsage] = defaultdict(TokenUsage)

        self._retryable_errors = {
            "rate_limit", "too many requests", "timeout", "connection",
            "service unavailable", "503", "502", "429", "500", "502",
        }

    def register_model(
        self,
        name: str,
        call_fn: Callable,
        *,
        cost_per_1k_input: float = 0.0,
        cost_per_1k_output: float = 0.0,
        max_tokens: int = 4096,
        is_async: bool = False,
        circuit_config: Optional[CircuitBreakerConfig] = None,
    ):
        """Register a model with its call function and cost info."""
        with self._lock:
            self._models[name] = ModelInfo(
                name=name,
                call_fn=call_fn,
                cost_per_1k_input=cost_per_1k_input,
                cost_per_1k_output=cost_per_1k_output,
                max_tokens=max_tokens,
                is_async=is_async,
            )
            self._breakers[name] = CircuitBreaker(
                model_name=name,
                config=circuit_config or self.circuit_cfg,
            )
            logger.info(f"[LLMRouter] Registered model: {name} "
                        f"(input=${cost_per_1k_input}/1k, output=${cost_per_1k_output}/1k)")

    def register_fallback_chain(self, chain: List[str]):
        """
        Register a fallback chain. The first model in the chain is the primary.

        Multiple chains can be registered. The first registered chain is the default.
        """
        for name in chain:
            if name not in self._models:
                raise ValueError(f"Model '{name}' must be registered before adding to chain")
        with self._lock:
            self._chains.append(list(chain))
            if not self._default_chain:
                self._default_chain = list(chain)

    def _get_breaker(self, model: str) -> CircuitBreaker:
        return self._breakers.get(model)

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _compute_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        info = self._models.get(model)
        if not info:
            return 0.0
        return (
            (prompt_tokens / 1000.0) * info.cost_per_1k_input
            + (completion_tokens / 1000.0) * info.cost_per_1k_output
        )

    def _record_usage(self, model: str, prompt_tokens: int, completion_tokens: int):
        with self._lock:
            usage = self._token_usage[model]
            cost = self._compute_cost(model, prompt_tokens, completion_tokens)
            usage.prompt_tokens += prompt_tokens
            usage.completion_tokens += completion_tokens
            usage.total_tokens += prompt_tokens + completion_tokens
            usage.total_cost += cost

    def _is_retryable(self, error: Exception) -> bool:
        msg = str(error).lower()
        return any(tag in msg for tag in self._retryable_errors)

    def _call_sync(
        self,
        model: str,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        model_info = self._models[model]
        fn = model_info.call_fn

        prompt_tokens = self._estimate_tokens(prompt + system_prompt)

        if asyncio.iscoroutinefunction(fn):
            return asyncio.run(fn(prompt, system_prompt, max_tokens, temperature))
        else:
            return fn(prompt, system_prompt, max_tokens, temperature)

    async def _call_async(
        self,
        model: str,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        model_info = self._models[model]
        fn = model_info.call_fn

        prompt_tokens = self._estimate_tokens(prompt + system_prompt)

        if asyncio.iscoroutinefunction(fn):
            return await fn(prompt, system_prompt, max_tokens, temperature)
        else:
            return fn(prompt, system_prompt, max_tokens, temperature)

    def call(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_prompt: str = "",
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        """
        Synchronous call with circuit breaker + exponential backoff + fallback.

        If `model` is None, uses the default fallback chain.
        """
        if model:
            return self._call_single_with_retry(
                model, prompt, system_prompt, max_tokens, temperature
            )

        chain = self._default_chain
        if not chain:
            raise ValueError("No fallback chain registered. Call register_fallback_chain() first.")

        last_error = None
        for attempt in range(self.backoff.max_retries):
            for m in chain:
                breaker = self._get_breaker(m)
                can_exec, retry_after = breaker.can_execute() if breaker else (True, 0.0)

                if not can_exec:
                    continue

                try:
                    result = self._call_single_with_retry(
                        m, prompt, system_prompt, max_tokens, temperature
                    )
                    return result
                except Exception as e:
                    last_error = e
                    if breaker:
                        breaker.record_failure()
                    if not self._is_retryable(e):
                        raise
                    continue

            if last_error and self._is_retryable(last_error):
                delay = compute_backoff(attempt, self.backoff)
                logger.warning(f"[LLMRouter] All models failed, retrying in {delay:.1f}s "
                               f"(attempt {attempt + 1}/{self.backoff.max_retries})")
                time.sleep(delay)

        raise AllModelsExhaustedError(chain) from last_error

    def _call_single_with_retry(
        self,
        model: str,
        prompt: str,
        system_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        breaker = self._get_breaker(model)
        can_exec, retry_after = breaker.can_execute() if breaker else (True, 0.0)

        if not can_exec:
            raise CircuitOpenError(model, retry_after)

        prompt_tokens = self._estimate_tokens(prompt + system_prompt)

        for attempt in range(self.backoff.max_retries):
            try:
                result = self._call_sync(model, prompt, system_prompt, max_tokens, temperature)
                if breaker:
                    breaker.record_success()

                completion_tokens = self._estimate_tokens(result)
                self._record_usage(model, prompt_tokens, completion_tokens)
                return result

            except Exception as e:
                if breaker:
                    breaker.record_failure()

                if not self._is_retryable(e):
                    raise

                if attempt < self.backoff.max_retries - 1:
                    delay = compute_backoff(attempt, self.backoff)
                    logger.warning(f"[LLMRouter] {model} failed (attempt {attempt + 1}): {e}. "
                                   f"Retrying in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    raise

        raise RetryableError(f"Max retries exceeded for model '{model}'")

    async def acall(
        self,
        prompt: str,
        model: Optional[str] = None,
        system_prompt: str = "",
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        """Async call with circuit breaker + exponential backoff + fallback."""
        if model:
            return await self._acall_single_with_retry(
                model, prompt, system_prompt, max_tokens, temperature
            )

        chain = self._default_chain
        if not chain:
            raise ValueError("No fallback chain registered. Call register_fallback_chain() first.")

        last_error = None
        for attempt in range(self.backoff.max_retries):
            for m in chain:
                breaker = self._get_breaker(m)
                can_exec, retry_after = breaker.can_execute() if breaker else (True, 0.0)

                if not can_exec:
                    continue

                try:
                    result = await self._acall_single_with_retry(
                        m, prompt, system_prompt, max_tokens, temperature
                    )
                    return result
                except Exception as e:
                    last_error = e
                    if breaker:
                        breaker.record_failure()
                    if not self._is_retryable(e):
                        raise
                    continue

            if last_error and self._is_retryable(last_error):
                delay = compute_backoff(attempt, self.backoff)
                logger.warning(f"[LLMRouter] All models failed (async), retrying in {delay:.1f}s")
                await asyncio.sleep(delay)

        raise AllModelsExhaustedError(chain) from last_error

    async def _acall_single_with_retry(
        self,
        model: str,
        prompt: str,
        system_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        breaker = self._get_breaker(model)
        can_exec, retry_after = breaker.can_execute() if breaker else (True, 0.0)

        if not can_exec:
            raise CircuitOpenError(model, retry_after)

        prompt_tokens = self._estimate_tokens(prompt + system_prompt)

        for attempt in range(self.backoff.max_retries):
            try:
                result = await self._call_async(model, prompt, system_prompt, max_tokens, temperature)
                if breaker:
                    breaker.record_success()

                completion_tokens = self._estimate_tokens(result)
                self._record_usage(model, prompt_tokens, completion_tokens)
                return result

            except Exception as e:
                if breaker:
                    breaker.record_failure()

                if not self._is_retryable(e):
                    raise

                if attempt < self.backoff.max_retries - 1:
                    delay = compute_backoff(attempt, self.backoff)
                    logger.warning(f"[LLMRouter] {model} failed async (attempt {attempt + 1}): {e}. "
                                   f"Retrying in {delay:.1f}s")
                    await asyncio.sleep(delay)
                else:
                    raise

        raise RetryableError(f"Max retries exceeded for model '{model}'")

    def get_circuit_states(self) -> Dict[str, Dict]:
        with self._lock:
            return {name: br.get_state() for name, br in self._breakers.items()}

    def get_token_usage(self, model: Optional[str] = None) -> Dict:
        with self._lock:
            if model:
                u = self._token_usage.get(model, TokenUsage())
                return {
                    "model": model,
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "total_tokens": u.total_tokens,
                    "total_cost_usd": round(u.total_cost, 6),
                }
            return {
                name: {
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "total_tokens": u.total_tokens,
                    "total_cost_usd": round(u.total_cost, 6),
                }
                for name, u in self._token_usage.items()
            }

    def reset_circuit(self, model: str):
        with self._lock:
            if model in self._breakers:
                with self._breakers[model]._lock:
                    self._breakers[model].state = CircuitState.CLOSED
                    self._breakers[model].failure_count = 0
                    self._breakers[model].success_count = 0
                    self._breakers[model].half_open_calls = 0
                logger.info(f"[LLMRouter] Circuit reset for {model}")

    def get_stats(self) -> Dict:
        return {
            "registered_models": list(self._models.keys()),
            "default_chain": self._default_chain,
            "circuit_breakers": self.get_circuit_states(),
            "token_usage": self.get_token_usage(),
        }


# ==================== Integration Helper ====================


def wrap_ollama_for_router(
    model_name: str,
    base_url: str = "http://localhost:11434",
    timeout: int = 120,
) -> Callable:
    """Wrap an Ollama model into a router-compatible call function."""

    def call_fn(prompt: str, system_prompt: str = "",
                max_new_tokens: int = 512, temperature: float = 0.7) -> str:
        import requests

        payload = {
            "model": model_name,
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
        resp = requests.post(
            f"{base_url}/api/chat",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    return call_fn


def wrap_openai_for_router(
    model: str,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
) -> Callable:
    """Wrap an OpenAI-compatible API into a router-compatible call function."""
    import requests

    def call_fn(prompt: str, system_prompt: str = "",
                max_new_tokens: int = 512, temperature: float = 0.7) -> str:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                *([{"role": "system", "content": system_prompt}] if system_prompt else []),
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_new_tokens,
            "temperature": temperature,
        }
        resp = requests.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    return call_fn
