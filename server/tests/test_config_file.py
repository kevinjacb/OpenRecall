"""Tests for the TOML config-file loader.

The config file is a persistent, file-based alternative to exporting a wall
of ``OPENRECALL_*`` env vars every launch. It is read once at startup and
merged into the env with real env vars winning (one-off overrides / CI keep
working). Sectioned keys flatten to ``OPENRECALL_{SECTION}_{KEY}``; top-level
keys are used verbatim as an escape hatch.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from openrecall_server.config_file import (
    apply_config_file,
    flatten_config,
    load_config_file,
)


# ---- flatten -----------------------------------------------------------------


def test_sectioned_key_flattens_to_openrecall_env():
    flat = flatten_config({"denoise": {"enabled": True}})
    assert flat == {"OPENRECALL_DENOISE_ENABLED": "true"}


def test_multiple_sections_each_get_their_prefix():
    flat = flatten_config({
        "asr": {"backend": "whisper"},
        "command": {"detector_enabled": True, "require_wearer": False},
    })
    assert flat == {
        "OPENRECALL_ASR_BACKEND": "whisper",
        "OPENRECALL_COMMAND_DETECTOR_ENABLED": "true",
        "OPENRECALL_COMMAND_REQUIRE_WEARER": "false",
    }


def test_toplevel_key_used_verbatim():
    """A key not under any table is taken literally — an escape hatch for any
    env var that does not fit the OPENRECALL_{SECTION}_{KEY} shape."""
    flat = flatten_config({"OPENRECALL_PARAKEET_MODEL": "tdt-small"})
    assert flat == {"OPENRECALL_PARAKEET_MODEL": "tdt-small"}


def test_bool_int_float_stringify():
    flat = flatten_config({
        "s": {
            "b": True, "bf": False, "i": 5, "f": 0.6, "t": "whisper",
        }
    })
    assert flat == {
        "OPENRECALL_S_B": "true", "OPENRECALL_S_BF": "false",
        "OPENRECALL_S_I": "5", "OPENRECALL_S_F": "0.6",
        "OPENRECALL_S_T": "whisper",
    }


def test_list_becomes_comma_joined_string():
    """Phrase lists and hallucination phrases are comma-split by the config
    parser; a TOML array is the natural way to write one."""
    flat = flatten_config({"whisper": {"hallucination_phrases": ["hello", "thank you", "hi"]}})
    assert flat == {"OPENRECALL_WHISPER_HALLUCINATION_PHRASES": "hello,thank you,hi"}


def test_nested_table_is_rejected():
    """The only map-typed env var (command phrases) uses a delimited string
    form; nested TOML tables would be ambiguous with sections, so reject."""
    with pytest.raises(ValueError, match="nested table"):
        flatten_config({"command": {"phrases": {"reminder": "remind me"}}})


def test_unknown_value_type_rejected():
    with pytest.raises(TypeError):
        flatten_config({"s": {"x": b"bytes"}})


# ---- load_config_file --------------------------------------------------------


def test_missing_file_is_empty_no_op(tmp_path):
    """No config file is the common case and must not be an error."""
    assert load_config_file(tmp_path / "absent.toml") == {}


def test_load_real_toml_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[asr]\nbackend = "parakeet"\n\n[denoise]\nenabled = true\n', encoding="utf-8",
    )
    assert load_config_file(p) == {
        "OPENRECALL_ASR_BACKEND": "parakeet",
        "OPENRECALL_DENOISE_ENABLED": "true",
    }


def test_malformed_toml_raises(tmp_path):
    """A typo'd config should fail loudly, not silently fall back to defaults."""
    p = tmp_path / "bad.toml"
    p.write_text("[asr\nbackend = ", encoding="utf-8")
    with pytest.raises(Exception):
        load_config_file(p)


# ---- apply_config_file (precedence: env wins) --------------------------------


def test_apply_merges_into_env_with_setdefault(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[denoise]\nenabled = true\n[asr]\nbackend = "parakeet"\n', encoding="utf-8")
    env = {"OPENRECALL_ASR_BACKEND": "whisper"}  # already set -> must win
    applied = apply_config_file(p, env=env)
    assert applied == {
        "OPENRECALL_DENOISE_ENABLED": "true",
        "OPENRECALL_ASR_BACKEND": "parakeet",
    }
    # Real env var wins; the file fills in the unset one.
    assert env == {
        "OPENRECALL_ASR_BACKEND": "whisper",   # preserved (not overwritten)
        "OPENRECALL_DENOISE_ENABLED": "true",   # added from the file
    }


def test_apply_missing_file_is_no_op_on_env(tmp_path):
    env = {"OPENRECALL_ASR_BACKEND": "whisper"}
    assert apply_config_file(tmp_path / "absent.toml", env=env) == {}
    assert env == {"OPENRECALL_ASR_BACKEND": "whisper"}


def test_apply_defaults_to_real_environ(tmp_path, monkeypatch):
    """Without an explicit `env=` it writes into os.environ (the production
    path). Use a throwaway key + clean it up so the real env is untouched."""
    p = tmp_path / "config.toml"
    p.write_text('[denoise]\nenabled = true\n', encoding="utf-8")
    monkeypatch.delenv("OPENRECALL_DENOISE_ENABLED", raising=False)
    try:
        apply_config_file(p)
        assert os.environ["OPENRECALL_DENOISE_ENABLED"] == "true"
    finally:
        os.environ.pop("OPENRECALL_DENOISE_ENABLED", None)


def test_example_config_keys_all_map_to_real_env_vars():
    """Every key in config.example.toml — including commented-out ones —
    must flatten to a var the source actually reads."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]           # server/
    example = root / "config.example.toml"

    section, declared = None, {}
    for line in example.read_text().splitlines():
        s = line.strip()
        if m := re.match(r"^\[([a-z_]+)\]", s):
            section = m.group(1)
            continue
        if m := re.match(r"^#?\s*([a-z_0-9]+)\s*=", s):
            key = m.group(1)
            declared[f"OPENRECALL_{section.upper()}_{key.upper()}"
                     if section else key] = (section, key)

    real = set()
    for path in list((root / "src").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
        real |= set(re.findall(r'"(OPENRECALL_[A-Z_0-9]+)"', path.read_text()))

    dead = sorted(set(declared) - real)
    assert not dead, f"config.example.toml keys map to nothing: {dead}"