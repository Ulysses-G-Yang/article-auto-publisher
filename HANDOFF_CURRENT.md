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
| 本批浏览器显示修复开始前的 `main` / `origin/main` | `1123d7ebc4bd5996066a102734a14997c00942ca` |
| 本批浏览器显示修复范围 | 仅小红书新开 headed Chrome 使用原生窗口 viewport；其他平台仍固定 1366×900 |
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
- 本批新增浏览器显示修复与离线回归：Base 默认仍固定 1366×900，仅小红书类级开关使用
  headed persistent Chrome 的 `no_viewport=True`；排版成功后最多滚动一次唯一精确的「下一步」
  到真实视口并回读 rect/中心遮挡提示，不点击、不导航、不保存，也不改变 DRAFT 成功门槛。

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

### 2026-09-07 小红书浏览器显示修复（本批源码变更）

- 本批从 `1123d7ebc4bd5996066a102734a14997c00942ca` 开始；Base 默认 viewport 行为保持不变，
  仅 `XiaohongshuPlatform` 使用 `no_viewport=True`，让新开的 headed Chrome 页面随实际窗口尺寸变化。
- 小红书排版 snapshot 保留原 `next_visible`/`save_visible` 语义，仅附加真实视口宽高、唯一按钮
  rect、完整落入视口和中心未被覆盖的提示；`prepare_next_step_visibility()` 只在排版成功后的
  `apply_cover` 路径最多调用一次，展示检查失败只附加 UI 提示，不把已完成排版改为失败。
- `human/simulator.py` 在原生视口下从页面实时 `innerWidth`/`innerHeight` 取鼠标范围，并对窄窗口
  做非负边界夹取；动作次数与节奏不变。
- 上一批现场记录：新开浏览器会采用本批修复；当时已运行的旧 helper/窗口未热更新，未重启、未接管，
  不能把源码修复写成旧窗口已生效；当时现场停在排版结果页，未点击「下一步」、未暂存离开、未发布。
- 本批离线验证：固定 py312 `compileall` 通过；定向 `probe/media/layout/viewport` 共 80 passed；
  全量 `pytest -q -p no:cacheprovider` 为 1259 passed、7 skipped、1 个既有 aiosqlite 线程告警。
  `git diff --check` 通过；全仓 Ruff 仍为既有 143 项，新增 XHS 与测试代码无 Ruff 项，Base 与
  `human/simulator.py` 仅报告本批前已存在的基线项。未用当前真实 helper 做 native-resize 烟测；
  未启动或重启生产服务或真实账号浏览器，仅隔离本地 HTML 测试启动过临时无账号 Chrome。

### 2026-09-07 用户授权后浏览器切换（最新只读现场）

- 用户已同意关闭旧窗重开；旧 helper 及其 Chrome 子树已退出、租约已释放，其他 Chrome 未动。新 helper/session
  已使用 `f4098b5c98c7b407e86239826f82fa827ce5ab63` 源码、canonical 账号/Profile；身份昵称匹配通过。本文不记录
  任何昵称、账号 UUID、平台 UID 或路径敏感值。
- 新页面为原生视口：`page.viewport_size=None`，页面 `innerWidth/innerHeight=1520/853`，窗口外框为
  `1382/918`。当前在长文 landing 页；“新创作”和“草稿箱”入口均在屏内。尚未进入编辑器，因此“下一步”不是
  当前页面控件，不能写成已验收下一步现场可见。
- 未点击新创作、未上传、未保存、未发布；云端草稿/仅自己可见能力仍未验证；旧文章是否持久化尚未回读，不能写成
  已恢复。

### 2026-09-07 小红书完整 Word 发布前检查（本轮新增，仅记录真实结果）

- 代码基线为 `fcc153654be3c49d3bc2a912fbff3e15ac07b4d8`；指定完整 Word 的离线内容契约已通过：35 字标题、17 个普通正文块、5 个 H2、7 张图片、29 个逻辑块。
- 真实账号复用了 canonical 账号库，没有复制 Cookie；一次身份 ID 与昵称匹配、空编辑器确认、H2 指纹与正文图片指纹匹配均通过。本文不记录任何账号 UUID、昵称或平台 UID。
- 仅执行一次正文填充：`title_readback_ok`、`text_ok=true`、7/7 图片上传、`media_status=completed`。
- 仅执行一次长文排版：`success=true`、`safe_to_continue=true`、7/7 图片加载；图文顺序与 H2 样式稳定回读通过，排版预览接口 2xx 门通过。
- 排版完成后的只读复核阶段 `navigated_to_editor=false`（不代表首次进入编辑器未导航）；页面已处于排版结果态，原编辑器定位不适用，不能把旧定位返回的图片数 0/标题数 0解释为失败。排版后截图抓取失败仅属于截图问题；未改文章状态、未重试。尚未点击「下一步」、尚未「暂存离开」、尚未发布；“仅自己可见”能力仍未确认。
- 云端草稿门禁与全局公开发布开关均未修改；生产 Web/MCP 未启停；临时辅助脚本只在系统临时目录，不进入 Git。
- 下一步等待用户在当前排版结果页点击一次「下一步」并提供发布前设置截图；明确禁止发布。不得把本次结果写成已实现云端草稿、已发布或封面已确认。
