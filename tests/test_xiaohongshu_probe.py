from __future__ import annotations

import json

import pytest

from article_mvp.tools.probe_xiaohongshu_editor import (
    BODY_EDITOR_SELECTOR,
    DOM_PROBE_SCRIPT,
    ProbeError,
    build_probe_result,
    classify_handoff_location,
    classify_probe_payload,
    landing_status_for,
    manual_action_for,
    safe_page_location,
    sanitize_probe_payload,
    wait_for_manual_handoff,
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


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleep_calls: list[float] = []

    def __call__(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.sleep_calls.append(delay)
        self.value += delay


class _FakeLocator:
    def __init__(self, counts: list[int]) -> None:
        self._counts = iter(counts)
        self.calls = 0
        self._last_count = 0

    async def count(self) -> int:
        self.calls += 1
        try:
            self._last_count = next(self._counts)
        except StopIteration:
            pass
        return self._last_count


class _FakePage:
    def __init__(self, urls: list[str], counts: list[int]) -> None:
        self._urls = iter(urls)
        self._current_url = urls[-1]
        self.locator_instance = _FakeLocator(counts)
        self.locator_selectors: list[str] = []

    @property
    def url(self) -> str:
        try:
            self._current_url = next(self._urls)
        except StopIteration:
            pass
        return self._current_url

    def locator(self, selector: str) -> _FakeLocator:
        self.locator_selectors.append(selector)
        return self.locator_instance


def test_toolbar_payload_is_whitelisted_and_redacted() -> None:
    payload = sanitize_probe_payload(
        {
            "toolbar_candidates": [
                {
                    "tag": "button",
                    "type": "button",
                    "role": "button",
                    "aria_label": "上传图片 token=abcdefghijklmnop",
                    "title": "图片工具",
                    "label_text": "上传图片",
                    "visible": True,
                    "near_body_editor": True,
                    "href": "https://example.invalid/private",
                    "value": "C:/secret/photo.png",
                    "src": "data:image/png;base64,secret",
                    "style": "background: secret",
                    "class": "user-content",
                }
            ]
        }
    )
    item = payload["toolbar_candidates"][0]
    assert set(item) == {
        "tag",
        "type",
        "role",
        "aria_label",
        "title",
        "label_text",
        "visible",
        "near_body_editor",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "abcdefghijklmnop" not in serialized
    assert "private" not in serialized
    assert "photo.png" not in serialized
    assert "data:image" not in serialized
    assert "user-content" not in serialized
    assert "<redacted>" in serialized


def test_safe_location_drops_query_and_fragment() -> None:
    origin, path = safe_page_location(
        "https://creator.xiaohongshu.com/publish/publish?draft=secret#fragment"
    )
    assert origin == "https://creator.xiaohongshu.com"
    assert path == "/publish/publish"


def test_handoff_location_classification_fails_closed() -> None:
    assert (
        classify_handoff_location("https://creator.xiaohongshu.com", "/login")
        == "LOGIN_REQUIRED"
    )
    assert (
        classify_handoff_location(
            "https://creator.xiaohongshu.com",
            "/security/challenge",
        )
        == "CHALLENGE"
    )
    assert (
        classify_handoff_location("https://example.invalid", "/publish/edit")
        == "UNEXPECTED_ORIGIN"
    )


def test_probe_result_does_not_emit_url_query_or_fragment() -> None:
    result = build_probe_result("EDITOR_READY")
    serialized = json.dumps(result, ensure_ascii=False)
    assert "draft=secret" not in serialized
    assert "fragment" not in serialized
    assert "?" not in serialized
    assert "#" not in serialized


@pytest.mark.asyncio
async def test_manual_handoff_waits_on_same_page_until_editor_appears() -> None:
    clock = _FakeClock()
    page = _FakePage(
        [
            "https://creator.xiaohongshu.com/publish/publish?landing=1#safe",
            "https://creator.xiaohongshu.com/publish/edit?draft=secret#fragment",
        ],
        [0, 1],
    )
    status = await wait_for_manual_handoff(
        page,
        timeout_seconds=5,
        sleep=clock.sleep,
        clock=clock,
    )
    assert status == "EDITOR_READY"
    assert page.locator_selectors == [BODY_EDITOR_SELECTOR, BODY_EDITOR_SELECTOR]
    assert clock.sleep_calls == [1]


@pytest.mark.asyncio
async def test_manual_handoff_times_out_without_editor() -> None:
    clock = _FakeClock()
    page = _FakePage(
        ["https://creator.xiaohongshu.com/publish/publish?draft=secret#fragment"],
        [0],
    )
    status = await wait_for_manual_handoff(
        page,
        timeout_seconds=2,
        sleep=clock.sleep,
        clock=clock,
    )
    assert status == "MANUAL_HANDOFF_TIMEOUT"
    assert clock.sleep_calls == [1, 1]


@pytest.mark.parametrize(
    ("url", "expected_status"),
    [
        (
            "https://creator.xiaohongshu.com/login?token=secret#fragment",
            "LOGIN_REQUIRED",
        ),
        (
            "https://creator.xiaohongshu.com/security/challenge?token=secret#fragment",
            "CHALLENGE",
        ),
    ],
)
@pytest.mark.asyncio
async def test_manual_handoff_stops_on_login_or_challenge(
    url: str,
    expected_status: str,
) -> None:
    clock = _FakeClock()
    page = _FakePage([url], [1])
    status = await wait_for_manual_handoff(
        page,
        timeout_seconds=5,
        sleep=clock.sleep,
        clock=clock,
    )
    assert status == expected_status
    assert page.locator_instance.calls == 0
    assert clock.sleep_calls == []


@pytest.mark.asyncio
async def test_manual_handoff_timeout_is_bounded() -> None:
    with pytest.raises(ProbeError, match="HANDOFF_TIMEOUT_INVALID"):
        await wait_for_manual_handoff(object(), timeout_seconds=601)


def test_landing_status_is_explicit_when_no_body_input() -> None:
    assert landing_status_for({"inputs": []}) == "LANDING_NO_INPUT"
