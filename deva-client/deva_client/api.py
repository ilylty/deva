"""deva 客户端 HTTP 封装 (零依赖, 仅用标准库)。"""
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlencode

log = logging.getLogger("deva.cli")


class DevaError(Exception):
    pass


class Client:
    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = self.url + path
        if params:
            url += "?" + urlencode(params)
        data = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        t0 = time.time()
        log.debug(">> %s %s body=%s", method, url, body)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                dur = (time.time() - t0) * 1000
                log.debug("<< %s %s %.0fms bytes=%d", method, url, dur, len(raw))
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            dur = (time.time() - t0) * 1000
            try:
                msg = json.loads(raw)
            except Exception:
                msg = raw
            log.warning("<< %s %s HTTP %d %.0fms %s", method, url, e.code, dur, msg)
            raise DevaError(f"HTTP {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            dur = (time.time() - t0) * 1000
            log.error("<< %s %s conn failed %.0fms %s", method, url, dur, e.reason)
            raise DevaError(f"连接失败: {e.reason}") from None

    # ---- 端点 ----
    def health(self):
        return self._req("GET", "/health")

    def gpu(self):
        return self._req("GET", "/gpu")

    def run(self, script, args=None, env=None, gpu=None, cwd=".", workspace="default"):
        return self._req("POST", "/run", body={
            "script": script, "args": args or [], "env": env,
            "gpu": gpu, "cwd": cwd, "workspace": workspace,
        })

    def tasks(self, all=False):
        return self._req("GET", "/tasks", params={"all": "1"} if all else None)

    def task(self, task_id):
        return self._req("GET", f"/tasks/{task_id}")

    def logs(self, task_id, tail=0):
        params = {"tail": str(tail)} if tail else None
        return self._req("GET", f"/tasks/{task_id}/logs", params=params)

    def kill(self, task_id):
        return self._req("POST", f"/tasks/{task_id}/kill")

    # ---- 同步 ----
    def sync_manifest(self, workspace, files):
        return self._req("POST", "/sync/manifest", body={"workspace": workspace, "files": files})

    def sync_delete(self, workspace, path):
        return self._req("DELETE", "/sync/file", params={"workspace": workspace, "path": path})

    def sync_upload(self, workspace, path, mtime, size, filebytes, filename=None):
        """multipart 上传单文件。"""
        boundary = "----deva" + uuid.uuid4().hex
        fields = {"workspace": workspace, "path": path, "mtime": str(mtime), "size": str(size)}
        parts = []
        for k, v in fields.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
            )
        fname = filename or path.split("/")[-1]
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        parts.append(filebytes)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        data = b"".join(parts)
        url = self.url + "/sync/file"
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        t0 = time.time()
        log.debug(">> POST %s path=%s size=%d", url, path, size)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                dur = (time.time() - t0) * 1000
                log.debug("<< POST %s %.0fms path=%s", url, dur, path)
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            dur = (time.time() - t0) * 1000
            log.warning("<< POST %s HTTP %d %.0fms path=%s %s", url, e.code, dur, path, raw)
            try:
                msg = json.loads(raw)
            except Exception:
                msg = raw
            raise DevaError(f"HTTP {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            dur = (time.time() - t0) * 1000
            log.error("<< POST %s conn failed %.0fms path=%s %s", url, dur, path, e.reason)
            raise DevaError(f"连接失败: {e.reason}") from None

    # ---- 文件操作 ----
    def ls(self, workspace, path="."):
        return self._req("GET", "/ls", params={"workspace": workspace, "path": path})

    def cat(self, workspace, path):
        return self._req("GET", "/cat", params={"workspace": workspace, "path": path})

    def rm(self, workspace, path):
        return self._req("DELETE", "/rm", params={"workspace": workspace, "path": path})

    def download_file(self, workspace, path, local_path):
        """流式下载到 local_path, 返回 (size, content_type)。"""
        url = self.url + "/download?" + urlencode({"workspace": workspace, "path": path})
        req = urllib.request.Request(
            url, method="GET", headers={"Authorization": f"Bearer {self.token}"}
        )
        t0 = time.time()
        log.debug(">> GET %s -> %s", url, local_path)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                ctype = r.headers.get("Content-Type", "")
                total = 0
                with open(local_path, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)
                dur = (time.time() - t0) * 1000
                log.debug("<< GET %s %.0fms bytes=%d ctype=%s", url, dur, total, ctype)
                return total, ctype
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            dur = (time.time() - t0) * 1000
            log.warning("<< GET %s HTTP %d %.0fms %s", url, e.code, dur, raw)
            try:
                msg = json.loads(raw)
            except Exception:
                msg = raw
            raise DevaError(f"HTTP {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            dur = (time.time() - t0) * 1000
            log.error("<< GET %s conn failed %.0fms %s", url, dur, e.reason)
            raise DevaError(f"连接失败: {e.reason}") from None

    # ---- 流式 ----
    def exec_run(self, script, args=None, env=None, gpu=None, cwd=".", workspace="default"):
        """前台同步执行, 返回 urlopen response (SSE 流)。"""
        body = {"script": script, "args": args or [], "env": env, "gpu": gpu, "cwd": cwd, "workspace": workspace}
        url = self.url + "/exec"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        log.debug(">> POST %s script=%s", url, script)
        try:
            return urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            log.warning("<< POST %s HTTP %d %s", url, e.code, raw)
            try:
                msg = json.loads(raw)
            except Exception:
                msg = raw
            raise DevaError(f"HTTP {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            log.error("<< POST %s conn failed %s", url, e.reason)
            raise DevaError(f"连接失败: {e.reason}") from None

    # ---- 环境管理 ----
    def env_ls(self):
        return self._req("GET", "/env/ls")

    def env_create(self, name, python=None):
        return self._req("POST", "/env/create", body={"name": name, "python": python})

    def env_run(self, name, cmd, cwd=".", workspace="default"):
        """在指定 venv 中执行 shell 命令, 返回 SSE 流 response。"""
        body = {"name": name, "cmd": cmd, "cwd": cwd, "workspace": workspace}
        url = self.url + "/env/run"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        log.debug(">> POST %s env=%s cmd=%s", url, name, cmd)
        try:
            return urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            log.warning("<< POST %s HTTP %d %s", url, e.code, raw)
            try:
                msg = json.loads(raw)
            except Exception:
                msg = raw
            raise DevaError(f"HTTP {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            log.error("<< POST %s conn failed %s", url, e.reason)
            raise DevaError(f"连接失败: {e.reason}") from None

    def env_pip(self, name, action, packages=None, extra=None):
        """在指定 venv 中管理 pip 包, 返回 SSE 流 response。"""
        body = {"name": name, "action": action, "packages": packages or [], "extra": extra}
        url = self.url + "/env/pip"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        log.debug(">> POST %s env=%s action=%s", url, name, action)
        try:
            return urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            log.warning("<< POST %s HTTP %d %s", url, e.code, raw)
            try:
                msg = json.loads(raw)
            except Exception:
                msg = raw
            raise DevaError(f"HTTP {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            log.error("<< POST %s conn failed %s", url, e.reason)
            raise DevaError(f"连接失败: {e.reason}") from None

    def logs_follow(self, task_id, tail=0):
        """流式 tail 日志, 返回 urlopen response (SSE 流)。"""
        params = {"follow": "1"}
        if tail:
            params["tail"] = str(tail)
        url = self.url + f"/tasks/{task_id}/logs?" + urlencode(params)
        req = urllib.request.Request(
            url, method="GET", headers={"Authorization": f"Bearer {self.token}"}
        )
        log.debug(">> GET %s (follow)", url)
        try:
            return urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                msg = json.loads(raw)
            except Exception:
                msg = raw
            raise DevaError(f"HTTP {e.code}: {msg}") from None
        except urllib.error.URLError as e:
            raise DevaError(f"连接失败: {e.reason}") from None
