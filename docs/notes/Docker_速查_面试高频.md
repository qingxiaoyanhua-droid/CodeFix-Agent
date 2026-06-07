# Docker 速查 — 面试高频指令

> 面试重点：删除容器/镜像、查看状态、日志、进入容器

## 一、删除（最高频！）

```bash
# 删除单个镜像
docker rmi <image_id_or_name>

# 强制删除镜像（正在被容器使用也能删）
docker rmi -f <image_id>

# 删除已停止的容器
docker rm <container_id_or_name>

# 删除运行中的容器（先停再删）
docker rm -f <container_id_or_name>

# 删除所有已停止的容器
docker container prune

# 删除所有镜像
docker rmi $(docker images -q)

# 删除 dangling（无标签）的镜像
docker image prune

# 一键清理：停止所有容器 + 删除所有镜像 + 删除所有网络（慎用！）
docker system prune -a
```

## 二、查看状态

```bash
# 列出运行中的容器
docker ps

# 列出所有容器（包括已停止的）
docker ps -a

# 列出所有镜像
docker images

# 查看容器详细信息（IP、端口映射、环境变量等）
docker inspect <container_id_or_name>

# 查看容器资源使用（CPU、内存）
docker stats

# 查看容器进程列表
docker top <container_id_or_name>

# 查看容器网络
docker network ls
docker network inspect bridge
```

## 三、日志

```bash
# 查看容器日志（实时跟踪）
docker logs -f <container_id_or_name>

# 查看最后 100 行
docker logs --tail 100 <container_id_or_name>

# 查看最近 1 小时的日志
docker logs --since 1h <container_id_or_name>

# 查找包含关键词的日志
docker logs <container_id> 2>&1 | grep "ERROR"
```

## 四、进入容器 & 操作

```bash
# 进入运行中的容器（交互式终端）
docker exec -it <container_id_or_name> /bin/bash
docker exec -it <container_id_or_name> /bin/sh   # 如果没有 bash

# 如果容器没在运行，先启动再进入
docker start -i <container_id_or_name>

# 在容器内执行单条命令（不进入）
docker exec <container_id> ls /app
docker exec <container_id> cat /etc/passwd
```

## 五、构建 & 运行

```bash
# 从 Dockerfile 构建镜像
docker build -t <image_name>:<tag> <path_to_dockerfile>
docker build -t myapp:v1.0 .

# 运行容器
docker run -d --name myapp -p 8080:80 myapp:v1.0
# -d: 后台运行  --name: 给容器起名  -p: 端口映射（宿主机:容器）

# 运行并进入交互式终端
docker run -it <image_name> /bin/bash

# 带环境变量和卷挂载
docker run -d -e NODE_ENV=production -v /host/path:/container/path myapp

# 覆盖容器启动命令
docker run -it <image_name> /bin/sh
```

## 六、打标签 & 推送（CI/CD 链路）

```bash
# 给镜像打标签（通常是 GitHub SHA 作为 tag）
docker build -t myapp:$(git rev-parse --short HEAD) .
docker tag myapp:latest dockerhub_username/myapp:$(git rev-parse --short HEAD)

# 推送镜像到 Docker Hub
docker login -u dockerhub_username
docker push dockerhub_username/myapp:<tag>

# 推送后，在服务器上拉取并运行
docker pull dockerhub_username/myapp:<tag>
docker run -d -p 80:80 dockerhub_username/myapp:<tag>
```

## 七、镜像多阶段构建（生产优化）

```dockerfile
# 多阶段构建：编译阶段用大镜像，构建阶段只复制产物
FROM golang:1.22 AS builder
WORKDIR /app
COPY . .
RUN go build -o myapp main.go

FROM alpine:latest
COPY --from=builder /app/myapp /myapp
ENTRYPOINT ["/myapp"]
```

## 八、容器互联 & 网络

```bash
# 创建自定义网络
docker network create mynet

# 容器加入网络
docker run -d --network mynet --name app1 myapp

# 同一网络的容器可以用容器名互相访问
docker run -d --network mynet --name app2 myapp2
# app2 里 curl http://app1:8080 能通（DNS 自动解析）

# 查看容器 IP
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' <container>
```

## 九、docker-compose（多条指令合一）

```bash
# 启动所有服务（后台运行）
docker-compose up -d

# 停止并删除容器、网络
docker-compose down

# 重新构建镜像 + 启动
docker-compose up -d --build

# 查看日志
docker-compose logs -f

# 查看运行状态
docker-compose ps
```

## 十、面试高频问题

| 问题 | 回答 |
|------|------|
| Docker 和虚拟机的区别？ | 容器共享宿主机内核，更轻量（MB 级 vs GB 级），启动更快（秒级 vs 分钟级），隔离性弱一些 |
| Docker 怎么实现资源限制？ | `--memory` 限制内存，`--cpus` 限制 CPU，`docker update` 可以动态修改 |
| 如何排查容器启动失败？ | `docker logs` 查日志 + `docker inspect` 看配置 + `docker exec` 进容器检查 |
| Dockerfile 的 CMD 和 ENTRYPOINT 区别？ | CMD 提供默认命令（可被 docker run 参数覆盖），ENTRYPOINT 固定入口 |
| 如何查看容器网络互通？ | `docker network inspect` 看 bridge，或在容器内 `curl` 测试连通性 |
