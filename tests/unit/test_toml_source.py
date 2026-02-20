"""Tests for TomlSettingsSource and supporting utilities."""

import tomlkit

from src.config.settings import Settings
from src.config.toml_source import FIELD_TO_SECTION, SECTION_MAP, TomlSettingsSource, _unwrap

# ── _unwrap ────────────────────────────────────────────────────────────────────


def test_unwrap_plain_value():
    assert _unwrap(42) == 42
    assert _unwrap("hello") == "hello"
    assert _unwrap(True) is True


def test_unwrap_tomlkit_integer():
    doc = tomlkit.parse("[s]\nx = 7\n")
    val = doc["s"]["x"]
    assert _unwrap(val) == 7


def test_unwrap_tomlkit_string():
    doc = tomlkit.parse('[s]\nx = "hello"\n')
    val = doc["s"]["x"]
    assert _unwrap(val) == "hello"


def test_unwrap_tomlkit_list():
    doc = tomlkit.parse("[s]\nx = [1, 2, 3]\n")
    val = doc["s"]["x"]
    result = _unwrap(val)
    assert result == [1, 2, 3]


def test_unwrap_nested_list():
    raw_list = [tomlkit.integer(1), tomlkit.integer(2)]
    assert _unwrap(raw_list) == [1, 2]


# ── SECTION_MAP / FIELD_TO_SECTION consistency ────────────────────────────────


def test_field_to_section_is_inverse_of_section_map():
    for section, fields in SECTION_MAP.items():
        for field in fields:
            assert FIELD_TO_SECTION[field] == section


def test_no_duplicate_fields_across_sections():
    seen: set[str] = set()
    for fields in SECTION_MAP.values():
        for f in fields:
            assert f not in seen, f"Duplicate field: {f}"
            seen.add(f)


def test_all_sections_are_non_empty():
    for section, fields in SECTION_MAP.items():
        assert fields, f"Section '{section}' has no fields"


# ── TomlSettingsSource ────────────────────────────────────────────────────────


def test_source_returns_empty_when_no_file(tmp_path):
    missing = tmp_path / "settings.toml"
    src = TomlSettingsSource(Settings, toml_path=missing)
    assert src() == {}


def test_source_reads_simple_values(tmp_path):
    toml_path = tmp_path / "settings.toml"
    toml_path.write_text(
        '[claude]\nclaude_model = "claude-opus-4-6"\nclaude_max_turns = 20\n',
        encoding="utf-8",
    )
    src = TomlSettingsSource(Settings, toml_path=toml_path)
    data = src()
    assert data["claude_model"] == "claude-opus-4-6"
    assert data["claude_max_turns"] == 20


def test_source_reads_bool(tmp_path):
    toml_path = tmp_path / "settings.toml"
    toml_path.write_text("[features]\nenable_mcp = true\nagentic_mode = false\n", encoding="utf-8")
    src = TomlSettingsSource(Settings, toml_path=toml_path)
    data = src()
    assert data["enable_mcp"] is True
    assert data["agentic_mode"] is False


def test_source_skips_empty_strings(tmp_path):
    toml_path = tmp_path / "settings.toml"
    toml_path.write_text('[required]\ntelegram_bot_token = ""\n', encoding="utf-8")
    src = TomlSettingsSource(Settings, toml_path=toml_path)
    data = src()
    assert "telegram_bot_token" not in data


def test_source_skips_empty_lists(tmp_path):
    toml_path = tmp_path / "settings.toml"
    toml_path.write_text("[required]\nallowed_users = []\n", encoding="utf-8")
    src = TomlSettingsSource(Settings, toml_path=toml_path)
    data = src()
    assert "allowed_users" not in data


def test_source_reads_int_list(tmp_path):
    toml_path = tmp_path / "settings.toml"
    toml_path.write_text("[required]\nallowed_users = [111, 222]\n", encoding="utf-8")
    src = TomlSettingsSource(Settings, toml_path=toml_path)
    data = src()
    assert data["allowed_users"] == [111, 222]


def test_source_ignores_unknown_sections(tmp_path):
    toml_path = tmp_path / "settings.toml"
    toml_path.write_text("[unknown_section]\nfoo = 42\n", encoding="utf-8")
    src = TomlSettingsSource(Settings, toml_path=toml_path)
    assert src() == {}


def test_source_reads_multiple_sections(tmp_path):
    toml_path = tmp_path / "settings.toml"
    toml_path.write_text(
        "[claude]\nclaude_max_turns = 15\n[memory]\nenable_memory = true\n",
        encoding="utf-8",
    )
    src = TomlSettingsSource(Settings, toml_path=toml_path)
    data = src()
    assert data["claude_max_turns"] == 15
    assert data["enable_memory"] is True


def test_monkeypatched_toml_path_is_used(monkeypatch, tmp_path):
    """Verify the autouse fixture pattern works — patched TOML_PATH is picked up."""
    import src.config.toml_source as _mod

    custom = tmp_path / "custom.toml"
    custom.write_text("[claude]\nclaude_max_turns = 99\n", encoding="utf-8")
    monkeypatch.setattr(_mod, "TOML_PATH", custom)

    src = TomlSettingsSource(Settings)  # no toml_path kwarg → uses module-level TOML_PATH
    assert src()["claude_max_turns"] == 99
