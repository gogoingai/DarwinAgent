"""Connection assembly, outside the asset optimization surface."""

import os
from pathlib import Path
from dotenv import load_dotenv

from darwinagent.config import Config


def load_legacy_connection(project_root, work_dir, prefix=""):
    load_dotenv(Path(project_root) / ".env")
    from darwinagent.presets import legacy_benchmark_config

    cfg = legacy_benchmark_config()
    cfg.work_dir = Path(work_dir)
    cfg.api_key = os.environ.get("ZHIPU_API_KEY", "")
    if not cfg.api_key:
        raise RuntimeError("ZHIPU_API_KEY is not configured")

    def setting(name):
        return os.environ.get(prefix + "_" + name, "") if prefix else ""

    # Explicit legacy benchmark routing; generic mode never enters this path.
    cfg.middle_base_url = setting("FAST_API_BASE") or os.environ.get("FAST_API_BASE", "")
    cfg.model_middle = setting("FAST_MODEL") or os.environ.get("FAST_MODEL", "") or cfg.model_middle
    cfg.middle_api_key = setting("FAST_API_KEY") or os.environ.get("FAST_API_KEY", "")
    if not cfg.middle_api_key:
        raise RuntimeError("Middle (MiniMax) endpoint requires its own credential")
    cfg.fast_base_url = os.environ.get(
        "COMMANDCODE_BASE_URL", "https://api.commandcode.ai/provider/v1"
    )
    cfg.model_fast = os.environ.get("COMMANDCODE_MODEL", "") or "deepseek/deepseek-v4.1-flash-fast"
    cfg.fast_api_key = os.environ.get("COMMANDCODE_API_KEY", "")
    if not cfg.fast_api_key:
        raise RuntimeError("Fast (DeepSeek) endpoint requires its own credential")
    cfg.thinking_disabled_roles.update({"extraction", "tools", "bootstrap", "proposal"})
    return cfg


def load_connection(project_root, work_dir, prefix=""):
    """Load the generic single-endpoint configuration from an explicit local .env."""
    load_dotenv(Path(project_root) / ".env", override=False)
    cfg = Config.from_env(work_dir=work_dir)
    cfg.validate_model()
    return cfg
