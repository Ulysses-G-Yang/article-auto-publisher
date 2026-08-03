"""自动化文章发布工具 - Flask 入口"""
import sys
import os
import asyncio
import threading
import signal
import subprocess

# 确保模块路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask

from config import get_config
from web.routes import register_routes

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def kill_zombie_chrome():
    """杀掉所有使用项目 Chrome Profile 的僵尸进程"""
    profile_base = os.path.join(BASE_DIR, "data", "chrome_profiles")
    killed = 0
    try:
        # 查找所有命令行包含 chrome_profiles 路径的 chrome 进程
        result = subprocess.run(
            ["wmic", "process", "where",
             f"name='chrome.exe'", "get", "processid,commandline"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.split("\n"):
            if "chrome_profiles" in line and line.strip():
                parts = line.strip().split()
                if parts:
                    pid = parts[-1]
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                        killed += 1
                    except Exception:
                        pass
    except Exception:
        pass

    # 清理 SingletonLock 文件
    for platform in ["zol", "xiaoheihe"]:
        profile_dir = os.path.join(profile_base, platform)
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lock_path = os.path.join(profile_dir, lock_file)
            try:
                if os.path.exists(lock_path):
                    os.remove(lock_path)
            except Exception:
                pass

    return killed


def create_app() -> Flask:
    cfg = get_config()
    app_cfg = cfg["app"]

    app = Flask(
        __name__,
        template_folder="web/templates",
        static_folder="web/static",
    )
    app.secret_key = app_cfg["secret_key"]
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB 上传限制

    # 注册路由
    register_routes(app)

    # 添加清理僵尸进程的 API
    @app.route("/api/cleanup", methods=["POST"])
    def api_cleanup():
        killed = kill_zombie_chrome()
        return {"status": "ok", "killed": killed}

    return app


def start_queue_worker():
    """在后台线程中启动队列 worker（只启动一次）"""
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        from core.queue_manager import get_queue_manager
        qm = get_queue_manager()
        loop.run_until_complete(qm.start())
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("  队列 worker 已启动")


def on_shutdown(signum, frame):
    """服务器关闭时清理所有 Chrome 进程"""
    print("\n  正在清理 Chrome 进程...")
    killed = kill_zombie_chrome()
    print(f"  已清理 {killed} 个进程")
    sys.exit(0)


if __name__ == "__main__":
    cfg = get_config()
    app_cfg = cfg["app"]

    # 启动前先清理上次残留的僵尸 Chrome
    killed = kill_zombie_chrome()
    if killed > 0:
        print(f"  启动前清理了 {killed} 个僵尸 Chrome 进程")

    app = create_app()

    # 注册信号处理（Ctrl+C 或 kill 时自动清理）
    signal.signal(signal.SIGINT, on_shutdown)
    signal.signal(signal.SIGTERM, on_shutdown)

    # 启动队列 worker
    start_queue_worker()

    print(f"\n  自动化文章发布工具启动中...")
    print(f"  访问地址: http://{app_cfg['host']}:{app_cfg['port']}\n")

    app.run(
        host=app_cfg["host"],
        port=app_cfg["port"],
        debug=True,
        use_reloader=False,
    )
