"""全局配置管理"""
import os
import copy
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认配置
DEFAULT_CONFIG = {
    "app": {
        "host": "127.0.0.1",
        "port": 5000,
        "debug": True,
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
            "login_url": "https://my.zol.com.cn/",
            "editor_url": "https://blog.zol.com.cn/post.php?act=add",
            "draft_url": "https://blog.zol.com.cn/post.php?act=draft",
            "title_max_length": 50,
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


def load_config(config_path: str = None) -> dict:
    """加载配置，优先使用 YAML 文件，否则使用默认配置"""
    global _config
    if _config is not None:
        return _config

    cfg = copy.deepcopy(DEFAULT_CONFIG)

    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
            if user_config:
                _deep_merge(cfg, user_config)

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
