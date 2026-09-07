# ArticleOps CoreUI runtime source

This directory contains the minimal, reproducible CoreUI source used by
ArticleOps. It is derived from CoreUI Free Bootstrap Admin Template `v5.6.0`
at upstream commit `da2c89f5e71a762fb46a3583f42d5f740d965b1d` and keeps
`@coreui/coreui` pinned to `5.9.0` and SimpleBar pinned to `6.3.3` in the
ArticleOps dependency manifest and lockfile.

The pre-trim ArticleOps CoreUI v5.6.0 vendored snapshot is retained in the
annotated archive tag `archive/2026-09-07/archive-coreui-full-v5.6.0`. That snapshot is not
a complete, unmodified copy of the official upstream repository. The
authoritative official tag `v5.6.0` source is available from
`git@github.com:coreui/coreui-free-bootstrap-admin-template.git` at commit
`da2c89f5e71a762fb46a3583f42d5f740d965b1d`.

The active tree intentionally excludes upstream demonstration pages, Pug
templates, sample images, the demonstration icon catalogue, and chart/widget
scripts. ArticleOps does not load those files at runtime.

## Build

```powershell
npm ci --ignore-scripts --no-audit
npm run build
```

The build produces only the five files required for the ArticleOps runtime:

```text
dist/LICENSE
dist/css/style.min.css
dist/js/coreui.bundle.min.js
dist/simplebar/simplebar.css
dist/simplebar/simplebar.min.js
```

To confirm that a local build matches the checked-in production vendor files,
run this command from this directory inside the ArticleOps repository:

```powershell
npm run verify-vendor
```

Production serves the checked-in files under
`web/static/vendor/coreui-template/`; it does not require Node.js.

License: MIT. See `LICENSE` and `../../docs/frontend/COREUI_UPSTREAM.md`.
