"""全局配置管理。

开发环境可以使用内置默认值；生产环境必须通过环境变量提供明确配置，
避免把开发调试开关、默认密钥或本机路径带入上线机器。
"""
import copy
import os
from pathlib import Path

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认配置
DEFAULT_CONFIG = {
    "app": {
        "host": "127.0.0.1",
        "port": 5000,
        "debug": False,
        "environment": "development",
        "secret_key": "change-me-in-production",
        # 本轮回归只保存草稿；显式开启后才允许调用平台公开发布动作。
        "publish_after_draft": False,
    },
    "paths": {
        "uploads": os.path.join(BASE_DIR, "uploads"),
        "images": os.path.join(BASE_DIR, "images"),
        "cookies": os.path.join(BASE_DIR, "data", "cookies"),
        "data": os.path.join(BASE_DIR, "data"),
        "database": os.path.join(BASE_DIR, "data", "app.db"),
        "logs": os.path.join(BASE_DIR, "data", "logs"),
    },
    "platforms": {
        "zol": {
            "name": "中关村在线",
            # ZOL 当前投稿/创作者中心入口；扫码在页面的“APP扫码登录”标签中生成。
            "login_url": "https://post.zol.com.cn/v2/login",
            "editor_url": "https://post.zol.com.cn/v2/create/article",
            "draft_url": "https://post.zol.com.cn/v2/manage/works/draft",
            # 创作者中心当前页面提示标题长度为 5~35 个字。
            "title_max_length": 35,
            "tag_max_count": 5,
        },
        "xiaoheihe": {
            "name": "小黑盒",
            "login_url": "https://www.xiaoheihe.cn/login",
            "editor_url": "https://www.xiaoheihe.cn/app/bbs/post",
            "draft_url": "https://www.xiaoheihe.cn/app/bbs/draft",
            "title_max_length": 30,
            "community_max_count": 2,
            "tag_max_count": 5,
        },
    },
    "human_simulation": {
        "delay": {"min": 0.5, "max": 3.0},
        "long_delay_probability": 0.1,
        "long_delay": {"min": 5.0, "max": 15.0},
        "typing": {"char_delay_min": 0.05, "char_delay_max": 0.25},
        "scroll": {"step_min": 100, "step_max": 300},
        "backspace_probability": 0.03,
    },
    "retry": {
        "max_retries": 3,
        "base_delay": 5,
        "max_delay": 120,
    },
    "nlp": {
        "top_keywords": 10,
        "title_max_length": 30,
    },
}

_config = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} 必须在 1 到 65535 之间")
    return value


def _apply_environment_overrides(cfg: dict) -> None:
    """应用启动时环境变量；配置在进程启动时固定，不做运行中热替换。"""
    environment = os.getenv("APP_ENV", cfg["app"].get("environment", "development"))
    environment = environment.strip().lower() or "development"
    if environment not in {"development", "test", "production"}:
        raise ValueError("APP_ENV 只能是 development、test 或 production")
    cfg["app"]["environment"] = environment

    if os.getenv("FLASK_HOST"):
        cfg["app"]["host"] = os.environ["FLASK_HOST"].strip()
    cfg["app"]["port"] = _env_int("FLASK_PORT", int(cfg["app"]["port"]))
    cfg["app"]["debug"] = _env_bool("APP_DEBUG", bool(cfg["app"].get("debug", False)))
    cfg["app"]["publish_after_draft"] = _env_bool(
        "PUBLISH_AFTER_DRAFT",
        bool(cfg["app"].get("publish_after_draft", False)),
    )
    if os.getenv("APP_SECRET_KEY"):
        cfg["app"]["secret_key"] = os.environ["APP_SECRET_KEY"]

    data_dir = os.getenv("APP_DATA_DIR")
    if data_dir:
        data_path = Path(data_dir).expanduser().resolve()
        cfg["paths"]["data"] = str(data_path)
        cfg["paths"]["database"] = str(data_path / "app.db")
        cfg["paths"]["logs"] = str(data_path / "logs")
        cfg["paths"]["cookies"] = str(data_path / "cookies")
        cfg["paths"]["uploads"] = str(data_path / "uploads")
        cfg["paths"]["images"] = str(data_path / "images")


def _validate_config(cfg: dict) -> None:
    """阻止明显不安全的生产配置启动。"""
    app_cfg = cfg["app"]
    if app_cfg["environment"] == "production":
        secret = str(app_cfg.get("secret_key") or "")
        if secret == "change-me-in-production" or len(secret) < 32:
            raise ValueError("生产环境必须设置长度不少于 32 的 APP_SECRET_KEY")
        if app_cfg.get("debug"):
            raise ValueError("生产环境禁止 APP_DEBUG=true")
        if app_cfg.get("publish_after_draft"):
            # 公开发布必须由上线后的显式审批/开关开启，不能随生产配置静默打开。
            raise ValueError("生产环境第一阶段必须保持 PUBLISH_AFTER_DRAFT=false")
    if not str(app_cfg.get("host") or "").strip():
        raise ValueError("FLASK_HOST 不能为空")


def load_config(config_path: str = None) -> dict:
    """加载配置，优先使用 YAML 文件，否则使用默认配置"""
    global _config
    if _config is not None:
        return _config

    cfg = copy.deepcopy(DEFAULT_CONFIG)

    config_path = config_path or os.getenv("APP_CONFIG_FILE")
    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
            if user_config:
                _deep_merge(cfg, user_config)

    _apply_environment_overrides(cfg)
    _validate_config(cfg)

    _config = cfg
    return cfg


def _deep_merge(base: dict, override: dict):
    """深度合并两个字典"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def get_config() -> dict:
    """获取当前配置"""
    global _config
    if _config is None:
        _config = load_config()
    return _config
