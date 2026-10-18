"""Ekspansi @file: sertakan isi file yang disebut pengguna ke dalam prompt."""

from __future__ import annotations

import os
import re

from maba_code import fsops
from maba_code.workspace import Workspace

_MENTION = re.compile(r'(?<![\w@])@(?:"([^"]+)"|([^\s@"]+))')
MAX_FILES = 5
MAX_FILE_BYTES = 200_000
MAX_TOTAL_CHARS = 120_000


def expand_mentions(text: str, ws: Workspace) -> tuple[str, list[str]]:
    """Kembalikan (prompt + lampiran, daftar file yang dilampirkan).

    File yang dilampirkan ditandai 'sudah dibaca' sehingga agen boleh langsung mengeditnya.
    Teks asli (dengan @jalur) dibiarkan utuh; hanya ditambah blok <file>.
    """
    attachments: list[str] = []
    labels: list[str] = []
    total = 0
    seen: set[str] = set()
    for match in _MENTION.finditer(text):
        token = (match.group(1) or match.group(2)).rstrip(".,;:!?)")
        path = ws.resolve(token)
        key = os.path.normcase(str(path))
        if key in seen or not path.exists() or fsops.is_sensitive_path(path):
            continue
        seen.add(key)
        label = ws.display(path)
        if path.is_dir():
            entries = sorted(os.listdir(path))[:100]
            attachments.append(f'<directory path="{label}">\n' + "\n".join(entries) + "\n</directory>")
            labels.append(label + "/")
            continue
        if len(labels) >= MAX_FILES:
            break
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            raw = path.read_bytes()
            if fsops.looks_binary(raw):
                continue
            body = fsops.decode_text(raw).text
        except (OSError, fsops.NotUtf8Error):
            continue
        lines = body.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        numbered = fsops.number_lines(lines, 1)
        if total + len(numbered) > MAX_TOTAL_CHARS:
            break
        total += len(numbered)
        ws.mark_read(path)
        attachments.append(f'<file path="{label}">\n{numbered}\n</file>')
        labels.append(label)
    if not attachments:
        return text, []
    return text + "\n\n" + "\n\n".join(attachments), labels
