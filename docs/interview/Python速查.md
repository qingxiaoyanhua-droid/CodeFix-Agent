# Python 面试速查（人话版）

> 能讲出来就行，别死记硬背。

---

## 一、装饰器

**干啥的**：给函数戴个帽子，函数执行前后自动加一些通用逻辑。比如计时、记录日志、权限检查。

```python
def timer(func):                    # func 是被戴帽子的那个函数
    def wrapper(*args, **kwargs):   # wrapper 包装一下
        start = time.time()
        result = func(*args, **kwargs)  # 真正执行原函数
        print(f"耗时 {time.time()-start:.2f}s")
        return result
    return wrapper                  # 返回包装后的函数

@timer                             # 相当于 my_func = timer(my_func)
def my_func():
    ...
```

**面试问**：
- `*args` 和 `**kwargs` 是什么？
  - `*args` 是打包所有位置参数成元组，`**kwargs` 是打包所有关键字参数成字典。拆包反过来用 `*` 和 `**`。
  - 简单说：`*args` 接收任意多个参数，`**kwargs` 接收任意多个带名字的参数。
- 装饰器怎么带参数？
  - 多包一层，用 `retry(max_times=3)` 的时候写法是 `@retry(max_times=3)`，相当于 `my_func = retry(max_times=3)(my_func)`。

---

## 二、上下文管理器

**干啥的**：自动帮你处理"开始"和"结束"的配对操作。比如打开文件要关、获取锁要释放。

```python
with open("a.txt") as f:    # 进去，自动调用 __enter__
    content = f.read()
                              # 出来，自动调用 __exit__
                              # 即使中间报错，__exit__ 也会执行
```

**不用 with 就要手动写**：
```python
f = open("a.txt")
try:
    content = f.read()
finally:
    f.close()   # 忘了关就麻烦了
```

**面试问**：
- `__exit__` 三个参数是什么？
  - `exc_type` 异常类型、`exc_val` 异常值、`exc_tb` 异常栈。如果没异常，三个都是 None。
- `__exit__` 返回 True 是什么意思？
  - 吞掉异常，不往外抛。正常情况返回 False。

---

## 三、生成器

**干啥的**：一次吐一个，用完就丢，不占内存。适合处理大数据流、无限序列。

```python
def fib():
    a, b = 0, 1
    while True:
        yield a      # 吐一个，暂停在这里，等下次调用再继续
        a, b = b, a+b

gen = fib()
print(next(gen))   # 0
print(next(gen))   # 1
print(next(gen))   # 1
```

**生成器表达式**（更短的写法）：
```python
squares = (x**2 for x in range(1000000))  # 括号是生成器，中括号是列表
# 列表一次生成100万个数占用内存，生成器一个都不占
```

**面试问**：
- 和列表的区别？
  - 列表一次全生成，占内存；生成器用多少生成多少，不占内存。
  - 生成器只能用一次，遍历完就没了；列表可以用多次。
- `yield` 和 `return` 的区别？
  - `return` 结束函数；`yield` 暂停函数，保存状态，下次调用从暂停处继续。

---

## 四、异步 async / await

**干啥的**：一个线程里同时干多个事。比如发10个网络请求，同时发不用排队等。

```python
import asyncio

async def fetch(url):           # async 定义异步函数
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:  # await 等结果
            return await resp.json()

async def main():
    urls = ["http://a.com", "http://b.com"]
    results = await asyncio.gather(*[fetch(u) for u in urls])  # 并发

asyncio.run(main())
```

**面试问**：
- 协程和线程的区别？
  - 线程是操作系统调度，多个线程可以同时跑，但切换有开销。
  - 协程是程序员自己控制切换，单线程里执行，遇到 `await` 等 IO 时才切换，不用系统切换所以快。
- 什么场景用 async？
  - IO 密集型用 async 很快（网络请求、文件读写）。
  - 计算密集型（跑模型、大量计算）用 `multiprocessing` 多进程，async 没帮助。

---

## 五、深拷贝和浅拷贝

**干啥的**：复制一个对象的时候，要搞清楚是复制引用还是复制内容。

```python
a = [1, [2, 3]]   # a[1] 是一个列表，是可变对象

b = a              # 根本不是拷贝，两个名字指向同一个东西
b[0] = 999
print(a[0])        # 999，a 跟着变了

c = a.copy()       # 浅拷贝，只拷贝第一层
c[0] = 888
print(a[0])        # 999，a 没变，第一层是独立的

c[1][0] = 777      # 但第二层还是同一个对象
print(a[1][0])     # 777，a 跟着变了

import copy
d = copy.deepcopy(a)   # 深拷贝，递归拷贝所有层级
d[1][0] = 666
print(a[1][0])         # 777，a 完全不受影响
```

**面试问**：
- 什么是可变对象和不可变对象？
  - 可变：`list`、`dict`、`set`——可以原地修改
  - 不可变：`int`、`str`、`tuple`——改的时候会创建新对象

---

## 六、闭包

**干啥的**：函数里返回一个函数，返回的函数记住了外部的变量。

```python
def make_adder(x):        # x 是外部变量
    def adder(y):
        return x + y      # adder 用到了 x，x 被记住了
    return adder

add5 = make_adder(5)      # 记住了 x=5
print(add5(3))            # 8
print(add5(10))            # 15，x 还是 5
```

**面试问**：
- 闭包是什么？
  - 就是一个函数 + 它引用的外部变量。函数可以"记住"创建时的环境。
- 闭包和装饰器有什么关系？
  - 装饰器本质上就是闭包。装饰器返回一个新函数，新函数引用了被装饰的函数。

---

## 七、`__init__` 和 `__new__`

**干啥的**：创建对象的两个步骤，`__new__` 先创建对象，`__init__` 再初始化属性。

```python
class Singleton:
    _instance = None

    def __new__(cls):                    # __new__ 负责创建对象
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self.data = "hello"             # __init__ 只负责初始化
```

**面试问**：
- 什么时候用 `__new__`？
  - 单例模式。还有一个经典场景：让 `str` 子类返回整数。
  - 正常情况下用 `__init__` 就够了。

---

## 八、GIL（全局解释器锁）

**干啥的**：CPython 的一个机制，保证同一时刻只有一个线程在执行 Python 字节码。

```python
import threading

def cpu_task():
    total = 0
    for i in range(10**7):
        total += i

threads = [threading.Thread(target=cpu_task) for _ in range(4)]
# 4 个线程，但因为 GIL，实际上同一时刻只有一个在跑
# 对于计算密集型任务，多线程反而比单线程慢
```

**面试问**：
- GIL 是什么？
  - 就是一把锁。Python 在执行字节码的时候要拿这把锁，同一时刻只有一个线程能拿到。
  - 所以多线程在 CPU 密集型任务上没加速效果。
- 怎么绕过？
  - IO 密集型用 `async`（协程，不涉及 CPU）
  - CPU 密集型用 `multiprocessing`（多进程，每个进程有自己的 GIL）

---

## 九、垃圾回收

**干啥的**：自动回收不再使用的内存，不用手动 `free`。

```python
a = [1, 2, 3]
a = None      # [1,2,3] 没人引用了，Python 自动回收
```

**但有循环引用就会出问题**：
```python
a = []
b = []
a.append(b)
b.append(a)   # a 和 b 互相引用，引用计数都不是 0
del a
del b         # 对象还在，因为互相引用，引用计数不为零
gc.collect()  # 手动触发垃圾回收器，回收循环引用
```

**面试问**：
- Python 用什么回收垃圾？
  - 引用计数（主要）+ 标记-清除（处理循环引用）+ 分代回收（频繁回收年轻对象）
- 什么时候要手动 `gc.collect()`？
  - 临时创建大量循环引用对象时。一般不用管。

---

## 十、猴子补丁

**干啥的**：运行时动态给类或模块打补丁，替换掉原来的方法。

```python
# 线上出了问题，不想重启服务，临时替换
import requests
original_get = requests.get

def patched_get(url, **kwargs):
    print(f"请求: {url}")
    return original_get(url, **kwargs)

requests.get = patched_get    # 替换掉原来的
```

**面试知道这个概念就行**，一般不会深问。

---

## 十一、`is` 和 `==`

```python
a = [1, 2, 3]
b = [1, 2, 3]

print(a == b)   # True，内容一样
print(a is b)   # False，两个不是同一个对象，内存地址不同

c = a
print(a is c)   # True，c 和 a 指向同一个对象
```

**面试问**：
- 区别是什么？
  - `==` 比的是值，`is` 比的是身份（内存地址）
  - 小整数池：`-5` 到 `256` 的整数是共用的，`is` 也是 True

---

## 十二、单例模式

**干啥的**：一个类只有一个实例，全局共享。比如配置对象。

```python
# 方式一：装饰器
def singleton(cls):
    instances = {}
    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]
    return get_instance

@singleton
class Config:
    ...

# 方式二：__new__
class Singleton:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
```

---

## 十三、常见标准库速记

| 库 | 用来干嘛 | 一句话 |
|----|---------|--------|
| `collections.defaultdict` | 字典取值不报错 | 访问不存在的 key 给默认值 |
| `collections.Counter` | 快速计数 | 一句话数出列表里每个元素出现几次 |
| `collections.deque` | 双端队列 | 头尾操作 O(1)，适合做队列 |
| `functools.lru_cache` | 缓存结果 | 加个装饰器，重复计算直接返回缓存 |
| `functools.partial` | 固定部分参数 | 把两个参数的函数变成一个参数的 |
| `json` | 序列化 | 字典变字符串，跨语言用 |
| `pickle` | 序列化 | Python 专用，能序列化任意对象 |

---

## 十四、面试最后过一遍

1. 装饰器能写出来吗？`*args` `**kwargs` 知道吗？
2. `with` 上下文管理器原理是什么？
3. 生成器和列表的根本区别是什么？
4. 什么是 GIL？什么情况下多线程没用？
5. 浅拷贝和深拷贝画图能说清楚吗？
6. `async` / `await` 是什么？什么场景用它？
7. `__init__` 和 `__new__` 的区别？
8. 闭包是什么？
9. `is` 和 `==` 的区别？
10. 单例模式能写出来吗？

这 10 条能答上来，Python 基础就够了。
