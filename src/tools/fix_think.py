with open(r'd:\VscodeProject\repobugfix_complete_project\llm_judge.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the split think tag - the problem is that the
#<think>(.*?)
#</think>' pattern got split across two lines
old = "r'<think>(.*?)\\n\\n</think>', cot_result, re.DOTALL)"
new = "r'<think>(.*?)\\n</think>', cot_result, re.DOTALL)"
if old in content:
    content = content.replace(old, new)
    print("Fixed: split think tag")
else:
    # Check if there's a different split pattern
    old2 = "r'<think>(.*?)\\n\\n</think>'"
    if old2 in content:
        print("Found different split pattern")
        print(repr(content[content.find(old2)-20:content.find(old2)+50]))
    else:
        print("Pattern not found, checking...")
        idx = content.find("<think>")
        print(repr(content[idx:idx+80]))

with open(r'd:\VscodeProject\repobugfix_complete_project\llm_judge.py', 'w', encoding='utf-8') as f:
    f.write(content)
