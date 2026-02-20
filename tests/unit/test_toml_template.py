"""Tests for toml_template: ensure_toml_config and migrate_env_to_toml."""

import tomlkit

from src.config.toml_template import (
    _TEMPLATE,
    ensure_toml_config,
    migrate_env_to_toml,
)

# ── ensure_toml_config ────────────────────────────────────────────────────────


def test_ensure_creates_template_when_absent(tmp_path):
    toml_path = tmp_path / "settings.toml"
    ensure_toml_config(toml_path)
    assert toml_path.exists()
    content = toml_path.read_text(encoding="utf-8")
    # Must contain recognisable sections from the template
    assert "[claude]" in content
    assert "[features]" in content
    assert "[memory]" in content


def test_ensure_is_noop_when_file_exists(tmp_path):
    toml_path = tmp_path / "settings.toml"
    toml_path.write_text("# custom\n[claude]\nclaude_max_turns = 99\n", encoding="utf-8")
    ensure_toml_config(toml_path)
    content = toml_path.read_text(encoding="utf-8")
    assert "# custom" in content  # not overwritten


def test_ensure_creates_parent_dirs(tmp_path):
    toml_path = tmp_path / "nested" / "dir" / "settings.toml"
    ensure_toml_config(toml_path)
    assert toml_path.exists()


def test_template_is_valid_toml():
    doc = tomlkit.parse(_TEMPLATE)
    assert "claude" in doc
    assert "features" in doc
    assert "checkins" in doc


# ── migrate_env_to_toml ───────────────────────────────────────────────────────


def test_migrate_returns_false_when_toml_already_exists(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("CLAUDE_MODEL=claude-opus-4-6\n", encoding="utf-8")
    toml_path = tmp_path / "settings.toml"
    toml_path.write_text("# existing\n", encoding="utf-8")
    migrated = tmp_path / ".env.migrated"

    result = migrate_env_to_toml(env_path, toml_path, migrated)
    assert result is False
    assert not migrated.exists()


def test_migrate_returns_false_when_env_absent(tmp_path):
    env_path = tmp_path / ".env"  # does not exist
    toml_path = tmp_path / "settings.toml"
    migrated = tmp_path / ".env.migrated"

    result = migrate_env_to_toml(env_path, toml_path, migrated)
    assert result is False


def test_migrate_creates_toml_and_renames_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("CLAUDE_MAX_TURNS=25\nENABLE_MEMORY=true\n", encoding="utf-8")
    toml_path = tmp_path / "settings.toml"
    migrated = tmp_path / ".env.migrated"

    result = migrate_env_to_toml(env_path, toml_path, migrated)

    assert result is True
    assert toml_path.exists()
    assert migrated.exists()
    assert not env_path.exists()


def test_migrate_coerces_int(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("CLAUDE_MAX_TURNS=30\n", encoding="utf-8")
    toml_path = tmp_path / "settings.toml"
    migrated = tmp_path / ".env.migrated"

    migrate_env_to_toml(env_path, toml_path, migrated)

    doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
    # Unwrap tomlkit integer
    val = doc["claude"]["claude_max_turns"]
    if hasattr(val, "unwrap"):
        val = val.unwrap()
    assert val == 30


def test_migrate_coerces_bool(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("ENABLE_MEMORY=true\n", encoding="utf-8")
    toml_path = tmp_path / "settings.toml"
    migrated = tmp_path / ".env.migrated"

    migrate_env_to_toml(env_path, toml_path, migrated)

    doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
    val = doc["memory"]["enable_memory"]
    if hasattr(val, "unwrap"):
        val = val.unwrap()
    assert val is True


def test_migrate_coerces_int_list(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("ALLOWED_USERS=111,222,333\n", encoding="utf-8")
    toml_path = tmp_path / "settings.toml"
    migrated = tmp_path / ".env.migrated"

    migrate_env_to_toml(env_path, toml_path, migrated)

    doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
    val = doc["required"]["allowed_users"]
    if hasattr(val, "unwrap"):
        val = val.unwrap()
    assert val == [111, 222, 333]


def test_migrate_ignores_unknown_env_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("NONEXISTENT_KEY=foo\n", encoding="utf-8")
    toml_path = tmp_path / "settings.toml"
    migrated = tmp_path / ".env.migrated"

    # Should not raise
    result = migrate_env_to_toml(env_path, toml_path, migrated)
    assert result is True


def test_migrate_skips_empty_env_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("CLAUDE_MODEL=\n", encoding="utf-8")
    toml_path = tmp_path / "settings.toml"
    migrated = tmp_path / ".env.migrated"

    migrate_env_to_toml(env_path, toml_path, migrated)

    # Should still produce valid TOML, template default retained
    doc = tomlkit.parse(toml_path.read_text(encoding="utf-8"))
    # Template default for claude_model is "claude-sonnet-4-5"
    val = doc["claude"]["claude_model"]
    if hasattr(val, "unwrap"):
        val = val.unwrap()
    assert val == "claude-sonnet-4-5"
