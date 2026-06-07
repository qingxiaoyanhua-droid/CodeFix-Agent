# ================================================================
# CodeFix Agent — 生产部署指南
# ================================================================

## 系统架构

```
                          ┌──────────────────────────┐
                          │      客户端 (Browser)     │
                          └────────────┬───────────┘
                                     │ HTTPS :443
                          ┌─────────▼───────────┐
                          │  Nginx (SSL 终止)    │
                          │  • L4 负载均衡       │
                          │  • 基础限流          │
                          │  • Gzip 压缩         │
                          └─────────┬───────────┘
                                    │ HTTP :8000
                          ┌─────────▼───────────┐
                          │  Gateway (鉴权层)    │
                          │  • JWT 鉴权          │
                          │  • 精细限流           │
                          │  • 路由分发           │
                          └─────────┬───────────┘
                                    │ HTTP :8001
                    ┌─────────────────┼─────────────────┐
                    │                 │                 │
          ┌─────────▼─────┐  ┌────────▼────┐  ┌────────▼────┐
          │  API Server 1 │  │ API Server 2│  │ API Server N│
          │  :8001        │  │  :8002      │  │  :800N      │
          │  (Ollama/Qwen)│  │  (Ollama)   │  │  (Ollama)   │
          └───────────────┘  └─────────────┘  └─────────────┘
```

## 1. 启动顺序

### 1.1 启动 API Server 实例（N 个）

```bash
# 实例 1
python -m uvicorn src.api.api_server:app \
  --host 0.0.0.0 --port 8001 \
  --workers 1 \
  --log-level info

# 实例 2（另一台机器）
python -m uvicorn src.api.api_server:app \
  --host 0.0.0.0 --port 8001 \
  --workers 1 \
  --log-level info
```

### 1.2 启动 Gateway

```bash
python -m uvicorn src.api.gateway:app \
  --host 0.0.0.0 --port 8000 \
  --workers 4 \
  --log-level info \
  --backend http://127.0.0.1:8001 \
  --secret your-very-long-random-secret-here
```

### 1.3 配置 Nginx

```bash
# 复制配置
sudo cp deploy/nginx.conf /etc/nginx/nginx.conf

# 修改 upstream 中的 IP 为实际 API Server 地址
# 编辑 /etc/nginx/nginx.conf，找到 upstream api_servers，填入真实 IP

# 测试并重载
sudo nginx -t && sudo nginx -s reload
```

## 2. 生成 JWT Token（管理员）

```bash
# 方式 1：通过 Gateway 管理接口
curl -X POST "http://localhost:8000/admin/token" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "alice", "role": "premium", "quota_daily": 500}'

# 响应：
# {"token": "eyJhbGciOiJIUzI1NiJ9....", "expires_in": 604800}

# 方式 2：直接用 Python
python -c "
from src.api.gateway import create_jwt
print(create_jwt('admin', 'enterprise', 999999))
"
```

## 3. 使用示例

```bash
# 普通请求（X-API-Key 方式，兼容现有接口）
curl -X POST http://localhost:8000/fix \
  -H "Content-Type: application/json" \
  -H "X-API-Key: cfx_your_key_here" \
  -d '{
    "bug_description": "off by one bug",
    "buggy_code": "def get_slices(items, n):\n    result = []\n    for i in range(len(items) / n):\n        result.append(items[i*n:(i+1)*n])\n    return result",
    "test_cases": [
      {"function": "get_slices", "input": [[1,2,3,4], 2], "output": [[1,2],[3,4]]}
    ]
  }'

# 流式 SSE 请求
curl -N -X POST http://localhost:8000/fix/stream \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9...." \
  -d '{
    "bug_description": "off by one bug",
    "buggy_code": "def get_slices(items, n):\n    result = []\n    for i in range(len(items) / n):\n        result.append(items[i*n:(i+1)*n])\n    return result"
  }'

# 健康检查
curl http://localhost:8000/health

# Prometheus 指标
curl http://localhost:8000/metrics
```

## 4. 性能测试

### 4.1 安装依赖

```bash
pip install locust httpx
```

### 4.2 运行 QPS 基线测试

```bash
# 快速基线（50 用户，60 秒）
cd CodeFix-Agent
locust -f src/load_test/load_test.py \
  --host=http://localhost:8000 \
  --users=50 --spawn-rate=10 \
  --run-time=60s \
  --headless

# 完整压测（100 用户，120 秒，生成报告）
mkdir -p results
locust -f src/load_test/load_test.py \
  --host=http://localhost:8000 \
  --users=100 --spawn-rate=5 \
  --run-time=120s \
  --headless \
  --csv=results/load_test \
  --html=results/report.html

# 分布式压测（4 台机器）
# 主机
locust -f src/load_test/load_test.py \
  --master --expect-workers=4
# Worker 1-4（分别在不同机器上）
locust -f src/load_test/load_test.py \
  --worker --master-host=<master-ip>
```

### 4.3 解读测试结果

关注指标：

| 指标 | 目标值 | 说明 |
|------|--------|------|
| QPS | >= 10/s | 单实例基准 |
| P50 延迟 | < 5s | 普通请求 |
| P95 延迟 | < 30s | 包含长请求 |
| P99 延迟 | < 60s | 极限情况 |
| 错误率 | < 1% | 无资源耗尽 |
| SSE 事件数 | > 5/event | 流式正常 |

## 5. 限流说明

| 角色 | /fix QPS | /fix/stream QPS | 每日配额 |
|------|-----------|-------------------|---------|
| free | 5/min | 2/min | 50 |
| premium | 60/min | 12/min | 500 |
| enterprise | 300/min | 60/min | 无限制 |

Nginx 额外限制：
- 全局：30 req/s
- 单 IP：10 req/s，突发 20
- SSE 连接：10 并发/IP

## 6. Prometheus 监控集成

Gateway 内置 `/metrics` 端点，输出 Prometheus 格式指标。

### 常用 PromQL 查询

```sql
-- QPS（每秒请求数）
rate(gateway_requests_total[1m])

-- P95 延迟
histogram_quantile(0.95, rate(gateway_request_duration_seconds_bucket[5m]))

-- 错误率
rate(gateway_errors_total[5m]) / rate(gateway_requests_total[5m])

-- 按端点分组 QPS
rate(gateway_requests_total[1m]) by (endpoint)
```

### Grafana 看板建议面板

| 面板名 | 查询 | 告警阈值 |
|---|---|---|
| QPS | `rate(gateway_requests_total[1m])` | < 5/s 告警 |
| P95 延迟 | `histogram_quantile(0.95, ...)` | > 10s 告警 |
| 错误率 | `rate(gateway_errors_total[5m]) / rate(...)` | > 1% 告警 |
| 按角色 QPS | `rate(...) by (role)` | 按业务需求 |

## 7. 常见问题

**Q: SSE 连接断线？**
A: 检查 Nginx 的 `proxy_read_timeout` 是否 >= 300s；检查防火墙是否超时。

**Q: 429 限流频繁？**
A: 免费用户默认 5 req/min，使用 JWT Token 切换到 premium/enterprise 角色。

**Q: 多实例下 session 不一致？**
A: Gateway 使用轮询简单负载均衡，API Server 无状态，不需要 sticky session。

**Q: 如何水平扩展？**
A: 在 Nginx upstream 中添加更多 `server IP:port`；Gateway 支持配置多个 backend URL。
