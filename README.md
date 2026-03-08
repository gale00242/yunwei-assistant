# 运维助手 - 服务监控与管理平台

轻量级服务器监控工具，支持 SSH 远程管理、Docker 容器管理、批量命令执行和 Telegram 告警通知。

## 功能特点

- 🖥️ **服务器监控** - CPU、内存、磁盘使用率监控
- 🐳 **Docker 管理** - 查看容器状态、执行命令、查看日志
- 📡 **批量命令** - 一键在多台服务器执行相同命令
- 🚨 **告警通知** - Telegram 消息推送
- 📊 **Web 界面** - Tailwind CSS 美观的响应式界面

## 快速开始

### 1. 构建镜像

```bash
docker build -t yunwei-assistant .
```

### 2. 准备数据目录

```bash
mkdir -p data
```

### 3. 运行容器

```bash
docker run -d \
  --name yunwei-assistant \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v ~/.ssh:/root/.ssh:ro \
  yunwei-assistant
```

或使用 docker-compose:

```bash
docker-compose up -d
```

### 4. 访问界面

打开浏览器访问 http://localhost:8000

## 配置

### Telegram 通知

1. 在 Telegram 中搜索 @BotFather
2. 发送 /newbot 创建 Bot
3. 复制 Bot Token
4. 向 Bot 发送消息后访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 获取 Chat ID
5. 在设置页面填入 Token 和 Chat ID

### SSH 连接

- **密钥认证**: 默认使用 `~/.ssh/id_rsa`，或指定自定义密钥路径
- **密码认证**: 在添加服务器时输入密码

## 目录结构

```
yunwei-assistant/
├── app/
│   ├── main.py           # 入口
│   ├── database.py       # 数据库操作
│   ├── ssh_client.py     # SSH 客户端
│   ├── collector.py      # 指标采集
│   ├── alerter.py        # 告警逻辑
│   ├── notifier.py       # Telegram 通知
│   └── web/
│       ├── app.py        # FastAPI 应用
│       └── templates/    # HTML 模板
├── data/
│   ├── config.yaml       # 配置文件
│   └── monitor.db        # SQLite 数据库
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 许可证

MIT