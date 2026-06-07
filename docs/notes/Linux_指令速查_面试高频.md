# Linux 指令速查 — 面试高频场景

> 面试重点：查文件、看大文件内容、搜索、权限、进程

## 一、查找文件

```bash
# 查找文件（按名字）
find / -name "*.py" -type f          # 全局搜索
find . -name "*.py" -type f          # 当前目录递归
find /home -name "app.log" -type f   # 指定目录

# 查找并对结果执行命令
find . -name "*.py" -exec grep -l "bug" {} \;   # 找包含 bug 的 py 文件

# 快速查找（基于数据库，不用遍历，updatedb 后才准）
locate filename

# 按类型查找
find . -type d -name "test"     # 找目录
find . -type f -size +100M      # 找大于 100M 的文件
find . -type f -mtime -7        # 找 7 天内修改过的文件

# 查找最近修改的文件
ls -lt | head                    # 按修改时间排序，最新的在前
```

## 二、看文件内容（最重点！）

### 小文件

```bash
cat file.txt                        # 一次性打印全部内容
cat -n file.txt                    # 带行号
cat file1.txt file2.txt            # 合并打印
```

### 中等文件（分页查看）— 正确回答！

```bash
# less 是看日志的标配！支持上下翻页、搜索、跳转
less file.log
# less 内部快捷键：
#   ↑ / ↓         上一行 / 下一行
#   PgUp / PgDn   上一页 / 下一页
#   /关键词        向下搜索
#   ?关键词        向上搜索
#   n              下一个搜索结果
#   N              上一个搜索结果
#   g / G          跳到首行 / 尾行
#   q              退出

more file.log                       # less 的简化版，只能向下翻
```

### 大文件（只读部分，不要全量加载）— 正确回答！

```bash
# head：只看开头（前 N 行）
head -n 20 file.log                # 看前 20 行
head -100 file.log                 # 看前 100 行

# tail：只看结尾（最常用！查日志标配）
tail -n 50 file.log                # 看最后 50 行
tail -f file.log                   # 实时跟踪（-f = follow，新增内容实时显示）
tail -f file.log | grep "ERROR"    # 实时跟踪 + 过滤 ERROR

# sed：查看特定范围的行（比 awk 简单）
sed -n '100,200p' file.log         # 查看第 100-200 行
sed -n '100p' file.log             # 只看第 100 行

# 按大小截取
split -l 10000 large_file.log part_   # 每 10000 行拆成一个文件

# 查看文件总行数
wc -l file.log                     # wc = word count，-l = line
```

### 关键词搜索

```bash
grep "ERROR" file.log              # 查找包含 ERROR 的行
grep -n "ERROR" file.log          # 带行号显示
grep -i "error" file.log          # 忽略大小写
grep -v "INFO" file.log           # 反向查找（不包含 INFO 的行）
grep -c "ERROR" file.log          # 统计出现次数
grep -A 5 "ERROR" file.log        # 显示匹配行及之后 5 行
grep -B 3 "ERROR" file.log        # 显示匹配行及之前 3 行
grep -E "ERROR|WARN" file.log     # 支持正则（-E = extended）

# 多文件搜索
grep "ERROR" *.log                # 当前目录所有 .log 文件
grep -r "bug" ./src/              # 递归搜索 src 目录
```

### 压缩文件内容查看（不用解压）

```bash
zcat largefile.gz                  # 查看 .gz 文件内容
zgrep "ERROR" largefile.gz        # 在 .gz 里搜索关键词
```

## 三、权限

```bash
ls -la                             # 查看文件权限（drwxr-xr-x）
chmod 755 file.sh                  # rwx r-x r-x（所有者可执行，所有人可读）
chmod +x file.sh                   # 添加执行权限
chown user:group file.txt          # 修改所有者
sudo chmod -R 755 /app/            # -R 递归
```

## 四、进程

```bash
ps aux                              # 查看所有进程
ps aux | grep python                # 找 python 进程
top                                 # 实时看 CPU/内存占用（q 退出）
htop                                # top 的增强版（彩色，更直观）

# 杀进程
kill -9 <pid>                      # 强制杀（-15 更温和，-9 强制）
pkill -f "python app.py"           # 按进程名杀

# 查端口
lsof -i :8080                      # 哪个进程占用了 8080 端口
netstat -tlnp | grep 8080          # 同上，查端口占用
```

## 五、网络

```bash
curl http://localhost:8080/api     # 测接口
curl -X POST -d '{"name":"test"}' http://api.com
ping 8.8.8.8                       # 网络连通性
wget https://example.com/file.zip  # 下载文件
```

## 六、磁盘 & 内存

```bash
df -h                              # 磁盘使用情况
du -sh *                           # 各目录大小
free -h                            # 内存使用情况
```

## 七、面试标准回答示例

**Q: 很大的日志文件怎么查看？**
> "用 `tail -f` 实时跟踪新增内容，或者用 `less` 分页查看，用 `/关键词` 搜索。如果要找特定时间的日志段，先用 `grep` 过滤出时间范围，再用 `sed -n '行1,行2p'` 截取那段来看。"

**Q: 查找包含某个关键词的所有文件？**
> "`grep -rn '关键词' ./src/` 或者 `find . -name '*.py' | xargs grep '关键词'`。`-r` 是递归，`-n` 是带行号，`-l` 是只显示文件名。"

**Q: Linux 怎么排查 CPU 100% 的问题？**
> "`top` 看一下哪个进程占用最高，然后 `ps aux | grep <pid>` 定位具体进程，再用 `strace -p <pid>` 看它在干什么，或者 `cat /proc/<pid>/fd` 看打开的文件描述符。"
