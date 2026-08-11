# Design QA — CoreUI + GridStack 数据中心

## Comparison target

- Source visual truth: `https://coreui.io/demos/bootstrap/latest/free/?theme=light`
- Source capture: `data/design-qa/coreui-live-reference-1280x720.png`
- Implementation URL: `http://127.0.0.1:5000/data-center/`
- Final implementation capture: `data/design-qa/coreui-implementation-final-1280x720.png`
- Full-view comparison: `data/design-qa/comparison-final.png`
- Focused shell/KPI comparison: `data/design-qa/comparison-focused-shell-kpi.png`
- Viewport and state: 1280 × 720 CSS px, light theme, dashboard overview, default layout.
- Pixel dimensions: both captures are 1280 × 720 px. No density resampling was required.

## Findings

- No actionable P0, P1, or P2 differences remain.
- [P3] The source KPI cards contain decorative trend charts, while the implementation uses
  operational labels and official CoreUI Icons. This is an intentional product constraint:
  the current API has point-in-time counts but no trustworthy time-series data, so the UI does
  not invent a trend. Add real charts only after snapshot history is available.

## Required fidelity surfaces

- Fonts and typography: CoreUI/system font proportions are retained, with Microsoft YaHei and
  PingFang SC fallbacks for Chinese. Heading, navigation, KPI, metadata, and table weights remain
  distinct and legible; long task titles are truncated in the table with their full value kept in
  the title attribute.
- Spacing and layout rhythm: the 256 px dark sidebar, 64 px header, light-gray canvas, card grid,
  compact radii, subtle elevation, and content alignment visibly match the reference structure.
  Desktop has no horizontal overflow. The 390 × 844 check reflows KPI cards into one column and
  expands the GridStack summary module to 528 px so cards do not overlap or scroll internally.
- Colors and visual tokens: the navy sidebar, violet active state, neutral canvas, and
  violet/blue/amber/red KPI sequence map to CoreUI tokens. Success, warning, and failure states
  use semantic colors with text labels rather than color alone.
- Image quality and asset fidelity: the page does not require photography or illustration.
  Visible UI icons use the official CoreUI Icons Free font; no custom SVG, emoji, placeholder
  raster, or approximate CSS-drawn icon replaces a source asset.
- Copy and content: all labels are rewritten for the real publishing and collection workflow.
  Counts come from the existing API. Empty article-mapping and collection-run states remain
  explicit and do not fabricate data.

## Full-view and focused evidence

- Full view: `comparison-final.png` confirms the same enterprise-admin composition: fixed dark
  navigation, white utility header, breadcrumb, colored KPI row, and bordered main work area.
- Focused region: `comparison-focused-shell-kpi.png` confirms sidebar width, header density,
  navigation hierarchy, active-state treatment, card palette, card spacing, and number hierarchy.
- Application-specific tables and the module settings drawer have no direct source equivalent;
  they use CoreUI table, badge, offcanvas, button, and form-control patterns and were checked in
  the rendered app.

## Comparison history

1. Pass 1: desktop comparison found a P2 table-density issue: long task titles forced important
   columns outside the immediately readable table area. Fix: added a fixed task-table layout,
   explicit column widths, and ellipsis behavior. Post-fix evidence: `comparison-pass-2.png`.
2. Pass 2: responsive check at 390 × 844 found a P2 issue: the four KPI cards were constrained to
   a 176 px GridStack module and created an internal scroll area. Fix: added responsive GridStack
   heights of 4 rows below 992 px and 6 rows below 576 px. Post-fix evidence showed a 528 px
   mobile summary module with all four cards visible and no horizontal overflow.
3. Final pass: 1280 × 720 desktop capture and 390 × 844 mobile verification found no remaining
   P0/P1/P2 issues. Browser console warnings/errors: none.

## Interaction and implementation checks

- Edit mode enters and exits correctly.
- GridStack module visibility toggles remove and restore widgets.
- Default-layout restore leaves all five modules visible.
- Layout save exits edit mode and persists through the page UI.
- Task search filters real task rows.
- Sidebar collapse, light/dark theme toggle, manual refresh, and 15-second refresh are wired.
- Primary API, vendor assets, and local page return HTTP 200 from the existing port 5000 service.

## Implementation checklist

- [x] Match CoreUI shell, palette, spacing, and navigation hierarchy.
- [x] Use official CoreUI and GridStack production assets with licenses.
- [x] Preserve real API data and explicit empty states.
- [x] Verify configurable modules, persistence, search, and responsive behavior.
- [x] Run automated and browser-rendered regression checks.

final result: passed
