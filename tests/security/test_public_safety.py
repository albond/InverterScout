"""Checks that prevent private deployment data from entering the public tree."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {
    ".bat",
    ".example",
    ".html",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
EXCLUDED_DIRECTORIES = {".git", ".idea", ".pytest_cache", ".venv", "__pycache__"}


def public_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if EXCLUDED_DIRECTORIES.intersection(path.relative_to(ROOT).parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name == ".env.example":
            yield path


def test_no_legacy_product_name_or_private_checkout_path():
    legacy_name = "Lux" + "Monitor"
    private_checkout_prefix = "/" + "Users" + "/"
    for path in public_text_files():
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert legacy_name not in content, path
        assert private_checkout_prefix not in content, path


def test_no_cyrillic_in_code_or_templates():
    cyrillic = re.compile(r"[\u0400-\u04ff]")
    code_files = [
        path
        for path in ROOT.rglob("*.py")
        if not any(part.startswith(".") for part in path.relative_to(ROOT).parts)
    ]
    code_files.extend(ROOT.glob("src/inverterscout/resources/templates/*.html"))
    code_files.extend(ROOT.rglob("*.sh"))
    code_files.extend(ROOT.glob("*.bat"))
    for path in code_files:
        assert not cyrillic.search(path.read_text(encoding="utf-8")), path


def test_no_credential_shaped_values_in_public_configuration():
    bot_token = re.compile(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b")
    for name in ("docker-compose.yml", ".env.example", ".dockerignore", "Dockerfile"):
        content = (ROOT / name).read_text(encoding="utf-8")
        assert not bot_token.search(content), name
        assert "TAPO_PASSWORD=" not in content, name
        assert "TUYA_ACCESS_SECRET=" not in content, name


def test_runtime_data_is_excluded_from_future_source_packages():
    ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
    docker_context = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "data/" in ignore_rules
    assert ".env" in ignore_rules
    assert docker_context.startswith("*\n")
    assert "!.env" not in docker_context
    assert "!data" not in docker_context
