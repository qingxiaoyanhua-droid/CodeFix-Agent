# Python 数据结构 — 面试标准答案

> 面试核心：能说清楚每种结构的底层实现、时间复杂度、适用场景

## 一、Python 有哪些数据结构

```
序列类型（Sequence）
  ├── str        字符串，不可变
  ├── list       列表，可变，动态数组
  ├── tuple      元组，不可变
  └── range      范围

散列类型（Mapping）
  └── dict       字典，Hash Map

集合类型（Set）
  ├── set        无序不重复集合
  └── frozenset  不可变集合

二进制类型
  └── bytes, bytearray, memoryview
```

## 二、最高频对比：list vs dict

### list（列表）— 动态数组

```python
lst = [1, 2, 3, "hello", 3.14]
lst.append(4)          # 追加：O(1) 均摊
lst.insert(0, 100)     # 插入中间：O(n)
lst.pop()              # 弹出不指定索引，默认末尾：O(1)
lst.pop(0)            # 指定索引弹出：O(n)
lst[2]                 # 按索引随机访问：O(1)
```

**底层**：CPython 是动态数组（`PyListObject`），预分配空槽，满了就翻倍扩容。

### dict（字典）— Hash Map

```python
d = {"name": "Alice", "age": 25}
d["city"] = "Beijing"      # 插入/更新：O(1) 均摊
d["name"]                 # 按键查找：O(1)
d.get("gender", "unknown") # 安全查找，key 不存在返回默认值
del d["age"]              # 删除：O(1)
"age" in d               # 键存在判断：O(1)
```

**底层**：开放寻址法 + 哈希表，哈希冲突用二次探测解决。Python 3.6+ 有序（insertion order）。

## 三、list 和 dict 的核心区别

| 维度 | list | dict |
|------|------|------|
| 访问方式 | 按索引（`lst[2]`）| 按键（`d["name"]`）|
| 查找复杂度 | O(n)（需遍历）| O(1)（哈希直接映射）|
| 插入复杂度 | 末尾 O(1)，中间 O(n) | O(1) 均摊 |
| 内存布局 | 连续内存（数组）| 哈希表（散列）|
| 有序性 | 插入有序（Python 3.7+）| 插入有序（Python 3.7+）|
| 键/值类型 | 值任意，索引必须为 int | 键必须可哈希（int/str/tuple），值任意 |
| 用途 | 有序存储、顺序访问 | 键值映射、快速查找 |

## 四、dict 的键为什么必须可哈希？

```python
# 可哈希 = 实现了 __hash__ 和 __eq__，且生命周期内哈希值不变
# 可哈希：int, str, float, tuple
# 不可哈希：list, dict, set（可变对象不能当键）

# 正确
d = {(1, 2): "tuple_key"}   # tuple 可哈希
d = {"name": "Alice"}        # str 可哈希

# 错误
d = {[1, 2]: "value"}       # TypeError: unhashable type 'list'
d = {{"a": 1}: "value"}     # TypeError: unhashable type 'dict'
```

## 五、set（集合）— 去重 + 快速判断

```python
s = {1, 2, 3, 2, 1}          # 自动去重: {1, 2, 3}
s.add(4)                     # O(1)
3 in s                       # O(1)，判断存在性极快
s1 = {1, 2, 3}
s2 = {2, 3, 4}
s1 & s2                     # 交集: {2, 3}
s1 | s2                     # 并集: {1, 2, 3, 4}
s1 - s2                     # 差集: {1}
```

**典型应用**：去重（`list(set(lst))`）、快速判断元素是否存在（代替 `if x in lst` 的 O(n) 查找）。

## 六、元组 vs 列表

| 维度 | tuple | list |
|------|-------|------|
| 可变性 | 不可变（创建后不能增删改）| 可变 |
| 性能 | 更轻量，略快 | 略慢 |
| 用途 | 函数返回值、固定配置、字典键 | 动态集合、需要修改的场景 |
| 语法 | `t = (1, 2)` 或 `t = 1, 2` | `l = [1, 2]` |

```python
# tuple 不可变，所以可以当 dict 的键
d = {(1, 2): "sum is 3"}

# tuple 用在函数多返回值
def divide(a, b):
    return a // b, a % b   # 返回 tuple
quotient, remainder = divide(10, 3)
```

## 七、面试标准回答模板

**Q: Python 有哪些数据结构？**

> "Python 有四大类：序列类型（list、tuple、str、range）用于有序存储；散列类型（dict）用于键值映射；集合类型（set、frozenset）用于去重和快速判断存在性；字节序列类型（bytes、bytearray）用于二进制数据。其中最核心的是 list 和 dict：list 是动态数组，按索引访问 O(1)，中间插入 O(n)；dict 是哈希表，按键查找 O(1)，键必须可哈希。"

**Q: dict 和 list 的区别？**

> "核心区别在于访问方式和查找效率。list 按索引访问，O(1)，但按值查找需要遍历，O(n)；dict 按键访问，底层是哈希表，插入和查找都是 O(1)。所以需要快速查找的场景用 dict，需要有序存储、按顺序遍历的场景用 list。dict 的键必须是可哈希的不可变对象（int、str、tuple），list 不能作为 dict 的键因为它是可变的。"
