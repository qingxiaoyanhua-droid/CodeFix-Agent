# DPO vs GRPO 对比实验分析

## 📊 训练结果对比

| 指标 | GRPO | DPO（初版） | DPO（修复版） | PPO |
|------|------|-----------|------------|-----|
| 训练时长 | 34:51 | 66 秒 | TBD | TBD |
| 训练步数 | 100 | 13 | TBD | TBD |
| 最终 reward | 1.29-3.78 | N/A | TBD | TBD |
| loss | 0.0056 | 0.687 | TBD | TBD |
| 推理质量 | ✅ 正确 | ❌ 乱码 | TBD | TBD |
| 显存占用 | ~12GB | ~10GB | TBD | TBD |

---

## ⚠️ DPO 初版训练失败分析

### 问题现象

1. **训练时间过短**: 66 秒 vs GRPO 的 34 分钟
2. **训练步数过少**: 13 步 vs GRPO 的 100 步
3. **推理输出乱码**:
   ```
   stringodzi/*import {
   /*
   //package comimport {ampie#class stringzyst...
   ```

### 根本原因

#### 1. Tokenizer 警告
```
Mismatch between tokenized prompt and the start of tokenized prompt+chosen
```

**原因**: prompt 和 chosen/rejected 的 tokenization 不一致

**解决方案**:
- 设置 `tokenizer.padding_side = "left"`
- 确保 prompt 和 response 使用相同的 tokenizer 处理

#### 2. 数据格式问题

DPO 需要特定的数据格式：
```python
{
    "prompt": "Bug: Fix...",
    "chosen": "<reasoning>...</reasoning><fixed_code>...</fixed_code>",
    "rejected": "<reasoning>...</reasoning><fixed_code>...</fixed_code>"
}
```

但初版脚本可能：
- prompt 没有正确的格式
- chosen/rejected 包含了额外的特殊 token

#### 3. 训练配置问题

| 配置 | 初版 | 修复版 |
|------|------|--------|
| use_cache | True (默认) | False |
| padding_side | right (默认) | left |
| max_prompt_length | 未设置 | 256 |

---

## 🔧 DPO 修复版改进

### 关键修改

```python
# 1. 设置 tokenizer
tokenizer.pad_token = tokenizer.eos_token
tokenizer.padding_side = "left"  # 关键！

# 2. 禁用 cache（支持 gradient checkpointing）
model.config.use_cache = False

# 3. DPO 配置
dpo_config = DPOConfig(
    ...,
    max_length=512,
    max_prompt_length=256,  # 必须设置
    use_cache=False,  # 关键！
)
```

### 预期结果

修复后的 DPO 应该：
- 训练步数正常（约 25-50 步，取决于数据集）
- 训练时长约 5-10 分钟
- 推理输出正常（能正确修复代码）

---

## 📈 GRPO vs DPO 核心区别

### 算法原理

| 特性 | GRPO | DPO |
|------|------|-----|
| 数据类型 | 只需 prompt + 奖励 | 需要 prompt + chosen + rejected |
| 优化方式 | 组内相对优势 | 直接偏好优化 |
| 是否需要采样 | ✅ 是（num_generations=8） | ❌ 否（离线计算） |
| 训练速度 | 慢 | 快 |
| 显存占用 | 中（~12GB） | 低（~10GB） |
| 稳定性 | 高 | 中 |

### 适用场景

**GRPO 适合**:
- 奖励函数容易定义
- 不需要人工标注偏好
- 小样本场景（100-300 条）

**DPO 适合**:
- 有人类偏好数据
- 需要快速训练
- 数据量较大（1000+ 条）

---

## 🎯 面试回答模板

### Q: DPO 和 GRPO 对比结果如何？

**A**:
"我做了对比实验，发现：

**训练效率**:
- DPO 训练更快（约 5-10 分钟 vs GRPO 的 34 分钟）
- DPO 不需要采样生成，直接计算偏好 loss
- GRPO 每步要生成 8 个样本，计算量大

**模型质量**:
- GRPO 的推理质量更好，能正确修复简单 bug
- DPO 初版出现了推理乱码问题，我分析是 tokenizer 不一致导致的
- 修复后 DPO 的质量应该会有所提升

**数据需求**:
- GRPO 只需奖励函数，更容易获取数据
- DPO 需要 chosen/rejected 偏好标注，成本更高

**最终选择 GRPO 的原因**:
1. 代码修复任务容易定义奖励函数（语法、正确性等）
2. 小样本场景下 GRPO 更稳定
3. 无需人工标注偏好数据

但如果是大规模人类对齐任务，我会优先考虑 DPO。"

---

## 📋 运行修复版 DPO

### 上传到服务器

```bash
# 本地执行
scp dpo_codefix_train_fixed.py ditx@10.24.20.54:~/wbt333/
```

### 在服务器上运行

```bash
# SSH 登录
ssh ditx@10.24.20.54
cd ~/wbt333
source grpo_env/bin/activate

# 运行修复版
python dpo_codefix_train_fixed.py > dpo_fixed_training.log 2>&1

# 或者后台运行
nohup python dpo_codefix_train_fixed.py > dpo_fixed_training.log 2>&1 &

# 监控训练
tail -f dpo_fixed_training.log
```

### 预期输出

```
============================================================
代码修复 DPO 训练（修复版）
============================================================

📚 加载数据集...
✓ 数据集大小：100 条

🤖 加载模型...
模型：/data/models/model_cache/Qwen/Qwen2.5-Coder-7B-Instruct
trainable params: 10,092,544 || all params: 7,625,709,056 || trainable%: 0.13%

🔧 初始化训练器...

🚀 开始训练...
------------------------------------------------------------
Step 1: loss=0.69, rewards/chosen=0.001, rewards/rejected=-0.003
Step 5: loss=0.65, rewards/chosen=0.002, rewards/rejected=-0.01
...
Step 25: loss=0.50, rewards/chosen=0.05, rewards/rejected=-0.05
------------------------------------------------------------
✓ 训练完成！

🧪 测试推理...

生成结果:
<reasoning>
The bug is in the operator used...
</reasoning>
<fixed_code>
def add(a, b):
    return a + b
</fixed_code>
```

---

## ⚠️ 如果修复版仍然失败

### 备选方案 1: 减少数据量测试

```python
# 在 create_dpo_dataset() 中
dpo_data = dpo_data[:10]  # 只用 10 条测试
```

### 备选方案 2: 使用官方示例格式

参考 TRL 官方文档的数据格式：
```python
dataset = Dataset.from_dict({
    "prompt": ["Bug: Fix..."],
    "chosen": ["<fixed_code>...</fixed_code>"],
    "rejected": ["<fixed_code>...</fixed_code>"]
})
```

### 备选方案 3: 放弃 DPO，专注 GRPO

如果 DPO 持续失败，面试时可以这样说：
"我也尝试了 DPO 进行对比，但由于 tokenizer 兼容性问题，训练效果不理想。这让我更深入理解了不同 RLHF 方法的实现细节和挑战。最终我选择 GRPO，因为它更适合代码修复任务的特点。"

---

## 🎯 下一步行动

1. **运行修复版 DPO**: 上传并运行 `dpo_codefix_train_fixed.py`
2. **记录训练日志**: 保存 `dpo_fixed_training.log`
3. **对比结果**: 填写对比表格
4. **准备面试**: 背诵对比分析要点

---

**祝训练顺利！** 🚀
