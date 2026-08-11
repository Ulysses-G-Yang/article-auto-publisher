"""现役发布工作流到新数据看板的只读展示桥。"""

from typing import Any

from models.database import Database


def build_current_workflow_snapshot(db: Database) -> dict[str, Any]:
    """只暴露看板需要的非敏感字段，不复制任务或文章到新模型。"""

    articles = db.get_all_articles()
    tasks = db.get_tasks()
    article_by_id = {article["id"]: article for article in articles}
    saved_statuses = {"completed", "completed_with_warnings"}

    task_rows = []
    for task in tasks[:50]:
        article = article_by_id.get(task["article_id"], {})
        task_rows.append(
            {
                "id": task["id"],
                "platform": task["platform"],
                "status": task["status"],
                "article_title": article.get("title") or article.get("filename") or "—",
                "title_used": task.get("title_used"),
                "created_at": task.get("created_at"),
            }
        )

    return {
        "available": True,
        "summary": {
            "total_articles": len(articles),
            "total_tasks": len(tasks),
            "xiaoheihe_tasks": sum(
                1 for task in tasks if task["platform"] == "xiaoheihe"
            ),
            "saved_drafts": sum(1 for task in tasks if task["status"] in saved_statuses),
        },
        "tasks": task_rows,
    }
