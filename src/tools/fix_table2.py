"""Fix the malformed table in the interview cheatsheet."""

with open(r'd:\VsCodeProject\repobugfix_complete_project\面试记录\阿里面试速查.md', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the problematic section: "## 四、代码项目" to "## 六、RAG"
sec4_start = content.find('\u4e8c\u3001\u4ee3\u7801\u9879\u76ee\u00b7\u516d\u7ef4Reward')
sec4_end = content.find('\u516d\u3001RAG\u6df7\u5408\u68c0\u7d22')

print(f"Section 4: {sec4_start} to {sec4_end}")

old_sec4 = content[sec4_start:sec4_end]
print(f"Old section length: {len(old_sec4)}")

# Fix the broken table rows
# Current state (BROKEN):
# | **格式** | 0.05 | CoT结构完整性 |  | 整体结构 |`...
# Should be:
# | **格式** | 0.05 | CoT结构完整性 | `...

# For each row, the pattern is: | col1 | col2 | col3 |  | col5 | real_col3_content |
# Fix: remove the spurious "  | 整体结构 |" and "| **每一步单独衡量** |" prefix on each row

# Simpler: just rebuild the whole section
new_sec4 = old_sec4

# Fix each row by removing the spurious columns
# Row pattern: | **xxx** | val | desc |  | note | real_content |
# Fix: | **xxx** | val | desc | real_content |

rows_to_fix = [
    # (broken_suffix, fixed_suffix)
    ("`\u5f00\u59cb0.25 + `\u5fc5\u8003\u7ed3\u675f0.25 + \u6bcf\u6b65`[Step i:]`\u5404` + `<fixed_code>`\u5404 + \u56db\u6b65\u5168\u9f50\u989d\u5916+0.2 |",
     "`\u5f00\u59cb0.25 + `\u5fc5\u8003\u7ed3\u675f0.25 + \u6bcf\u6b65`[Step i:]`\u5404 + `<fixed_code>`\u5404 + \u56db\u6b65\u5168\u9f50\u989d\u5916+0.2 |  \u6574\u4f53\u7ed3\u6784 |"),
    ("Python `compile()` \u901a\u8fc7=1.0\uff0cSyntaxError=0.0 |",
     "Python `compile()` \u901a\u8fc7=1.0\uff0cSyntaxError=0.0 |  \u6574\u4f53\u7ed3\u6784 |"),
    ("Jaccard(\u8282\u70b9\u7c7b\u578b\u96c6\u5408)\xd70.6 + \u51fd\u6570\u7b7e\u540d\u5339\u914d\xd70.4 |",
     "Jaccard(\u8282\u70b9\u7c7b\u578b\u96c6\u5408)\xd70.6 + \u51fd\u6570\u7b7e\u540d\u5339\u914d\xd70.4 |  \u6574\u4f53\u7ed3\u6784 |"),
    ("\u901a\u8fc7\u6570/\u603b\u6570\uff0cACES\u98ce\u683c\u52a0\u6743 |",
     "\u901a\u8fc7\u6570/\u603b\u6570\uff0cACES\u98ce\u683c\u52a0\u6743 |  \u6574\u4f53\u7ed3\u6784 |"),
    ("FIPO\u52a0\u6743[0.15, 0.20, 0.30, 0.35] |",
     "FIPO\u52a0\u6743[0.15, 0.20, 0.30, 0.35] |  **\u6bcf\u4e00\u6b65\u5355\u72ec\u8861\u91cf** |"),
    ("BGE embedding cosine similarity |",
     "BGE embedding cosine similarity |  \u6574\u4f53\u7ed3\u6784 |"),
]

# The broken rows have: | col1 | col2 | col3 |  | note | real_col3 |
# We need: | col1 | col2 | col3 |  note | (but col3 is broken into two parts)
# The real fix: remove the note column and merge the col3 back together

# Actually, the simplest fix is to rebuild the entire section from scratch
# Let me try a different approach - just write the correct section

# New section content (complete and correct):
new_sec4_correct = """
## 四、代码项目·六维Reward（权重相加=1.00）

> 注意：这是代码修复任务的六维！衡量CoT推理链完整性！不是会议总结的六维！不能混用！

| 维度 | 权重 | 信号来源 | 怎么算 | 衡量CoT哪一步 |
|------|------|---------|--------|------------|
| **格式** | 0.05 | CoT结构完整性 | `'<think>'`开始0.25 + `'</think>'`结束0.25 + 每步`[Step i:]`各0.125 + `<fixed_code>`各0.25 + 4步全齐额外+0.2 | 整体结构 |
| **语法** | 0.10 | compile()能过吗 | Python `compile()` 通过=1.0，SyntaxError=0.0 | 整体结构 |
| **AST结构** | 0.15 | 和参考答案结构相似 | Jaccard(节点类型集合)×0.6 + 函数签名匹配×0.4 | 整体结构 |
| **执行** | 0.30 | 测试用例通过率（最重要） | 通过数/总数，ACES风格加权 | 整体结构 |
| **步骤级PRM** | 0.25 | 7B对CoT每步打分 | FIPO加权[0.15, 0.20, 0.30, 0.35] | **每一步单独衡量** |
| **语义** | 0.15 | 和参考答案语义相似 | BGE embedding cosine similarity | 整体结构 |

> **六维核心逻辑**：一个正确的修复 = CoT四步全对 → 代码才能修对。六维分别对应推理链上的不同环节。步骤级PRM是唯一能追溯到CoT每步质量的维度，信息密度是困惑度的4倍。

**格式奖励详细打分规则**：
"""

# Check if the old section starts with the right content
print("Old section starts with:", repr(old_sec4[:100]))
if "\u4e8c\u3001\u4ee3\u7801\u9879\u76ee" in old_sec4[:50]:
    print("YES - section header found")
    new_sec4_correct = "\u4e8c\u3001\u4ee3\u7801\u9879\u76ee\u00b7\u516d\u7ef4Reward\uff08\u6743\u91cd\u76f8\u52a0=1.00\uff09\n\n> \u6ce8\u610f\uff1a\u8fd9\u662f\u4ee3\u7801\u4fee\u590d\u4efb\u52a1\u7684\u516d\u7ef4\uff01\u衔量CoT\u63a8\u7406\u94c3\u5b8c\u6574\u6027\uff01\u4e0d\u662f\u4f1a\u8bae\u603b\u7ed3\u7684\u516d\u7ef4\uff01\u4e0d\u80fd\u6df7\u7528\uff01\n\n" + new_sec4_correct[50:]

content = content[:sec4_start] + new_sec4_correct + content[sec4_end:]

with open(r'd:\VsCodeProject\repobugfix_complete_project\面试记录\阿里面试速查.md', 'w', encoding='utf-8') as f:
    f.write(content)

print("Done! New length:", len(content))
