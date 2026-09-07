# ArticleOps 分支归档清单

日期：2026-09-07

本清单记录分支收口前的远端 head。17 个非 `main` head 已创建并推送为注释标签；
下表 SHA 是标签的 peeled commit SHA，不是注释标签对象自身的 SHA。删除任何 branch
前必须再次执行 peeled SHA 比对。`main` 是唯一长期分支，不在删除清单中。

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
已经是 `ai-guided-publication` 的 `main`；以下 10 个旧 worktree 只可在文件原样、
运行目录不变的前提下 detach，以解除旧分支引用占用：

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
- 本地待解除/删除的旧分支共 13 个；`main` 不动。没有远端 open PR，删除前仍需复核
  远端 head 与本表标签一一对应。
- 不触碰任何 `data`、uploads、images、真实 Profile、Cookie、Token、用户内容或 `uv.lock`。

## 收口顺序

1. 在 `main` 完成文档 focused commit，SSH 推送并核对 `origin/main` 完整 40 位 SHA。
2. 再次用 `git ls-remote --tags origin` 的 `^{}` peeled 值逐项核对本表 17 个标签。
3. detach 旧 worktree（仅解除分支占用，文件和运行目录原样保留）。
4. 删除本表 17 个远端 heads 和 13 个本地旧分支；禁止 force push，标签不删。
5. fetch 后确认 `main` 为唯一长期分支，复核标签、stash、脏文档和旧 worktree 均仍按本表保全。
