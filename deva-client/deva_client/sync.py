"""deva 同步: 本地 workspace 扫描 + .devaignore 过滤。"""
import fnmatch
from pathlib import Path


def load_ignore(root: Path) -> list[str]:
    """读 .devaignore, 返回 pattern 列表 (gitignore 简化语法, fnmatch)。"""
    f = root / ".devaignore"
    if not f.exists():
        return []
    patterns = []
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def is_ignored(relpath: str, patterns: list[str]) -> bool:
    """匹配 relpath 或 basename。支持:
    - 普通 fnmatch 模式 (*.pyc, *.ckpt)
    - 目录模式 (以 / 结尾, 如 __pycache__/) → relpath 经过该目录即忽略
    """
    base = relpath.split("/")[-1]
    parts = relpath.split("/")
    for pat in patterns:
        if pat.endswith("/"):
            if pat[:-1] in parts:
                return True
            continue
        if fnmatch.fnmatch(relpath, pat) or fnmatch.fnmatch(base, pat):
            return True
    return False


def scan(root: Path, patterns: list[str]) -> dict:
    """扫描 root 下所有文件, 返回 {relpath: {mtime, size}}。"""
    files = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == ".devaignore":
            continue
        if is_ignored(rel, patterns):
            continue
        st = p.stat()
        files[rel] = {"mtime": st.st_mtime, "size": st.st_size}
    return files
