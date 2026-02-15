"""Scan a local directory for BG3 mod files and extract metadata.

For .pak files the mod name, UUID, version and author are read directly
from the embedded ``meta.lsx`` via the LSPK parser.  For .zip archives
we look inside for .pak files and parse those.  The Nexus mod ID is
still extracted from the download filename when possible.
"""

import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Optional

from lspk_parser import ModMetadata, extract_mod_metadata


# ── Nexus filename patterns ────────────────────────────────────────
# Nexus download filenames look like:
#   "Mod Name-<mod_id>-<version_parts>-<timestamp>.<ext>"
#   e.g. "RecruitAnyNPC-18471-0-2-1758147930.rar"
#   e.g. "ImprovedUI-366-1-0-0-1623456789.zip"
#
# BG3 .pak files often include a UUID suffix like:
#   "ModName_8hex-4hex-4hex-4hex-12hex.pak"
# These UUIDs contain digit-only segments that the old regex
# confused for Nexus mod IDs.  We must NOT match those.

# Matches a standard UUID (8-4-4-4-12 hex), possibly preceded by _ or -
_UUID_RE = re.compile(
    r"[_-][0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Also match partial/mangled UUIDs like "621b50e9-9ffb-382-7ccm"
_PARTIAL_UUID_RE = re.compile(
    r"[_-][0-9a-f]{6,8}-[0-9a-f]{3,4}-[0-9a-f]{3,4}(?:-[0-9a-z]{3,4})?",
    re.IGNORECASE,
)

# Strict Nexus download pattern — only matches after UUIDs are stripped.
# Requires: name, then a purely-numeric mod ID (1-7 digits), then at
# least one version segment (digit groups separated by hyphens), and
# an optional unix timestamp (10-13 digits) at the end.
_NEXUS_FILENAME_RE = re.compile(
    r"^(?P<name>.+?)-(?P<mod_id>\d{1,7})-(?P<version>(?:\d+-)*\d+)(?:-(?P<ts>\d{10,13}))?$",
    re.IGNORECASE,
)

MOD_EXTENSIONS = {".zip", ".rar", ".7z", ".pak"}


def _pretty_name(raw: str) -> str:
    """Turn a filename slug into a human-readable mod name."""
    return raw.replace("_", " ").replace("-", " ").strip()


def _extract_nexus_id(filename: str) -> Optional[int]:
    """Try to pull a Nexus mod ID out of a Nexus download filename.

    Only matches the strict pattern:  Name-<ID>-<version>[-timestamp].ext
    Ignores UUIDs embedded in filenames (common in BG3 .pak files).
    """
    # Strip the extension first so the regex doesn't have to handle it
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in MOD_EXTENSIONS:
        return None

    # Remove any UUID / partial-UUID suffixes so they can't confuse us
    cleaned = _UUID_RE.sub("", stem)
    cleaned = _PARTIAL_UUID_RE.sub("", cleaned)

    m = _NEXUS_FILENAME_RE.match(cleaned)
    if m:
        return int(m.group("mod_id"))
    return None


def _metadata_from_zip(filepath: str) -> Optional[ModMetadata]:
    """
    Open a .zip archive, find the first .pak inside, extract it
    to a temp file, and parse meta.lsx from it.
    """
    try:
        with zipfile.ZipFile(filepath, "r") as zf:
            pak_names = [n for n in zf.namelist() if n.lower().endswith(".pak")]
            if not pak_names:
                return None
            # Extract the first .pak to a temp file so the LSPK parser
            # can seek freely (ZipExtFile is not seekable).
            with tempfile.NamedTemporaryFile(suffix=".pak", delete=False) as tmp:
                tmp_path = tmp.name
                tmp.write(zf.read(pak_names[0]))
            try:
                return extract_mod_metadata(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except (zipfile.BadZipFile, OSError):
        return None


def scan_mod_directory(directory: str) -> list[dict]:
    """
    Scan *directory* for mod files and return a list of mod info dicts.

    Each dict has:
        filename     – original filename
        filepath     – full path
        mod_name     – best available display name (from meta.lsx or filename)
        mod_id       – Nexus mod ID (int) or None
        local_date   – last-modified datetime (UTC) of the local file
        uuid         – mod UUID from meta.lsx, or ""
        version      – mod version from meta.lsx, or ""
        author       – mod author from meta.lsx, or ""
        description  – mod description from meta.lsx, or ""
    """
    mods: list[dict] = []
    if not os.path.isdir(directory):
        return mods

    seen_ids: set[int] = set()

    for entry in sorted(os.listdir(directory)):
        filepath = os.path.join(directory, entry)
        if not os.path.isfile(filepath):
            continue

        ext = os.path.splitext(entry)[1].lower()
        if ext not in MOD_EXTENSIONS:
            continue

        # ── Extract metadata from the file itself ───────────────
        meta: Optional[ModMetadata] = None
        if ext == ".pak":
            meta = extract_mod_metadata(filepath)
        elif ext == ".zip":
            meta = _metadata_from_zip(filepath)
        # .rar / .7z: no built-in support; fall back to filename

        # ── Mod name: prefer meta.lsx, fall back to filename ────
        if meta and meta.name:
            mod_name = meta.name
        else:
            # If the filename matches the Nexus pattern, use only the
            # name portion (before the ID) instead of the full stem.
            stem = os.path.splitext(entry)[0]
            cleaned = _UUID_RE.sub("", stem)
            cleaned = _PARTIAL_UUID_RE.sub("", cleaned)
            m = _NEXUS_FILENAME_RE.match(cleaned)
            if m:
                mod_name = _pretty_name(m.group("name"))
            else:
                mod_name = _pretty_name(stem)

        # ── Nexus mod ID: always from filename ──────────────────
        mod_id = _extract_nexus_id(entry)

        # ── File modification time ──────────────────────────────
        mtime = os.path.getmtime(filepath)
        local_date = datetime.fromtimestamp(mtime, tz=timezone.utc)

        # Skip duplicate mod IDs (keep first occurrence)
        if mod_id is not None:
            if mod_id in seen_ids:
                continue
            seen_ids.add(mod_id)

        mods.append(
            {
                "filename": entry,
                "filepath": filepath,
                "mod_name": mod_name,
                "mod_id": mod_id,
                "local_date": local_date,
                "uuid": meta.uuid if meta else "",
                "version": meta.version if meta else "",
                "author": meta.author if meta else "",
                "description": meta.description if meta else "",
            }
        )

    return mods
