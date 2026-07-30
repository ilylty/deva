# deva 部署文档

`deva` 是一套自建双端系统：**本机 CLI**（`deva`）经网络连接到 **设备A 上的服务端**（`server.py`），把脚本/命令下发到设备A 的算力（CPU/GPU）执行。不走 SSH、纯 CLI、无队列、无 Jupyter。

```
本机 (Termux / 任意有 Python 的机器)
   │  deva CLI  (urllib, 零依赖)
   │  HTTP + Bearer token
   ▼
设备A / 服务器  (FastAPI + uvicorn)
   └─ server.py  ── 执行脚本 / 命令 / 管理 venv 环境
```

---

## 1. 服务端部署

### 1.1 准备

- Python 3.10+（设备A/服务器）
- 一个监听端口（默认 `27404`）
- 一个 Bearer token（自行设定，本机与服务器需一致）

### 1.2 安装依赖

```bash
# 普通环境
python3 -m pip install fastapi uvicorn pydantic python-multipart

# Ubuntu 24.04 等 PEP 668 受管环境 (系统 pip 被锁) 需加 --break-system-packages
python3 -m pip install --break-system-packages fastapi uvicorn pydantic python-multipart

# 或建 venv 后用 venv 的 pip 安装 (推荐，不污染系统)
python3 -m venv /opt/deva-venv
/opt/deva-venv/bin/pip install fastapi uvicorn pydantic python-multipart
# 此时启动命令改为 /opt/deva-venv/bin/python /opt/deva/server.py
```

### 1.3 放置服务端文件

把 `deva-server/` 目录（含 `server.py` 与 `config.json`）放到服务器，例如 `/opt/deva/`：

```
/opt/deva/
├── server.py
├── config.json
├── workspaces/      # 各 workspace 的工作目录 (自动创建)
├── logs/            # 任务日志 (自动创建)
├── envs/            # venv 环境 (自动创建, 不被 sync 删除)
├── manifests/       # 同步清单 (自动创建)
└── deva-server.log  # 轮转日志
```

### 1.4 配置 `config.json`

```json
{
  "token": "d3vA-t0k3n-27404-2026",
  "host": "0.0.0.0",
  "port": 27404,
  "work_root": "/opt/deva/workspaces",
  "logs_dir": "/opt/deva/logs",
  "db_path": "/opt/deva/tasks.db",
  "manifests_dir": "/opt/deva/manifests"
}
```

| 字段 | 说明 |
|------|------|
| `token` | 鉴权 token，本机 `~/.deva/config.json` 必须一致 |
| `host` | 监听地址。`0.0.0.0` 允许外部连接；仅本机用 `127.0.0.1` |
| `port` | 监听端口 |
| `work_root` | workspace 根目录 |
| `logs_dir` | 任务 stdout 日志目录 |
| `db_path` | 任务状态 SQLite 路径 |
| `manifests_dir` | 同步清单目录 |

> 注意：`work_root` / `logs_dir` / `db_path` / `manifests_dir` 使用 `~` 会被展开为运行用户家目录；建议写绝对路径，避免与文件实际位置不一致。

### 1.5 启动（后台常驻）

```bash
# 直接启动
nohup python3 /opt/deva/server.py > /opt/deva/run.out 2>&1 &

# 若用 venv 安装
nohup /opt/deva-venv/bin/python /opt/deva/server.py > /opt/deva/run.out 2>&1 &

# 用 systemd (推荐生产) — 见附录 A
```

启动后确认监听：

```bash
ss -ltnp | grep 27404
# LISTEN 0 2048 0.0.0.0:27404 ...
```

服务端日志写到 `run.out` 与 `deva-server.log`（5MB 轮转 ×3），排查问题时优先看这两个。

### 1.6 重启 / 停止

```bash
# 按 PID 精确停止 (不要用 pkill -f 'server.py', 会误杀运维 SSH 进程)
for p in $(pgrep -f 'server\.py'); do kill "$p"; done
sleep 1
nohup python3 /opt/deva/server.py > /opt/deva/run.out 2>&1 &
```

> 崩溃恢复：服务端启动时把 `running` 状态的历史任务标记为 `interrupted`，避免僵尸状态。

### 1.7 Colab 快速部署（一键代码块）

在 Google Colab 新建 notebook，**整段复制下面的代码块运行**，即可拉起 deva 服务端并通过 `cloudflared` 暴露公网地址。端口固定 `18080`，token 直接写在代码里（`d3vA-c0l4b-18080`）。

```python
# ============================================================
# deva 服务端 · Colab 一键部署
# 端口: 18080    Token: d3vA-c0l4b-18080
# 运行后往下看「Your deva tunnel URL」那行, 复制到本机 config 的 url
# ============================================================
import os, json, subprocess, textwrap, getpass

DEVA_PORT   = 18080
DEVA_TOKEN  = "d3vA-c0l4b-18080"          # 客户端连接用的 token
DEVA_DIR    = "/content/deva"

# 1) 装依赖
subprocess.run("pip install -q fastapi uvicorn pydantic python-multipart", shell=True, check=True)

# 2) 下载 server.py (每次重跑都拉 GitHub main 最新版, 修复会自动生效)
import shutil
shutil.rmtree(f"{DEVA_DIR}/envs", ignore_errors=True)   # 清掉可能损坏的旧 venv
os.makedirs(DEVA_DIR, exist_ok=True)
subprocess.run(
    "curl -L -o server.py https://raw.githubusercontent.com/ilylty/deva/main/deva-server/server.py",
    cwd=DEVA_DIR, shell=True, check=True,
)

# 3) 写 config.json (监听 0.0.0.0:18080)
config = {
    "token": DEVA_TOKEN,
    "host": "0.0.0.0",
    "port": DEVA_PORT,
    "work_root": f"{DEVA_DIR}/workspaces",
    "logs_dir": f"{DEVA_DIR}/logs",
    "db_path": f"{DEVA_DIR}/tasks.db",
    "manifests_dir": f"{DEVA_DIR}/manifests",
}
open(f"{DEVA_DIR}/config.json", "w").write(json.dumps(config, indent=2))

# 4) 后台启动服务端
subprocess.Popen(
    f"python3 {DEVA_DIR}/server.py > {DEVA_DIR}/run.out 2>&1",
    shell=True, start_new_session=True,
)
print("deva server starting on port", DEVA_PORT, "...")

# 5) 用 cloudflared 暴露公网隧道 (Colab 免费版无稳定公网 IP)
subprocess.run("curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /content/cloudflared && chmod +x /content/cloudflared", shell=True, check=True)
tunnel = subprocess.Popen(
    f"/content/cloudflared tunnel --url http://localhost:{DEVA_PORT}",
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    start_new_session=True,
)
import time; time.sleep(8)
# 抓隧道地址
url = ""
for line in tunnel.stdout:
    if "trycloudflare.com" in line:
        url = line.split("https://")[-1].split()[0].strip().rstrip("'\"")
        url = "https://" + url
        break
print("\n========================================")
if url:
    print("Your deva tunnel URL:", url)
    print(f"Token: {DEVA_TOKEN}")
    print("\n本机客户端配置:")
    print(f"  deva config set url {url}")
    print(f"  deva config set token {DEVA_TOKEN}")
else:
    print("隧道启动较慢, 请稍候在上方日志找 trycloudflare.com 开头的地址")
print("========================================")
```

> 说明：
> - Colab 运行时是临时的，重启后需重跑代码块；token 与端口已固定，本机配置一次即可。
> - 若不想用 cloudflared，可把 `url` 直接设为 Colab 分配的 `https://<id>.gradio.live` 之类，原理相同。
> - 第 2 步的 `raw` 地址需替换成你实际 repo 的 `server.py` 路径（见下方「客户端部署」前先把代码推到 GitHub）。
> - **安全**：此 token 为示例值，生产请改成强随机串；Colab 公网暴露等于把机器执行权限交给持有 token 的人。

---

## 2. 客户端部署（本机）

### 2.1 安装

```bash
cd deva-client
pip install -e .        # 装成 `deva` 命令 (editable)
deva status             # 验证安装
```

客户端零第三方依赖，仅用 Python 标准库（urllib）。

### 2.2 配置

首次运行会生成 `~/.deva/config.json`，或直接编辑：

```json
{
  "url": "http://8.148.29.241:27404",
  "token": "d3vA-t0k3n-27404-2026",
  "workspace": "default",
  "local_root": "."
}
```

| 字段 | 说明 |
|------|------|
| `url` | 服务端地址（含端口）。设备A 经 Cloudflare Tunnel 暴露时填隧道地址 |
| `token` | 必须与服务端 `config.json` 的 `token` 一致 |
| `workspace` | 默认 workspace 名 |
| `local_root` | `deva sync` 默认本地根目录 |

命令行改配置：

```bash
deva config set url http://<host>:<port>
deva config set token <token>
```

---

## 3. 命令速查

### 连通 / 状态
| 命令 | 说明 |
|------|------|
| `deva status` | 连通性 + 服务端版本 |
| `deva gpu` | GPU 状态（无 GPU 时提示） |

### 执行
| 命令 | 说明 |
|------|------|
| `deva run <script> [-- args]` | 异步提交脚本（用 `python` 解释器） |
| `deva exec "<shell cmd>"` | 前台同步执行 shell 命令（流式） |
| `deva run --env <name> <script>` | 在指定 venv 环境中运行脚本 |
| `deva run --gpu 0,1 <script>` | 限定 CUDA_VISIBLE_DEVICES |

### 任务 / 日志
| 命令 | 说明 |
|------|------|
| `deva ps [-a]` | 列出运行中的（或全部的）任务 |
| `deva logs <task_id> [-f] [-n N]` | 查日志，`-f` 流式跟随 |
| `deva kill <task_id>` | 取消任务（kill 整个进程组） |

### 文件
| 命令 | 说明 |
|------|------|
| `deva sync [--root DIR] [--ws NAME]` | 本地 → 服务端单向镜像同步 |
| `deva ls [path]` | 浏览服务端 workspace |
| `deva cat <path>` | 读服务端文件 |
| `deva fetch <remote> [local]` | 从服务端拉文件到本地 |
| `deva rm <path> [--yes]` | 删服务端文件 |

### 环境管理（venv）
| 命令 | 说明 |
|------|------|
| `deva env ls` | 列出所有 venv 环境 |
| `deva env create <name> [--python PY]` | 创建 venv |
| `deva env run <name> "<cmd>"` | 在环境中执行 shell 命令 |
| `deva pip <name> install <pkgs> [--extra "-r req.txt"]` | 装包 |
| `deva pip <name> uninstall <pkgs>` | 卸包 |
| `deva pip <name> show <pkgs>` | 查看包信息 |
| `deva pip <name> freeze` | 导出已装包清单 |

---

## 4. 环境管理（venv）使用流程

deva 用 **venv** 做环境隔离（兼容无 conda 的机器）。环境统一放在服务端 `work_root` 同级的 `envs/` 目录，**不会被 `deva sync` 误删**。

```bash
# 1. 建环境
deva env create torch213

# 2. 装依赖
deva pip torch213 install torch torchvision
#   或按 requirements
deva pip torch213 install - --extra "-r requirements.txt"   # 见下方说明

# 3. 在该环境跑脚本
deva sync                      # 先把脚本推上去
deva run --env torch213 train.py -- --epochs 10

# 4. 在环境里直接跑命令
deva env run torch213 "python -c 'import torch; print(torch.__version__)'"

# 5. 查看已装包
deva pip torch213 freeze
```

> `pip install` 的 `--extra` 用于传额外 pip 参数（如 `-r requirements.txt`、`-i <mirror>`）。
> 注意 `packages` 与 `--extra` 二选一或组合：`deva pip <name> install pkg1 pkg2 --extra "-i https://pypi.tuna.tsinghua.edu.cn/simple"`。

`run --env` / `exec --env` / `env run` 都会把该 venv 的 `bin/` 加到 `PATH` 前面，
使 `python` / `pip` 等指向 venv 内解释器。

---

## 5. 典型工作流（训练脚本）

```bash
# 本机
mkdir -p myproj && cd myproj
# 写 train.py ...

# 1. 准备环境（首次）
deva env create train
deva pip train install torch --extra "-i https://pypi.tuna.tsinghua.edu.cn/simple"

# 2. 推代码
deva sync --root .

# 3. 跑（GPU 机用 --gpu）
deva run --env train --gpu 0 train.py -- --data /data/xxx

# 4. 看日志
deva logs <task_id> -f

# 5. 拉结果
deva fetch outputs/model.pt
```

---

## 6. 故障排查

| 现象 | 可能原因 / 处理 |
|------|----------------|
| `HTTP 500: start failed: [Errno 2] No such file or directory: 'python'` | 服务器没有 `python` 命令（只有 `python3`）。最新版已改用 `sys.executable`，升级 `server.py` 即可 |
| `连接失败: timed out` | 检查服务端是否监听、端口/防火墙、token 是否匹配；偶发因 uvicorn 单 worker 繁忙，重试即可 |
| `unrecognized arguments` | 子命令参数写错，看 `deva <cmd> --help`（`fetch` 用位置参数，`rm` 需 `--yes`） |
| `非交互模式需 --yes 确认` | `deva rm` 加 `--yes` |
| `pip install` 被 PEP 668 拒绝 | 用 `--break-system-packages` 或 venv（见 1.2） |
| `env` 不存在 | 先 `deva env create`；`run --env` 指定的环境必须在服务端已建 |
| 日志不更新 / 进程卡死 | `deva kill <task_id>`；必要时服务端按 PID 重启（见 1.6） |

日志位置：
- 服务端：`/opt/deva/run.out`（启动输出）+ `/opt/deva/deva-server.log`（运行日志，轮转）
- 客户端：加 `-v` 看 DEBUG；加 `--log-file FILE` 落盘

---

## 附录 A：systemd 服务单元（生产推荐）

`/etc/systemd/system/deva.service`：

```ini
[Unit]
Description=deva server
After=network.target

[Service]
User=root
WorkingDirectory=/opt/deva
ExecStart=/opt/deva-venv/bin/python /opt/deva/server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now deva
journalctl -u deva -f
```

## 附录 B：安全提示

- token 是唯一的鉴权手段，**务必使用强随机值**，不要使用示例中的 token。
- `host: 0.0.0.0` + 公网 IP 会把服务端暴露到公网。生产建议：
  - 仅监听 `127.0.0.1`，前面套 Cloudflare Tunnel / 反向代理 + TLS；
  - 或加防火墙限制来源 IP。
- `deva exec` / `env run` 在服务端执行任意 shell 命令，等同于拥有该机器执行权限，token 泄露即失控。
