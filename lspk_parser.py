"""
Parse BG3 .pak (LSPK) files to extract meta.lsx mod metadata.

Supports LSPK package versions 13, 15, 16, and 18 (BG3 Release).
"""

import io
import os
import struct
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from typing import Optional

try:
    import lz4.block as _lz4_block

    HAS_LZ4 = True
except ImportError:
    _lz4_block = None
    HAS_LZ4 = False


# ── Constants ───────────────────────────────────────────────────────

LSPK_SIGNATURE = 0x4B50534C  # "LSPK" little-endian

# Compression methods (lower 4 bits of a file entry's flags byte)
COMPRESS_NONE = 0
COMPRESS_ZLIB = 1
COMPRESS_LZ4 = 2


# ── Data classes ────────────────────────────────────────────────────

@dataclass
class PakFileEntry:
    """One entry in the LSPK file table."""
    name: str
    offset: int
    size_on_disk: int
    uncompressed_size: int
    archive_part: int
    flags: int

    @property
    def compression(self) -> int:
        return self.flags & 0x0F


@dataclass
class ModMetadata:
    """Information extracted from meta.lsx inside a .pak."""
    name: str = ""
    uuid: str = ""
    folder: str = ""
    version: str = ""
    author: str = ""
    description: str = ""


# ── Version-specific struct formats ─────────────────────────────────
#
# All structs are little-endian, packed (no padding).
#
# FileEntry formats (the 256-byte name prefix is handled separately):
#   V7  : <IIII         (offset32, size_on_disk32, uncompressed32, part32)
#   V10 : <IIIIII       (offset32, size_on_disk32, uncompressed32, part32, flags32, crc32)
#   V15 : <QQQIIII      (offset64, size_on_disk64, uncompressed64, part32, flags32, crc32, unk32)
#   V18 : <IHBBII       (offset_lo32, offset_hi16, part8, flags8, size_on_disk32, uncompressed32)

_ENTRY_V7_FMT = struct.Struct("<IIII")        # 16 bytes after name
_ENTRY_V10_FMT = struct.Struct("<IIIIII")     # 24 bytes after name
_ENTRY_V15_FMT = struct.Struct("<QQQIIII")    # 40 bytes after name
_ENTRY_V18_FMT = struct.Struct("<IHBBII")     # 16 bytes after name

_NAME_LEN = 256  # fixed-length name field in every entry variant


def _entry_size(version: int) -> int:
    """Total size in bytes of one file entry for the given LSPK version."""
    tail = {7: _ENTRY_V7_FMT.size, 9: _ENTRY_V7_FMT.size,
            10: _ENTRY_V10_FMT.size, 13: _ENTRY_V10_FMT.size,
            15: _ENTRY_V15_FMT.size, 16: _ENTRY_V15_FMT.size,
            18: _ENTRY_V18_FMT.size}
    return _NAME_LEN + tail.get(version, _ENTRY_V15_FMT.size)


def _parse_entry(data: bytes, offset: int, version: int) -> PakFileEntry:
    """Parse a single file entry from *data* at *offset*."""
    name_raw = data[offset:offset + _NAME_LEN]
    name = name_raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    tail = offset + _NAME_LEN

    if version in (7, 9):
        off, sod, unc, part = _ENTRY_V7_FMT.unpack_from(data, tail)
        flags = COMPRESS_ZLIB if unc > 0 else COMPRESS_NONE
        return PakFileEntry(name, off, sod, unc if unc else sod, part, flags)

    if version in (10, 13):
        off, sod, unc, part, flags, _crc = _ENTRY_V10_FMT.unpack_from(data, tail)
        return PakFileEntry(name, off, sod, unc if unc else sod, part, flags)

    if version in (15, 16):
        off, sod, unc, part, flags, _crc, _unk = _ENTRY_V15_FMT.unpack_from(data, tail)
        return PakFileEntry(name, off, sod, unc if unc else sod, part, flags)

    # V18
    off_lo, off_hi, part, flags, sod, unc = _ENTRY_V18_FMT.unpack_from(data, tail)
    off = off_lo | (off_hi << 32)
    return PakFileEntry(name, off, sod, unc if unc else sod, part, flags)


# ── Header structs ──────────────────────────────────────────────────

# After the 4-byte LSPK signature:
_HDR_V15_FMT = struct.Struct("<IQI2s16s")   # ver, list_off, list_size, flags+pri, md5  → 34 bytes
_HDR_V16_FMT = struct.Struct("<IQI2s16sH")  # same + num_parts → 36 bytes

# V13 header (found at end of file):
_HDR_V13_FMT = struct.Struct("<III2s2s16s")  # ver, list_off, list_size, parts, flags+pri, md5  → 28 bytes


# ── Decompression helpers ───────────────────────────────────────────

def _decompress(data: bytes, uncompressed_size: int, method: int) -> bytes:
    """Decompress *data* using the given compression method."""
    if method == COMPRESS_NONE:
        return data
    if method == COMPRESS_ZLIB:
        return zlib.decompress(data)
    if method == COMPRESS_LZ4:
        if not HAS_LZ4:
            raise ImportError(
                "The 'lz4' package is required to read LZ4-compressed .pak files.  "
                "Install it with:  pip install lz4"
            )
        return _lz4_block.decompress(data, uncompressed_size=uncompressed_size)
    raise ValueError(f"Unsupported compression method {method}")


# ── Core reader ─────────────────────────────────────────────────────

def _read_file_list_v15plus(f, version: int, file_list_offset: int) -> list[PakFileEntry]:
    """Read the LZ4-compressed file list for v > 10 packages."""
    f.seek(file_list_offset)
    num_files = struct.unpack("<I", f.read(4))[0]
    compressed_size = struct.unpack("<I", f.read(4))[0] if version > 13 else None

    if compressed_size is not None:
        compressed = f.read(compressed_size)
    else:
        # v13: read remaining file list bytes
        compressed = f.read()

    entry_sz = _entry_size(version)
    decompressed_size = entry_sz * num_files
    raw = _decompress(compressed, decompressed_size, COMPRESS_LZ4)

    entries: list[PakFileEntry] = []
    for i in range(num_files):
        entries.append(_parse_entry(raw, i * entry_sz, version))
    return entries


def _read_file_list_legacy(f, version: int, header_size: int, num_files: int) -> list[PakFileEntry]:
    """Read uncompressed file list for v <= 10 packages."""
    entry_sz = _entry_size(version)
    data_offset = header_size + entry_sz * num_files
    # Align data offset
    padding = 64 if version >= 10 else 1
    if data_offset % padding:
        data_offset += padding - (data_offset % padding)

    raw = f.read(entry_sz * num_files)
    entries: list[PakFileEntry] = []
    for i in range(num_files):
        e = _parse_entry(raw, i * entry_sz, version)
        if e.archive_part == 0:
            e.offset += data_offset
        entries.append(e)
    return entries


def read_pak_file_list(filepath: str) -> tuple[int, list[PakFileEntry]]:
    """
    Read the file list from an LSPK .pak file.

    Returns (version, [PakFileEntry, ...]).
    """
    with open(filepath, "rb") as f:
        file_size = f.seek(0, 2)
        f.seek(0)

        # ── Try v13 (signature at END of file) ──────────────────
        if file_size >= 8:
            f.seek(file_size - 4)
            end_sig = struct.unpack("<I", f.read(4))[0]
            if end_sig == LSPK_SIGNATURE:
                f.seek(file_size - 8)
                hdr_total_size = struct.unpack("<I", f.read(4))[0]
                hdr_offset = file_size - hdr_total_size
                f.seek(hdr_offset)
                hdr_data = f.read(_HDR_V13_FMT.size)
                ver, list_off, list_size, parts_raw, flagspri, _md5 = _HDR_V13_FMT.unpack(hdr_data)
                if ver == 13:
                    return ver, _read_file_list_v15plus(f, ver, list_off)

        # ── Try v15 / v16 / v18 (signature at START) ────────────
        f.seek(0)
        start_sig = struct.unpack("<I", f.read(4))[0]
        if start_sig == LSPK_SIGNATURE:
            ver = struct.unpack("<I", f.read(4))[0]
            f.seek(4)  # back to header start (after signature)

            if ver == 15:
                hdr = f.read(_HDR_V15_FMT.size)
                _v, list_off, _ls, _fp, _md5 = _HDR_V15_FMT.unpack(hdr)
                return ver, _read_file_list_v15plus(f, ver, list_off)
            elif ver in (16, 18):
                hdr = f.read(_HDR_V16_FMT.size)
                _v, list_off, _ls, _fp, _md5, _np = _HDR_V16_FMT.unpack(hdr)
                return ver, _read_file_list_v15plus(f, ver, list_off)
            else:
                raise ValueError(f"Unsupported LSPK version {ver}")

        # ── Try v7 / v9 / v10 (version at byte 0, no signature) ─
        f.seek(0)
        ver = struct.unpack("<I", f.read(4))[0]
        if ver in (7, 9):
            # LSPKHeader7: version(4) dataOffset(4) numParts(4) fileListSize(4) littleEndian(1) numFiles(4)
            data_offset, _num_parts, _fls, _le = struct.unpack("<IIIB", f.read(13))
            num_files = struct.unpack("<I", f.read(4))[0]
            hdr_size = 4 + 13 + 4  # total header
            f.seek(hdr_size)
            return ver, _read_file_list_legacy(f, ver, hdr_size, num_files)

        if ver == 10:
            # LSPKHeader10: version(4) dataOffset(4) fileListSize(4) numParts(2) flags(1) priority(1) numFiles(4)
            _do, _fls, _np, _fl, _pr = struct.unpack("<IIHBB", f.read(12))
            num_files = struct.unpack("<I", f.read(4))[0]
            hdr_size = 4 + 12 + 4
            f.seek(hdr_size)
            return ver, _read_file_list_legacy(f, ver, hdr_size, num_files)

        raise ValueError(
            f"Not a recognized LSPK package (first 4 bytes: 0x{start_sig:08X})"
        )


def read_file_content(filepath: str, entry: PakFileEntry) -> bytes:
    """Read and decompress a single file from the .pak archive."""
    with open(filepath, "rb") as f:
        f.seek(entry.offset)
        raw = f.read(entry.size_on_disk)
    return _decompress(raw, entry.uncompressed_size, entry.compression)


# ── meta.lsx XML parsing ───────────────────────────────────────────

def _decode_version64(val: int) -> str:
    """Decode a Larian Version64 packed integer into a readable string."""
    major = (val >> 55) & 0x1FF
    minor = (val >> 47) & 0xFF
    revision = (val >> 31) & 0xFFFF
    build = val & 0x7FFFFFFF
    return f"{major}.{minor}.{revision}.{build}"


def parse_meta_lsx(xml_bytes: bytes) -> Optional[ModMetadata]:
    """
    Parse a meta.lsx XML blob and return a ModMetadata, or None on failure.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    meta = ModMetadata()

    # Look for <node id="ModuleInfo"> anywhere in the tree
    for node in root.iter("node"):
        if node.attrib.get("id") == "ModuleInfo":
            # Only inspect DIRECT child <attribute> elements.
            # Using iter() would descend into nested Script nodes
            # whose UUID attributes would overwrite the mod's own UUID.
            for attr in node.findall("attribute"):
                aid = attr.attrib.get("id", "")
                val = attr.attrib.get("value", "")
                if aid == "Name":
                    meta.name = val
                elif aid == "UUID":
                    meta.uuid = val
                elif aid == "Folder":
                    meta.folder = val
                elif aid == "Author":
                    meta.author = val
                elif aid == "Description":
                    meta.description = val
                elif aid == "Version64":
                    try:
                        meta.version = _decode_version64(int(val))
                    except (ValueError, TypeError):
                        meta.version = val
                elif aid == "Version" and not meta.version:
                    meta.version = val
            break  # found ModuleInfo, no need to keep searching

    if not meta.name and not meta.uuid:
        return None
    return meta


# ── High-level API ──────────────────────────────────────────────────

def extract_mod_metadata(pak_path: str) -> Optional[ModMetadata]:
    """
    Open a .pak file, locate meta.lsx, and return ModMetadata.

    Returns None if the package can't be parsed or contains no meta.lsx.
    """
    try:
        version, entries = read_pak_file_list(pak_path)
    except Exception:
        return None

    # Find the meta.lsx entry — path usually like  Mods/<folder>/meta.lsx
    meta_entry: Optional[PakFileEntry] = None
    for e in entries:
        lower = e.name.lower().replace("\\", "/")
        if lower.endswith("meta.lsx"):
            meta_entry = e
            break

    if meta_entry is None:
        return None

    try:
        content = read_file_content(pak_path, meta_entry)
    except Exception:
        return None

    return parse_meta_lsx(content)


def list_pak_contents(pak_path: str) -> list[str]:
    """Return all file names inside a .pak (for debugging)."""
    try:
        _, entries = read_pak_file_list(pak_path)
        return [e.name for e in entries]
    except Exception:
        return []
