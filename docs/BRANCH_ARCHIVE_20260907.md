# ArticleOps 分支归档清单

日期：2026-09-07

本清单记录 2026-09-07 已完成的分支收口。17 个非 `main` head 已先创建并推送为注释
标签，再在逐项比对 peeled SHA 后删除；下表 SHA 是标签的 peeled commit SHA，不是注释
标签对象自身的 SHA。`main` 是唯一长期分支，不在删除清单中，当前首个文档提交为
`80f2640da878659dc27ffdecb5332108c1204e61`。

## 远端 head → 归档 tag → commit

| 远端分支（`refs/heads/`） | 归档注释标签（`refs/tags/`） | peeled commit SHA |
| --- | --- | --- |
| `archive/agent-source-package-setup` | `archive/2026-09-07/archive-agent-source-package-setup` | `2200c5417af084f8712fddc19bd6fb31832dece5` |
| `archive/coreui-full-v5.6.0` | `archive/2026-09-07/archive-coreui-full-v5.6.0` | `378bd2f153350edfb6a751e603daf59cbbca5dfa` |
| `archive/delivery-tools-reference-20260825` | `archive/2026-09-07/archive-delivery-tools-reference-20260825` | `393ae74fd3914bab640610c98747bcfbb0a04c44` |
| `archive/hermes-frontend-matrix-ui` | `archive/2026-09-07/archive-hermes-frontend-matrix-ui` | `7e4fc2e7a6ae218139d072203aa6be3205801480` |
| `chore/coreui-source-trim` | `archive/2026-09-07/chore-coreui-source-trim` | `055a2406e596cb56352ed25298d1b9fcaf5f41fe` |
| `feat/zhihu-real-delivery` | `archive/2026-09-07/feat-zhihu-real-delivery` | `8483c3486b0536c48fc6b260fd4af87b0e7920fc` |
| `feature/ai-guided-publication` | `archive/2026-09-07/feature-ai-guided-publication` | `ef74303255676f70b2d5e1d050030cae583c5e04` |
| `feature/shared-sdk-heartbeat-delay` | `archive/2026-09-07/feature-shared-sdk-heartbeat-delay` | `8feb5de5e9e28977362a315a35ffd032f55ec480` |
| `feature/toutiao-draft` | `archive/2026-09-07/feature-toutiao-draft` | `a907b75ee08f8fc04ee08b773f49d6e35c8b8b02` |
| `fix/draft-evidence-integration` | `archive/2026-09-07/fix-draft-evidence-integration` | `771b4e23d9ad8d2a7dd291445e56d7038197589b` |
| `fix/weibo-draft-acceptance` | `archive/2026-09-07/fix-weibo-draft-acceptance` | `d205947460d0e91a3a8441c4117339df2d51df79` |
| `integrate/weibo-draft-v0.4.3` | `archive/2026-09-07/integrate-weibo-draft-v0.4.3` | `cd4eebb4b5435162e4594a74c886115361d92328` |
| `refactor/dashboard-ai-model-picker` | `archive/2026-09-07/refactor-dashboard-ai-model-picker` | `378bd2f153350edfb6a751e603daf59cbbca5dfa` |
| `refactor/delivery-diagnostics-v2` | `archive/2026-09-07/refactor-delivery-diagnostics-v2` | `378bd2f153350edfb6a751e603daf59cbbca5dfa` |
| `refactor/frontend-design-system-v2` | `archive/2026-09-07/refactor-frontend-design-system-v2` | `0091eda1dcc4170a90641c41de5ccc9e64bc2006` |
| `release/v0.4.5` | `archive/2026-09-07/release-v0.4.5` | `cc5128ff4dbe97a7577dd1f401db5d8f2169850e` |
| `release/v0.4.6` | `archive/2026-09-07/release-v0.4.6` | `cd4eebb4b5435162e4594a74c886115361d92328` |

## 本地 worktree 占用与保全

本轮不删除任何旧 worktree 目录，不使用 `git worktree remove --force`。主 worktree
已经是 `ai-guided-publication` 的 `main`；以下 10 个旧 worktree 已在文件原样、运行目录
不变的前提下 detach，仅解除旧分支引用占用：

| worktree | 收口前分支 | HEAD | 保全状态 |
| --- | --- | --- | --- |
| `D:\Backup\Documents\article-auto-publisher` | `feat/zhihu-real-delivery` | `8483c3486b0536c48fc6b260fd4af87b0e7920fc` | 未跟踪 `uv.lock`，原地保留 |
| `...\coreui-source-trim` | `chore/coreui-source-trim` | `055a2406e596cb56352ed25298d1b9fcaf5f41fe` | 干净，目录保留 |
| `...\delivery-diagnostics-v2` | `refactor/delivery-diagnostics-v2` | `378bd2f153350edfb6a751e603daf59cbbca5dfa` | 干净，目录保留 |
| `...\draft-evidence-integration` | `fix/draft-evidence-integration` | `771b4e23d9ad8d2a7dd291445e56d7038197589b` | 干净；upstream 曾指向旧 integrate，本轮未修改 |
| `...\frontend-design-system-v2` | `refactor/frontend-design-system-v2` | `0091eda1dcc4170a90641c41de5ccc9e64bc2006` | 干净，目录保留 |
| `...\release-v0.4.6` | `release/v0.4.6` | `cd4eebb4b5435162e4594a74c886115361d92328` | 干净，目录保留 |
| `...\shared-sdk-heartbeat-delay` | `feature/shared-sdk-heartbeat-delay` | `8feb5de5e9e28977362a315a35ffd032f55ec480` | 干净，目录保留 |
| `...\toutiao-draft` | `feature/toutiao-draft` | `a907b75ee08f8fc04ee08b773f49d6e35c8b8b02` | 干净，目录保留 |
| `...\weibo-draft-acceptance` | `fix/weibo-draft-acceptance` | `d205947460d0e91a3a8441c4117339df2d51df79` | 干净，目录保留 |
| `...\weibo-v043-integration` | `integrate/weibo-draft-v0.4.3` | `a49293cfc3e949f31a84a6e022c4e978d8128586` | 脏 `HANDOFF_CURRENT.md`，原地保留 |

表中省略的 `...` 均表示
`D:\Backup\Documents\article-auto-publisher-worktrees`，不是待删除路径。
各 worktree 根层存在 `data` 等运行目录；本轮不读取内容、不复制、不迁移、不清理。

## 其他本地状态

- 两个 stash 原样保留，不上传、不恢复：
  `stash@{2026-08-13 14:13:50}`（`temp-codex-platform-grid-ui-before-hermes-e0bfe4a`）和
  `stash@{2026-08-13 13:07:57}`（`hermes-stopgap-before-multiline-validation-20260813`）。
- 本地旧分支 refs 共 13 个，已全部删除；`main` 不动。远端没有 open PR，删除动作前已复核
  远端 head 与本表标签一一对应。
- 不触碰任何 `data`、uploads、images、真实 Profile、Cookie、Token、用户内容或 `uv.lock`。

## 验证记录

- 当前 `main` worktree 的 `tests/frontend/test_coreui_source_trim.py -q` 为 `4 passed`。
- 该测试文件 Ruff 为 `All checks passed`；`scripts/production_env.example.ps1` 仅做
  PowerShell ParseFile 语法校验，为 `0 errors`，未执行真实配置。
- 固定 Python 3.12 全量 `pytest -q` 为 `1251 passed, 7 skipped`（69.43s）。
- 17 个归档标签的远端 peeled SHA 在删除前后均逐项匹配；删除后远端 heads 和本地 heads
  均只剩 `main`，未使用 force push。

## 收口执行记录

1. 首个文档 focused commit `80f2640da878659dc27ffdecb5332108c1204e61` 已在 `main`
   SSH 推送，并核对 `origin/main` 完整 40 位 SHA。
2. 用 `git ls-remote --tags origin` 的 `^{}` peeled 值逐项核对本表 17 个标签，17/17 匹配。
3. detach 10 个旧 worktree；每个 HEAD、文件和工作树脏状态均与收口前一致。
4. 删除本表 17 个远端 heads 和 13 个本地旧分支；标签不删，未使用 force push。
5. `git fetch origin --prune` 后确认本地/远端仅 `main`，17 个归档标签、2 个 stash、canonical
   未跟踪 `uv.lock` 和旧脏 HANDOFF 均按本表保全。
