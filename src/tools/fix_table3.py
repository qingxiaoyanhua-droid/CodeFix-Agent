"""Rebuild the interview cheatsheet with all new content."""
with open(r'd:\VsCodeProject\repobugfix_complete_project\面试记录\阿里面试速查.md', 'r', encoding='utf-8') as f:
    content = f.read()

# Section 4: "## 四、代码项目·六维Reward" to "## 六、RAG混合检索"
# Find the section boundaries
idx4 = content.find('\u516d\u3001RAG\u6df7\u5408\u68c0\u7d22')
print(f"Section 4 end (六、RAG): {idx4}")

# The section from "## 四" to before "## 六"
# Let's find "## 四" more precisely
for marker in ['\u56db\u3001\u4ee3\u7801\u9879\u76ee', '\u56db\u3001\u4ee3\u7801\u9879\u76ee\u00b7\u516d\u7ef4']:
    idx = content.find(marker)
    print(f"Marker {repr(marker)}: {idx}")

# Find the broken section
idx4_start = content.find('## \u56db\u3001\u4ee3\u7801\u9879\u76ee')
idx6_start = content.find('## \u516d\u3001RAG')
print(f"Section 4: {idx4_start} to {idx6_start}")

if idx4_start == -1 or idx6_start == -1:
    print("Could not find sections!")
    exit(1)

old_sec4 = content[idx4_start:idx6_start]
print(f"Old section 4 length: {len(old_sec4)}")
print(f"Old section 4 preview: {old_sec4[:200]}")

# Build the correct new section 4
new_sec4 = """## 四、代码项目·六维Reward（权重相加=1.00）

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

"""

# Find the end of the section - look for the start of next major section "# 五、会议项目"
# or just append the format reward section
# The section after the table originally had "格式奖励详细打分规则" and the python code
# Let's find it in the old section
idx_format = old_sec4.find('**格式奖励详细打分规则**')
if idx_format != -1:
    format_section = old_sec4[idx_format:]
    new_sec4 += format_section
    print("Appended format reward section")
else:
    print("Format section not found, appending default")

# Now replace
content = content[:idx4_start] + new_sec4 + content[idx6_start:]
print(f"New content length: {len(content)}")

with open(r'd:\VsCodeProject\repobugfix_complete_project\面试记录\阿里面试速查.md', 'w', encoding='utf-8') as f:
    f.write(content)

print("Done!")
