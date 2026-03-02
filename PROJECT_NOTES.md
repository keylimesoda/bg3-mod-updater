# Nexus Mod Updater - Project Notes

## Purpose
Desktop utility to scan local Baldur's Gate 3 mods and check for updates on Nexus Mods.

## Current Scope
- Scan a selected local mod directory
- Parse mod information from LSPK/download naming patterns
- Query Nexus Mods API for latest file/update data
- Show update status in the tkinter UI

## Tech Stack
- Python 3.10+
- tkinter
- requests

## Local Run
1. Activate virtual environment
2. Install dependencies from `requirements.txt`
3. Run `main.py`

## Configuration
- API and app settings are managed by `config.py` and `config.json`.
- Keep API keys and personal tokens out of source control when possible.

## Next Ideas
- Add richer error messages for API/network issues
- Improve filename/mod-ID matching heuristics
- Add export of update results (CSV/JSON)
