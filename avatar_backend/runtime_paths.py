from __future__ import annotations

import os
from pathlib import Path


_DEFAULT_INSTALL_DIR = "/opt/avatar-server"


def install_dir() -> Path:
    return Path(os.environ.get("NOVA_APP_ROOT", _DEFAULT_INSTALL_DIR))


def env_file() -> Path:
    default_env = install_dir() / ".env"
    return Path(os.environ.get("NOVA_ENV_FILE", str(default_env)))


def config_dir() -> Path:
    return install_dir() / "config"


def static_dir() -> Path:
    return install_dir() / "static"


def logs_dir() -> Path:
    return install_dir() / "logs"


def data_dir() -> Path:
    return install_dir() / "data"
