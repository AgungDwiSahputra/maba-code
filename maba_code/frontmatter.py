"""Frontmatter YAML untuk file markdown (sub-agen, skill): ``---\\nkunci: nilai\\n---\\nisi``."""

from __future__ import annotations

import re
from typing import Any

import yaml

_FRONTMATTER = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)$", re.S)


def split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Kembalikan (metadata, isi). Frontmatter wajib di baris pertama; YAML rusak dianggap kosong."""
    match = _FRONTMATTER.match(raw)
    if not match:
        return {}, raw
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), match.group(2)


def as_list(value: Any) -> list[str] | None:
    """``"Read, Grep"`` atau ``[Read, Grep]`` -> ``["Read", "Grep"]``; tidak ada -> None."""
    if value is None:
        return None
    if isinstance(value, str):
        # Pisah di koma/spasi, kecuali di dalam kurung: "Bash(git add *) Read" -> ["Bash(git add *)", "Read"].
        parts, buf, depth = [], "", 0
        for ch in value:
            depth += (ch == "(") - (ch == ")")
            if depth <= 0 and (ch == "," or ch.isspace()):
                if buf.strip():
                    parts.append(buf.strip())
                buf, depth = "", 0
            else:
                buf += ch
        if buf.strip():
            parts.append(buf.strip())
        return parts
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return None


def as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1", "false", "no", "0"}:
        return value.strip().lower() in {"true", "yes", "1"}
    return default
