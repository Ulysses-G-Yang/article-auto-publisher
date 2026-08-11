# Third-party frontend notices

The data-center frontend is a business-specific adaptation of the CoreUI Free Bootstrap Admin
Template, version 5.6.0. Visual source: `git@github.com:coreui/coreui-free-bootstrap-admin-template.git`.
Its MIT license is stored in
`src/article_mvp/web/static/vendor/coreui/LICENSE-template.txt`.

The implementation vendors these unmodified production assets so the local dashboard does not
depend on a public CDN:

- CoreUI for Bootstrap 5, version 5.9.0. Source: `git@github.com:coreui/coreui.git`.
  License: MIT. The full license text is stored in
  `src/article_mvp/web/static/vendor/coreui/LICENSE.txt`.
- CoreUI Icons Free, version 3.1.0. Source:
  `git@github.com:coreui/coreui-icons.git`. Font, icon, and code licenses are documented in
  `src/article_mvp/web/static/vendor/coreui-icons/LICENSE.txt`.
- GridStack, version 13.1.2. Source: `git@github.com:gridstack/gridstack.js.git`.
  License: MIT. The full license text is stored in
  `src/article_mvp/web/static/vendor/gridstack/LICENSE.txt`.

Application-specific HTML, CSS, and JavaScript in `src/article_mvp/web/` are maintained by
this project. CoreUI provides the enterprise admin component system and visual foundation;
GridStack provides dashboard drag, resize, and serialization behavior.
