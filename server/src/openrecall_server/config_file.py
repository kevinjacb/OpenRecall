"""Load a TOML config file and flatten it into ``OPENRECALL_*`` env-var entries.

A persistent, file-based alternative to exporting a wall of ``OPENRECALL_*``
environment variables every time the gateway is launched. The file is read
once at startup and merged into ``os.environ`` with **real env vars winning**
(``setdefault``), so one-off overrides such as
``OPENRECALL_DENOISE_ENABLED=1 python scripts/run_gateway.py`` and CI keep
working unchanged — the file is the convenient default, not a new source of
truth that overrides explicit settings.

Sectioned keys flatten by prefix: ``[denoise] enabled = true`` becomes
``OPENRECALL_DENOISE_ENABLED=true``; ``[asr] backend = "whisper"`` becomes
``OPENRECALL_ASR_BACKEND=whisper``. The rule is
``OPENRECALL_{SECTION}_{KEY}`` (both uppercased), which matches every runtime
knob the gateway reads when the section name matches the env-var's middle
component (``[asr]``, ``[denoise]``, ``[whisper]``, ``[command]``,
``[confidence]``, ``[llm]``, ``[vlm]``, ``[vision]``, ``[speaker]``,
``[embed]``, ``[parakeet]``, ``[sentence]``, ``[proactive]``, ``[rate_limit]``,
``[log]``). Top-level keys are used verbatim — an escape hatch for any env var
that does not fit that shape.

TOML ``bool``/``int``/``float`` become their string form (the config parser
already parses env strings); a TOML array becomes a comma-joined string
(matching the comma-split phrase-list parsers). The one map-typed env var
(command phrases, ``type:phrase;...``) is written as that delimited string,
not a nested table — nested tables are rejected to stay unambiguous with
sections.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Mapping


def _stringify(value: Any) -> str:
    if isinstance(value, bool):  # bool is an int subclass — check first
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Phrase lists / hallucination phrases are comma-split downstream.
        return ",".join(_stringify(v) for v in value)
    raise TypeError(
        f"config value of type {type(value).__name__} is not a scalar or list",
    )


def flatten_config(data: Mapping[str, Any]) -> dict[str, str]:
    """Flatten a parsed TOML mapping into a flat env-var dict.

    Sectioned keys -> ``OPENRECALL_{SECTION}_{KEY}`` (uppercased). Top-level
    keys are used verbatim. A nested table (a table inside a section) is
    rejected — the only map-typed env var uses a delimited string form.
    """
    out: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            prefix = f"OPENRECALL_{key.upper()}_"
            for sub, subval in value.items():
                if isinstance(subval, dict):
                    raise ValueError(
                        f"config: nested table under [{key}] is not supported "
                        f"(use a delimited string for '{prefix}{sub.upper()}')",
                    )
                out[prefix + sub.upper()] = _stringify(subval)
        else:
            out[key] = _stringify(value)
    return out


def load_config_file(path: str | Path) -> dict[str, str]:
    """Parse a TOML config file and return its flattened env-var dict.

    A missing file returns ``{}`` — no config file is the common case and must
    not be an error. A malformed file raises (a typo'd config should fail
    loudly, not silently fall back to defaults).
    """
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("rb") as f:
        data = tomllib.load(f)
    return flatten_config(data)


def apply_config_file(
    path: str | Path, env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Load the config file and merge it into ``env`` (defaults to os.environ).

    Real env vars win: the file only fills keys that are not already set
    (``setdefault``), so explicit exports / CI overrides take precedence.
    Returns the flattened file entries (for logging).
    """
    cfg = load_config_file(path)
    target = env if env is not None else os.environ
    for key, value in cfg.items():
        target.setdefault(key, value)
    return cfg