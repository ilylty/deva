# deva

自建双端算力调度系统：**本机 CLI**（`deva`）经网络连接到 **远程服务端**（`server.py`），把脚本/命令下发到远程机器的算力（CPU/GPU）执行。

- 不走 SSH、纯 CLI、无任务队列、无 Jupyter
- 客户端零第三方依赖（仅 Python 标准库 urllib）
- 服务端基于 FastAPI + uvicorn，支持脚本执行、流式日志、venv 环境管理、文件同步

```
本机 (Termux / 任意有 Python 的机器)
   │  deva CLI
   │  HTTP + Bearer token
   ▼
远程机器 (FastAPI + uvicorn)
   └─ server.py  ── 执行脚本 / 命令 / 管理 venv 环境
```

## 目录结构

```
deva/
├── deva-server/          # 服务端
│   ├── server.py         # FastAPI 应用
│   └── config.json.example
├── deva-client/          # 客户端
│   ├── setup.py
│   └── deva_client/      # CLI 实现 (api.py / cli.py / config.py / sync.py)
├── DEPLOY.md             # 部署文档 (含 Colab 一键部署)
└── README.md
```

## 快速开始

### 服务端

```bash
pip install fastapi uvicorn pydantic python-multipart
# Ubuntu 24.04 等 PEP 668 受管环境:
# pip install --break-system-packages fastapi uvicorn pydantic python-multipart

cp deva-server/config.json.example deva-server/config.json
# 编辑 config.json, 设置自己的 token / 端口 / 路径
nohup python3 deva-server/server.py > deva-server/run.out 2>&1 &
```

### 客户端

```bash
pip install -e deva-client
deva config set url http://<host>:<port>
deva config set token <token>
deva status
```

详细部署、环境管理（venv）、命令速查、故障排查见 **[DEPLOY.md](DEPLOY.md)**。
Colab 一键部署代码块也在其中。

## 能力一览

- 脚本执行：`run`（异步）/ `exec`（流式 shell）
- 任务管理：`ps` / `logs -f` / `kill` / 崩溃恢复
- 文件：单向 `sync` / `ls` / `cat` / `fetch` / `rm`
- 环境（venv）：`env ls|create|run` / `pip install|uninstall|show|freeze`
- GPU：`gpu` 状态查询

## 安全提示

token 是唯一鉴权手段，请使用强随机值；公网暴露时建议套 Cloudflared Tunnel 或反向代理 + TLS，
切勿直接把 `0.0.0.0` + 弱 token 暴露到公网。
