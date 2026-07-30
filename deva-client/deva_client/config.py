"""deva 客户端配置管理。配置文件: ~/.deva/config.json"""
import json
import os
from pathlib import Path

CONFIG_PATH = Path("~/.deva/config.json").expanduser()

DEFAULTS = {
    "url": "http://127.0.0.1:8766",
    "token": "deva-secret-change-me",
    "workspace": "default",
    "local_root": ".",
}


def load() -> dict:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        # 补默认值
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, v)
        return cfg
    return dict(DEFAULTS)


def save(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def set_key(key: str, value: str) -> dict:
    cfg = load()
    cfg[key] = value
    save(cfg)
    return cfg
