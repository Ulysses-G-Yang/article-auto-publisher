"""Keep offline adapter fixtures fast while exercising the real action path."""

import pytest

from platforms.action_pacing import ActionPacer


@pytest.fixture(autouse=True)
def virtual_action_waits(monkeypatch):
    # Timing tests inject and assert their own virtual clock. All other adapter
    # fixtures still use the real pacer, including character input and no retry.
    async def no_wait(self, seconds):
        pass

    monkeypatch.setattr(ActionPacer, "_sleep", no_wait)
