import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COREUI_ROOT = PROJECT_ROOT / "frontend" / "coreui-free-bootstrap-admin-template"
RUNTIME_ROOT = PROJECT_ROOT / "web" / "static" / "vendor" / "coreui-template"


def test_coreui_manifest_only_keeps_articleops_runtime_dependencies() -> None:
    package = json.loads((COREUI_ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["version"] == "5.6.0"
    assert package["private"] is True
    assert package["dependencies"] == {
        "@coreui/coreui": "5.9.0",
        "simplebar": "6.3.3",
    }
    assert set(package["scripts"]) == {
        "build",
        "clean",
        "css",
        "css-compile",
        "css-prefix",
        "css-minify",
        "css-clean",
        "runtime",
        "verify",
        "verify-vendor",
    }


def test_coreui_demo_sources_are_not_kept_in_the_active_tree() -> None:
    assert not (COREUI_ROOT / "src" / "assets").exists()
    assert not (COREUI_ROOT / "src" / "js").exists()
    assert not (COREUI_ROOT / "src" / "pug").exists()
    assert not (COREUI_ROOT / "src" / "views").exists()
    assert not (COREUI_ROOT / "src" / "scss" / "examples.scss").exists()


def test_required_coreui_source_and_runtime_assets_remain_available() -> None:
    required_sources = {
        COREUI_ROOT / "LICENSE",
        COREUI_ROOT / "package.json",
        COREUI_ROOT / "package-lock.json",
        COREUI_ROOT / "build" / "postcss.config.mjs",
        COREUI_ROOT / "build" / "runtime-assets.mjs",
        COREUI_ROOT / "src" / "scss" / "style.scss",
        COREUI_ROOT / "src" / "scss" / "vendors" / "simplebar.scss",
    }
    required_runtime = {
        RUNTIME_ROOT / "LICENSE",
        RUNTIME_ROOT / "css" / "style.min.css",
        RUNTIME_ROOT / "js" / "coreui.bundle.min.js",
        RUNTIME_ROOT / "simplebar" / "simplebar.css",
        RUNTIME_ROOT / "simplebar" / "simplebar.min.js",
    }

    assert all(path.is_file() for path in required_sources)
    assert all(path.is_file() for path in required_runtime)

    assert not (RUNTIME_ROOT / "css" / "style.min.css.map").exists()
    assert not (RUNTIME_ROOT / "js" / "coreui.bundle.min.js.map").exists()
    assert "sourceMappingURL" not in (
        RUNTIME_ROOT / "css" / "style.min.css"
    ).read_text(encoding="utf-8")
    assert "sourceMappingURL" not in (
        RUNTIME_ROOT / "js" / "coreui.bundle.min.js"
    ).read_text(encoding="utf-8")
