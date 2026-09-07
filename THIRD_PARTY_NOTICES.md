# Third-party notices

## CoreUI Free Bootstrap Admin Template

- Upstream repository: `git@github.com:coreui/coreui-free-bootstrap-admin-template.git`
- Upstream release: `v5.6.0`
- Vendored commit: `da2c89f5e71a762fb46a3583f42d5f740d965b1d`
- Framework runtime: `@coreui/coreui` `5.9.0` (exactly pinned in the vendored build manifest and lockfile)
- License: MIT, copyright 2026 creativeLabs Łukasz Holeczek

The active `frontend/coreui-free-bootstrap-admin-template/` directory keeps the
minimum reproducible CoreUI build source required by ArticleOps, including its
MIT license and the ArticleOps-pinned dependency manifest and lockfile. The
pre-trim ArticleOps CoreUI v5.6.0 vendored snapshot is retained in
`archive/2026-09-07/archive-coreui-full-v5.6.0`; that snapshot is not a complete,
unmodified copy of the official upstream repository. The authoritative
official v5.6.0 source is the upstream SSH repository at commit
`da2c89f5e71a762fb46a3583f42d5f740d965b1d`.

Runtime assets in `web/static/vendor/coreui-template/` are generated from the
active minimal source so the Python service does not require Node.js in
production.

ArticleOps templates and styles are derivative application work. Their changes
are documented in `docs/frontend/COREUI_UPSTREAM.md`.

## CoreUI Icons Free

CoreUI Icons Free 3.1.0 is sourced from
`git@github.com:coreui/coreui-icons.git`. License details are retained in
`web/static/vendor/coreui-icons/LICENSE.txt`.
