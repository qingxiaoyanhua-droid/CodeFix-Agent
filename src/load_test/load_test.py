#!/usr/bin/env python3
"""
CodeFix Agent — Locust 性能测试套件
=================================
测试目标：
  1. QPS 基线：单实例能承受多少并发 /fix 请求
  2. 延迟分布：P50 / P95 / P99 响应时间
  3. 流式接口：/fix/stream SSE 的并发表现
  4. 限流验证：触发 Gateway 限流后的行为
  5. 错误率：高并发下的失败率

─────────────────────────────────────────
指标说明（可追溯）
─────────────────────────────────────────
QPS (Queries Per Second):
  定义：每秒处理的请求数，衡量系统吞吐量。
  算法：QPS = 总请求数 / 测试持续秒数
  示例：60秒内处理3000个请求 → QPS = 3000/60 = 50/s

P50 / P95 / P99（延迟分位数）:
  定义：将所有请求响应时间从小到大排列，取指定百分比位置的值。
  算法：将所有响应时间存入数组 times[]，排序后：
    P50 = times[ceil(len(times) * 0.50) - 1]  # 中位数
    P95 = times[ceil(len(times) * 0.95) - 1]  # 95%请求比这快
    P99 = times[ceil(len(times) * 0.99) - 1]  # 99%请求比这快
  含义：
    P50 < 1s → 大多数用户体验流畅
    P95 < 10s → 95%用户不觉得卡
    P99 < 30s → 几乎没人遇到极端慢请求

错误率:
  定义：失败请求占总请求的比例。
  算法：错误率 = 失败请求数 / 总请求数 × 100%
  评判：
    < 1%   → 优秀，系统稳定
    1%~5%  → 需关注，可能在限流或资源争抢
    > 5%   → 严重，后端过载或配置有问题

扩缩容建议:
  QPS < 5/s   → 单实例足够，考虑优化模型推理
  QPS 5~30/s  → 2~3个API Server实例，Nginx负载均衡
  QPS 30~100/s→ Gateway + 多实例 + Redis共享限流状态
  QPS > 100/s → Kubernetes HPA自动扩缩容

使用方式：
  # 单机测试（快速基线）
  locust -f src/load_test/load_test.py \
    --host=http://localhost:8000 \
    --users=50 --spawn-rate=10 --run-time=60s --headless

  # 分布式压测（模拟真实流量）
  locust -f src/load_test/load_test.py \
    --host=https://codefix.example.com \
    --master \
    --expect-workers=4

  # 报告输出
  locust -f src/load_test/load_test.py \
    --host=http://localhost:8000 \
    --csv=results/load_test \
    --html=results/report.html \
    --users=100 --spawn-rate=5 --run-time=120s --headless

依赖：
  pip install locust httpx
"""

from __future__ import annotations

import os
import json
import time
import random
import hashlib
from typing import Optional

from locust import (
    HttpUser, task, between, events, tag,
    ResponseError, User as LocustUser
)
from locust import stats as locust_stats

# 共享测试数据：多组不同复杂度的 bug
SAMPLE_BUGS = [
    {
        "name": "off_by_one",
        "description": "off by one bug in range function",
        "code": '''def get_slices(items, n):
    result = []
    for i in range(len(items) / n):  # Bug: should be //
        result.append(items[i*n:(i+1)*n])
    return result
'''.strip(),
    },
    {
        "name": "null_pointer",
        "description": "TypeError: cannot concatenate str and NoneType",
        "code": '''def greet(name):
    return "Hello, " + name
'''.strip(),
    },
    {
        "name": "logic_error",
        "description": "wrong comparison operator causing incorrect filter",
        "code": '''def filter_positive(numbers):
    result = []
    for n in numbers:
        if n < 0:  # Bug: should be > 0
            result.append(n)
    return result
'''.strip(),
    },
    {
        "name": "infinite_loop",
        "description": "while loop never terminates",
        "code": '''def find_index(items, target):
    i = 0
    while i < len(items):
        if items[i] == target:
            return i
        i += 1
    return -1
'''.strip(),
    },
    {
        "name": "initialization",
        "description": "sum not reset between calls",
        "code": '''total = 0

def add_to_total(values):
    global total
    for v in values:
        total += v
    return total
'''.strip(),
    },
]


def get_random_bug():
    return random.choice(SAMPLE_BUGS)


def make_request_body(bug: Optional[dict] = None) -> dict:
    if bug is None:
        bug = get_random_bug()
    return {
        "bug_description": bug["description"],
        "buggy_code": bug["code"],
        "test_cases": [
            {
                "function": "get_slices" if "get_slices" in bug["code"] else
                             "greet" if "greet" in bug["code"] else
                             "filter_positive" if "filter_positive" in bug["code"] else
                             "find_index" if "find_index" in bug["code"] else
                             "add_to_total",
                "input": [[1, 2, 3, 4], 2] if "get_slices" in bug["code"] else
                        ["World"] if "greet" in bug["code"] else
                        [[1, -2, 3, -4]] if "filter_positive" in bug["code"] else
                        [[1, 2, 3], 2] if "find_index" in bug["code"] else
                        [[1, 2, 3]],
                "output": [[1, 2], [3, 4]] if "get_slices" in bug["code"] else
                          "Hello, World" if "greet" in bug["code"] else
                          [-2, -4] if "filter_positive" in bug["code"] else
                          2 if "find_index" in bug["code"] else
                          6,
            }
        ],
        "language": "python",
        "max_iterations": 2,  # 压测时减少迭代次数，加速测试
    }


# ==================== 测试场景：普通用户 ====================

class FixUser(HttpUser):
    """
    普通用户：按固定 QPS 发送 /fix 请求。
    模拟真实用户行为：思考 1-3 秒后再次请求。
    """
    wait_time = between(1, 3)
    weight = 10  # 10x more common than stream users

    def on_start(self):
        """每个虚拟用户启动时的初始化"""
        # 生成随机 API Key（演示用）
        key_seed = f"{self.environment.runner_id}_{self.user_id}_{random.random()}"
        self.api_key = "cfx_" + hashlib.sha256(key_seed.encode()).hexdigest()[:24]

    @task(20)
    def fix_bug(self):
        """核心任务：提交一个 bug 修复请求"""
        bug = get_random_bug()
        body = make_request_body(bug)

        with self.client.post(
            "/fix",
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
            },
            timeout=120,
            catch_response=True,
            name="/fix",
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    resp.success()
                else:
                    # 修复失败但不是服务端错误，标记为 partial
                    resp.success()
            elif resp.status_code == 429:
                # 限流正常响应
                resp.success()
            elif resp.status_code == 401:
                resp.failure(f"Auth failed: {resp.text[:50]}")
            else:
                resp.failure(f"Unexpected {resp.status_code}: {resp.text[:100]}")

    @task(5)
    def health_check(self):
        """健康检查：低频"""
        with self.client.get("/health", catch_response=True, name="/health") as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Health check failed: {resp.status_code}")


# ==================== 测试场景：流式用户 ====================

class StreamUser(HttpUser):
    """
    流式用户：发送 SSE /fix/stream 请求。
    SSE 连接时间长，需要特殊处理。
    """
    wait_time = between(3, 8)
    weight = 3

    def on_start(self):
        key_seed = f"{self.environment.runner_id}_{self.user_id}_{random.random()}"
        self.api_key = "cfx_" + hashlib.sha256(key_seed.encode()).hexdigest()[:24]

    @task
    def fix_stream(self):
        """SSE 流式修复请求"""
        bug = get_random_bug()
        body = make_request_body(bug)

        event_count = 0
        last_event_time = 0
        response_complete = False

        def on_event(event_type: str, data: str):
            nonlocal event_count, last_event_time
            event_count += 1
            last_event_time = time.time()

        start = time.time()
        with self.client.post(
            "/fix/stream",
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key,
            },
            stream=True,
            timeout=180,
            catch_response=True,
            name="/fix/stream",
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"SSE stream failed: {resp.status_code}")
                return

            try:
                # 实时消费 SSE 流
                for line in resp.iter_content(decode_unicode=True):
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str:
                            try:
                                event = json.loads(data_str)
                                on_event(event.get("type", "unknown"), data_str)
                                # 收到 done 事件即为成功
                                if event.get("type") == "done":
                                    response_complete = True
                            except json.JSONDecodeError:
                                pass
            except Exception as e:
                pass

            duration = time.time() - start
            if event_count == 0:
                resp.failure("No SSE events received")
            elif response_complete or event_count > 0:
                resp.success()


# ==================== 限流专项测试 ====================

class BurstUser(HttpUser):
    """
    突发用户：短时间内发送大量请求，测试限流行为。
    """
    weight = 1

    def on_start(self):
        key_seed = f"burst_{self.environment.runner_id}_{self.user_id}"
        self.api_key = "cfx_" + hashlib.sha256(key_seed.encode()).hexdigest()[:24]

    @task(1)
    @tag("burst")
    def burst_requests(self):
        """10 秒内发 20 个请求，触发限流"""
        bug = get_random_bug()
        body = make_request_body(bug)

        success_count = 0
        rate_limited = 0

        for i in range(20):
            with self.client.post(
                "/fix",
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self.api_key,
                },
                timeout=30,
                catch_response=True,
                name="/fix [burst]",
            ) as resp:
                if resp.status_code == 200:
                    success_count += 1
                    resp.success()
                elif resp.status_code == 429:
                    rate_limited += 1
                    resp.success()  # 429 是预期行为，不算失败
                else:
                    resp.failure(f"Burst request failed: {resp.status_code}")

            # 每个请求间隔 0.5 秒
            time.sleep(0.5)


# ==================== 测试钩子：自定义统计 ====================

results_summary = {
    "total_requests": 0,
    "success_requests": 0,
    "error_requests": 0,
    "rate_limited": 0,
    "stream_events": 0,
}


@events.request.add_listener
def on_request(request_type, name, response_time, response_length, exception, **kwargs):
    results_summary["total_requests"] += 1
    if exception:
        results_summary["error_requests"] += 1
    elif isinstance(exception, Exception):
        results_summary["error_requests"] += 1


@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    """
    测试结束时打印详细报告。

    各指标计算方式（Locust 内置）：
      QPS     = num_requests / duration           （来自 stats.total）
      P50/P95/P99 = get_response_time_percentile(p)  （内置百分位数）
      错误率  = num_failures / num_requests × 100%
      带宽    = total_content_length / duration    （bytes/s → MB/s）
    """
    stats = environment.stats

    print("\n" + "=" * 60)
    print("  CodeFix Agent — Load Test Report")
    print("=" * 60)

    # ── 总请求数 & QPS ──────────────────────────────────
    # QPS = 总请求数 / 测试总时长（秒）
    total = stats.total.num_requests
    failed = stats.total.num_failures
    duration = stats.total.last_request_timestamp - stats.total.start_time
    rps = total / duration if duration > 0 else 0

    # 错误率 = 失败数 / 总数 × 100%，若 total=0 则显示 0%
    error_pct = (failed / total * 100) if total > 0 else 0

    print(f"\n[Overall]")
    print(f"  Total Requests : {total:,}")
    print(f"  Failed        : {failed:,} ({error_pct:.1f}%)")
    print(f"  Duration      : {duration:.0f}s")
    print(f"  Avg QPS       : {rps:.1f}/s   ← 算法: total/duration")

    # ── 各端点延迟分位数 ─────────────────────────────────
    # P50 = get_response_time_percentile(0.50)  # 50%请求比这快
    # P95 = get_response_time_percentile(0.95)  # 95%请求比这快
    # P99 = get_response_time_percentile(0.99)  # 99%请求比这快
    print(f"\n[Endpoints]")
    for endpoint in ["/fix", "/fix/stream", "/health", "/fix [burst]"]:
        s = stats.get(endpoint, None)
        if s and s.num_requests > 0:
            p50 = s.get_response_time_percentile(0.50)
            p95 = s.get_response_time_percentile(0.95)
            p99 = s.get_response_time_percentile(0.99)
            rps_ep = s.num_requests / duration if duration > 0 else 0
            print(f"  {endpoint:<20} {s.num_requests:>6} req  "
                  f"P50={p50:>6.0f}ms  P95={p95:>7.0f}ms  "
                  f"P99={p99:>7.0f}ms  QPS={rps_ep:>5.1f}/s")

    # ── 带宽统计 ─────────────────────────────────────────
    # 带宽(MB/s) = 响应总字节数 / 1024 / 1024 / 时长(秒)
    total_bytes = stats.total.total_content_length
    mb = total_bytes / 1024 / 1024
    bw = mb / duration if duration > 0 else 0
    print(f"\n[Bandwidth]")
    print(f"  Total Response: {mb:.1f} MB  ({bw:.1f} MB/s)")

    # ── 扩缩容建议 ────────────────────────────────────────
    # 建议阈值（根据 QPS 判定当前规模）
    print(f"\n[Throughput Targets]")
    for target_qps in [10, 30, 50, 100]:
        status = "✅ PASS" if rps >= target_qps else "⚠️  NEEDS SCALING"
        print(f"  {target_qps:>3} req/s target: {status}  (actual: {rps:.1f})")

    print("\n" + "=" * 60)
    print("  Recommendation:")
    # 错误率 > 5% → 后端过载或限流
    if failed / total > 0.05 and total > 0:
        print("  ⚠️  Error rate > 5%, check backend errors and limits")
    # QPS < 5 → 性能严重不足
    if rps < 5:
        print("  ⚠️  QPS < 5, consider upgrading model or adding instances")
    # QPS >= 30 → 单实例部署良好
    if rps >= 30:
        print("  ✅ Performance is good for single-instance deployment")
    print("=" * 60 + "\n")
    if rps < 5:
        print("  ⚠️  QPS < 5, consider upgrading model or adding instances")
    if rps >= 30:
        print("  ✅ Performance is good for single-instance deployment")
    print("=" * 60 + "\n")


# ==================== 快捷运行入口 ====================

if __name__ == "__main__":
    import subprocess, sys

    host = os.getenv("LOCUST_HOST", "http://localhost:8000")
    users = int(os.getenv("LOCUST_USERS", "50"))
    runtime = os.getenv("LOCUST_RUNTIME", "60s")
    workers = int(os.getenv("LOCUST_WORKERS", "1"))

    cmd = [
        sys.executable, "-m", "locust",
        "-f", __file__,
        "--host", host,
        "--users", str(users),
        "--spawn-rate", str(max(1, users // 10)),
        "--run-time", runtime,
        "--headless",
        "--csv", f"results/load_test_{int(time.time())}",
        "--html", f"results/report_{int(time.time())}.html",
    ]

    if workers > 1:
        cmd += ["--master"]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    sys.exit(result.returncode)
