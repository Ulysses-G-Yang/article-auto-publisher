"""执行真实目标选择函数，确保私密发布不会混入草稿全选或显示为公开。"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_private_publish_target_controls_use_explicit_mode():
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 执行前端行为测试")
    root = Path(__file__).resolve().parents[2]
    source = (root / "web/static/js/content-studio.js").read_text(encoding="utf-8")
    functions = []
    for name in (
        "canSelectPlatform", "deliverablePlatforms", "enabledPlatformIds",
        "switcherMode", "modeLabel", "updateCreatePlanButton",
    ):
        start = source.index(f"    function {name}(")
        following = re.search(r"\n    (?:async )?function ", source[start + 1:])
        assert following is not None
        functions.append(source[start:start + 1 + following.start()])
    script = """
      const assert = require('node:assert/strict');
      const button = {textContent: ''};
      const byId = () => button;
      const state = {
        platforms: [
          {id: 'zhihu', delivery_enabled: true},
          {id: 'xiaohongshu', delivery_enabled: false, private_publish_enabled: true},
          {id: 'douyin', delivery_enabled: false}
        ],
        switcherToggles: {xiaohongshu: true}, switcherModes: {},
        draft: {targets: [{platform: 'xiaohongshu', mode: 'PRIVATE_PUBLISH'}]}
      };
    """ + "\n".join(functions) + """
      assert.equal(canSelectPlatform(state.platforms[1]), true);
      assert.equal(canSelectPlatform(state.platforms[2]), false);
      assert.deepEqual(deliverablePlatforms().map(p => p.id), ['zhihu']);
      assert.deepEqual(enabledPlatformIds(), ['xiaohongshu']);
      assert.equal(switcherMode('xiaohongshu'), 'PRIVATE_PUBLISH');
      state.switcherModes.xiaohongshu = 'PUBLISH';
      assert.equal(switcherMode('xiaohongshu'), 'PRIVATE_PUBLISH');
      assert.equal(switcherMode('zhihu'), 'DRAFT');
      assert.equal(modeLabel('PRIVATE_PUBLISH'), '仅自己可见发布');
      updateCreatePlanButton();
      assert.equal(button.textContent, '确认 1 个仅自己可见发布');
      state.draft.targets.push({mode: 'DRAFT'});
      updateCreatePlanButton();
      assert.equal(button.textContent, '保存到 1 个草稿箱，确认 1 个仅自己可见发布');
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    template = (root / "web/templates/upload.html").read_text(encoding="utf-8")
    assert 'id="publish-confirm-description"' in template
