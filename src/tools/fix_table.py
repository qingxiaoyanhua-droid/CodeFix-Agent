import codecs

filepath = r'd:\VsCodeProject\repobugfix_complete_project\面试记录\阿里面试速查.md'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# The file uses backtick template literals in table cells
# Let's find the table by looking for the section header and the next section

# Find "## 四、代码项目" section
section_start = content.find('## 四、代码项目')
if section_start == -1:
    print('Section not found!')
    exit(1)

# Find the next section after the table
section_end = content.find('\n## 五、会议项目', section_start)
if section_end == -1:
    print('Next section not found!')
    exit(1)

old_section = content[section_start:section_end]
print(f'Old section length: {len(old_section)}')
print('First 300 chars:', repr(old_section[:300]))

# Build the new section
new_section = old_section

# Find and replace the table header - add new column
old_header = '| 维度 | 权重 | 信号来源 | 怎么算 |'
new_header = '| 维度 | 权重 | 信号来源 | 怎么算 | 衡量CoT哪一步 |'
new_section = new_section.replace(old_header, new_header, 1)
print(f'Header replaced: {old_header in new_section}')

# Add new column to each data row
old_rows = [
    ('| **格式** | 0.05 | CoT结构完整性 | ', " | 整体结构 |"),
    ('| **语法** | 0.10 | compile()能过吗 | ', " | 整体结构 |"),
    ('| **AST结构** | 0.15 | 和参考答案结构相似 | ', " | 整体结构 |"),
    ('| **执行** | 0.30 | 测试用例通过率（最重要） | ', " | 整体结构 |"),
    ('| **步骤级PRM** | 0.25 | 7B对CoT每步打分 | ', " | **每一步单独衡量** |"),
    ('| **语义** | 0.15 | 和参考答案语义相似 | ', " | 整体结构 |"),
]

replaced_count = 0
for old_suffix, new_suffix in old_rows:
    if old_suffix in new_section:
        new_section = new_section.replace(old_suffix, old_suffix + new_suffix, 1)
        replaced_count += 1
        print(f'Replaced row: {old_suffix[:40]}...')
    else:
        print(f'NOT FOUND: {old_suffix[:60]}')

print(f'Total rows replaced: {replaced_count}')

# Add the explanation paragraph after the table
# Find the line "**格式奖励详细打分规则**："
old_explanation_marker = "**格式奖励详细打分规则**："
explanation_text = "\n> **六维核心逻辑**：一个正确的修复 = CoT四步全对 → 代码才能修对。六维分别对应推理链上的不同环节。步骤级PRM是唯一能追溯到CoT每步质量的维度，信息密度是困惑度的4倍。\n"
new_explanation_marker = explanation_text + "\n**格式奖励详细打分规则**："

if old_explanation_marker in new_section:
    new_section = new_section.replace(old_explanation_marker, new_explanation_marker, 1)
    print('Explanation added')
else:
    print('Explanation marker NOT FOUND')

# Replace in content
content = content[:section_start] + new_section + content[section_end:]

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)

print('Done!')
