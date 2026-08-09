"""Contract tests for the shared responsive interface system."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RESOURCES = ROOT / "src" / "inverterscout" / "resources"
TEMPLATES = RESOURCES / "templates"
STATIC = RESOURCES / "static"
SCREENSHOTS = ROOT / "docs" / "screenshots"


def test_every_full_page_uses_shared_assets_without_inline_styles_or_scripts():
    full_pages = ["base.html", "setup.html", "setup_complete.html"]
    for name in full_pages:
        content = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "/static/css/app.css" in content, name
        assert "/static/js/theme-init.js" in content, name
        assert "<style" not in content, name
        assert "style=" not in content, name
        assert not re.search(r"<script(?![^>]*\bsrc=)", content), name


def test_design_assets_are_local_and_packaged():
    expected_assets = [
        STATIC / "css" / "app.css",
        STATIC / "images" / "brand.svg",
        STATIC / "images" / "icons.svg",
        STATIC / "js" / "app.js",
        STATIC / "js" / "admin.js",
        STATIC / "js" / "devices.js",
        STATIC / "js" / "setup.js",
        STATIC / "js" / "theme-init.js",
    ]
    assert all(path.is_file() and path.stat().st_size > 0 for path in expected_assets)

    package_config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for pattern in (
        "resources/static/css/*.css",
        "resources/static/images/*.svg",
        "resources/static/js/*.js",
    ):
        assert pattern in package_config


def test_every_locale_has_accessible_theme_and_navigation_labels():
    required = {"common.navigation", "common.theme_light", "common.theme_dark"}
    for path in sorted((RESOURCES / "locales").glob("*.json")):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        assert required <= catalog.keys(), path.name
        assert all(catalog[key].strip() for key in required), path.name


def test_interface_css_covers_themes_responsive_navigation_and_accessibility():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert 'html[data-theme="dark"]' in css
    assert "inset-block-end: max(12px, env(safe-area-inset-bottom))" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (prefers-reduced-transparency: reduce)" in css
    assert "@media (prefers-contrast: more)" in css


def test_populated_interfaces_keep_motion_and_form_actions_regression_safe():
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    devices_js = (STATIC / "js" / "devices.js").read_text(encoding="utf-8")
    app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    admin_js = (STATIC / "js" / "admin.js").read_text(encoding="utf-8")

    assert "@view-transition" in css
    assert "@keyframes device-state-morph" in css
    assert "@keyframes signal-breathe" in css
    assert "@keyframes surface-arrive" in css
    assert 'form.getAttribute("action")' in devices_js
    assert "fetch(form.action" not in devices_js
    assert "viewTransitionName" in admin_js
    assert "--card-tilt-x" in css
    assert "--card-tilt-x" in app_js
    assert "(hover: hover) and (pointer: fine)" in app_js
    assert "is-pointer-hovered" in app_js
    assert 'card.addEventListener("pointerleave"' in app_js


def test_readme_screenshots_are_real_images_without_embedded_identity_metadata():
    expected = {
        "dashboard-dark.jpg": "jpeg",
        "dashboard-light.jpg": "jpeg",
        "dashboard-mobile-light.jpg": "jpeg",
        "setup-dark.jpg": "jpeg",
        "devices-dark.jpg": "jpeg",
        "access-light.jpg": "jpeg",
        "devices-mobile-dark.jpg": "jpeg",
    }
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert {path.name for path in SCREENSHOTS.glob("*")} == expected.keys()
    for name, image_type in expected.items():
        image = (SCREENSHOTS / name).read_bytes()
        if image_type == "jpeg":
            assert image.startswith(b"\xff\xd8\xff"), name
            assert b"Exif\x00\x00" not in image, name
        assert f"docs/screenshots/{name}" in readme, name
