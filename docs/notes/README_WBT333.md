# WBT333 项目 - 完整目录结构

## 📁 项目结构

```
repobugfix-agent/
│
├── 📂 datasets/                      # 数据集目录
│   ├── collect_dataset.py           # 数据采集脚本 ⭐
│   ├── clean_dataset.py             # 数据清洗脚本
│   ├── validate_dataset.py          # 数据验证脚本
│   ├── start_dataset.sh             # 快速启动脚本 ⭐
│   │
│   ├── 📂 raw_codefix/              # 原始数据
│   │   └── codefix_dataset.json     # 采集的原始数据
│   │
│   └── 📂 cleaned/                  # 清洗后数据
│       └── codefix_dataset_cleaned.json  # 清洗后的数据
│
├── 📂 models/                       # 训练好的模型（训练后生成）
│   └── grpo_final/                  # 最终模型
│
├── 📂 outputs/                      # 训练输出（训练时生成）
│   └── codefix-grpo/
│       ├── checkpoint-*/
│       └── final/
│
├── grpo_codefix_train.py            # GRPO 训练脚本 ⭐
├── dpo_codefix_train.py             # DPO 对比训练脚本（新增）
├── ppo_codefix_train.py             # PPO 对比训练脚本（新增）
├── enhanced_agent.py                # 企业级 RAG+ReAct Agent
├── enterprise_rag_pipeline.py       # 企业级 RAG 流水线
├── optimized_rag_pipeline.py        # 优化版 RAG 流水线
│
├── 数据集制作指南.md                 # 数据集制作文档 ⭐
├── GRPO 训练指南.md                  # GRPO 训练文档 ⭐
├── 服务器部署指南.md                 # 服务器部署文档 ⭐
├── 项目量化指标详解.md               # 量化指标文档
├── 面试量化指标速查.md               # 面试速查卡片
│
├── 字节面试简历-FAISS 最终版.md       # 简历
├── 字节面试准备-FAISS 优化版.md       # 面试准备
└── README_WBT333.md                 # 本文档
```

---

## 🚀 快速开始流程

### 阶段 1: 数据集制作（今天）

```bash
# 1. 进入项目目录
cd /Users/setupmac/repobugfix-agent

# 2. 运行快速启动脚本
bash start_dataset.sh

# 3. 查看生成的数据集
cat datasets/raw_codefix/codefix_dataset.json

# 4. 手动扩展数据集（使用 VSCode 编辑）
# 目标：50-100 条

# 5. 验证数据集
python datasets/validate_dataset.py
```

**预计耗时**: 30-60 分钟  
**目标数据量**: 50-100 条

---

### 阶段 2: 本地测试（明天）

```bash
# 1. 安装依赖（如果还没安装）
pip install transformers trl datasets peft torch

# 2. 修改训练脚本（使用小数据集）
# 编辑 grpo_codefix_train.py:
# - num_train_epochs=1
# - num_generations=4

# 3. 运行测试训练
python grpo_codefix_train.py
```

**预计耗时**: 15-30 分钟  
**目标**: 验证流程正常

---

### 阶段 3: 服务器训练（后天）

```bash
# 1. 上传代码到服务器
scp -r /Users/setupmac/repobugfix-agent ditx@10.24.20.54:~/wbt333/

# 2. SSH 登录服务器
ssh ditx@10.24.20.54

# 3. 创建虚拟环境
cd ~/wbt333
python3 -m venv grpo_env
source grpo_env/bin/activate

# 4. 安装依赖
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple transformers trl datasets peft torch

# 5. 正式训练
python grpo_codefix_train.py

# 6. 后台运行（推荐）
nohup python grpo_codefix_train.py > training.log 2>&1 &

# 7. 监控训练
tail -f training.log
watch -n 1 nvidia-smi
```

**预计耗时**: 30-60 分钟  
**目标**: 训练出最终模型

---

### 阶段 4: 集成和测试（大后天）

```bash
# 1. 下载训练好的模型
# 在本地电脑执行
scp -r ditx@10.24.20.54:~/wbt333/outputs/codefix-grpo/final \
  /Users/setupmac/repobugfix-agent/models/grpo_final/

# 2. 集成到 enhanced_agent.py
# 修改 enhanced_agent.py，使用训练好的模型

# 3. 测试集成效果
python enhanced_agent.py --repo ./requests --inject iter_slices_pos
```

**预计耗时**: 1-2 小时  
**目标**: 完成集成

---

## 📋 每日任务清单

### Day 1: 数据集制作 ✅
- [ ] 运行 `start_dataset.sh`
- [ ] 查看生成的数据集
- [ ] 手动扩展到 50-100 条
- [ ] 运行 `validate_dataset.py`
- [ ] 检查数据质量

### Day 2: 本地测试 ⏳
- [ ] 安装依赖
- [ ] 修改训练配置
- [ ] 运行测试训练（10 条数据）
- [ ] 检查训练输出
- [ ] 准备服务器部署

### Day 3: 服务器训练 ⏳
- [ ] 上传代码到服务器
- [ ] 安装服务器环境
- [ ] 正式训练（100 条数据）
- [ ] 监控训练进度
- [ ] 下载训练好的模型

### Day 4: 集成测试 ⏳
- [ ] 集成到 enhanced_agent.py
- [ ] 测试完整流程
- [ ] 准备面试材料
- [ ] 模拟面试练习

---

## 🎯 关键文件说明

### 核心脚本（必须掌握）

| 文件 | 作用 | 重要性 |
|------|------|--------|
| `start_dataset.sh` | 数据集快速启动 | ⭐⭐⭐⭐⭐ |
| `datasets/collect_dataset.py` | 数据采集 | ⭐⭐⭐⭐ |
| `grpo_codefix_train.py` | GRPO 训练 | ⭐⭐⭐⭐⭐ |
| `dpo_codefix_train.py` | DPO 对比实验 | ⭐⭐⭐⭐ |
| `ppo_codefix_train.py` | PPO 对比实验 | ⭐⭐⭐⭐ |
| `enhanced_agent.py` | 企业级 Agent | ⭐⭐⭐⭐ |

### 核心文档（必须熟读）

| 文档 | 内容 | 重要性 |
|------|------|--------|
| `数据集制作指南.md` | 数据集制作流程 | ⭐⭐⭐⭐⭐ |
| `GRPO 训练指南.md` | GRPO 训练详解 | ⭐⭐⭐⭐⭐ |
| `对比实验指南.md` | GRPO vs DPO vs PPO | ⭐⭐⭐⭐⭐ |
| `服务器部署指南.md` | 服务器部署步骤 | ⭐⭐⭐⭐ |
| `面试量化指标速查.md` | 面试数据速查 | ⭐⭐⭐⭐⭐ |

---

## ⚠️ 注意事项

### 1. 公司服务器使用
- **创建独立目录**: `~/wbt333/`（避免污染其他项目）
- **使用虚拟环境**: `python3 -m venv grpo_env`
- **后台运行**: 使用 `nohup` 防止 SSH 断开

### 2. 数据隐私
- **不要上传**: 公司敏感代码到公开平台
- **脱敏处理**: 移除公司特定信息
- **仅内部使用**: 训练数据仅限公司内部

### 3. 资源管理
- **显存监控**: `watch -n 1 nvidia-smi`
- **及时清理**: 训练完成后清理临时文件
- **合理分配**: 不要占用过多 GPU 资源

---

## 📞 需要帮助？

### 常见问题

**Q: 数据集在哪里？**
A: `datasets/raw_codefix/codefix_dataset.json`

**Q: 如何手动添加数据？**
A: 用 VSCode 打开 JSON 文件，复制现有示例并修改

**Q: 训练需要多久？**
A: 100 条数据约 30-45 分钟（GB10 服务器）

**Q: 如何查看训练进度？**
A: `tail -f training.log`

---

## 🎉 总结

你现在有：
- ✅ 完整的数据集制作流程
- ✅ GRPO 训练脚本和指南
- ✅ 服务器部署方案
- ✅ 面试准备材料

**立即开始**:
```bash
bash start_dataset.sh
```

**祝你顺利！** 🚀