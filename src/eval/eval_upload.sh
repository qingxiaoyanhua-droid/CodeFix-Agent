#!/bin/bash
# =============================================================================
# Upload & Run Script — uploads eval_server.py + cot_react_agent.py to server,
# runs evaluation, and downloads results.
#
# Usage:
#   bash eval_upload.sh                    # Full benchmark (default)
#   bash eval_upload.sh --mini             # Quick 5-program test
#   bash eval_upload.sh --reset            # Clear memory first
#   bash eval_upload.sh --1.5b            # Use 1.5B only (if 7B VRAM is tight)
#
# Requirements:
#   - Passwordless SSH/SCP to server configured (ssh-copy-id or SSH key)
#   - Server: conda env 'grpo_env' exists
# =============================================================================

set -e

# ---- Configuration ----
SERVER_IP="10.20.126.25"
SERVER_USER="wbt333"
SERVER_BASE="/data/wbt333/eval_runs"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
EVAL_SCRIPT="eval_server.py"
AGENT_FILE="cot_react_agent.py"
REMOTE_DIR=""

# ---- Parse arguments ----
MODE=""
RESET_FLAG=""
USE_1_5B_FLAG=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --mini)      MODE="--mini"; shift ;;
        --reset)     RESET_FLAG="--reset-memory"; shift ;;
        --1.5b)     USE_1_5B_FLAG="--use-1.5b"; shift ;;
        *)           echo "Unknown option: $1"; exit 1 ;;
    esac
done

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
REMOTE_DIR="${SERVER_BASE}/run_${TIMESTAMP}"

echo "============================================================"
echo "  Upload & Run — Three-Layer Memory Evaluation"
echo "============================================================"
echo "  Server:      ${SERVER_USER}@${SERVER_IP}"
echo "  Remote dir:  ${REMOTE_DIR}"
echo "  Mode:        ${MODE:-full}  ${USE_1_5B_FLAG:-7B}  ${RESET_FLAG:-}"
echo "============================================================"

# ---- Step 1: Create remote directory ----
echo "[1/5] Creating remote directory..."
ssh ${SERVER_USER}@${SERVER_IP} "mkdir -p ${REMOTE_DIR}"

# ---- Step 2: Upload files ----
echo "[2/5] Uploading files..."
scp ${LOCAL_DIR}/${EVAL_SCRIPT} ${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}/
scp ${LOCAL_DIR}/${AGENT_FILE}  ${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}/

# Also upload the benchmark data if it exists
if [ -f "${LOCAL_DIR}/quixbugs.json" ]; then
    scp ${LOCAL_DIR}/quixbugs.json ${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}/
fi

echo "  Uploaded: ${EVAL_SCRIPT}, ${AGENT_FILE}"

# ---- Step 3: Run evaluation ----
echo "[3/5] Starting evaluation on server..."
echo "  (This may take 30-60 minutes for full benchmark)"
echo "  Logs will be written to: ${REMOTE_DIR}/eval.log"

ssh ${SERVER_USER}@${SERVER_IP} << 'SSH_EOF'
set -e
cd /data/wbt333/eval_runs/run_*

source /data/wbt333/conda_envs/grpo_env/bin/activate grpo_env

echo "Conda env activated: $(which python)"
echo "Python version: $(python --version)"
echo "CUDA available: $(python -c 'import torch; print(torch.cuda.is_available())')"

# Install sentence-transformers if needed
pip install -q sentence-transformers 2>/dev/null || true

# Run evaluation
python eval_server.py \
    --output-dir ./runs \
    ${MODE:-} \
    ${RESET_FLAG:-} \
    ${USE_1_5B_FLAG:-} \
    2>&1 | tee ./runs/eval.log

echo "============================================================"
echo "Evaluation complete!"
echo "============================================================"
SSH_EOF

# ---- Step 4: Download results ----
echo "[4/5] Downloading results..."
LOCAL_OUT="${LOCAL_DIR}/runs/server_${TIMESTAMP}"
mkdir -p "${LOCAL_OUT}"
scp -r ${SERVER_USER}@${SERVER_IP}:${REMOTE_DIR}/runs/ "${LOCAL_OUT}/"

# ---- Step 5: Cleanup remote directory (optional, comment out to keep) ----
echo "[5/5] Done! Results saved to: ${LOCAL_OUT}"
echo ""
echo "============================================================"
echo "  Results:"
echo "============================================================"
if [ -f "${LOCAL_OUT}/runs/server_eval_results.json" ]; then
    python -c "
import json, sys
with open('${LOCAL_OUT}/runs/server_eval_results.json') as f:
    data = json.load(f)
s = data['summary']
print(f'  Programs:     {s[\"total\"]}')
print(f'  Passed:       {s[\"passed\"]}/{s[\"total\"]} ({s[\"pass_rate\"]:.1%})')
print(f'  Total time:   {s[\"total_time_s\"]:.1f}s')
results = data['results']
refls = sum(1 for r in results if r['used_reflections'] > 0)
skills = sum(1 for r in results if r['used_skills'])
extracted = sum(1 for r in results if r['skill_extracted'])
print(f'  Reflections used: {refls}/{len(results)} ({refls/len(results):.0%})')
print(f'  Skills used:      {skills}/{len(results)} ({skills/len(results):.0%})')
print(f'  Skills extracted:  {extracted}/{len(results)}')
"
else
    echo "  Results file not found. Check ${LOCAL_OUT}/runs/eval.log"
fi

echo ""
echo "  Full logs:     ${LOCAL_OUT}/runs/eval.log"
echo "  Detailed JSON: ${LOCAL_OUT}/runs/server_eval_results.json"
echo "============================================================"
