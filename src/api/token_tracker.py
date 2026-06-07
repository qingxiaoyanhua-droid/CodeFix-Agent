"""
Token Usage Tracker — Per-API-key统计 + 成本计算 + 配额告警
================================================================================

Features:
  - Per-API-key token 统计（prompt / completion / total）
  - Per-API-key 成本计算（基于 model pricing）
  - 配额告警（配额 80% / 95% / 100% 三档）
  - 支持多 API-key 聚合视图
  - 持久化到 JSON，进程重启后数据不丢失
  - 回调式告警钩子（webhook / 日志 / 自定义函数）

Usage:
    tracker = TokenTracker()
    tracker.register_model("gpt-4o", cost_per_1k_input=0.005, cost_per_1k_output=0.015)
    tracker.register_model("gpt-4o-mini", cost_per_1k_input=0.00015, cost_per_1k_output=0.0006)

    tracker.record(
        api_key="cfx_abc123...",
        model="gpt-4o",
        prompt_tokens=1000,
        completion_tokens=500,
    )

    stats = tracker.get_stats(api_key="cfx_abc123...")
    quota = tracker.get_quota_status(api_key="cfx_abc123...")
"""

from __future__ import annotations

import os
import json
import time
import logging
import threading
import hashlib
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable, List, Dict, Any
from collections import defaultdict
from datetime import datetime, timedelta

logger = logging.getLogger("token_tracker")

# ==================== Alert Levels ====================


class AlertLevel:
    OK = "ok"
    WARNING = "warning"   # >= 80% quota used
    CRITICAL = "critical" # >= 95% quota used
    EXCEEDED = "exceeded" # >= 100% quota used


# ==================== Alert Hook ====================


@dataclass
class QuotaAlert:
    api_key: str
    owner: str
    level: str
    quota_used: int
    quota_limit: int
    usage_percent: float
    cost_usd: float
    reset_at: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


# ==================== Model Pricing ====================


@dataclass
class ModelPricing:
    cost_per_1k_input: float
    cost_per_1k_output: float
    max_context_tokens: int = 128000


# ==================== Per-Key Stats ====================


@dataclass
class KeyUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_requests: int = 0
    total_cost_usd: float = 0.0
    last_request_at: Optional[float] = None


@dataclass
class KeyStats:
    api_key: str
    owner: str
    quota_per_day: int
    used_today: int = 0
    used_tokens_prompt: int = 0
    used_tokens_completion: int = 0
    used_tokens_total: int = 0
    total_requests: int = 0
    total_cost_usd: float = 0.0
    created_at: float = field(default_factory=time.time)
    reset_at: float = field(default_factory=time.time)
    alert_level: str = AlertLevel.OK
    model_breakdown: Dict[str, KeyUsage] = field(default_factory=dict)
    request_history: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["usage_percent"] = round(self.used_today / max(self.quota_per_day, 1) * 100, 2)
        return d


# ==================== Token Tracker ====================


class TokenTracker:
    """
    Per-API-key token tracking with cost calculation and quota alerting.

    All data is persisted to JSON on every write so it survives process restarts.
    Alert callbacks are invoked synchronously when a quota threshold is crossed.
    """

    DEFAULT_PERSIST_PATH = "./runs/token_tracker.json"

    def __init__(
        self,
        persist_path: Optional[str] = None,
        alert_thresholds: tuple = (0.80, 0.95, 1.00),
        alert_callback: Optional[Callable[[QuotaAlert], None]] = None,
    ):
        self.persist_path = Path(persist_path or self.DEFAULT_PERSIST_PATH)
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        self.alert_thresholds = alert_thresholds
        self.alert_callback = alert_callback

        self._model_pricing: Dict[str, ModelPricing] = {}
        self._key_stats: Dict[str, KeyStats] = {}
        self._alert_history: List[QuotaAlert] = []
        self._lock = threading.Lock()
        self._alerted_keys: Dict[str, str] = {}  # key -> last_alert_level

        self._load()

        # Default model pricing (can be overridden)
        self._set_default_pricing()

    def _set_default_pricing(self):
        defaults = {
            "gpt-4o": ModelPricing(cost_per_1k_input=0.005, cost_per_1k_output=0.015),
            "gpt-4o-mini": ModelPricing(cost_per_1k_input=0.00015, cost_per_1k_output=0.0006),
            "gpt-4-turbo": ModelPricing(cost_per_1k_input=0.01, cost_per_1k_output=0.03),
            "gpt-3.5-turbo": ModelPricing(cost_per_1k_input=0.0005, cost_per_1k_output=0.0015),
            "claude-3-opus": ModelPricing(cost_per_1k_input=0.015, cost_per_1k_output=0.075),
            "claude-3-sonnet": ModelPricing(cost_per_1k_input=0.003, cost_per_1k_output=0.015),
            "claude-3-haiku": ModelPricing(cost_per_1k_input=0.00025, cost_per_1k_output=0.00125),
            "gemini-1.5-pro": ModelPricing(cost_per_1k_input=0.00125, cost_per_1k_output=0.005),
            "gemini-1.5-flash": ModelPricing(cost_per_1k_input=0.000075, cost_per_1k_output=0.0003),
        }
        for name, pricing in defaults.items():
            if name not in self._model_pricing:
                self._model_pricing[name] = pricing

    def register_model(
        self,
        model: str,
        cost_per_1k_input: float,
        cost_per_1k_output: float,
        max_context_tokens: int = 128000,
    ):
        """Register or update pricing for a model."""
        with self._lock:
            self._model_pricing[model] = ModelPricing(
                cost_per_1k_input=cost_per_1k_input,
                cost_per_1k_output=cost_per_1k_output,
                max_context_tokens=max_context_tokens,
            )

    def register_api_key(
        self,
        api_key: str,
        owner: str,
        quota_per_day: int = 1000,
    ):
        """Pre-register an API key with its quota."""
        with self._lock:
            if api_key not in self._key_stats:
                now = time.time()
                self._key_stats[api_key] = KeyStats(
                    api_key=api_key,
                    owner=owner,
                    quota_per_day=quota_per_day,
                    reset_at=now,
                )

    def _load(self):
        if not self.persist_path.exists():
            return
        try:
            with open(self.persist_path, encoding="utf-8") as f:
                raw = json.load(f)

            model_pricing_raw = raw.get("model_pricing", {})
            self._model_pricing = {
                k: ModelPricing(**v) for k, v in model_pricing_raw.items()
            }

            self._key_stats = {}
            for k, v in raw.get("key_stats", {}).items():
                ms = {}
                for mk, mv in v.get("model_breakdown", {}).items():
                    ms[mk] = KeyUsage(**mv)
                v["model_breakdown"] = ms
                self._key_stats[k] = KeyStats(**v)

            self._alert_history = [
                QuotaAlert(**a) for a in raw.get("alert_history", [])
            ]

            logger.info(f"[TokenTracker] Loaded {len(self._key_stats)} keys from {self.persist_path}")
        except (json.JSONDecodeError, IOError, TypeError) as e:
            logger.warning(f"[TokenTracker] Failed to load persisted data: {e}")

    def _save(self):
        try:
            data = {
                "model_pricing": {k: asdict(v) for k, v in self._model_pricing.items()},
                "key_stats": {
                    k: {
                        **asdict(v),
                        "model_breakdown": {mk: asdict(mv) for mk, mv in v.model_breakdown.items()},
                    }
                    for k, v in self._key_stats.items()
                },
                "alert_history": [asdict(a) for a in self._alert_history[-1000:]],
            }
            with open(self.persist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"[TokenTracker] Failed to persist data: {e}")

    def _compute_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        pricing = self._model_pricing.get(model)
        if not pricing:
            return 0.0
        return (
            (prompt_tokens / 1000.0) * pricing.cost_per_1k_input
            + (completion_tokens / 1000.0) * pricing.cost_per_1k_output
        )

    def _check_quota_reset(self, stats: KeyStats):
        now = time.time()
        if now - stats.reset_at >= 86400:
            stats.used_today = 0
            stats.reset_at = now
            stats.alert_level = AlertLevel.OK
            self._alerted_keys.pop(stats.api_key, None)

    def _compute_alert_level(self, stats: KeyStats) -> str:
        pct = stats.used_today / max(stats.quota_per_day, 1)
        if pct >= 1.0:
            return AlertLevel.EXCEEDED
        elif pct >= self.alert_thresholds[1]:
            return AlertLevel.CRITICAL
        elif pct >= self.alert_thresholds[0]:
            return AlertLevel.WARNING
        return AlertLevel.OK

    def _should_fire_alert(self, stats: KeyStats, new_level: str) -> bool:
        last = self._alerted_keys.get(stats.api_key, AlertLevel.OK)
        priority = {AlertLevel.OK: 0, AlertLevel.WARNING: 1, AlertLevel.CRITICAL: 2, AlertLevel.EXCEEDED: 3}
        return priority.get(new_level, 0) > priority.get(last, 0)

    def _fire_alert(self, stats: KeyStats, level: str):
        alert = QuotaAlert(
            api_key=self._mask_key(stats.api_key),
            owner=stats.owner,
            level=level,
            quota_used=stats.used_today,
            quota_limit=stats.quota_per_day,
            usage_percent=round(stats.used_today / max(stats.quota_per_day, 1) * 100, 2),
            cost_usd=round(stats.total_cost_usd, 6),
        )

        self._alert_history.append(alert)
        self._alerted_keys[stats.api_key] = level
        stats.alert_level = level

        if self.alert_callback:
            try:
                self.alert_callback(alert)
            except Exception as e:
                logger.error(f"[TokenTracker] Alert callback failed: {e}")

        level_str = {
            AlertLevel.WARNING: "WARNING",
            AlertLevel.CRITICAL: "CRITICAL",
            AlertLevel.EXCEEDED: "QUOTA EXCEEDED",
        }.get(level, level.upper())

        logger.warning(
            f"[TokenTracker][{level_str}] {stats.owner} "
            f"({self._mask_key(stats.api_key)}): "
            f"{stats.used_today}/{stats.quota_per_day} "
            f"({alert.usage_percent:.1f}%), cost=${alert.cost_usd:.4f}"
        )

    @staticmethod
    def _mask_key(key: str) -> str:
        if not key or len(key) < 12:
            return "***"
        return key[:10] + "***"

    def record(
        self,
        api_key: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: Optional[int] = None,
        owner: Optional[str] = None,
    ):
        """
        Record a single LLM call for an API key.

        If total_tokens is provided, it takes precedence.
        If token counts are 0, a rough estimate from text length will be used.
        """
        if total_tokens is None:
            total_tokens = prompt_tokens + completion_tokens
        elif prompt_tokens == 0 and completion_tokens == 0:
            prompt_tokens = total_tokens // 2
            completion_tokens = total_tokens - prompt_tokens

        cost = self._compute_cost(model, prompt_tokens, completion_tokens)
        now = time.time()

        with self._lock:
            if api_key not in self._key_stats:
                self._key_stats[api_key] = KeyStats(
                    api_key=api_key,
                    owner=owner or "unknown",
                    quota_per_day=1000,
                    reset_at=now,
                )

            stats = self._key_stats[api_key]
            self._check_quota_reset(stats)

            stats.used_today += 1
            stats.used_tokens_prompt += prompt_tokens
            stats.used_tokens_completion += completion_tokens
            stats.used_tokens_total += total_tokens
            stats.total_requests += 1
            stats.total_cost_usd += cost
            stats.last_request_at = now

            if model not in stats.model_breakdown:
                stats.model_breakdown[model] = KeyUsage()
            mb = stats.model_breakdown[model]
            mb.prompt_tokens += prompt_tokens
            mb.completion_tokens += completion_tokens
            mb.total_tokens += total_tokens
            mb.total_requests += 1
            mb.total_cost_usd += cost
            mb.last_request_at = now

            stats.request_history.append({
                "timestamp": now,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_usd": cost,
            })
            if len(stats.request_history) > 1000:
                stats.request_history = stats.request_history[-500:]

            new_level = self._compute_alert_level(stats)
            if self._should_fire_alert(stats, new_level):
                self._fire_alert(stats, new_level)

            self._save()

    def get_stats(self, api_key: Optional[str] = None) -> Dict[str, Any]:
        """Get stats for a specific API key, or all keys."""
        with self._lock:
            if api_key:
                stats = self._key_stats.get(api_key)
                if not stats:
                    return {}
                self._check_quota_reset(stats)
                result = stats.to_dict()
                result["api_key"] = self._mask_key(stats.api_key)
                result["usage_percent"] = round(
                    stats.used_today / max(stats.quota_per_day, 1) * 100, 2
                )
                return result

            total_cost = 0.0
            total_tokens = 0
            total_requests = 0
            by_owner: Dict[str, Dict] = defaultdict(lambda: {
                "cost_usd": 0.0, "tokens": 0, "requests": 0, "keys": []
            })

            for k, s in self._key_stats.items():
                self._check_quota_reset(s)
                total_cost += s.total_cost_usd
                total_tokens += s.used_tokens_total
                total_requests += s.total_requests
                by_owner[s.owner]["cost_usd"] += s.total_cost_usd
                by_owner[s.owner]["tokens"] += s.used_tokens_total
                by_owner[s.owner]["requests"] += s.total_requests
                by_owner[s.owner]["keys"].append(self._mask_key(k))

            return {
                "total_cost_usd": round(total_cost, 6),
                "total_tokens": total_tokens,
                "total_requests": total_requests,
                "total_keys": len(self._key_stats),
                "by_owner": dict(by_owner),
            }

    def get_quota_status(self, api_key: str) -> Dict:
        """Get quota status for a specific API key."""
        with self._lock:
            stats = self._key_stats.get(api_key)
            if not stats:
                return {"error": "API key not found"}
            self._check_quota_reset(stats)
            pct = stats.used_today / max(stats.quota_per_day, 1)
            return {
                "api_key": self._mask_key(stats.api_key),
                "owner": stats.owner,
                "used_today": stats.used_today,
                "quota_per_day": stats.quota_per_day,
                "remaining": max(0, stats.quota_per_day - stats.used_today),
                "usage_percent": round(pct * 100, 2),
                "alert_level": stats.alert_level,
                "reset_at": datetime.fromtimestamp(stats.reset_at).isoformat() + "Z",
                "total_cost_usd": round(stats.total_cost_usd, 6),
                "total_tokens": stats.used_tokens_total,
            }

    def get_model_pricing(self) -> Dict:
        with self._lock:
            return {k: asdict(v) for k, v in self._model_pricing.items()}

    def get_alert_history(self, limit: int = 50) -> List[Dict]:
        with self._lock:
            return [asdict(a) for a in self._alert_history[-limit:]]

    def get_top_consumers(self, limit: int = 10, by: str = "cost") -> List[Dict]:
        """Return top N API keys by cost or tokens."""
        with self._lock:
            items = []
            for k, s in self._key_stats.items():
                self._check_quota_reset(s)
                items.append({
                    "api_key": self._mask_key(k),
                    "owner": s.owner,
                    "cost_usd": round(s.total_cost_usd, 6),
                    "total_tokens": s.used_tokens_total,
                    "total_requests": s.total_requests,
                    "usage_percent": round(s.used_today / max(s.quota_per_day, 1) * 100, 2),
                })
            key = {"cost": "cost_usd", "tokens": "total_tokens", "requests": "total_requests"}.get(by, "cost")
            items.sort(key=lambda x: x[key], reverse=True)
            return items[:limit]

    def set_quota(self, api_key: str, quota_per_day: int):
        """Update the daily quota for an API key."""
        with self._lock:
            if api_key in self._key_stats:
                self._key_stats[api_key].quota_per_day = quota_per_day
                self._save()

    def update_model_pricing(
        self,
        api_key: str,
        model: str,
        cost_per_1k_input: float,
        cost_per_1k_output: float,
    ):
        """Retroactively update cost for a model and recompute all tracked costs."""
        with self._lock:
            if model not in self._model_pricing:
                self._model_pricing[model] = ModelPricing(cost_per_1k_input, cost_per_1k_output)
            old = self._model_pricing[model]
            new = ModelPricing(cost_per_1k_input, cost_per_1k_output)
            self._model_pricing[model] = new

            if api_key in self._key_stats:
                stats = self._key_stats[api_key]
                if model in stats.model_breakdown:
                    mb = stats.model_breakdown[model]
                    stats.total_cost_usd -= mb.total_cost_usd
                    mb.total_cost_usd = (
                        (mb.prompt_tokens / 1000.0) * cost_per_1k_input
                        + (mb.completion_tokens / 1000.0) * cost_per_1k_output
                    )
                    stats.total_cost_usd += mb.total_cost_usd

            self._save()

    def delete_key(self, api_key: str):
        with self._lock:
            if api_key in self._key_stats:
                del self._key_stats[api_key]
                self._alerted_keys.pop(api_key, None)
                self._save()
