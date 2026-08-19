"""平台媒体进度的无副作用、安全投影。"""

from __future__ import annotations

_MEDIA_PROGRESS_STATUSES = frozenset(
    {"not_required", "completed", "partial", "failed", "in_progress"}
)
_MAX_MEDIA_PROGRESS_IMAGES = 10000


def safe_media_progress(value: object) -> dict[str, int | str] | None:
    """投影媒体进度，拒绝不可信计数、状态和隐私字段。"""

    if not isinstance(value, dict):
        return None
    fields = ("expected_images", "uploaded_images", "failed_image_count")
    counts: dict[str, int] = {}
    for field in fields:
        item = value.get(field)
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            or item > _MAX_MEDIA_PROGRESS_IMAGES
        ):
            return None
        counts[field] = item
    expected = counts["expected_images"]
    uploaded = counts["uploaded_images"]
    failed = counts["failed_image_count"]
    if uploaded > expected or failed > expected or uploaded + failed > expected:
        return None
    if expected == 0:
        derived_status = "not_required"
    elif uploaded == expected and failed == 0:
        derived_status = "completed"
    elif uploaded == 0 and failed == expected:
        derived_status = "failed"
    elif uploaded + failed == expected:
        derived_status = "partial"
    else:
        derived_status = "in_progress"
    status = value.get("media_status")
    if not isinstance(status, str) or status not in _MEDIA_PROGRESS_STATUSES:
        return None
    if status != derived_status:
        return None
    return {**counts, "media_status": status}


__all__ = ["safe_media_progress"]
