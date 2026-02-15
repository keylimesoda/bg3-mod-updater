# Nexus Mod Updater - Copilot Instructions

## Project Overview
A Python desktop application for checking Baldur's Gate 3 mod updates from Nexus Mods.

## Tech Stack
- Python 3.10+
- tkinter for GUI
- requests for HTTP/API calls
- Nexus Mods API for mod lookup

## Architecture
- `main.py` - Application entry point
- `gui.py` - tkinter GUI components
- `nexus_api.py` - Nexus Mods API integration
- `mod_scanner.py` - Local mod directory scanning
- `config.py` - Configuration management

## Key Features
- Scan local mod directory for BG3 mods
- Extract mod IDs from Nexus download filenames
- Check for updates via Nexus Mods API
- Visual status indicators for outdated mods
