"""在隔离配置和数据库中装配 MoviePilot V3 的插件测试环境。"""

import os
import sys
from pathlib import Path


REPO_PATH = Path(__file__).resolve().parents[1]
BACKEND_PATH = Path(os.environ["MOVIEPILOT_BACKEND_PATH"])
if not (BACKEND_PATH / "app").is_dir():
    raise RuntimeError("MOVIEPILOT_BACKEND_PATH 必须指向 MoviePilot 后端")
sys.path.insert(0, str(BACKEND_PATH))

from app.testing.bootstrap import prepare_v3_backend  # noqa: E402


def prepare() -> None:
    """隔离配置并暴露本仓 V3 插件，禁止测试接触运行实例。"""
    prepare_v3_backend(REPO_PATH)
