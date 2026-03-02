# BG3 Mod Updater

A Python desktop app that scans your local Baldur's Gate 3 mod folder, matches mods to their Nexus Mods pages, and checks for updates — with optional one-click download and installation.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue) ![License MIT](https://img.shields.io/badge/license-MIT-green)

## Features

### Scanning & Matching
- **Scan** a local folder for mod files (`.pak`, `.zip`, `.rar`, `.7z`)
- **Auto-detect** Nexus mod IDs from standard download filenames (e.g. `ModName-12345-1-0-0-1623456789.zip`)
- **Smart lookup** — cascade scoring model that matches local mods to Nexus by author + name similarity, with web search fallback via [Tavily](https://tavily.com)
- **Duplicate conflict resolution** — clickable choice-card dialog when two local mods claim the same Nexus ID
- **Inline ID editing** — double-click any Nexus ID cell to manually assign or correct it

### Update Checking
- **Check for updates** via Nexus Mods API (REST v1 + GraphQL v2) with web-scraping fallback
- **Visual status indicators** — green (up to date), yellow (update available), red (error/unknown), grey (not on Nexus)
- **Sortable columns** — click any column header to sort

### Update Installation
- **Single mod update** — right-click an outdated mod → "Update This Mod"
- **Batch update** — "Update All Outdated" button for bulk downloads
- **Archive extraction** — automatically extracts `.pak` files from downloaded `.zip`, `.7z`, and `.rar` archives
- **Post-install verification** — re-scans and re-checks after download to confirm the update took
- **Premium / Free handling** — Premium API keys get direct downloads; free keys open the Nexus files page in your browser

### Management
- **Right-click context menu** — Mark as Not on Nexus, Update, Edit ID, Open on Nexus
- **"Not on Nexus" tagging** — permanently exclude mods that aren't from Nexus (survives cache clears)
- **Persistent cache** — Nexus ID mappings cached between sessions so you don't re-match every time
- **LSPK parsing** — reads UUID, version, and author directly from `.pak` file metadata

## Prerequisites

- Python 3.10+
- A free [Nexus Mods](https://www.nexusmods.com) account
- A **Personal API Key** from Nexus Mods
  - Go to [nexusmods.com](https://www.nexusmods.com) → *My Account* → *API Access* → *Personal API Key*
- *(Optional)* A [Tavily](https://tavily.com) API key for improved web-search fallback
- *(Optional)* A Nexus Premium account for direct mod downloads

## Setup

```bash
# Clone the repo
git clone https://github.com/keylimesoda/bg3-mod-updater.git
cd bg3-mod-updater

# Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# Install dependencies
pip install -r requirements.txt
```

## Running

```bash
python main.py
```

1. Enter or browse to your BG3 mods folder
2. Paste your Nexus Mods API key
3. *(Optional)* Add a Tavily API key for better web-search matching
4. Click **Scan Mods** to list all detected mod files
5. Click **Look Up Mods** to match unidentified mods to Nexus pages
6. Click **Check for Updates** to compare local vs. Nexus versions
7. Right-click any outdated mod to update, or use **Update All Outdated**

## Project Structure

| File | Purpose |
|------|---------|
| `main.py` | Entry point with logging setup |
| `gui.py` | tkinter GUI — dark-themed table, dialogs, context menu, download logic |
| `mod_scanner.py` | Scans a directory for mod files, extracts metadata from `.pak` and filenames |
| `nexus_api.py` | Nexus Mods REST API v1 client (mod info, files, download links) |
| `nexus_search.py` | GraphQL v2 search + cascade scoring engine + Tavily web fallback |
| `config.py` | JSON config/cache management (ID mappings, skip lists, exclusions) |
| `lspk_parser.py` | LSPK `.pak` file parser — reads `meta.lsx` for UUID, version, author |

## Project Notes

Additional implementation and roadmap notes are tracked in [PROJECT_NOTES.md](PROJECT_NOTES.md).

## Filename Detection

Nexus Mods downloads follow this naming convention:

```
ModName-<MOD_ID>-<VERSION_PARTS>-<UNIX_TIMESTAMP>.<ext>
```

For example: `ImprovedUI-366-1-0-0-1623456789.zip` → mod ID **366**.

Renamed files won't auto-detect, but the **Look Up Mods** feature will find them by name.

## License

MIT
