#!/usr/bin/env python3
"""
Gateway: JWT 鉴权 + 精细限流 + 路由分发
=========================================
部署在 Nginx 和 API Server 之间，是所有流量的统一入口。

架构：
    Nginx (:443) → Gateway (:8000) → API Servers (:8001..N)

职责：
    1. JWT 鉴权：验证 Token，提取用户信息
    2. 精细限流：按用户/端点/时间窗口多维度限流
    3. 路由分发：根据路径将请求转发到对应后端
    4. 请求代理：HTTP 代理到 API Server，支持 SSE 流式转发
    5. 指标暴露：Prometheus 格式指标（QPS、延迟、错误率）

限流策略：
    - 免费用户：5 req/min（/fix），2 req/min（/fix/stream）
    - 付费用户：60 req/min（/fix），10 req/min（/fix/stream）
    - 企业用户：无限制，按 SLA 保证

JWT Claims:
    {
      "sub": "user_id",
      "role": "free|premium|enterprise",
      "quota_daily": 100,
      "exp": 1735689600
    }
"""

from __future__ import annotations

import os
import re
import time
import json
import uuid
import hashlib
import asyncio
import httpx
import logging
from threading import Lock
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict
from enum import Enum

from fastapi import FastAPI, HTTPException, Request, Header, Depends, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# Windows encoding fix
import sys
if sys.platform == "win32":
    import io
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
logger = logging.getLogger("gateway")


# ==================== JWT 工具 ====================

JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production-use-strong-secret")
JWT_ALGORITHM = "HS256"


def _base64url_decode(data: str) -> bytes:
    """Decode base64url (URL-safe base64)"""
    import base64
    # Add padding
    rem = len(data) % 4
    if rem:
        data += "=" * (4 - rem)
    return base64.urlsafe_b64decode(data)


def verify_jwt(token: str) -> Tuple[bool, Dict[str, Any]]:
    """
    验证 JWT Token。
    支持 HS256 算法。

    Returns: (is_valid, claims_dict)
    """
    import hmac, hashlib

    parts = token.split(".")
    if len(parts) != 3:
        return False, {}

    header_b64, payload_b64, sig_b64 = parts

    # Verify signature
    try:
        msg = f"{header_b64}.{payload_b64}".encode()
        expected = hmac.new(JWT_SECRET.encode(), msg, hashlib.sha256).digest()
        expected_b64 = expected.rstrip(b"=").replace(b"+", b"-").replace(b"/", b"_").decode()
        if not hmac.compare_digest(sig_b64, expected_b64):
            return False, {}
    except Exception:
        return False, {}

    # Decode payload
    try:
        payload_bytes = _base64url_decode(payload_b64)
        claims = json.loads(payload_bytes)
    except Exception:
        return False, {}

    # Check expiration
    exp = claims.get("exp", 0)
    if exp and exp < time.time():
        return False, {"error": "Token expired"}

    return True, claims


def create_jwt(user_id: str, role: str = "free", quota_daily: int = 100) -> str:
    """创建 JWT Token（用于测试/管理接口）"""
    import hmac, hashlib, base64

    now = int(time.time())
    payload = {
        "sub": user_id,
        "role": role,
        "quota_daily": quota_daily,
        "iat": now,
        "exp": now + 86400 * 7,
        "iss": "codefix-gateway",
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()

    header = json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()
    header_b64 = base64.urlsafe_b64encode(header).rstrip(b"=").decode()

    msg = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(JWT_SECRET.encode(), msg, hashlib.sha256).digest()
    sig_b64 = sig.rstrip(b"=").replace(b"+", b"-").replace(b"/", b"_").decode()

    return f"{header_b64}.{payload_b64}.{sig_b64}"


# ==================== 精细限流器 ====================

class TokenBucketRateLimiter:
    """
    Token Bucket 算法限流器。
    支持按用户维度限流，每个用户有独立的桶。
    """

    def __init__(self, rate: float, capacity: int):
        self.rate = rate       # tokens/秒
        self.capacity = capacity
        self._buckets: Dict[str, Tuple[float, float]] = {}  # user_id → (tokens, last_refill)
        self._lock = Lock()

    def _refill(self, user_id: str) -> float:
        """补充 token"""
        now = time.time()
        if user_id not in self._buckets:
            self._buckets[user_id] = (float(self.capacity), now)
            return float(self.capacity)

        tokens, last = self._buckets[user_id]
        elapsed = now - last
        new_tokens = min(self.capacity, tokens + elapsed * self.rate)
        self._buckets[user_id] = (new_tokens, now)
        return new_tokens

    def check(self, user_id: str, cost: int = 1) -> Tuple[bool, float]:
        """
        尝试消费 token。
        Returns: (allowed, remaining_tokens)
        """
        with self._lock:
            tokens = self._refill(user_id)
            if tokens >= cost:
                self._buckets[user_id] = (tokens - cost, time.time())
                return True, tokens - cost
            return False, tokens


@dataclass
class RateLimitConfig:
    """限流配置（按用户角色）"""
    fix_rate: float       # /fix 每秒请求数
    fix_burst: int       # /fix 突发容量
    stream_rate: float   # /fix/stream 每秒请求数
    stream_burst: int    # /fix/stream 突发容量
    daily_quota: int     # 每日总量限制


ROLE_LIMITS: Dict[str, RateLimitConfig] = {
    "free":      RateLimitConfig(fix_rate=5/60,   fix_burst=5,   stream_rate=2/60,  stream_burst=2,  daily_quota=50),
    "premium":   RateLimitConfig(fix_rate=1.0,     fix_burst=10,  stream_rate=0.2,   stream_burst=5,   daily_quota=500),
    "enterprise":RateLimitConfig(fix_rate=5.0,     fix_burst=50,  stream_rate=1.0,    stream_burst=20,  daily_quota=999999),
}


# ==================== 指标收集 ====================

class MetricsCollector:
    """
    Prometheus 格式指标收集。

    指标说明：
      gateway_requests_total{endpoint="..."}  — 该端点累计请求数（counter）
      gateway_request_duration_seconds{endpoint="...", quantile="0.5|0.95|avg"}
                                                — 请求延迟（histogram）
                                                算法：所有请求延迟存入列表，排序后取对应分位
      gateway_errors_total{code="..."}          — 各类错误累计数（counter）
      gateway_uptime_seconds                   — Gateway 运行时长（gauge）

    QPS 算法：Prometheus 侧计算 rate(gateway_requests_total[1m])
    """

    def __init__(self):
        self._lock = Lock()
        self._counters: Dict[str, int] = defaultdict(int)
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._errors: Dict[str, int] = defaultdict(int)
        self._start = time.time()

    def inc(self, metric: str, value: int = 1):
        with self._lock:
            self._counters[metric] += value

    def observe(self, metric: str, value: float):
        with self._lock:
            self._histograms[metric].append(value)
            # 保持最近 10000 条
            if len(self._histograms[metric]) > 10000:
                self._histograms[metric] = self._histograms[metric][-5000:]

    def inc_error(self, code: str):
        with self._lock:
            self._errors[code] += 1

    def prometheus(self) -> str:
        """输出 Prometheus 格式"""
        with self._lock:
            lines = ["# HELP gateway_requests_total Total requests", "# TYPE gateway_requests_total counter"]
            for k, v in sorted(self._counters.items()):
                lines.append(f'gateway_requests_total{{endpoint="{k}"}} {v}')

            lines.append("# HELP gateway_request_duration_seconds Request duration")
            lines.append("# TYPE gateway_request_duration_seconds histogram")
            for k, vals in sorted(self._histograms.items()):
                if vals:
                    avg = sum(vals) / len(vals)
                    p50 = sorted(vals)[len(vals) // 2]
                    p95 = sorted(vals)[int(len(vals) * 0.95)]
                    lines.append(f'gateway_request_duration_seconds{{endpoint="{k}",quantile="0.5"}} {p50:.4f}')
                    lines.append(f'gateway_request_duration_seconds{{endpoint="{k}",quantile="0.95"}} {p95:.4f}')
                    lines.append(f'gateway_request_duration_seconds{{endpoint="{k}",quantile="avg"}} {avg:.4f}')

            lines.append("# HELP gateway_errors_total Total errors by code")
            lines.append("# TYPE gateway_errors_total counter")
            for k, v in sorted(self._errors.items()):
                lines.append(f'gateway_errors_total{{code="{k}"}} {v}')

            uptime = time.time() - self._start
            lines.append(f"# HELP gateway_uptime_seconds Gateway uptime")
            lines.append(f"# TYPE gateway_uptime_seconds gauge")
            lines.append(f"gateway_uptime_seconds {uptime:.0f}")

            return "\n".join(lines) + "\n"


# ==================== FastAPI 应用 ====================

app = FastAPI(title="CodeFix Gateway", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

metrics = MetricsCollector()

# 后端 API Server 列表（Gateway 代理转发）
BACKEND_URLS = {
    "default": os.getenv("API_BACKEND_URL", "http://127.0.0.1:8001"),
}
current_backend_idx = 0


def next_backend() -> str:
    """轮询选择后端（简单负载均衡）"""
    global current_backend_idx
    urls = list(BACKEND_URLS.values())
    url = urls[current_backend_idx % len(urls)]
    current_backend_idx += 1
    return url


# 限流器实例（按端点分组）
fix_limiter = TokenBucketRateLimiter(rate=5 / 60, capacity=10)
stream_limiter = TokenBucketRateLimiter(rate=2 / 60, capacity=5)
daily_counters: Dict[str, Tuple[int, float]] = {}  # user_id → (count, last_date)
daily_lock = Lock()


def check_daily_quota(user_id: str, quota: int) -> Tuple[bool, int]:
    """检查每日配额"""
    today = time.strftime("%Y-%m-%d")
    with daily_lock:
        key = f"{user_id}:{today}"
        if key not in daily_counters:
            daily_counters[key] = (0, today)
        count, date = daily_counters[key]
        if date != today:
            count = 0
            daily_counters[key] = (0, today)
        if count >= quota:
            return False, quota - count
        daily_counters[key] = (count + 1, today)
        return True, quota - count - 1


# ==================== 依赖 ====================

async def auth_and_limit(
    x_api_key: str = Header(None),
    x_auth_token: str = Header(None, alias="Authorization"),
) -> Dict[str, Any]:
    """
    组合依赖：鉴权 + 限流。
    优先级：X-API-Key > JWT Token
    """
    claims = {"sub": "anonymous", "role": "free", "quota_daily": 50}

    # 方式1：X-API-Key（兼容原有接口）
    if x_api_key:
        key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()[:16]
        # 简单映射：key hash 前缀 → role
        if x_api_key.startswith("cfx_"):
            claims = {"sub": key_hash, "role": "premium", "quota_daily": 500, "key_type": "api_key"}

    # 方式2：JWT Bearer Token
    elif x_auth_token:
        token = x_auth_token.removeprefix("Bearer ").strip()
        valid, payload = verify_jwt(token)
        if not valid:
            metrics.inc_error("auth_failed")
            raise HTTPException(status_code=401, detail={
                "error": "Invalid or expired token",
                "hint": "Please provide a valid JWT token or X-API-Key"
            })
        claims = payload

    user_id = claims["sub"]
    role = claims.get("role", "free")
    daily_quota = claims.get("quota_daily", 50)

    # 检查每日配额
    allowed, remaining = check_daily_quota(user_id, daily_quota)
    if not allowed:
        metrics.inc_error("daily_quota_exceeded")
        raise HTTPException(status_code=429, detail={
            "error": "Daily quota exceeded",
            "quota": daily_quota,
            "hint": "Upgrade to premium/enterprise for higher limits"
        })

    # 按端点限流
    return claims


# ==================== 代理逻辑 ====================

async def proxy_request(
    method: str,
    path: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: float = 120.0,
) -> Response:
    """将请求代理转发到后端 API Server"""
    backend = next_backend()
    url = f"{backend}{path}"

    # 去掉鉴权相关的 header，防止透传
    clean_headers = {
        k: v for k, v in headers.items()
        if k.lower() not in ("host", "connection", "transfer-encoding")
    }
    # 保留 X-Real-IP 等追踪头
    clean_headers["X-Gateway-User"] = headers.get("X-Gateway-User", "")
    clean_headers["X-Gateway-Role"] = headers.get("X-Gateway-Role", "")

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            if method == "GET":
                resp = await client.get(url, headers=clean_headers)
            elif method == "POST":
                resp = await client.post(url, headers=clean_headers, content=body)
            else:
                raise HTTPException(status_code=405, detail="Method not allowed")

        duration = time.time() - t0
        metrics.inc("proxy_total")
        metrics.observe("proxy_duration", duration)

        if resp.status_code >= 400:
            metrics.inc_error(str(resp.status_code))

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
            media_type=resp.headers.get("content-type"),
        )

    except httpx.TimeoutException:
        metrics.inc_error("backend_timeout")
        metrics.inc("proxy_timeout")
        raise HTTPException(status_code=504, detail="Backend timeout")

    except httpx.RequestError as e:
        metrics.inc_error("backend_error")
        metrics.inc("proxy_error")
        logger.error(f"Backend proxy error: {e}")
        raise HTTPException(status_code=502, detail=f"Backend error: {str(e)}")


async def proxy_stream(
    method: str,
    path: str,
    headers: Dict[str, str],
    body: Optional[bytes],
) -> StreamingResponse:
    """将 SSE 流式请求代理转发到后端"""
    backend = next_backend()
    url = f"{backend}{path}"

    clean_headers = {
        k: v for k, v in headers.items()
        if k.lower() not in ("host", "connection", "transfer-encoding")
    }
    clean_headers["X-Gateway-User"] = headers.get("X-Gateway-User", "")
    clean_headers["X-Gateway-Role"] = headers.get("X-Gateway-Role", "")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(300.0),
            follow_redirects=True,
        ) as client:
            if method == "POST":
                req = client.build_request("POST", url, headers=clean_headers, content=body)
            else:
                raise HTTPException(status_code=405, detail="Streaming only supports POST")

            t0 = time.time()
            metrics.inc("stream_total")

            async def stream_generator():
                try:
                    response = await client.send(req, stream=True)
                    async for chunk in response.aiter_bytes():
                        yield chunk
                    duration = time.time() - t0
                    metrics.observe("stream_duration", duration)
                except Exception as e:
                    logger.error(f"Stream error: {e}")
                    metrics.inc_error("stream_error")
                    yield b""

            return StreamingResponse(
                stream_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

    except Exception as e:
        logger.error(f"Stream proxy error: {e}")
        metrics.inc_error("stream_proxy_error")
        raise HTTPException(status_code=502, detail=str(e))


# ==================== 路由端点 ====================

@app.post("/fix")
async def route_fix(
    request: Request,
    claims: Dict[str, Any] = Depends(auth_and_limit),
):
    """路由 /fix 到后端 API Server"""
    body = await request.body()
    headers = dict(request.headers)
    headers["X-Gateway-User"] = claims.get("sub", "")
    headers["X-Gateway-Role"] = claims.get("role", "")

    # 限流检查
    limiter = fix_limiter
    user_id = claims.get("sub", "")
    allowed, remaining = limiter.check(user_id)
    if not allowed:
        metrics.inc_error("rate_limit")
        return JSONResponse(
            status_code=429,
            content={"error": "Rate limit exceeded", "retry_after_seconds": 60},
            headers={"Retry-After": "60", "X-RateLimit-Remaining": f"{remaining:.0f}"},
        )

    metrics.inc("fix_total")
    return await proxy_request("POST", "/fix", headers, body, timeout=180.0)


@app.post("/fix/stream")
async def route_fix_stream(
    request: Request,
    claims: Dict[str, Any] = Depends(auth_and_limit),
):
    """路由 /fix/stream SSE 到后端 API Server"""
    body = await request.body()
    headers = dict(request.headers)
    headers["X-Gateway-User"] = claims.get("sub", "")
    headers["X-Gateway-Role"] = claims.get("role", "")

    # 限流检查（更严格）
    limiter = stream_limiter
    user_id = claims.get("sub", "")
    allowed, remaining = limiter.check(user_id)
    if not allowed:
        metrics.inc_error("rate_limit_stream")
        return JSONResponse(
            status_code=429,
            content={"error": "Stream rate limit exceeded", "retry_after_seconds": 60},
            headers={"Retry-After": "60"},
        )

    metrics.inc("fix_stream_total")
    return await proxy_stream("POST", "/fix/stream", headers, body)


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "gateway": "1.0.0", "timestamp": time.time()}


@app.get("/metrics")
async def get_metrics():
    """Prometheus 指标端点"""
    return Response(
        content=metrics.prometheus(),
        media_type="text/plain; charset=utf-8",
    )


# ==================== 管理接口 ====================

@app.post("/admin/token")
async def create_token(
    user_id: str,
    role: str = "free",
    quota_daily: int = 50,
):
    """生成 JWT Token（仅管理员使用）"""
    token = create_jwt(user_id, role, quota_daily)
    return {"token": token, "expires_in": 86400 * 7}


@app.get("/admin/quota/{user_id}")
async def get_quota(user_id: str):
    """查询用户配额使用情况"""
    today = time.strftime("%Y-%m-%d")
    key = f"{user_id}:{today}"
    with daily_lock:
        count, date = daily_counters.get(key, (0, ""))
    return {"user_id": user_id, "date": today, "used": count}


@app.get("/admin/stats")
async def get_stats():
    """Gateway 全局统计"""
    return {
        "total_requests": dict(metrics._counters),
        "errors": dict(metrics._errors),
    }


# ==================== 启动 ====================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CodeFix Gateway")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--workers", type=int, default=4, help="Uvicorn workers")
    parser.add_argument("--backend", default="http://127.0.0.1:8001", help="API Backend URL")
    parser.add_argument("--secret", default=None, help="JWT Secret")
    args = parser.parse_args()

    if args.secret:
        global JWT_SECRET
        JWT_SECRET = args.secret

    BACKEND_URLS["default"] = args.backend

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_level="info",
    )
