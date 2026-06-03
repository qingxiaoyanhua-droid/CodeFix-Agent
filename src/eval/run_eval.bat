@echo off
REM ============================================================
REM CodeFix Agent - 腾讯面试服务器一键评测
REM ============================================================

cd /d "%~dp0"

echo ============================================================
echo   CodeFix Agent - Server Evaluation
echo ============================================================
echo.

REM 1. Oracle模式 - 验证数据
echo [1/3] Oracle Mode - Validating test data...
python server_eval.py --oracle --output runs\quixbugs_oracle.json
echo.

REM 2. 本地评测 - CoT + Executor
echo [2/3] Local Evaluation - CoT + Executor...
python local_eval_demo.py --full --output runs\local_eval.json
echo.

REM 3. 端到端测试
echo [3/3] E2E Pipeline Test...
python test_pipeline.py
echo.

echo ============================================================
echo   Done! Results are in:
echo   - runs\quixbugs_oracle.json
echo   - runs\local_eval.json
echo   - runs_pipeline\local_eval_*.json
echo ============================================================
