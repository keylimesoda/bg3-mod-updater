"""Configuration management for Nexus Mod Updater."""

import json
import os

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "nexus_api_key": "",
    "tavily_api_key": "",
    "mod_directory": "",
    "game_domain": "baldursgate3",
    "uuid_to_nexus_id": {},
    "name_to_nexus_id": {},
    "skipped_uuids": [],
    "skipped_names": [],
}


def load_config() -> dict:
    """Load configuration from disk, returning defaults if file doesn't exist."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # Merge with defaults so new keys are always present
            merged = {**DEFAULT_CONFIG, **saved}
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """Persist configuration to disk."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ── UUID ↔ Nexus ID cache ──────────────────────────────────────────

def get_cached_nexus_id(cfg: dict, uuid: str) -> int | None:
    """Look up a cached Nexus mod ID for a given mod UUID."""
    mapping = cfg.get("uuid_to_nexus_id", {})
    val = mapping.get(uuid)
    if val is None:
        return None
    # Support both old (int) and new ({nexus_id, confidence}) formats
    if isinstance(val, dict):
        return int(val["nexus_id"])
    return int(val)


def get_cached_confidence(cfg: dict, uuid: str = "", mod_name: str = "") -> str:
    """Return the confidence string for a cached mapping, or '' if unknown."""
    if uuid:
        val = cfg.get("uuid_to_nexus_id", {}).get(uuid)
        if isinstance(val, dict):
            return val.get("confidence", "")
    if mod_name:
        val = cfg.get("name_to_nexus_id", {}).get(mod_name.strip().lower())
        if isinstance(val, dict):
            return val.get("confidence", "")
    return ""


def cache_nexus_id(cfg: dict, uuid: str, nexus_id: int,
                   confidence: str = "") -> None:
    """Store a UUID → Nexus ID mapping (with confidence) and persist."""
    if "uuid_to_nexus_id" not in cfg:
        cfg["uuid_to_nexus_id"] = {}
    cfg["uuid_to_nexus_id"][uuid] = {
        "nexus_id": nexus_id,
        "confidence": confidence,
    }
    save_config(cfg)


# ── Name → Nexus ID cache (fallback for mods without UUIDs) ────────

def get_cached_nexus_id_by_name(cfg: dict, mod_name: str) -> int | None:
    """Look up a cached Nexus mod ID by normalised mod name."""
    mapping = cfg.get("name_to_nexus_id", {})
    val = mapping.get(mod_name.strip().lower())
    if val is None:
        return None
    if isinstance(val, dict):
        return int(val["nexus_id"])
    return int(val)


def cache_nexus_id_by_name(cfg: dict, mod_name: str, nexus_id: int,
                           confidence: str = "") -> None:
    """Store a mod-name → Nexus ID mapping (with confidence) and persist."""
    if "name_to_nexus_id" not in cfg:
        cfg["name_to_nexus_id"] = {}
    cfg["name_to_nexus_id"][mod_name.strip().lower()] = {
        "nexus_id": nexus_id,
        "confidence": confidence,
    }
    save_config(cfg)


# ── "Not on Nexus" skip list ───────────────────────────────────────

def is_skipped(cfg: dict, uuid: str, mod_name: str) -> bool:
    """Return True if this mod was previously marked as not on Nexus."""
    if uuid and uuid in cfg.get("skipped_uuids", []):
        return True
    if mod_name and mod_name.strip().lower() in cfg.get("skipped_names", []):
        return True
    return False


def mark_skipped(cfg: dict, uuid: str, mod_name: str) -> None:
    """Record a mod as not found on Nexus so it won't be re-searched."""
    if "skipped_uuids" not in cfg:
        cfg["skipped_uuids"] = []
    if "skipped_names" not in cfg:
        cfg["skipped_names"] = []
    if uuid and uuid not in cfg["skipped_uuids"]:
        cfg["skipped_uuids"].append(uuid)
    key = mod_name.strip().lower() if mod_name else ""
    if key and key not in cfg["skipped_names"]:
        cfg["skipped_names"].append(key)
    save_config(cfg)


# ── "Not on Nexus" permanent exclusion ─────────────────────────────

def is_not_nexus(cfg: dict, uuid: str = "", mod_name: str = "") -> bool:
    """Return True if this mod was permanently tagged as not from Nexus."""
    excluded = cfg.get("not_on_nexus", [])
    if uuid and uuid in excluded:
        return True
    key = mod_name.strip().lower() if mod_name else ""
    if key and key in excluded:
        return True
    return False


def mark_not_nexus(cfg: dict, uuid: str = "", mod_name: str = "") -> None:
    """Permanently tag a mod as not from Nexus (survives cache clears)."""
    if "not_on_nexus" not in cfg:
        cfg["not_on_nexus"] = []
    if uuid and uuid not in cfg["not_on_nexus"]:
        cfg["not_on_nexus"].append(uuid)
    key = mod_name.strip().lower() if mod_name else ""
    if key and key not in cfg["not_on_nexus"]:
        cfg["not_on_nexus"].append(key)
    save_config(cfg)


def unmark_not_nexus(cfg: dict, uuid: str = "", mod_name: str = "") -> None:
    """Remove the 'not on Nexus' tag for a mod."""
    excluded = cfg.get("not_on_nexus", [])
    if uuid and uuid in excluded:
        excluded.remove(uuid)
    key = mod_name.strip().lower() if mod_name else ""
    if key and key in excluded:
        excluded.remove(key)
    save_config(cfg)


# ── Cache management ───────────────────────────────────────────────

def clear_cache(cfg: dict) -> None:
    """Clear all cached Nexus ID mappings and skip lists.

    Note: 'not_on_nexus' exclusions are intentionally preserved.
    """
    cfg["uuid_to_nexus_id"] = {}
    cfg["name_to_nexus_id"] = {}
    cfg["skipped_uuids"] = []
    cfg["skipped_names"] = []
    save_config(cfg)
