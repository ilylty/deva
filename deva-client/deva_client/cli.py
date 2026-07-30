"""deva CLI 主入口。用法见各子命令 --help。"""
import argparse
import io
import json
import logging
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import config
from . import sync as syncmod
from .api import Client, DevaError


def _client() -> Client:
    cfg = config.load()
    return Client(cfg["url"], cfg["token"])


def _die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _unwrap(resp: dict):
    """统一响应壳: {"ok":bool,"data":...} / {"ok":false,"error":...}"""
    if not resp.get("ok"):
        raise DevaError(resp.get("error") or resp.get("detail") or "unknown error")
    return resp.get("data")


# ---- 子命令实现 ----
def cmd_status(args):
    c = _client()
    try:
        data = _unwrap(c.health())
    except DevaError as e:
        _die(str(e))
    cfg = config.load()
    print(f"deva-server: {data.get('version','?')}  ok")
    print(f"url:       {cfg['url']}")
    print(f"workspace: {cfg['workspace']}")
    print(f"token:     {cfg['token'][:4]}***")


def cmd_gpu(args):
    try:
        data = _unwrap(_client().gpu())
    except DevaError as e:
        _die(str(e))
    gpus = data.get("gpus", [])
    if not gpus:
        print(data.get("error") or "no GPU")
        return
    print(f"{'idx':>3} {'name':<20} {'util%':>6} {'mem':>16} {'temp':>5}")
    for g in gpus:
        mem = f"{g['mem_used']}/{g['mem_total']}MiB"
        print(f"{g['index']:>3} {g['name']:<20} {g['util']:>6} {mem:>16} {g['temp']:>5}C")


def cmd_run(args):
    script_args = [a for a in (args.script_args or []) if a != "--"]
    cfg = config.load()
    try:
        data = _unwrap(_client().run(
            script=args.script,
            args=script_args,
            env=args.env,
            gpu=args.gpu,
            cwd=args.cwd,
            workspace=cfg["workspace"],
        ))
    except DevaError as e:
        _die(str(e))
    print(f"submitted task {data['task_id']}  pid={data.get('pid')}  workspace={cfg['workspace']}")
    print(f"  deva logs {data['task_id']} -f   # 看日志")
    print(f"  deva kill {data['task_id']}      # 取消")


def cmd_exec(args):
    cfg = config.load()
    script_args = [a for a in (args.script_args or []) if a != "--"]
    client = _client()
    try:
        resp = client.exec_run(
            args.script, script_args, env=args.env, gpu=args.gpu,
            cwd=args.cwd, workspace=cfg["workspace"],
        )
    except DevaError as e:
        _die(str(e))
    rc = 0
    for ev in _read_sse(resp):
        t = ev.get("type")
        if t == "stdout":
            sys.stdout.write(ev.get("data", ""))
            sys.stdout.flush()
        elif t == "end":
            rc = ev.get("returncode", 0)
            print(f"\n[exec] {ev.get('status')} returncode={rc}", file=sys.stderr)
            break
    sys.exit(rc or 0)


def cmd_ps(args):
    try:
        rows = _unwrap(_client().tasks(all=args.all))
    except DevaError as e:
        _die(str(e))
    if not rows:
        print("(no tasks)" if args.all else "(no running tasks)")
        return
    print(f"{'id':<10} {'status':<11} {'script':<24} {'env':<10} {'gpu':<5} {'started':<20}")
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["started"])) if r["started"] else "-"
        print(f"{r['id']:<10} {r['status']:<11} {r['script'][:24]:<24} {(r['env'] or '-'):<10} {(r['gpu'] or '-'):<5} {ts:<20}")


def _read_sse(resp):
    """读 SSE 流, yield 事件 dict。resp 是 urlopen 返回的可迭代 response。"""
    for raw in resp:
        if not raw:
            continue
        line = raw.decode(errors="replace")
        if line.startswith("data: "):
            payload = line[6:].strip()
            try:
                yield json.loads(payload)
            except Exception:
                pass


def cmd_logs(args):
    if args.follow:
        _cmd_logs_follow(args)
        return
    try:
        data = _unwrap(_client().logs(args.task_id, tail=args.tail))
    except DevaError as e:
        _die(str(e))
    sys.stdout.write(data["log"])
    if data["log"] and not data["log"].endswith("\n"):
        sys.stdout.write("\n")


def _cmd_logs_follow(args):
    client = _client()
    try:
        resp = client.logs_follow(args.task_id, tail=args.tail)
    except DevaError as e:
        _die(str(e))
    try:
        for ev in _read_sse(resp):
            t = ev.get("type")
            if t == "log":
                sys.stdout.write(ev.get("data", ""))
                sys.stdout.flush()
            elif t == "end":
                print(f"\n[logs] 任务结束: {ev.get('status')}", file=sys.stderr)
                break
    except KeyboardInterrupt:
        print("\n[logs] 已停止", file=sys.stderr)


def cmd_kill(args):
    try:
        data = _unwrap(_client().kill(args.task_id))
    except DevaError as e:
        _die(str(e))
    print(f"task {args.task_id}: killed={data.get('killed')}")


def cmd_sync(args):
    cfg = config.load()
    root = Path(args.root or cfg.get("local_root", ".")).expanduser().resolve()
    ws = args.ws or cfg["workspace"]
    if not root.is_dir():
        _die(f"local root 不是目录: {root}")
    client = _client()
    patterns = syncmod.load_ignore(root)
    files = syncmod.scan(root, patterns)
    try:
        diff = _unwrap(client.sync_manifest(ws, files))
    except DevaError as e:
        _die(str(e))
    to_upload = diff.get("to_upload", [])
    to_delete = diff.get("to_delete", [])
    print(f"sync ws={ws} root={root}")
    print(f"  本地 {len(files)} 文件, 待上传 {len(to_upload)}, 待删除 {len(to_delete)}")
    fail = 0
    for p in to_upload:
        info = files[p]
        try:
            b = (root / p).read_bytes()
            _unwrap(client.sync_upload(ws, p, info["mtime"], info["size"], b))
            print(f"  + {p} ({info['size']}B)")
        except (DevaError, OSError) as e:
            print(f"  ! {p} 失败: {e}", file=sys.stderr)
            fail += 1
    for p in to_delete:
        try:
            _unwrap(client.sync_delete(ws, p))
            print(f"  - {p}")
        except DevaError as e:
            print(f"  ! 删除 {p} 失败: {e}", file=sys.stderr)
            fail += 1
    print(f"完成{' (' + str(fail) + ' 失败)' if fail else ''}")


def cmd_config(args):
    if args.action == "show" or not args.action:
        print(f"# {config.CONFIG_PATH}")
        print(json.dumps(config.load(), indent=2, ensure_ascii=False))
    elif args.action == "set":
        if not args.key or args.value is None:
            _die("usage: deva config set <key> <value>")
        cfg = config.set_key(args.key, args.value)
        print(f"set {args.key} = {args.value}")
        print(f"# {config.CONFIG_PATH}")
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    elif args.action == "path":
        print(config.CONFIG_PATH)


def cmd_ls(args):
    cfg = config.load()
    try:
        data = _unwrap(_client().ls(cfg["workspace"], args.path or "."))
    except DevaError as e:
        _die(str(e))
    items = data.get("items", [])
    print(f"{data.get('path', '.')}  ({len(items)} 项)")
    if not items:
        return
    for it in items:
        t = "d" if it["type"] == "dir" else "f"
        size = it["size"] if it["type"] == "file" else ""
        print(f"  {t} {it['name']:<32} {size}")


def cmd_cat(args):
    cfg = config.load()
    try:
        data = _unwrap(_client().cat(cfg["workspace"], args.path))
    except DevaError as e:
        _die(str(e))
    sys.stdout.write(data["content"])
    if data["content"] and not data["content"].endswith("\n"):
        sys.stdout.write("\n")


def cmd_fetch(args):
    cfg = config.load()
    remote = args.remote_path
    local = args.local_path
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    tmp.close()
    try:
        size, ctype = _client().download_file(cfg["workspace"], remote, tmp.name)
    except DevaError as e:
        os.unlink(tmp.name)
        _die(str(e))
    if "x-tar" in ctype:
        target = local or Path(remote).name
        Path(target).mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tmp.name, mode="r:*") as tar:
                tar.extractall(target)
        except Exception as e:
            os.unlink(tmp.name)
            _die(f"解压失败: {e}")
        os.unlink(tmp.name)
        print(f"目录已拉取: {target}/ ({size}B tar)")
    else:
        target = local or Path(remote).name
        # 确保目标父目录存在 (fetch 到子目录时不会因目录缺失而失败)
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        os.rename(tmp.name, target)
        print(f"文件已拉取: {target} ({size}B)")


def cmd_rm(args):
    cfg = config.load()
    if not args.yes:
        if not sys.stdin.isatty():
            _die("非交互模式需 --yes 确认")
        ans = input(f"确认删除 {args.path}? (y/N) ")
        if ans.lower() not in ("y", "yes"):
            print("已取消")
            return
    try:
        _unwrap(_client().rm(cfg["workspace"], args.path))
    except DevaError as e:
        _die(str(e))
    print(f"已删除 {args.path}")


def _stream_out(resp):
    """通用: 把 SSE 流输出到 stdout, 返回最终 returncode/status。"""
    rc = None
    status = None
    for ev in _read_sse(resp):
        t = ev.get("type")
        if t == "stdout":
            sys.stdout.write(ev.get("data", ""))
            sys.stdout.flush()
        elif t == "end":
            rc = ev.get("returncode")
            status = ev.get("status")
    return rc, status


def cmd_env(args):
    client = _client()
    try:
        if args.env_action == "ls":
            data = _unwrap(client.env_ls())
            envs = data.get("envs", [])
            print(f"envs_dir: {data.get('envs_dir')}")
            if not envs:
                print("(无环境, 用 `deva env create <name>` 创建)")
                return
            for e in envs:
                pkgs = e.get("packages")
                pkgs = f"  ({pkgs} pkgs)" if pkgs is not None else ""
                print(f"  {e['name']:<20} {e.get('python','?'):<28}{pkgs}")
            return
        if args.env_action == "create":
            data = _unwrap(client.env_create(args.name, python=args.python))
            print(f"已创建环境 {data['name']} @ {data['dir']}")
            return
        if args.env_action == "run":
            try:
                resp = client.env_run(args.name, args.cmd, cwd=args.cwd,
                                      workspace=config.load().get("workspace", "default"))
            except DevaError as e:
                _die(str(e))
            rc, status = _stream_out(resp)
            print(f"\n[env-run] 结束: status={status} rc={rc}", file=sys.stderr)
            if rc not in (0, None):
                sys.exit(rc)
            return
    except DevaError as e:
        _die(str(e))


def cmd_pip(args):
    client = _client()
    try:
        try:
            resp = client.env_pip(args.name, args.action, packages=args.packages, extra=args.extra)
        except DevaError as e:
            _die(str(e))
        rc, status = _stream_out(resp)
        print(f"\n[pip] 结束: status={status} rc={rc}", file=sys.stderr)
        if rc not in (0, None):
            sys.exit(rc)
    except DevaError as e:
        _die(str(e))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deva", description="设备A GPU 算力远程调度 CLI")
    p.add_argument("-v", "--verbose", action="store_true", help="详细日志(DEBUG, 输出到 stderr)")
    p.add_argument("--log-file", help="日志写入文件(append, DEBUG 级), 便于排查")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="连通性 + 服务端版本")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("gpu", help="设备A GPU 状态 (nvidia-smi)")
    sp.set_defaults(func=cmd_gpu)

    sp = sub.add_parser("run", help="异步提交脚本")
    sp.add_argument("script", help="脚本路径 (相对 workspace)")
    sp.add_argument("--env", help="conda 环境名")
    sp.add_argument("--gpu", help="CUDA_VISIBLE_DEVICES, 如 0 或 0,1")
    sp.add_argument("--cwd", default=".", help="工作目录 (相对 workspace, 默认 .)")
    sp.add_argument("script_args", nargs=argparse.REMAINDER, help="脚本参数, 用 -- 分隔")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("exec", help="前台同步执行脚本(流式输出)")
    sp.add_argument("script", help="脚本路径 (相对 workspace)")
    sp.add_argument("--env", help="conda 环境名")
    sp.add_argument("--gpu", help="CUDA_VISIBLE_DEVICES, 如 0")
    sp.add_argument("--cwd", default=".", help="工作目录 (相对 workspace)")
    sp.add_argument("script_args", nargs=argparse.REMAINDER, help="脚本参数, 用 -- 分隔")
    sp.set_defaults(func=cmd_exec)

    sp = sub.add_parser("ps", help="列任务")
    sp.add_argument("-a", "--all", action="store_true", help="全部任务 (默认只看 running)")
    sp.set_defaults(func=cmd_ps)

    sp = sub.add_parser("logs", help="查任务日志")
    sp.add_argument("task_id")
    sp.add_argument("-n", "--tail", type=int, default=0, help="尾部 N 行 (0=全部)")
    sp.add_argument("-f", "--follow", action="store_true", help="流式跟随直到任务结束")
    sp.set_defaults(func=cmd_logs)

    sp = sub.add_parser("kill", help="取消任务")
    sp.add_argument("task_id")
    sp.set_defaults(func=cmd_kill)

    sp = sub.add_parser("sync", help="本地 workspace → A 单向镜像同步")
    sp.add_argument("--root", help="本地根目录 (默认 config.local_root)")
    sp.add_argument("--ws", help="workspace 名 (默认 config.workspace)")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("ls", help="浏览 A 上 workspace 目录")
    sp.add_argument("path", nargs="?", default=".", help="相对 workspace 的路径")
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("cat", help="读 A 上文件内容")
    sp.add_argument("path", help="相对 workspace 的文件路径")
    sp.set_defaults(func=cmd_cat)

    sp = sub.add_parser("fetch", help="从 A 拉文件/目录到本地")
    sp.add_argument("remote_path", help="相对 workspace 的远端路径")
    sp.add_argument("local_path", nargs="?", help="本地保存路径 (默认同名)")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("rm", help="删 A 上文件/目录")
    sp.add_argument("path", help="相对 workspace 的路径")
    sp.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser("env", help="环境管理 (venv)")
    env_sub = sp.add_subparsers(dest="env_action", required=True)
    sp_ls = env_sub.add_parser("ls", help="列出所有环境")
    sp_ls.set_defaults(func=cmd_env)
    sp_cr = env_sub.add_parser("create", help="创建 venv 环境")
    sp_cr.add_argument("name", help="环境名")
    sp_cr.add_argument("--python", help="指定 python 解释器 (默认服务端 sys.executable)")
    sp_cr.set_defaults(func=cmd_env)
    sp_rn = env_sub.add_parser("run", help="在环境中执行 shell 命令 (流式)")
    sp_rn.add_argument("name", help="环境名")
    sp_rn.add_argument("cmd", help="shell 命令")
    sp_rn.add_argument("--cwd", default=".", help="工作目录 (相对 workspace)")
    sp_rn.set_defaults(func=cmd_env)

    sp = sub.add_parser("pip", help="在指定环境中管理 pip 包 (流式)")
    sp.add_argument("name", help="环境名")
    sp.add_argument("action", choices=["install", "uninstall", "show", "freeze"], help="操作")
    sp.add_argument("packages", nargs="*", help="包名 (install/uninstall/show)")
    sp.add_argument("--extra", help="额外 pip 参数, 如 '-r requirements.txt'")
    sp.set_defaults(func=cmd_pip)

    sp = sub.add_parser("config", help="查看/编辑配置")
    sp.add_argument("action", nargs="?", choices=["show", "set", "path"], default="show")
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.set_defaults(func=cmd_config)

    return p


def _setup_logging(verbose: bool, log_file: str | None):
    """客户端日志: 默认 WARNING(只警告/错误) 到 stderr, 不污染 stdout 命令输出。
    -v 提到 DEBUG; --log-file 总记 DEBUG 到文件。"""
    level = logging.DEBUG if verbose else logging.WARNING
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # root 放最低, 各 handler 自行控级
    # 清空可能残留的 handler (避免重复)
    root.handlers.clear()
    sh = logging.StreamHandler()  # 默认 stderr
    sh.setLevel(level)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)


def main(argv=None):
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False), getattr(args, "log_file", None))
    logging.getLogger("deva.cli").debug("deva start: cmd=%s verbose=%s", args.cmd, args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
