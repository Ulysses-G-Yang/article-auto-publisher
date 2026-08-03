"""Flask 路由 —— 上传、任务管理、账号管理 API"""
import os
import json
import uuid
import asyncio
import threading
from datetime import datetime

from flask import (
    Blueprint, render_template, request, jsonify, Response,
    current_app, send_from_directory,
)
from aiofiles import open as aio_open

from models.database import Database
from core.docx_parser import DocxParser
from core.nlp_analyzer import NLPAnalyzer
from core.queue_manager import get_queue_manager
from config import get_config


def register_routes(app):
    db = Database.get_instance()
    parser = DocxParser()
    nlp = NLPAnalyzer()
    cfg = get_config()

    # ==================== 页面路由 ====================

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/upload")
    def upload_page():
        return render_template("upload.html")

    @app.route("/accounts")
    def accounts_page():
        accounts = db.get_all_accounts()
        return render_template("accounts.html", accounts=accounts)

    @app.route("/task/<int:task_id>")
    def task_detail(task_id):
        task = db.get_task(task_id)
        article = db.get_article(task["article_id"]) if task else None
        logs = db.get_task_logs(task_id)
        return render_template("task_detail.html", task=task, article=article, logs=logs)

    # ==================== API: 文件上传 ====================

    @app.route("/api/upload", methods=["POST"])
    def api_upload():
        """批量上传 .docx 文件"""
        files = request.files.getlist("files")
        platforms = request.form.getlist("platforms") or ["zol", "xiaoheihe"]

        if not files:
            return jsonify({"error": "请选择文件"}), 400

        results = []
        for file in files:
            if not file.filename.endswith(".docx"):
                continue

            # 保存文件
            filename = f"{uuid.uuid4().hex}_{file.filename}"
            filepath = os.path.join(cfg["paths"]["uploads"], filename)
            os.makedirs(cfg["paths"]["uploads"], exist_ok=True)
            file.save(filepath)

            try:
                # 解析文档
                article = parser.parse(filepath)

                # NLP 分析
                keywords = nlp.extract_keywords(article.plain_text)
                keywords_str = nlp.get_keywords_string(keywords)
                keywords_list = [kw[0] for kw in keywords]

                # 话题匹配
                zol_topics = db.get_cached_topics("zol")
                xiaoheihe_topics = db.get_cached_topics("xiaoheihe")

                zol_matches = nlp.match_topic(keywords_list, zol_topics)
                xh_matches = nlp.match_topic(keywords_list, xiaoheihe_topics)

                topic_zol = zol_matches[0][0] if zol_matches else ""
                topic_xiaoheihe = xh_matches[0][0] if xh_matches else ""

                # 标题
                raw_title = parser.get_title_from_article(article)
                title_zol = nlp.generate_title(article.plain_text, raw_title, "zol")

                # 保存到数据库
                content_json = parser.to_json(article)
                article_id = db.insert_article(
                    filename=file.filename,
                    original_path=filepath,
                    content_text=article.plain_text,
                    content_json=content_json,
                    keywords=keywords_str,
                    image_count=article.image_count,
                    char_count=article.char_count,
                )

                # 保存图片记录
                for i, block in enumerate(article.blocks):
                    if block.type == "image":
                        image_idx = sum(1 for b in article.blocks[:i] if b.type == "image")
                        db.insert_image(
                            article_id=article_id,
                            filename=block.image_filename or "",
                            local_path=block.image_path or "",
                            position_index=block.position,
                            width=block.image_width or 0,
                            height=block.image_height or 0,
                            file_size=os.path.getsize(block.image_path) if block.image_path and os.path.exists(block.image_path) else 0,
                        )

                # 更新话题和标题
                db.update_article_topics(article_id, topic_zol, topic_xiaoheihe, title_zol)

                # 创建发布任务
                task_ids = []
                for plat in platforms:
                    if plat in ("zol", "xiaoheihe"):
                        task_id = db.create_task(article_id, plat)
                        task_ids.append({"platform": plat, "task_id": task_id})
                        get_queue_manager().enqueue(task_id)

                results.append({
                    "id": article_id,
                    "filename": file.filename,
                    "title": title_zol,
                    "keywords": keywords_list,
                    "topic_zol": topic_zol,
                    "topic_xiaoheihe": topic_xiaoheihe,
                    "image_count": article.image_count,
                    "char_count": article.char_count,
                    "tasks": task_ids,
                    "status": "ok",
                })

            except Exception as e:
                results.append({
                    "filename": file.filename,
                    "status": "error",
                    "error": str(e),
                })

        return jsonify({"results": results})

    # ==================== API: 任务管理 ====================

    @app.route("/api/tasks")
    def api_tasks():
        """获取所有任务列表"""
        tasks = db.get_tasks()
        # 关联文章信息
        for task in tasks:
            article = db.get_article(task["article_id"])
            task["article_title"] = article["title"] if article else ""
            task["article_filename"] = article["filename"] if article else ""
        return jsonify(tasks)

    @app.route("/api/articles")
    def api_articles():
        """获取所有文章"""
        articles = db.get_all_articles()
        for article in articles:
            article["tasks"] = db.get_tasks(article["id"])
        return jsonify(articles)

    @app.route("/api/status")
    def api_status():
        """获取队列状态"""
        qm = get_queue_manager()
        return jsonify(qm.get_queue_status())

    @app.route("/api/task/<int:task_id>/logs")
    def api_task_logs(task_id):
        logs = db.get_task_logs(task_id)
        return jsonify(logs)

    @app.route("/api/accounts")
    def api_accounts():
        """获取平台账号状态（基于 DB 记录，不自动猜测登录态）"""
        # 不再通过 Cookie 名猜测登录状态
        # 登录状态只在用户手动登录并验证通过后才设为 logged_in
        return jsonify(db.get_all_accounts())

    @app.route("/api/accounts/<platform>/login", methods=["POST"])
    def api_accounts_login(platform):
        """触发平台登录（在新线程中打开浏览器）"""
        if platform not in ("zol", "xiaoheihe"):
            return jsonify({"status": "error", "message": "不支持的平台"}), 400

        # 防止重复登录
        acc = db.get_account(platform)
        if acc and acc["status"] == "logging_in":
            return jsonify({"status": "already_logging_in", "message": "正在登录中，请在浏览器中完成操作"}), 409

        # 在新线程中运行异步登录流程
        thread = threading.Thread(
            target=_run_login_in_thread,
            args=(platform,),
            daemon=True,
        )
        thread.start()
        return jsonify({"status": "login_started", "platform": platform})

    return app


def _run_login_in_thread(platform: str):
    """在新线程中运行登录流程，使用独立的平台实例避免与队列 worker 冲突"""
    if "NODE_OPTIONS" in os.environ:
        del os.environ["NODE_OPTIONS"]

    db = Database.get_instance()
    db.upsert_account(platform, status="logging_in")

    async def _do_login():
        # 创建独立的平台实例，不与队列 worker 共享
        from platforms.zol import ZOLPlatform
        from platforms.xiaoheihe import XiaoheihePlatform

        plat = ZOLPlatform() if platform == "zol" else XiaoheihePlatform()
        try:
            await plat.initialize()
            await plat.login()
            # 登录完成后，实际访问平台页面验证登录态（不只看 Cookie）
            verified = await plat.check_login()
            if verified:
                db.upsert_account(platform, status="logged_in", last_login_time=datetime.now().isoformat())
            else:
                db.upsert_account(platform, status="login_failed")
                print(f"登录后验证失败 [{platform}]: 页面未显示登录态")
        except Exception as e:
            db.upsert_account(platform, status="login_failed")
            print(f"登录失败 [{platform}]: {e}")
        finally:
            try:
                await plat.cleanup()
            except Exception:
                pass

    try:
        asyncio.run(_do_login())
    except Exception as e:
        db.upsert_account(platform, status="login_failed")
        print(f"登录线程异常 [{platform}]: {e}")
