# Third-party notices

## CoreUI Free Bootstrap Admin Template

- Upstream repository: `git@github.com:coreui/coreui-free-bootstrap-admin-template.git`
- Upstream release: `v5.6.0`
- Vendored commit: `da2c89f5e71a762fb46a3583f42d5f740d965b1d`
- Framework runtime: `@coreui/coreui` `5.9.0` (exactly pinned in the vendored build manifest and lockfile)
- License: MIT, copyright 2026 creativeLabs Łukasz Holeczek

The unmodified upstream source, build configuration, lockfile, and license are
kept under `frontend/coreui-free-bootstrap-admin-template/`. Runtime assets in
`web/static/vendor/coreui-template/` are generated from that source so the
Python service does not require Node.js in production.

ArticleOps templates and styles are derivative application work. Their changes
are documented in `docs/frontend/COREUI_UPSTREAM.md`.

## Existing dashboard libraries

- CoreUI Icons Free 3.1.0. Source:
  `git@github.com:coreui/coreui-icons.git`. License details are retained in
  `src/article_mvp/web/static/vendor/coreui-icons/LICENSE.txt` and
  `web/static/vendor/coreui-icons/LICENSE.txt`.
- GridStack 13.1.2. Source:
  `git@github.com:gridstack/gridstack.js.git`. The MIT license is retained in
  `src/article_mvp/web/static/vendor/gridstack/LICENSE.txt`.
