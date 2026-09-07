# ArticleOps 当前主线交接

日期：2026-09-07

本文是当前唯一有效的协作接力入口。没有新的明确任务包就停止写入；旧
`codex_handoff*.md`、旧 `RESUMABLE_SESSION.md` 和旧分支说明不再作为任务来源。

## 1. 当前可信基线

| 项目 | 当前值 |
| --- | --- |
| 仓库 | `D:\Backup\Documents\article-auto-publisher` |
| 唯一主工作树 | `D:\Backup\Documents\article-auto-publisher-worktrees\ai-guided-publication` |
| 长期分支 | `main` |
| 文档批次开始前的主线代码 SHA | `055a2406e596cb56352ed25298d1b9fcaf5f41fe` |
| 本批首个文档 focused commit | `80f2640da878659dc27ffdecb5332108c1204e61` |
| 当前 `main` / `origin/main`（本轮文档更新前） | `fcc153654be3c49d3bc2a912fbff3e15ac07b4d8` |
| Git 远端 | `git@github.com:Ulysses-G-Yang/article-auto-publisher.git` |
| 固定 Python | `C:\Users\Administrator\miniconda3\envs\article-publisher-py312\python.exe` |
| CoreUI 代码状态 | 最小可复现源码和生产资产已在 `main`；瘦身提交为 `74288d0e`、`055a2406` |

`main` 是业务和开发的唯一长期基线。本批文档提交后的最终 SHA 必须以 Git
和远端 40 位 SHA 核验结果为准，不能在文档中制造自引用的最终 SHA。

## 2. 任务与提交纪律

- 每个完成任务必须更新本文，并与该任务实现放在同一个 focused commit。
- 只暂存任务文件；禁止 `git add -A`，禁止夹带 `uv.lock`、`data`、真实用户内容或运行产物。
- 推送使用 SSH，推送后核对目标远端分支完整 40 位 SHA；禁止 force push。
- 真实登录、保存平台草稿、公开发布、删除和不确定状态重试都需要单独明确授权。
- 本轮公开发布保持默认关闭，不改监听 host/port，不启动或停止服务。
- 本轮仅新增验收记录，没有生产业务代码变更。

## 3. 分支收口状态

- `main` 已从 `809527f65e49f8c82bd394aa2f5925b365cbac56` fast-forward 到
  `055a2406e596cb56352ed25298d1b9fcaf5f41fe`，再由本批首个文档提交推进到
  `80f2640da878659dc27ffdecb5332108c1204e61`；包含 `378bd2f1` 全部主线提交及
  CoreUI 瘦身/版权指向修正，不从 shared/weibo 历史分支整条合并。
- 17 个非 `main` 远端 head 已由注释标签 `archive/2026-09-07/*` 保全并删除；标签和
  精确 peeled SHA 见 `docs/BRANCH_ARCHIVE_20260907.md`，标签不删。
- 13 个本地旧分支 refs 已删除；本地和远端长期分支均只剩 `main`，未使用 force push。
- 10 个旧 worktree 已 `git switch --detach HEAD`，目录、文件、HEAD 和脏状态均保留；不执行
  `git worktree remove --force`，不得搬移、清理或覆盖运行数据。

## 4. 必须原地保全的本机状态

- canonical worktree 的未跟踪 `uv.lock` 原样保留，绝不读取、暂存、覆盖或提交。
- 旧 `weibo-v043-integration` 的脏 `HANDOFF_CURRENT.md` 原样保留，不夹带到主线。
- 两个本地 stash 原样保留，不上传、不恢复、不在未审查秘密前读取内容。
- 各旧 worktree 的 `data`、`uploads`、`images`、Profile、Cookie、Token 和其他忽略运行目录
  原样保留；本轮只记录根层目录名，不读取其内容。

## 5. AI 配置边界

AI 只提供只读“生成平台建议”，默认关闭。生产 Windows 继续复用管理员本机维护的
`data\production_env.ps1`；现有 launcher 在启动时加载并让进程继承，不新增脚本生成、覆写、
迁移或持久化真实配置。本仓库只保留注释示例，不接触真实文件。

运维配置项仅为：`ARTICLEOPS_AI_GUIDANCE_ENABLED`、`DEEPSEEK_BASE_URL`、
`DEEPSEEK_MODEL`、`DEEPSEEK_API_KEY`。长期 Key 只从本机环境注入，页面临时 Key 仅存当前
Python 进程；二者都不写 YAML、Git、日志、数据库或响应。本机配置是明文文件，应由 Windows
ACL 限制管理员和运行账号，不宣称为加密金库。网页永久保存需先实现管理员鉴权，本轮不做。
所有数据路径和账号/Profile 保持不变。

## 6. 已取消、外部和后续目标

- 完整诊断/脱敏包已取消，不再列待办。
- 后续不再制作 ZIP 或新的发行包；已有 release 仅作为历史回滚参考。
- 数据看板属于项目外部系统，本仓库不接管其代码、部署或数据。
- 本轮收口后才开始三阶段目标：单平台仅自己可见真实发布 → AI 真实选项写入 → 多平台多账号。
  这些是后续目标，不是当前已有能力的宣称。

## 7. 验证与最终状态

CoreUI 定向测试已在当前 `main` worktree 通过：`tests/frontend/test_coreui_source_trim.py -q`
为 `4 passed`；该测试文件 Ruff 为 `All checks passed`，示例 PowerShell 仅做 ParseFile 语法
校验且为 `0 errors`。固定 Python 3.12 全量 `pytest -q` 实测为 `1251 passed, 7 skipped`
（69.43s）。首个 focused commit 的 `git diff --check`、SSH 推送和 `origin/main` 完整 40 位
SHA 核验均已通过；删除前后 17 个归档标签 peeled SHA 均匹配，最终本地/远端只剩 `main`。
此前文档批次未启动服务、未登录平台、未保存草稿、未发布、未删除用户数据，也未重试任何不确定副作用；本次真实小红书检查的状态见下节。

### 2026-09-07 小红书完整 Word 发布前检查（本轮新增，仅记录真实结果）

- 代码基线为 `fcc153654be3c49d3bc2a912fbff3e15ac07b4d8`；指定完整 Word 的离线内容契约已通过：35 字标题、17 个普通正文块、5 个 H2、7 张图片、29 个逻辑块。
- 真实账号复用了 canonical 账号库，没有复制 Cookie；一次身份 ID 与昵称匹配、空编辑器确认、H2 指纹与正文图片指纹匹配均通过。本文不记录任何账号 UUID、昵称或平台 UID。
- 仅执行一次正文填充：`title_readback_ok`、`text_ok=true`、7/7 图片上传、`media_status=completed`。
- 仅执行一次长文排版：`success=true`、`safe_to_continue=true`、7/7 图片加载；图文顺序与 H2 样式稳定回读通过，排版预览接口 2xx 门通过。
- 排版完成后的只读复核阶段 `navigated_to_editor=false`（不代表首次进入编辑器未导航）；页面已处于排版结果态，原编辑器定位不适用，不能把旧定位返回的图片数 0/标题数 0解释为失败。排版后截图抓取失败仅属于截图问题；未改文章状态、未重试。尚未点击「下一步」、尚未「暂存离开」、尚未发布；“仅自己可见”能力仍未确认。
- 云端草稿门禁与全局公开发布开关均未修改；生产 Web/MCP 未启停；临时辅助脚本只在系统临时目录，不进入 Git。
- 下一步等待用户在当前排版结果页点击一次「下一步」并提供发布前设置截图；明确禁止发布。不得把本次结果写成已实现云端草稿、已发布或封面已确认。
