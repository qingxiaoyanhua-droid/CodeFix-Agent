#!/bin/bash
# ============================================================
# CodeFix Agent - 服务器一键部署 + 评测脚本
# Tencent Interview Demo
#
# 使用方式:
#   1. 将整个项目目录 scp 到服务器
#   2. 在服务器上运行: bash deploy_server.sh
#   3. 查看结果: cat runs_pipeline/results.txt
# ============================================================

set -e
cd "$(dirname "$0")"

echo "============================================================"
echo "  CodeFix Agent - 服务器一键部署"
echo "============================================================"
echo ""

# 1. 检查 GPU
echo "[1/5] 检查 GPU..."
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv,noheader 2>/dev/null || echo "  nvidia-smi 可用但无法读取详细信息"
else
    echo "  警告: nvidia-smi 未找到，跳过GPU检查"
fi
echo ""

# 2. 创建 conda 环境（如果不存在）
echo "[2/5] 设置 Python 环境..."
if ! command -v conda &>/dev/null; then
    echo "  conda 未找到，使用系统 Python"
else
    ENV_NAME="grpo_env"
    if conda env list | grep -q "^${ENV_NAME}"; then
        echo "  conda 环境 '${ENV_NAME}' 已存在，激活"
    else
        echo "  创建 conda 环境: ${ENV_NAME}"
        conda create -n ${ENV_NAME} python=3.10 -y
    fi
    eval "$(conda shell.bash hook)"
    conda activate ${ENV_NAME}
fi
echo "  Python: $(python --version)"
echo ""

# 3. 安装依赖
echo "[3/5] 安装依赖包..."
pip install -q torch transformers peft trl datasets sentence-transformers faiss-cpu \
    huggingface_hub huggingface_transfer accelerate bitsandbytes \
    -q 2>/dev/null || true

# 安装 FAISS GPU 版本（如果有 GPU）
if command -v nvidia-smi &>/dev/null; then
    pip install -q faiss-gpu -q 2>/dev/null || true
fi

echo "  依赖安装完成"
echo ""

# 4. 下载模型（如果不存在）
echo "[4/5] 检查模型..."
MODEL_DIR="${HOME}/models/Qwen2.5-Coder-1.5B-Instruct"
if [ -d "$MODEL_DIR" ]; then
    echo "  模型已存在: $MODEL_DIR"
else
    echo "  下载模型到: $MODEL_DIR"
    mkdir -p "$(dirname "$MODEL_DIR")"
    python -c "
from modelscope import snapshot_download
snapshot_download('Qwen/Qwen2.5-Coder-1.5B-Instruct', cache_dir='${HOME}/models')
" 2>/dev/null || echo "  模型下载失败，跳过（评测脚本会自动降级）"
fi
echo ""

# 5. 运行评测
echo "[5/5] 运行评测..."
mkdir -p runs_pipeline runs

# Oracle 模式（验证测试数据）
echo ""
echo "--- Oracle 模式（验证测试数据正确性）---"
python server_eval.py --oracle --output runs_pipeline/quixbugs_oracle.json 2>&1 || true

echo ""
echo "--- 本地评测模式（使用 CoT + Executor 评测 pipeline）---"
python local_eval_demo.py --full --output runs_pipeline/local_eval.json 2>&1 || true

# 如果有模型，运行完整评测
if [ -d "$MODEL_DIR" ]; then
    echo ""
    echo "--- 模型评测模式（使用 Qwen 模型）---"
    python server_eval.py --model "$MODEL_DIR" --output runs_pipeline/quixbugs_model.json 2>&1 || true
else
    echo ""
    echo "--- 模型评测模式: 跳过（无模型）---"
fi

# 6. 生成报告
echo ""
echo "============================================================"
echo "  评测完成！"
echo "============================================================"
echo ""
echo "  结果文件:"
[ -f runs_pipeline/quixbugs_oracle.json ] && echo "    - Oracle测试:     runs_pipeline/quixbugs_oracle.json"
[ -f runs_pipeline/local_eval.json ] && echo "    - 本地评测:      runs_pipeline/local_eval.json"
[ -f runs_pipeline/quixbugs_model.json ] && echo "    - 模型评测:      runs_pipeline/quixbugs_model.json"
echo ""

# 打印关键指标
if [ -f runs_pipeline/local_eval.json ]; then
    echo "  关键指标:"
    python -c "
import json
try:
    with open('runs_pipeline/local_eval.json') as f:
        r = json.load(f)
    print(f'    Pass@1: {r.get(\"pass_rate\", 0)*100:.1f}%')
    print(f'    95% CI: [{r.get(\"ci_low\",0)*100:.1f}%, {r.get(\"ci_high\",0)*100:.1f}%]')
    print(f'    通过/总数: {r.get(\"passed\",0)}/{r.get(\"total\",0)}')
except Exception as e:
    print(f'    读取失败: {e}')
" 2>&1
fi

echo ""
echo "  部署完成！"
