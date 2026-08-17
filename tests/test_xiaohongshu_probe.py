from __future__ import annotations

from article_mvp.tools.probe_xiaohongshu_editor import (
    DOM_PROBE_SCRIPT,
    classify_probe_payload,
    manual_action_for,
    sanitize_probe_payload,
)


def test_probe_classifies_body_image_input_after_cover_input() -> None:
    payload = sanitize_probe_payload(
        {
            "inputs": [
                {
                    "type": "file",
                    "accept": "image/*",
                    "multiple": False,
                    "visible": True,
                    "in_body_editor": False,
                    "neighbor_labels": ["封面"],
                },
                {
                    "type": "file",
                    "accept": "image/png,image/jpeg",
                    "multiple": True,
                    "visible": False,
                    "in_body_editor": True,
                    "neighbor_labels": ["正文"],
                },
            ]
        }
    )
    assert payload["file_input_count"] == 2
    assert classify_probe_payload(payload) == "BODY_IMAGE_INPUT_CANDIDATE"


def test_probe_fails_closed_without_body_input() -> None:
    payload = sanitize_probe_payload(
        {
            "inputs": [
                {
                    "type": "file",
                    "accept": "image/*",
                    "multiple": False,
                    "visible": True,
                    "in_body_editor": False,
                    "neighbor_labels": ["封面"],
                }
            ]
        }
    )
    assert classify_probe_payload(payload) == "NO_BODY_IMAGE_INPUT"


def test_probe_redacts_sensitive_neighbor_values() -> None:
    payload = sanitize_probe_payload(
        {
            "inputs": [
                {
                    "type": "file",
                    "accept": "image/*",
                    "multiple": False,
                    "visible": True,
                    "in_body_editor": True,
                    "neighbor_labels": [
                        r"D:\secret\photo.png token=abcdefghijklmnop",
                    ],
                }
            ]
        }
    )
    serialized = repr(payload)
    assert r"D:\secret" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "<redacted>" in serialized


def test_dom_probe_script_has_no_business_actions_or_sensitive_reads() -> None:
    lowered = DOM_PROBE_SCRIPT.lower()
    assert "set_input_files" not in lowered
    assert ".click(" not in lowered
    assert "新的创作" not in DOM_PROBE_SCRIPT
    assert ".value" not in lowered
    assert "cookie" not in lowered
    assert "response" not in lowered


def test_no_body_input_reports_manual_existing_draft_action() -> None:
    assert manual_action_for("NO_BODY_IMAGE_INPUT")
    assert "不会自动创建" in manual_action_for("NO_BODY_IMAGE_INPUT")
