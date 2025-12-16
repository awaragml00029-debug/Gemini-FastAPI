# Gemini-FastAPI Docker 镜像构建指南

本文档说明如何手动编译和部署 Gemini-FastAPI 的 Docker 镜像。

## 目录
- [前置要求](#前置要求)
- [快速开始](#快速开始)
- [详细步骤](#详细步骤)
- [常见问题](#常见问题)
- [高级配置](#高级配置)

---

## 前置要求

### 系统要求
- Docker Engine 20.10 或更高版本
- Docker Compose V2 或更高版本
- 至少 2GB 可用磁盘空间
- 稳定的网络连接（用于拉取基础镜像和依赖）

### 检查环境
```bash
# 检查 Docker 版本
docker --version
# 输出示例: Docker version 24.0.7, build afdd53b

# 检查 Docker Compose 版本
docker compose version
# 输出示例: Docker Compose version v2.23.0

# 检查 Docker 是否运行
docker ps
```

---

## 快速开始

### 最简单的构建方式

```bash
# 1. 进入项目目录
cd /root/tread/Gemini-FastAPI

# 2. 构建镜像（带缓存，速度快）
docker build -t gemini-fastapi:latest .

# 3. 运行容器
docker run -d --name gemini-fastapi -p 8092:8000 gemini-fastapi:latest
```

---

## 详细步骤

### 1. 准备代码

#### 方式 A：使用你的 Fork（推荐 - 包含 bug 修复）
```bash
cd /root/tread

# 如果还没有克隆，先克隆你的 fork
git clone https://github.com/awaragml00029-debug/Gemini-FastAPI.git
cd Gemini-FastAPI

# 切换到修复分支
git checkout fix/reference-image-generation-bug
```

#### 方式 B：使用官方仓库（不包含 bug 修复）
```bash
cd /root/tread

# 克隆官方仓库
git clone https://github.com/Nativu5/Gemini-FastAPI.git
cd Gemini-FastAPI

# 查看最新标签（可选）
git tag -l
```

### 2. 检查 Dockerfile

查看 Dockerfile 内容，确保配置正确：
```bash
cat Dockerfile
```

应该看到类似内容：
```dockerfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-cache --no-dev
COPY app/ app/
COPY config/ config/
COPY run.py .
CMD ["uv", "run", "run.py"]
```

### 3. 构建 Docker 镜像

#### 方式 A：普通构建（使用缓存）
```bash
docker build -t gemini-fastapi:latest .
```

**优点**：速度快，适合日常开发
**缺点**：可能使用旧的缓存层

#### 方式 B：无缓存构建（推荐用于生产）
```bash
docker build --no-cache -t gemini-fastapi:latest .
```

**优点**：确保所有依赖都是最新的，避免缓存问题
**缺点**：速度较慢（约 1-2 分钟）

**⚠️ 重要**：每次修改代码后，必须使用无缓存构建才能确保代码生效！

#### 方式 C：构建特定版本
```bash
# 构建并打上版本标签
docker build --no-cache -t gemini-fastapi:v1.0.0 -t gemini-fastapi:latest .
```

#### 方式 D：指定平台构建
```bash
# 为特定平台构建
docker build --platform linux/amd64 -t gemini-fastapi:latest .

# 多平台构建（需要 buildx）
docker buildx build --platform linux/amd64,linux/arm64 -t gemini-fastapi:latest .
```

### 4. 验证镜像构建成功

```bash
# 查看镜像列表
docker images | grep gemini-fastapi

# 输出示例：
# gemini-fastapi    latest    abc123def456    2 minutes ago    500MB

# 查看镜像详细信息
docker inspect gemini-fastapi:latest

# 查看镜像层历史
docker history gemini-fastapi:latest
```

### 5. 测试运行

#### 快速测试
```bash
# 启动容器（前台运行，便于查看日志）
docker run --rm -p 8092:8000 gemini-fastapi:latest

# 在另一个终端测试健康检查
curl http://localhost:8092/v1/health
```

#### 生产运行
```bash
# 后台运行，自动重启
docker run -d \
  --name gemini-fastapi \
  --restart unless-stopped \
  -p 8092:8000 \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/data:/app/data \
  gemini-fastapi:latest

# 查看日志
docker logs -f gemini-fastapi

# 查看容器状态
docker ps | grep gemini-fastapi
```

### 6. 使用 Docker Compose 部署（推荐）

创建或编辑 `docker-compose.yml`：
```yaml
version: '3.8'

services:
  gemini-fastapi:
    image: gemini-fastapi:latest
    container_name: gemini-fastapi
    restart: unless-stopped
    ports:
      - "8092:8000"
    volumes:
      - ./config:/app/config:ro
      - ./data:/app/data
    environment:
      - TZ=Asia/Shanghai
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

启动服务：
```bash
# 启动
docker compose up -d

# 查看日志
docker compose logs -f

# 停止
docker compose down

# 重启
docker compose restart
```

---

## 常见问题

### Q1: 构建时提示 "permission denied"
```bash
# 解决方案：确保 Docker socket 权限正确
sudo chmod 666 /var/run/docker.sock

# 或者将当前用户加入 docker 组
sudo usermod -aG docker $USER
# 需要重新登录才能生效
```

### Q2: 构建时网络超时
```bash
# 方案 1：使用国内镜像加速
# 编辑 /etc/docker/daemon.json
{
  "registry-mirrors": [
    "https://docker.mirrors.ustc.edu.cn",
    "https://hub-mirror.c.163.com"
  ]
}

# 重启 Docker
sudo systemctl restart docker

# 方案 2：增加超时时间
docker build --network=host --no-cache -t gemini-fastapi:latest .
```

### Q3: 构建后代码修改没生效
```bash
# 原因：Docker 使用了旧的缓存层
# 解决方案：强制无缓存构建
docker build --no-cache -t gemini-fastapi:latest .

# 或者删除旧镜像后重新构建
docker rmi gemini-fastapi:latest
docker build -t gemini-fastapi:latest .
```

### Q4: 端口冲突
```bash
# 查看端口占用
lsof -i :8092
# 或
netstat -tulpn | grep 8092

# 解决方案 1：停止占用端口的容器
docker ps | grep 8092
docker stop <容器ID>

# 解决方案 2：更换端口
docker run -d -p 8093:8000 gemini-fastapi:latest
```

### Q5: 容器无法启动
```bash
# 查看详细错误信息
docker logs gemini-fastapi

# 查看容器退出代码
docker inspect gemini-fastapi --format='{{.State.ExitCode}}'

# 交互式进入容器调试
docker run -it --entrypoint /bin/bash gemini-fastapi:latest
```

### Q6: 镜像体积过大
```bash
# 查看镜像大小
docker images gemini-fastapi

# 清理未使用的层
docker image prune

# 使用多阶段构建（需要修改 Dockerfile）
# 参考高级配置部分
```

---

## 高级配置

### 1. 清理 Docker 资源

```bash
# 清理停止的容器
docker container prune -f

# 清理未使用的镜像
docker image prune -a -f

# 清理所有未使用的资源（危险！）
docker system prune -a --volumes -f

# 查看 Docker 磁盘使用情况
docker system df
```

### 2. 优化构建速度

#### 使用 BuildKit（更快的构建引擎）
```bash
# 临时启用
DOCKER_BUILDKIT=1 docker build -t gemini-fastapi:latest .

# 永久启用（编辑 /etc/docker/daemon.json）
{
  "features": {
    "buildkit": true
  }
}
```

#### 使用构建缓存
```bash
# 导出构建缓存
docker build --build-arg BUILDKIT_INLINE_CACHE=1 -t gemini-fastapi:latest .

# 使用缓存构建
docker build --cache-from gemini-fastapi:latest -t gemini-fastapi:latest .
```

### 3. 自定义构建参数

```bash
# 传递构建参数
docker build \
  --build-arg PYTHON_VERSION=3.12 \
  --build-arg UV_VERSION=latest \
  -t gemini-fastapi:latest .

# 在 Dockerfile 中接收参数
# ARG PYTHON_VERSION=3.12
# FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim
```

### 4. 推送到私有镜像仓库

```bash
# 登录到私有仓库
docker login your-registry.com

# 打标签
docker tag gemini-fastapi:latest your-registry.com/gemini-fastapi:latest

# 推送
docker push your-registry.com/gemini-fastapi:latest

# 从私有仓库拉取
docker pull your-registry.com/gemini-fastapi:latest
```

### 5. 监控和日志

```bash
# 实时查看资源使用
docker stats gemini-fastapi

# 导出日志到文件
docker logs gemini-fastapi > /var/log/gemini-fastapi.log 2>&1

# 限制日志大小（docker-compose.yml）
services:
  gemini-fastapi:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### 6. 健康检查和自动重启

```bash
# 在 docker-compose.yml 中配置
services:
  gemini-fastapi:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    restart: unless-stopped
```

### 7. 资源限制

```bash
# 限制 CPU 和内存
docker run -d \
  --name gemini-fastapi \
  --cpus="2.0" \
  --memory="2g" \
  --memory-swap="2g" \
  -p 8092:8000 \
  gemini-fastapi:latest

# 在 docker-compose.yml 中配置
services:
  gemini-fastapi:
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 2G
        reservations:
          cpus: '1.0'
          memory: 1G
```

---

## 完整的更新和部署流程

### 场景 1：代码有更新，需要重新部署

```bash
cd /root/tread/Gemini-FastAPI

# 1. 拉取最新代码
git pull origin fix/reference-image-generation-bug

# 2. 停止旧容器
docker compose down
# 或者
docker stop gemini-fastapi && docker rm gemini-fastapi

# 3. 删除旧镜像（可选但推荐）
docker rmi gemini-fastapi:latest

# 4. 无缓存重新构建镜像
docker build --no-cache -t gemini-fastapi:latest .

# 5. 启动新容器
docker compose up -d
# 或者
docker run -d --name gemini-fastapi -p 8092:8000 gemini-fastapi:latest

# 6. 查看日志确认启动成功
docker logs -f gemini-fastapi
```

### 场景 2：官方仓库有更新，需要合并

```bash
cd /root/tread/Gemini-FastAPI

# 1. 获取官方更新
git fetch upstream

# 2. 合并到你的分支
git checkout fix/reference-image-generation-bug
git merge upstream/main
# 或者
git rebase upstream/main

# 3. 如果有冲突，解决冲突后继续
git add <冲突文件>
git rebase --continue

# 4. 推送到你的 fork
git push origin fix/reference-image-generation-bug --force-with-lease

# 5. 重新构建和部署（同场景 1 的步骤 2-6）
docker compose down
docker build --no-cache -t gemini-fastapi:latest .
docker compose up -d
```

### 场景 3：只修改配置，不需要重新构建

```bash
# 1. 修改配置文件
vim config/settings.yaml

# 2. 重启容器
docker compose restart
# 或者
docker restart gemini-fastapi
```

---

## 快速命令参考

```bash
# 构建
docker build --no-cache -t gemini-fastapi:latest .

# 运行
docker run -d --name gemini-fastapi -p 8092:8000 gemini-fastapi:latest

# 停止
docker stop gemini-fastapi

# 删除容器
docker rm gemini-fastapi

# 删除镜像
docker rmi gemini-fastapi:latest

# 查看日志
docker logs -f gemini-fastapi

# 进入容器
docker exec -it gemini-fastapi /bin/bash

# 查看容器状态
docker ps -a | grep gemini-fastapi

# 查看资源使用
docker stats gemini-fastapi

# 使用 compose
docker compose up -d              # 启动
docker compose down               # 停止并删除
docker compose restart            # 重启
docker compose logs -f            # 查看日志
docker compose ps                 # 查看状态
```

---

## 安全建议

1. **不要在镜像中包含敏感信息**
   - 使用环境变量或 secrets 管理敏感配置
   - 使用 `.dockerignore` 排除敏感文件

2. **定期更新基础镜像**
   ```bash
   docker pull ghcr.io/astral-sh/uv:python3.12-bookworm-slim
   docker build --no-cache -t gemini-fastapi:latest .
   ```

3. **使用非 root 用户运行容器**
   ```dockerfile
   # 在 Dockerfile 中添加
   RUN useradd -m -u 1000 appuser
   USER appuser
   ```

4. **限制容器权限**
   ```bash
   docker run -d \
     --name gemini-fastapi \
     --read-only \
     --cap-drop=ALL \
     --security-opt=no-new-privileges:true \
     -p 8092:8000 \
     gemini-fastapi:latest
   ```

---

## 故障排查检查清单

遇到问题时，按照以下顺序检查：

- [ ] Docker 服务是否运行：`docker ps`
- [ ] 端口是否被占用：`lsof -i :8092`
- [ ] 镜像是否存在：`docker images | grep gemini-fastapi`
- [ ] 容器是否启动：`docker ps -a | grep gemini-fastapi`
- [ ] 查看容器日志：`docker logs gemini-fastapi`
- [ ] 查看容器退出代码：`docker inspect gemini-fastapi --format='{{.State.ExitCode}}'`
- [ ] 磁盘空间是否充足：`df -h`
- [ ] Docker 磁盘使用：`docker system df`
- [ ] 网络是否可达：`docker network ls`
- [ ] 配置文件是否正确：`cat config/settings.yaml`

---

## 参考资源

- **官方仓库**: https://github.com/Nativu5/Gemini-FastAPI
- **你的 Fork**: https://github.com/awaragml00029-debug/Gemini-FastAPI
- **Docker 文档**: https://docs.docker.com/
- **Bug 修复文档**: [big_bug_ref_img.md](./big_bug_ref_img.md)

---

**最后更新时间**: 2025-12-16
**维护者**: awaragml00029-debug
