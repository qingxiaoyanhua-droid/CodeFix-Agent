#!/usr/bin/env python3
"""
QuixBugs Benchmark - 正确格式版本
修复了数据中的换行符问题
"""

QUIXBUGS_PROGRAMS = [
    {
        "name": "bitcount",
        "buggy": """def bitcount(n):
    count = 0
    while n:
        n ^= n - 1
        count += 1
    return count""",
        "fixed": """def bitcount(n):
    count = 0
    while n:
        n &= n - 1
        count += 1
    return count""",
        "bug_type": "wrong_operator",
        "tests": [
            {"function": "bitcount", "input": [127], "output": 7},
            {"function": "bitcount", "input": [0], "output": 0},
            {"function": "bitcount", "input": [1], "output": 1},
        ]
    },
    {
        "name": "find_first_in_sorted",
        "buggy": """def find_first_in_sorted(arr, x):
    lo = 0
    hi = len(arr)
    while lo <= hi:
        mid = (lo + hi) // 2
        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):
            return mid
        elif x <= arr[mid]:
            hi = mid
        else:
            lo = mid + 1
    return -1""",
        "fixed": """def find_first_in_sorted(arr, x):
    lo = 0
    hi = len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if x == arr[mid] and (mid == 0 or x != arr[mid - 1]):
            return mid
        elif x <= arr[mid]:
            hi = mid - 1
        else:
            lo = mid + 1
    return -1""",
        "bug_type": "off_by_one",
        "tests": [
            {"function": "find_first_in_sorted", "input": [[1, 2, 3, 4], 3], "output": 2},
            {"function": "find_first_in_sorted", "input": [[1, 2, 3, 4], 5], "output": -1},
            {"function": "find_first_in_sorted", "input": [[1, 1, 2, 3], 1], "output": 0},
        ]
    },
    {
        "name": "flatten",
        "buggy": """def flatten(arr):
    for x in arr:
        if isinstance(x, list):
            for y in flatten(x):
                yield y
        else:
            yield flatten(x)""",
        "fixed": """def flatten(arr):
    for x in arr:
        if isinstance(x, list):
            for y in flatten(x):
                yield y
        else:
            yield x""",
        "bug_type": "wrong_variable",
        "tests": [
            {"function": "flatten", "input": [[[1, [2]], 3]], "output": [1, 2, 3]},
            {"function": "flatten", "input": [[[[1], 2, [3, [4]]]]], "output": [1, 2, 3, 4]},
        ]
    },
    {
        "name": "gcd",
        "buggy": """def gcd(a, b):
    if b:
        return gcd(a % b, b)
    else:
        return a""",
        "fixed": """def gcd(a, b):
    if b:
        return gcd(b, a % b)
    else:
        return a""",
        "bug_type": "wrong_argument_order",
        "tests": [
            {"function": "gcd", "input": [12, 8], "output": 4},
            {"function": "gcd", "input": [17, 5], "output": 1},
            {"function": "gcd", "input": [100, 25], "output": 25},
        ]
    },
    {
        "name": "is_valid_parenthesization",
        "buggy": """def is_valid_parenthesization(parens):
    depth = 0
    for paren in parens:
        if paren == '(':
            depth += 1
        else:
            depth -= 1
            if depth < 0:
                return False
    return True""",
        "fixed": """def is_valid_parenthesization(parens):
    depth = 0
    for paren in parens:
        if paren == '(':
            depth += 1
        else:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0""",
        "bug_type": "missing_check",
        "tests": [
            {"function": "is_valid_parenthesization", "input": ["(())"], "output": True},
            {"function": "is_valid_parenthesization", "input": ["(()"], "output": False},
            {"function": "is_valid_parenthesization", "input": [")("], "output": False},
            {"function": "is_valid_parenthesization", "input": [""], "output": True},
        ]
    },
    {
        "name": "max_sublist_sum",
        "buggy": """def max_sublist_sum(arr):
    max_ending_here = 0
    max_so_far = 0
    for x in arr:
        max_ending_here = max(0, max_ending_here + x)
        max_so_far = max(max_so_far, max_ending_here)
    return max_so_far""",
        "fixed": """def max_sublist_sum(arr):
    if not arr:
        return 0
    max_ending_here = arr[0]
    max_so_far = arr[0]
    for x in arr[1:]:
        max_ending_here = max(x, max_ending_here + x)
        max_so_far = max(max_so_far, max_ending_here)
    return max_so_far""",
        "bug_type": "wrong_initialization",
        "tests": [
            {"function": "max_sublist_sum", "input": [[4, -5, 2, 1, -1, 3]], "output": 5},
            {"function": "max_sublist_sum", "input": [[-1, -2, -3]], "output": 0},
            {"function": "max_sublist_sum", "input": [[1, 2, 3]], "output": 6},
        ]
    },
    {
        "name": "sqrt",
        "buggy": """def sqrt(x, epsilon):
    approx = x / 2
    while abs(x - approx) > epsilon:
        approx = 0.5 * (approx + x / approx)
    return approx""",
        "fixed": """def sqrt(x, epsilon):
    approx = x / 2
    while abs(approx * approx - x) > epsilon:
        approx = 0.5 * (approx + x / approx)
    return approx""",
        "bug_type": "wrong_expression",
        "tests": [
            {"function": "sqrt", "input": [4, 0.01], "output": 2.0},
            {"function": "sqrt", "input": [9, 0.01], "output": 3.0},
        ]
    },
    {
        "name": "quicksort",
        "buggy": """def quicksort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    lesser = quicksort([x for x in arr[1:] if x < pivot])
    greater = quicksort([x for x in arr[1:] if x > pivot])
    return lesser + [pivot] + greater""",
        "fixed": """def quicksort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    lesser = quicksort([x for x in arr[1:] if x < pivot])
    greater = quicksort([x for x in arr[1:] if x >= pivot])
    return lesser + [pivot] + greater""",
        "bug_type": "off_by_one",
        "tests": [
            {"function": "quicksort", "input": [[3, 1, 2]], "output": [1, 2, 3]},
            {"function": "quicksort", "input": [[5, 3, 3, 1]], "output": [1, 3, 3, 5]},
            {"function": "quicksort", "input": [[]], "output": []},
        ]
    },
    {
        "name": "shortest_path_lengths",
        "buggy": """def shortest_path_lengths(graph, source):
    dist = {source: 0}
    queue = [source]
    while queue:
        node = queue.pop(0)
        for neighbor in graph.get(node, []):
            new_dist = dist[node] + 1
            if neighbor not in dist or new_dist < dist[neighbor]:
                dist[neighbor] = new_dist
                queue.append(neighbor)
    return dist""",
        "fixed": """def shortest_path_lengths(graph, source):
    dist = {source: 0}
    queue = [source]
    while queue:
        node = queue.pop(0)
        for neighbor, weight in graph.get(node, []):
            new_dist = dist[node] + weight
            if neighbor not in dist or new_dist < dist[neighbor]:
                dist[neighbor] = new_dist
                queue.append(neighbor)
    return dist""",
        "bug_type": "wrong_data_structure",
        "tests": []  # 需要图结构，暂不测试
    },
    {
        "name": "maximum_subarray",
        "buggy": """def maximum_subarray(nums):
    max_sum = nums[0]
    current_sum = nums[0]
    for num in nums[1:]:
        current_sum = max(num, current_sum + num)
        max_sum = max(max_sum, current_sum)
    return max_sum""",
        "fixed": """def maximum_subarray(nums):
    if not nums:
        return 0
    max_sum = nums[0]
    current_sum = nums[0]
    for num in nums[1:]:
        current_sum = max(num, current_sum + num)
        max_sum = max(max_sum, current_sum)
    return max_sum""",
        "bug_type": "wrong_initialization",
        "tests": [
            {"function": "maximum_subarray", "input": [[-2, 1, -3, 4, -1, 2, 1, -5, 4]], "output": 6},
        ]
    },
    {
        "name": "kmp",
        "buggy": """def kmp(pattern, text):
    n = len(pattern)
    lps = [0] * n
    length = 0
    i = 1
    while i < n:
        if pattern[i] == pattern[length]:
            length += 1
            lps[i] = length
            i += 1
        else:
            if length != 0:
                length = lps[length - 1]
            else:
                lps[i] = 0
                i += 1
    return lps""",
        "fixed": """def kmp(pattern, text):
    n = len(pattern)
    lps = [0] * n
    length = 0
    i = 1
    while i < n:
        if pattern[i] == pattern[length]:
            length += 1
            lps[i] = length
            i += 1
        else:
            if length != 0:
                length = lps[length - 1]
            else:
                lps[i] = 0
                i += 1
    return lps""",
        "bug_type": "none",
        "tests": []  # KMP需要完整实现
    },
]
