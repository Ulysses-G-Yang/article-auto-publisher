# ArticleOps CoreUI runtime source

This directory contains the minimal, reproducible CoreUI source used by
ArticleOps. It is derived from CoreUI Free Bootstrap Admin Template `v5.6.0`
at upstream commit `da2c89f5e71a762fb46a3583f42d5f740d965b1d` and keeps
`@coreui/coreui` pinned to `5.9.0`.

The complete, unmodified upstream template is retained in the remote archive
branch `origin/archive/coreui-full-v5.6.0`.

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
