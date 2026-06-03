#!/usr/bin/env python3
"""
LLM Critic 奖励系统
使用大模型（Qwen/Qwen-Max）作为代码修复的裁判
"""

import os
import json
import re
from typing import List, Dict, Tuple
from openai import OpenAI


class LLMPatchCritic:
    """使用大模型评估代码修复补丁的质量"""
    
    def __init__(self, model_name: str = "qwen-plus", api_key: str = None):
        """
        初始化 Critic
        
        Args:
            model_name: 使用的模型（qwen-turbo/qwen-plus/qwen-max）
            api_key: DashScope API Key
        """
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        self.model_name = model_name
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        
        # 缓存，避免重复调用
        self.cache = {}
    
    def evaluate_patch(self, bug_description: str, buggy_code: str, 
                      generated_patch: str) -> Dict:
        """
        评估单个补丁
        
        Returns:
            {
                "correctness": 0-10,
                "code_quality": 0-10,
                "explanation": "...",
                "is_valid": True/False
            }
        """
        # 构造缓存 key
        cache_key = hash(f"{bug_description}{buggy_code}{generated_patch}")
        if cache_key in self.cache:
            return self.cache[cache_key]
        
        # 构造 prompt
        prompt = self._build_critic_prompt(bug_description, buggy_code, generated_patch)
        
        # 调用大模型
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": "You are a senior software engineer and code reviewer."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )
        
        # 解析响应
        result = self._parse_response(response.choices[0].message.content)
        
        # 缓存结果
        self.cache[cache_key] = result
        return result
    
    def _build_critic_prompt(self, bug_description: str, buggy_code: str, 
                            generated_patch: str) -> str:
        """构建 Critic 的 prompt"""
        
        return f"""You are a senior software engineer. Please evaluate the following code patch.

**Bug Description:**
{bug_description}

**Buggy Code:**
```python
{buggy_code}
```

**Generated Patch:**
```diff
{generated_patch}
```

Please evaluate the patch on the following criteria:

1. **Correctness (0-10)**: Does the patch correctly fix the bug?
2. **Code Quality (0-10)**: Is the code clean, readable, and follows best practices?
3. **Minimality (0-10)**: Is the patch minimal and focused on the issue?

Respond in the following JSON format:
```json
{{
  "correctness": 8,
  "code_quality": 7,
  "minimality": 9,
  "explanation": "The patch correctly fixes the operator bug...",
  "is_valid": true
}}
```

Be strict and specific. If the patch has issues, explain them clearly."""
    
    def _parse_response(self, response_text: str) -> Dict:
        """解析大模型的响应"""
        
        # 尝试提取 JSON
        json_match = re.search(r'```json\s*(.*?)\s*```', response_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # 如果没有 JSON 格式，尝试直接解析
            json_str = response_text.strip()
        
        try:
            result = json.loads(json_str)
            return {
                "correctness": result.get("correctness", 0),
                "code_quality": result.get("code_quality", 0),
                "minimality": result.get("minimality", 0),
                "explanation": result.get("explanation", ""),
                "is_valid": result.get("is_valid", False)
            }
        except:
            # 解析失败，返回默认值
            return {
                "correctness": 5,
                "code_quality": 5,
                "minimality": 5,
                "explanation": "Failed to parse response",
                "is_valid": False
            }
    
    def batch_evaluate(self, patches: List[Dict]) -> List[Dict]:
        """
        批量评估多个补丁（节省 API 调用）
        
        Args:
            patches: [{"bug_description": "...", "buggy_code": "...", "patch": "..."}, ...]
        
        Returns:
            [evaluation_result, ...]
        """
        results = []
        for patch in patches:
            result = self.evaluate_patch(
                patch["bug_description"],
                patch["buggy_code"],
                patch["patch"]
            )
            results.append(result)
        return results
    
    def rank_patches(self, patches: List[Dict]) -> List[Tuple[int, float]]:
        """
        对多个补丁进行排序（GRPO 专用）
        
        Args:
            patches: 补丁列表
        
        Returns:
            [(patch_index, score), ...] 按分数降序排列
        """
        evaluations = []
        for i, patch in enumerate(patches):
            eval_result = self.evaluate_patch(
                patch["bug_description"],
                patch["buggy_code"],
                patch["patch"]
            )
            # 综合分数
            score = (
                eval_result["correctness"] * 0.6 +
                eval_result["code_quality"] * 0.3 +
                eval_result["minimality"] * 0.1
            )
            evaluations.append((i, score))
        
        # 按分数降序排列
        return sorted(evaluations, key=lambda x: x[1], reverse=True)


# ==================== GRPO Reward 函数集成 ====================

def create_grpo_reward_function(critic: LLMPatchCritic = None):
    """
    创建 GRPO 的奖励函数（混合奖励）
    
    Reward = Compile Reward + Test Reward + LLM Critic Reward
    """
    
    import subprocess
    import tempfile
    import os
    
    def reward_function(completions, prompts=None, **kwargs):
        """
        GRPO 奖励函数
        
        Args:
            completions: 模型生成的补丁列表
            prompts: 对应的 prompt 列表
        
        Returns:
            scores: 每个补丁的奖励分数
        """
        scores = []
        
        for i, completion in enumerate(completions):
            score = 0.0
            
            # 1. 格式奖励（快速检查）
            has_reasoning = '<reasoning>' in completion and '</reasoning>' in completion
            has_fixed_code = '<fixed_code>' in completion and '</fixed_code>' in completion
            
            if has_reasoning and has_fixed_code:
                score += 0.3
            elif has_fixed_code:
                score += 0.15
            
            # 2. 语法奖励（编译检查）
            import re
            match = re.search(r'<fixed_code>(.*?)</fixed_code>', completion, re.DOTALL)
            if match:
                code = match.group(1).strip()
                try:
                    compile(code, '<string>', 'exec')
                    score += 0.3  # 语法正确
                except SyntaxError:
                    pass
            
            # 3. LLM Critic 奖励（大模型评估）
            if critic and prompts:
                prompt = prompts[i] if i < len(prompts) else ""
                
                # 从 prompt 中提取 bug 描述和 buggy code
                bug_desc, buggy_code = extract_bug_info(prompt)
                
                # 调用大模型评估
                eval_result = critic.evaluate_patch(
                    bug_desc,
                    buggy_code,
                    match.group(1) if match else ""
                )
                
                # LLM 分数（0-10 归一化到 0-0.4）
                llm_score = (
                    eval_result["correctness"] * 0.6 +
                    eval_result["code_quality"] * 0.3 +
                    eval_result["minimality"] * 0.1
                ) / 10.0
                score += llm_score * 0.4
            
            scores.append(score)
        
        return scores
    
    return reward_function


def extract_bug_info(prompt: str) -> Tuple[str, str]:
    """从 prompt 中提取 bug 描述和 buggy code"""
    import re
    
    # 提取 bug 描述
    bug_match = re.search(r'Bug:\s*(.*?)\n\n', prompt, re.DOTALL)
    bug_description = bug_match.group(1).strip() if bug_match else ""
    
    # 提取 buggy code
    code_match = re.search(r'```python\s*(.*?)\s*```', prompt, re.DOTALL)
    buggy_code = code_match.group(1).strip() if code_match else ""
    
    return bug_description, buggy_code


# ==================== 使用示例 ====================

if __name__ == "__main__":
    # 初始化 Critic
    critic = LLMPatchCritic(model_name="qwen-plus")
    
    # 示例评估
    bug_desc = "Fix the addition function"
    buggy_code = """def add(a, b):
    return a - b"""
    
    generated_patch = """diff --git a/utils.py b/utils.py
--- a/utils.py
+++ b/utils.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b"""
    
    # 评估
    result = critic.evaluate_patch(bug_desc, buggy_code, generated_patch)
    
    print("评估结果:")
    print(f"  正确性：{result['correctness']}/10")
    print(f"  代码质量：{result['code_quality']}/10")
    print(f"  简洁性：{result['minimality']}/10")
    print(f"  是否有效：{result['is_valid']}")
    print(f"  解释：{result['explanation']}")
    
    # 计算奖励分数
    reward = (
        result['correctness'] * 0.6 +
        result['code_quality'] * 0.3 +
        result['minimality'] * 0.1
    ) / 10.0
    
    print(f"\n最终奖励分数：{reward:.3f}")
