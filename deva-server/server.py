#!/usr/bin/env python3
"""deva-server: 设备A上的 GPU 算力调度服务端。

通过 Cloudflare Tunnel 暴露给本机 deva CLI，提供脚本执行/任务管理/
日志/GPU 状态能力。不走 SSH。

运行: python3 server.py  (或 uvicorn server:app)
"""
import io
import json
import logging
import logging.handlers
import os
import sys
import tarfile
import tempfile
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

# ---------- 日志 ----------
LOG_FMT = logging.Formatter(
    "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("deva")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    sh = logging.StreamHandler()
    sh.setFormatter(LOG_FMT)
    logger.addHandler(sh)
    # 轮转文件日志, 5MB x 3
    log_file = Path(__file__).parent / "deva-server.log"
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(LOG_FMT)
        logger.addHandler(fh)
    except OSError:
        logger.warning("无法写入日志文件 %s, 仅输出到 stderr", log_file)
    return logger


log = setup_logging()

# ---------- 配置 ----------
CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    for k in ("work_root", "logs_dir", "db_path"):
        if k in cfg and isinstance(cfg[k], str):
            cfg[k] = os.path.expanduser(cfg[k])
    return cfg


CFG = load_config()
HOST = CFG.get("host", "127.0.0.1")
PORT = int(CFG.get("port", 8765))
TOKEN = CFG["token"]
WORK_ROOT = Path(CFG["work_root"])
LOGS_DIR = Path(CFG["logs_dir"])
DB_PATH = Path(CFG["db_path"])
WORK_ROOT.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
MANIFESTS_DIR = Path(CFG.get("manifests_dir", str(Path(__file__).parent / "manifests")))
MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)

VERSION = "0.1.0"

app = FastAPI(title="deva-server", version=VERSION)


# ---------- 请求日志中间件 ----------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    # 跳过 /health 的 INFO 噪音, 降为 DEBUG
    path = request.url.path
    is_health = path == "/health"
    try:
        response = await call_next(request)
    except Exception:
        dur = (time.time() - start) * 1000
        log.exception("%s %s -> ERROR %.0fms", request.method, path, dur)
        raise
    dur = (time.time() - start) * 1000
    msg = f"{request.method} {path} -> {response.status_code} {dur:.0f}ms"
    if response.status_code >= 400:
        log.warning(msg)
    elif is_health:
        log.debug(msg)
    else:
        log.info(msg)
    return response


# ---------- DB ----------
SCHEMA = """CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    script TEXT,
    args TEXT,
    status TEXT,
    pid INTEGER,
    env TEXT,
    gpu TEXT,
    cwd TEXT,
    workspace TEXT,
    started REAL,
    finished REAL,
    returncode INTEGER
)"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(SCHEMA)
        # 崩溃恢复: 服务端重启后丢失进程句柄, running 的任务标记为 interrupted
        c.execute("UPDATE tasks SET status='interrupted' WHERE status='running'")
        recovered = c.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status='interrupted'"
        ).fetchone()
        if recovered and recovered["n"]:
            log.warning("崩溃恢复: %d 个 running 任务标记为 interrupted", recovered["n"])


init_db()

# 内存中的进程句柄 (服务端重启后丢失, 由上面的恢复逻辑兜底)
PROCS: dict[str, subprocess.Popen] = {}
PROCS_LOCK = threading.Lock()


# ---------- 工具 ----------
def ok(data=None):
    return {"ok": True, "data": data}


def auth(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        log.warning("auth failed: missing bearer token")
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.split(" ", 1)[1] != TOKEN:
        log.warning("auth failed: bad token")
        raise HTTPException(status_code=401, detail="bad token")


def ws_root(workspace: str) -> Path:
    """返回 workspace 根目录, 防路径逃逸。"""
    ws = Path(workspace)
    if ws.is_absolute() or ".." in ws.parts:
        raise HTTPException(status_code=400, detail="bad workspace name")
    root = WORK_ROOT / ws
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_rel_path(path: str) -> Path:
    """校验相对路径安全(非绝对/无..), 返回 Path。"""
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(status_code=400, detail=f"bad path: {path}")
    return p


def manifest_path(workspace: str) -> Path:
    return MANIFESTS_DIR / f"{workspace}.json"


def load_manifest(workspace: str) -> dict:
    f = manifest_path(workspace)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            log.warning("manifest 损坏, 重置: %s", f)
    return {}


def save_manifest(workspace: str, m: dict):
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path(workspace).write_text(json.dumps(m, indent=2))


def reap_task(task_id: str, proc: subprocess.Popen, logf):
    """后台线程: 等待进程结束, 更新状态。"""
    proc.wait()
    status = "done" if proc.returncode == 0 else "failed"
    finished = time.time()
    with db() as c:
        row = c.execute("SELECT started FROM tasks WHERE id=?", (task_id,)).fetchone()
        started = row["started"] if row else finished
        # returncode/finished 总要记录; 但 status 只在仍 running 时更新,
        # 避免覆盖 kill_task 已设置的 'killed' 状态
        c.execute(
            "UPDATE tasks SET finished=?, returncode=? WHERE id=?",
            (finished, proc.returncode, task_id),
        )
        c.execute(
            "UPDATE tasks SET status=? WHERE id=? AND status='running'",
            (status, task_id),
        )
    dur = finished - started if started else 0.0
    log.info(
        "task=%s finished status=%s rc=%s dur=%.1fs",
        task_id, status, proc.returncode, dur,
    )
    try:
        logf.close()
    except Exception:
        pass
    with PROCS_LOCK:
        PROCS.pop(task_id, None)


# ---------- 环境管理 (venv) ----------
# 环境存在 work_root 同级 envs/ 下, 避免被 sync 误删
ENVS_DIR = WORK_ROOT.parent / "envs"
ENVS_DIR.mkdir(parents=True, exist_ok=True)


def env_dir(name: str) -> Path:
    # 防路径注入: 仅允许简单名称
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="非法环境名")
    return ENVS_DIR / name


def env_python(name: str) -> Path:
    d = env_dir(name)
    cand = d / ("Scripts" if os.name == "nt" else "bin") / "python"
    if not cand.exists():
        raise HTTPException(status_code=404, detail=f"环境 {name} 不存在或未初始化")
    return cand


def list_envs() -> list:
    out = []
    if not ENVS_DIR.exists():
        return out
    for d in sorted(ENVS_DIR.iterdir()):
        if not d.is_dir():
            continue
        py = d / ("Scripts" if os.name == "nt" else "bin") / "python"
        info = {"name": d.name, "exists": py.exists()}
        if py.exists():
            try:
                ver = subprocess.check_output(
                    [str(py), "--version"], stderr=subprocess.STDOUT, timeout=10
                ).decode().strip()
                info["python"] = ver
            except Exception as e:
                info["python"] = f"(error: {e})"
            # 粗略统计已装包数量
            try:
                out_txt = subprocess.check_output(
                    [str(py), "-m", "pip", "freeze"], stderr=subprocess.DEVNULL, timeout=30
                ).decode()
                info["packages"] = len([l for l in out_txt.splitlines() if l.strip()])
            except Exception:
                info["packages"] = None
        out.append(info)
    return out


# ---------- 请求模型 ----------
class RunReq(BaseModel):
    script: str
    args: list[str] = []
    env: str | None = None       # conda 环境名
    gpu: str | None = None       # CUDA_VISIBLE_DEVICES
    cwd: str = "."               # 相对 workspace 根
    workspace: str = "default"


# ---------- 路由 ----------
@app.get("/health")
def health():
    return ok({"ok": True, "version": VERSION})


@app.post("/run")
def run(req: RunReq, _: None = Depends(auth)):
    task_id = uuid.uuid4().hex[:8]
    root = ws_root(req.workspace).resolve()
    cwd = (root / req.cwd).resolve()
    # 防 cwd 逃逸出 workspace
    try:
        cwd.relative_to(root)
    except ValueError:
        log.warning("run task=%s rejected: cwd escapes workspace (%s)", task_id, req.cwd)
        raise HTTPException(status_code=400, detail="cwd escapes workspace")

    log_path = LOGS_DIR / f"{task_id}.log"
    logf = open(log_path, "w", buffering=1)

    cmd: list[str] = []
    if req.env:
        # venv 模式: 直接用该环境的 python 解释器 (兼容无 conda 场景)
        cmd += [str(env_python(req.env)), req.script] + list(req.args)
    else:
        cmd += [sys.executable, req.script] + list(req.args)

    env = os.environ.copy()
    if req.gpu is not None:
        env["CUDA_VISIBLE_DEVICES"] = req.gpu

    log.info(
        "submit task=%s script=%s env=%s gpu=%s ws=%s cwd=%s",
        task_id, req.script, req.env or "-", req.gpu or "-", req.workspace, req.cwd,
    )
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,  # 独立进程组, 便于 kill 整组
        )
    except Exception as e:
        log.exception("submit task=%s failed to start: %s", task_id, e)
        logf.write(f"[deva-server] failed to start: {e}\n")
        logf.close()
        raise HTTPException(status_code=500, detail=f"start failed: {e}")

    with db() as c:
        c.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                req.script,
                json.dumps(req.args),
                "running",
                proc.pid,
                req.env,
                req.gpu,
                req.cwd,
                req.workspace,
                time.time(),
                None,
                None,
            ),
        )

    with PROCS_LOCK:
        PROCS[task_id] = proc
    threading.Thread(target=reap_task, args=(task_id, proc, logf), daemon=True).start()
    log.info("start task=%s pid=%s cmd=%s", task_id, proc.pid, " ".join(cmd))
    return ok({"task_id": task_id, "pid": proc.pid})


@app.post("/exec")
def exec_run(req: RunReq, _: None = Depends(auth)):
    """前台同步执行, SSE 流式回传 stdout, 结束返回 returncode。"""
    task_id = uuid.uuid4().hex[:8]
    root = ws_root(req.workspace).resolve()
    cwd = (root / req.cwd).resolve()
    try:
        cwd.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="cwd escapes workspace")
    # exec: 把 req.script 当作 shell 命令执行 (bash -lc)
    cmd: list[str] = []
    env = os.environ.copy()
    if req.env:
        # venv 模式: 把该环境的 bin 加到 PATH 前面, 让 python/pip 等指向 venv
        bin_dir = str(env_dir(req.env) / ("Scripts" if os.name == "nt" else "bin"))
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    shell_cmd = req.script + (" " + " ".join(req.args) if req.args else "")
    cmd += ["bash", "-lc", shell_cmd]
    if req.gpu is not None:
        env["CUDA_VISIBLE_DEVICES"] = req.gpu
    log.info("exec task=%s script=%s cmd=%s", task_id, req.script, " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except Exception as e:
        log.exception("exec task=%s failed to start: %s", task_id, e)
        raise HTTPException(status_code=500, detail=f"start failed: {e}")
    with db() as c:
        c.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, req.script, json.dumps(req.args), "running", proc.pid,
             req.env, req.gpu, req.cwd, req.workspace, time.time(), None, None),
        )
    with PROCS_LOCK:
        PROCS[task_id] = proc

    def gen():
        try:
            for chunk in iter(lambda: proc.stdout.read(4096), b""):
                yield f"data: {json.dumps({'type': 'stdout', 'data': chunk.decode(errors='replace')})}\n\n"
            proc.wait()
            status = "done" if proc.returncode == 0 else "failed"
            with db() as c:
                c.execute(
                    "UPDATE tasks SET status=?, finished=?, returncode=? WHERE id=?",
                    (status, time.time(), proc.returncode, task_id),
                )
            log.info("exec task=%s finished status=%s rc=%s", task_id, status, proc.returncode)
            yield f"data: {json.dumps({'type': 'end', 'returncode': proc.returncode, 'status': status})}\n\n"
        except GeneratorExit:
            # 客户端断开, 终止进程组
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                pass
            with db() as c:
                c.execute(
                    "UPDATE tasks SET status='killed', finished=? WHERE id=? AND status='running'",
                    (time.time(), task_id),
                )
            log.info("exec task=%s client disconnected, killed", task_id)
        finally:
            with PROCS_LOCK:
                PROCS.pop(task_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/tasks")
def list_tasks(all: bool = False, _: None = Depends(auth)):
    with db() as c:
        if all:
            rows = c.execute("SELECT * FROM tasks ORDER BY started DESC").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM tasks WHERE status='running' ORDER BY started DESC"
            ).fetchall()
    return ok([dict(r) for r in rows])


@app.get("/tasks/{task_id}")
def get_task(task_id: str, _: None = Depends(auth)):
    with db() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="no such task")
    return ok(dict(r))


@app.get("/tasks/{task_id}/logs")
def get_logs(task_id: str, tail: int = 0, follow: bool = False, _: None = Depends(auth)):
    f = LOGS_DIR / f"{task_id}.log"
    if not f.exists():
        raise HTTPException(status_code=404, detail="no log file")
    if not follow:
        text = f.read_text(errors="replace")
        if tail and tail > 0:
            text = "\n".join(text.splitlines()[-tail:])
        return ok({"log": text, "tail": tail})

    # SSE 流式 tail
    def gen():
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            if tail and tail > 0:
                lines = fh.readlines()
                for line in lines[-tail:]:
                    yield f"data: {json.dumps({'type': 'log', 'data': line})}\n\n"
            else:
                fh.seek(0, 2)  # 跳到末尾, 只看新输出
            while True:
                line = fh.readline()
                if line:
                    yield f"data: {json.dumps({'type': 'log', 'data': line})}\n\n"
                    continue
                with db() as c:
                    row = c.execute(
                        "SELECT status FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()
                status = row["status"] if row else None
                if status and status not in ("running", "pending"):
                    yield f"data: {json.dumps({'type': 'end', 'status': status})}\n\n"
                    break
                time.sleep(0.3)

    log.info("logs follow task=%s tail=%s", task_id, tail)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/tasks/{task_id}/kill")
def kill_task(task_id: str, _: None = Depends(auth)):
    # 先标记 killed, 防止与 reap_task 竞态时被覆盖成 failed
    with db() as c:
        c.execute(
            "UPDATE tasks SET status='killed', finished=? WHERE id=? AND status='running'",
            (time.time(), task_id),
        )
    killed = False
    with PROCS_LOCK:
        proc = PROCS.get(task_id)
    if proc and proc.poll() is None:
        # kill 整个进程组 (start_new_session 创建的)
        try:
            os.killpg(os.getpgid(proc.pid), 15)
            time.sleep(2)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), 9)
        except ProcessLookupError:
            pass
        killed = True
    log.info("kill task=%s killed=%s", task_id, killed)
    return ok({"task_id": task_id, "killed": killed})


# ---------- 环境管理端点 ----------
def _stream_proc(task_id: str, proc: subprocess.Popen, reason: str = "exec"):
    """通用 SSE 流式: 把子进程 stdout 实时回传, 结束带 returncode。"""
    with PROCS_LOCK:
        PROCS[task_id] = proc

    def gen():
        try:
            for chunk in iter(lambda: proc.stdout.read(4096), b""):
                yield f"data: {json.dumps({'type': 'stdout', 'data': chunk.decode(errors='replace')})}\n\n"
            proc.wait()
            status = "done" if proc.returncode == 0 else "failed"
            log.info("%s task=%s finished status=%s rc=%s", reason, task_id, status, proc.returncode)
            yield f"data: {json.dumps({'type': 'end', 'returncode': proc.returncode, 'status': status})}\n\n"
        except GeneratorExit:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                pass
            log.info("%s task=%s client disconnected, killed", reason, task_id)
        finally:
            with PROCS_LOCK:
                PROCS.pop(task_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


class EnvCreateReq(BaseModel):
    name: str
    python: str | None = None   # 可选: 指定 python 解释器 (默认 sys.executable)


class EnvRunReq(BaseModel):
    name: str
    cmd: str                    # shell 命令
    cwd: str = "."              # 相对 workspace
    workspace: str = "default"


class EnvPipReq(BaseModel):
    name: str
    action: str                 # install | show | freeze | uninstall
    packages: list[str] = []    # install/uninstall 时的包名列表
    extra: str | None = None    # 额外 pip 参数, 如 "-r requirements.txt"


@app.get("/env/ls")
def env_ls(_: None = Depends(auth)):
    return ok({"envs": list_envs(), "envs_dir": str(ENVS_DIR)})


@app.post("/env/create")
def env_create(req: EnvCreateReq, _: None = Depends(auth)):
    d = env_dir(req.name)
    if d.exists():
        raise HTTPException(status_code=409, detail=f"环境 {req.name} 已存在")
    py = req.python or sys.executable
    log.info("create env=%s python=%s", req.name, py)
    try:
        subprocess.run([py, "-m", "venv", str(d)], check=True, timeout=300,
                       capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"venv 创建失败: {e.stderr or e}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="venv 创建超时")
    # 升级 pip
    try:
        subprocess.run([str(d / 'bin' / 'python'), "-m", "pip", "install", "--upgrade", "pip"],
                       capture_output=True, text=True, timeout=120)
    except Exception:
        pass
    return ok({"name": req.name, "python": py, "dir": str(d)})


@app.post("/env/run")
def env_run(req: EnvRunReq, _: None = Depends(auth)):
    """在指定 venv 中执行 shell 命令 (SSE 流式)。"""
    d = env_dir(req.name)
    if not (d / "bin" / "python").exists():
        raise HTTPException(status_code=404, detail=f"环境 {req.name} 不存在")
    task_id = uuid.uuid4().hex[:8]
    root = ws_root(req.workspace).resolve()
    cwd = (root / req.cwd).resolve()
    try:
        cwd.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="cwd escapes workspace")
    env = os.environ.copy()
    bin_dir = str(d / "bin")
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(d)
    log.info("env-run task=%s env=%s cmd=%s", task_id, req.name, req.cmd)
    try:
        proc = subprocess.Popen(
            ["bash", "-lc", req.cmd],
            cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, start_new_session=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"start failed: {e}")
    return _stream_proc(task_id, proc, reason="env-run")


@app.post("/env/pip")
def env_pip(req: EnvPipReq, _: None = Depends(auth)):
    """在指定 venv 中管理 pip 包 (SSE 流式)。"""
    d = env_dir(req.name)
    pip = str(d / "bin" / "python") + " -m pip"
    if req.action == "install":
        if not req.packages and not req.extra:
            raise HTTPException(status_code=400, detail="install 需指定 packages 或 extra")
        parts = ["install"] + list(req.packages)
        if req.extra:
            parts += req.extra.split()
    elif req.action == "uninstall":
        if not req.packages:
            raise HTTPException(status_code=400, detail="uninstall 需指定 packages")
        parts = ["uninstall", "-y"] + list(req.packages)
    elif req.action == "show":
        if not req.packages:
            raise HTTPException(status_code=400, detail="show 需指定 packages")
        parts = ["show"] + list(req.packages)
    elif req.action == "freeze":
        parts = ["freeze"]
    else:
        raise HTTPException(status_code=400, detail=f"未知 action: {req.action}")
    task_id = uuid.uuid4().hex[:8]
    cmd = f"{pip} {' '.join(parts)}"
    log.info("env-pip task=%s env=%s action=%s", task_id, req.name, req.action)
    try:
        proc = subprocess.Popen(
            ["bash", "-lc", cmd],
            cwd=str(d), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"start failed: {e}")
    return _stream_proc(task_id, proc, reason="env-pip")


@app.get("/gpu")
def gpu(_: None = Depends(auth)):
    fields = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
    try:
        out = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
            timeout=10,
        ).decode()
    except FileNotFoundError:
        log.debug("nvidia-smi not found")
        return ok({"gpus": [], "error": "nvidia-smi not found (device has no GPU?)"})
    except subprocess.CalledProcessError as e:
        log.warning("nvidia-smi failed: %s", e)
        return ok({"gpus": [], "error": f"nvidia-smi failed: {e}"})
    except subprocess.TimeoutExpired:
        log.warning("nvidia-smi timeout")
        return ok({"gpus": [], "error": "nvidia-smi timeout"})

    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 6:
            gpus.append(
                {
                    "index": parts[0],
                    "name": parts[1],
                    "util": parts[2],
                    "mem_used": parts[3],
                    "mem_total": parts[4],
                    "temp": parts[5],
                }
            )
    log.debug("gpu query ok, %d cards", len(gpus))
    return ok({"gpus": gpus})


# ---------- 同步 ----------
class SyncManifestReq(BaseModel):
    workspace: str
    files: dict  # {path: {"mtime": float, "size": int}}


@app.post("/sync/manifest")
def sync_manifest(req: SyncManifestReq, _: None = Depends(auth)):
    ws_root(req.workspace)  # 校验 + 建目录
    manifest = load_manifest(req.workspace)
    files = req.files or {}
    to_upload = []
    for p, info in files.items():
        try:
            safe_rel_path(p)
        except HTTPException:
            log.warning("sync manifest 跳过非法路径: %s", p)
            continue
        cur = manifest.get(p)
        if (
            cur is None
            or cur.get("mtime") != info.get("mtime")
            or cur.get("size") != info.get("size")
        ):
            to_upload.append(p)
    to_delete = [p for p in manifest if p not in files]
    log.info(
        "sync manifest ws=%s local=%d remote=%d to_upload=%d to_delete=%d",
        req.workspace, len(files), len(manifest), len(to_upload), len(to_delete),
    )
    return ok({"to_upload": to_upload, "to_delete": to_delete})


@app.post("/sync/file")
async def sync_file(
    workspace: str = Form(...),
    path: str = Form(...),
    mtime: float = Form(...),
    size: int = Form(...),
    file: UploadFile = File(...),
    _: None = Depends(auth),
):
    root = ws_root(workspace).resolve()
    rel = safe_rel_path(path)
    dest = (root / rel).resolve()
    try:
        dest.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes workspace")
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = await file.read()
    dest.write_bytes(content)
    manifest = load_manifest(workspace)
    manifest[path] = {"mtime": mtime, "size": size}
    save_manifest(workspace, manifest)
    log.debug("sync upload ws=%s path=%s size=%d", workspace, path, size)
    return ok({"path": path, "size": size})


@app.delete("/sync/file")
def sync_delete(
    workspace: str = Query(...),
    path: str = Query(...),
    _: None = Depends(auth),
):
    root = ws_root(workspace).resolve()
    rel = safe_rel_path(path)
    dest = (root / rel).resolve()
    try:
        dest.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes workspace")
    if dest.is_file():
        dest.unlink()
    manifest = load_manifest(workspace)
    manifest.pop(path, None)
    save_manifest(workspace, manifest)
    log.debug("sync delete ws=%s path=%s", workspace, path)
    return ok({"path": path})


# ---------- 文件操作 ----------
def _resolve_safe(workspace: str, path: str) -> Path:
    """解析 workspace 下安全路径, 返回绝对 Path。"""
    root = ws_root(workspace).resolve()
    rel = safe_rel_path(path)
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes workspace")
    return p


@app.get("/ls")
def ls(workspace: str = Query(...), path: str = Query("."), _: None = Depends(auth)):
    p = _resolve_safe(workspace, path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="no such path")
    if p.is_file():
        st = p.stat()
        items = [{"name": p.name, "type": "file", "size": st.st_size, "mtime": st.st_mtime}]
    else:
        items = []
        for c in sorted(p.iterdir()):
            st = c.stat()
            items.append({
                "name": c.name,
                "type": "dir" if c.is_dir() else "file",
                "size": st.st_size if c.is_file() else 0,
                "mtime": st.st_mtime,
            })
    log.debug("ls ws=%s path=%s -> %d items", workspace, path, len(items))
    return ok({"items": items, "path": path})


@app.get("/cat")
def cat(workspace: str = Query(...), path: str = Query(...), _: None = Depends(auth)):
    p = _resolve_safe(workspace, path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    log.debug("cat ws=%s path=%s size=%d", workspace, path, p.stat().st_size)
    return ok({"content": p.read_text(errors="replace"), "path": path})


@app.get("/download")
def download(workspace: str = Query(...), path: str = Query(...), _: None = Depends(auth)):
    p = _resolve_safe(workspace, path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="no such path")
    if p.is_file():
        log.info("download file ws=%s path=%s size=%d", workspace, path, p.stat().st_size)
        return Response(
            content=p.read_bytes(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
        )
    # 目录 → tar 流式
    log.info("download dir ws=%s path=%s as tar", workspace, path)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    tmp.close()
    try:
        with tarfile.open(tmp.name, mode="w") as tar:
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    tar.add(str(f), arcname=f.relative_to(p).as_posix())
    except Exception as e:
        os.unlink(tmp.name)
        raise HTTPException(status_code=500, detail=f"tar failed: {e}")

    def gen():
        try:
            with open(tmp.name, "rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    return StreamingResponse(
        gen(),
        media_type="application/x-tar",
        headers={"Content-Disposition": f'attachment; filename="{p.name}.tar"'},
    )


@app.delete("/rm")
def rm(workspace: str = Query(...), path: str = Query(...), _: None = Depends(auth)):
    import shutil
    p = _resolve_safe(workspace, path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="no such path")
    root = ws_root(workspace).resolve()
    if p == root:
        raise HTTPException(status_code=400, detail="refuse to remove workspace root")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    log.warning("rm ws=%s path=%s", workspace, path)
    return ok({"path": path})


if __name__ == "__main__":
    import uvicorn

    log.info("listening on %s:%s  work_root=%s  logs_dir=%s", HOST, PORT, WORK_ROOT, LOGS_DIR)
    # access_log=False: 用我们自己的中间件记录请求, 避免重复
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)
