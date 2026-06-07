#!/usr/bin/env python3
"""
构建 RAG 知识库
从数据集、GitHub、本地代码中收集 bug fix 示例
"""

import json
import os
import hashlib
from pathlib import Path
from typing import List, Dict
import re


class RAGKnowledgeBase:
    """RAG 知识库构建器"""
    
    def __init__(self, output_path: str = "runs/bug_fixes.json"):
        self.output_path = output_path
        self.knowledge_base = []
    
    def load_from_dataset(self, dataset_path: str = "datasets/dpo_dataset_merged.json"):
        """从 DPO 数据集中加载"""
        if not os.path.exists(dataset_path):
            print(f"⚠️ 数据集不存在：{dataset_path}")
            return 0
        
        with open(dataset_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        count = 0
        for item in data:
            # 提取 bug 信息
            prompt = item.get('prompt', '')
            chosen = item.get('chosen', '')
            
            # 提取 buggy code 和 fixed code
            buggy_match = re.search(r'```python\s*(.*?)\s*```', prompt, re.DOTALL)
            fixed_match = re.search(r'<fixed_code>\s*(.*?)\s*</fixed_code>', chosen, re.DOTALL)
            
            if buggy_match and fixed_match:
                buggy_code = buggy_match.group(1).strip()
                fixed_code = fixed_match.group(1).strip()
                
                # 创建知识库条目
                entry = {
                    "bug_type": item.get('bug_type', 'unknown'),
                    "bug_description": self._extract_bug_description(prompt),
                    "original_code": buggy_code,
                    "fixed_code": fixed_code,
                    "patch": self._generate_patch(buggy_code, fixed_code),
                    "source": item.get('source', 'dataset')
                }
                
                self.knowledge_base.append(entry)
                count += 1
        
        print(f"✅ 从数据集加载了 {count} 条 bug fix 示例")
        return count
    
    def _extract_bug_description(self, prompt: str) -> str:
        """从 prompt 中提取 bug 描述"""
        match = re.search(r'Bug:\s*(.*?)\n\n', prompt, re.DOTALL)
        return match.group(1).strip() if match else "Unknown bug"
    
    def _generate_patch(self, original: str, fixed: str) -> str:
        """生成简单的 diff patch"""
        lines_orig = original.split('\n')
        lines_fixed = fixed.split('\n')
        
        patch_lines = ["diff --git a/code.py b/code.py"]
        patch_lines.append("--- a/code.py")
        patch_lines.append("+++ b/code.py")
        
        # 简化的 diff 生成
        for i, (orig, fix) in enumerate(zip(lines_orig, lines_fixed)):
            if orig != fix:
                patch_lines.append(f"@@ -{i+1} +{i+1} @@")
                patch_lines.append(f"-{orig}")
                patch_lines.append(f"+{fix}")
        
        return '\n'.join(patch_lines)
    
    def save(self):
        """保存知识库到文件"""
        output_file = Path(self.output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(self.knowledge_base, f, ensure_ascii=False, indent=2)
        
        print(f"💾 知识库已保存到 {self.output_path}")
        print(f"   总计：{len(self.knowledge_base)} 条")
        
        # 统计 bug 类型
        bug_types = {}
        for item in self.knowledge_base:
            bt = item.get('bug_type', 'unknown')
            bug_types[bt] = bug_types.get(bt, 0) + 1
        
        print("\n📊 Bug 类型分布:")
        for bt, count in sorted(bug_types.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"  {bt}: {count} 条")
        
        return self.output_path


def build_knowledgebase():
    """主函数：构建知识库"""
    print("=" * 60)
    print("构建 RAG 知识库")
    print("=" * 60)
    
    kb = RAGKnowledgeBase()
    
    # 1. 从数据集加载
    print("\n📚 步骤 1: 从数据集加载")
    kb.load_from_dataset()
    
    # 2. 保存
    print("\n💾 步骤 2: 保存知识库")
    kb.save()
    
    print("\n✅ 完成！")
    print("\n📝 下一步:")
    print("  1. 检查知识库：cat runs/bug_fixes.json | head -50")
    print("  2. 在 GRPO 训练中使用 RAG")
    print("  3. 在推理时检索相似 bug")


if __name__ == "__main__":
    build_knowledgebase()
