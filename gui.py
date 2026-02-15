"""Nexus Mod Updater – tkinter GUI."""

import logging
import os
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox, ttk
import webbrowser
from datetime import datetime, timezone

from config import (
    load_config, save_config,
    get_cached_nexus_id, cache_nexus_id,
    get_cached_nexus_id_by_name, cache_nexus_id_by_name,
    is_skipped, mark_skipped, clear_cache,
    is_not_nexus, mark_not_nexus, unmark_not_nexus,
)
from mod_scanner import scan_mod_directory
from nexus_api import NexusAPI, NexusAPIError, scrape_mod_updated
from nexus_search import (
    search_all_sources, rank_matches, NexusSearchResult, ScoredMatch,
    AUTO_MATCH_THRESHOLD, SEARCH_DELAY, LOOKUP_WORKERS,
)

log = logging.getLogger("gui")


# ── colour palette ──────────────────────────────────────────────────
BG = "#1e1e2e"          # dark background
BG_CARD = "#2a2a3c"     # card / row background
FG = "#cdd6f4"          # primary text
FG_DIM = "#7f849c"      # muted text
ACCENT = "#89b4fa"       # accent / links
GREEN = "#a6e3a1"        # up-to-date
YELLOW = "#f9e2af"       # outdated
RED = "#f38ba8"          # error / unknown
BORDER = "#45475a"


# ── Duplicate-conflict resolution dialog ────────────────────────────

class DuplicateConflictDialog(tk.Toplevel):
    """Modal dialog shown when an auto-match candidate ID already belongs to
    another local mod.  Two clickable choice-cards let the user pick which
    mod keeps the Nexus ID with minimal friction.
    """

    # Possible outcomes stored in self.choice:
    REASSIGN = "reassign"    # give the ID to the current mod (evict holder)
    KEEP     = "keep"        # keep existing assignment, skip current mod
    SEARCH   = "search"      # open full NexusMatchDialog for alternatives

    # Card colours
    _CARD_BG        = "#2a2a3c"
    _CARD_HOVER     = "#343450"
    _CARD_SELECTED  = "#2e3d2e"
    _CARD_BORDER    = "#45475a"
    _CARD_SEL_BD    = "#a6e3a1"

    def __init__(
        self,
        parent,
        mod: dict,
        candidate: ScoredMatch,
        holder: dict,
        current: int = 0,
        total: int = 0,
    ):
        super().__init__(parent)
        self.transient(parent)
        self.grab_set()
        self.configure(bg=BG)
        self.title("Duplicate Nexus ID Conflict")
        self.resizable(True, True)
        self.minsize(560, 340)
        self.geometry("640x410")

        self.choice: str = self.KEEP
        self._candidate = candidate
        self._selected: str = ""  # tracks which card is pre-selected

        nexus_id = candidate.result.mod_id
        r = candidate.result
        pct = int(candidate.score * 100)

        game_domain = (parent.cfg.get("game_domain", "baldursgate3")
                       if hasattr(parent, "cfg") else "baldursgate3")
        nexus_url = r.url or f"https://www.nexusmods.com/{game_domain}/mods/{nexus_id}"

        # ── Header: one-liner context ───────────────────────────
        head = tk.Frame(self, bg=BG, padx=18)
        head.pack(fill="x", pady=(12, 6))

        counter = f"  ({current}/{total})" if current and total else ""
        tk.Label(
            head,
            text=f"Which mod is \"{r.name}\"?{counter}",
            bg=BG, fg=FG,
            font=("Segoe UI", 13, "bold"),
            wraplength=600, justify="left",
        ).pack(anchor="w")

        # Nexus link + stats on one line
        info_row = tk.Frame(head, bg=BG)
        info_row.pack(anchor="w", pady=(2, 0))
        link = tk.Label(info_row,
                        text=f"Nexus ID {nexus_id}",
                        bg=BG, fg=ACCENT,
                        font=("Segoe UI", 9, "underline"),
                        cursor="hand2")
        link.pack(side="left")
        link.bind("<Button-1>", lambda e, u=nexus_url: webbrowser.open(u))

        info_parts = [f"by {r.author}"]
        if r.unique_downloads:
            if r.unique_downloads >= 1_000_000:
                info_parts.append(f"{r.unique_downloads / 1_000_000:.1f}M downloads")
            elif r.unique_downloads >= 1_000:
                info_parts.append(f"{r.unique_downloads / 1_000:.0f}K downloads")
        tk.Label(info_row, text="  ·  " + "  ·  ".join(info_parts),
                 bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left")

        # ── Prompt ──────────────────────────────────────────────
        tk.Label(self, text="Click to assign this Nexus ID to:",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
                 padx=18).pack(anchor="w", pady=(8, 4))

        # ── Choice cards ────────────────────────────────────────
        cards_area = tk.Frame(self, bg=BG, padx=18)
        cards_area.pack(fill="both", expand=True, pady=(0, 4))
        cards_area.columnconfigure(0, weight=1)
        cards_area.columnconfigure(1, weight=1)

        # Card A — assign to the new mod
        self._card_a = self._build_choice_card(
            cards_area, column=0, padx=(0, 6),
            mod_name=mod.get("mod_name", "?"),
            meta=self._meta_text(mod),
            badge_text=f"{pct}% match",
            badge_fg=GREEN if pct >= 80 else YELLOW,
            tag="reassign",
        )

        # Card B — keep for existing holder
        hold_conf = self._holder_confidence(parent, holder)
        badge = hold_conf if hold_conf else "Currently assigned"
        self._card_b = self._build_choice_card(
            cards_area, column=1, padx=(6, 0),
            mod_name=holder.get("mod_name", "?"),
            meta=self._meta_text(holder),
            badge_text=badge,
            badge_fg=FG_DIM,
            tag="keep",
        )

        # ── Bottom bar: search link ─────────────────────────────
        bottom = tk.Frame(self, bg=BG, padx=18)
        bottom.pack(fill="x", pady=(4, 14))

        alt_link = tk.Label(
            bottom,
            text="\U0001F50D  Neither of these — search for alternatives…",
            bg=BG, fg=ACCENT,
            font=("Segoe UI", 9, "underline"),
            cursor="hand2",
        )
        alt_link.pack(side="left")
        alt_link.bind("<Button-1>", lambda e: self._do_search())

        skip_link = tk.Label(
            bottom,
            text="Skip",
            bg=BG, fg=FG_DIM,
            font=("Segoe UI", 9, "underline"),
            cursor="hand2",
        )
        skip_link.pack(side="right")
        skip_link.bind("<Button-1>", lambda e: self._do_keep())

        # Centre on parent
        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(px, 0)}+{max(py, 0)}")

        self.protocol("WM_DELETE_WINDOW", self._do_keep)
        self.wait_window()

    # ── Card builder ────────────────────────────────────────────

    def _build_choice_card(
        self, parent_frame, column: int, padx: tuple,
        mod_name: str, meta: str, badge_text: str,
        badge_fg: str, tag: str,
    ) -> tk.Frame:
        """Build a clickable choice card and grid it into *parent_frame*."""
        card = tk.Frame(
            parent_frame, bg=self._CARD_BG,
            highlightbackground=self._CARD_BORDER,
            highlightthickness=2, padx=14, pady=12,
            cursor="hand2",
        )
        card.grid(row=0, column=column, sticky="nsew", padx=padx, pady=4)

        name_lbl = tk.Label(card, text=mod_name, bg=self._CARD_BG, fg=FG,
                            font=("Segoe UI", 11, "bold"), wraplength=240,
                            justify="left", cursor="hand2")
        name_lbl.pack(anchor="w")

        if meta:
            meta_lbl = tk.Label(card, text=meta, bg=self._CARD_BG, fg=FG_DIM,
                                font=("Segoe UI", 8), wraplength=240,
                                justify="left", cursor="hand2")
            meta_lbl.pack(anchor="w", pady=(2, 0))

        badge_lbl = tk.Label(card, text=badge_text, bg=self._CARD_BG, fg=badge_fg,
                             font=("Segoe UI", 9, "bold"), cursor="hand2")
        badge_lbl.pack(anchor="w", pady=(6, 0))

        action_lbl = tk.Label(card, text="▶  Assign to this mod",
                              bg=self._CARD_BG, fg=ACCENT,
                              font=("Segoe UI", 9), cursor="hand2")
        action_lbl.pack(anchor="w", pady=(8, 0))

        # Click anywhere on the card → select it
        action = self._do_reassign if tag == "reassign" else self._do_keep_assign
        for widget in (card, name_lbl, badge_lbl, action_lbl) + ((meta_lbl,) if meta else ()):
            widget.bind("<Button-1>", lambda e, a=action: a())
            widget.bind("<Enter>", lambda e, c=card: self._card_hover(c, True))
            widget.bind("<Leave>", lambda e, c=card: self._card_hover(c, False))

        return card

    def _card_hover(self, card: tk.Frame, entering: bool):
        """Lighten the card background on hover."""
        bg = self._CARD_HOVER if entering else self._CARD_BG
        card.configure(bg=bg)
        for child in card.winfo_children():
            try:
                child.configure(bg=bg)
            except tk.TclError:
                pass

    # ── helpers ─────────────────────────────────────────────────

    @staticmethod
    def _meta_text(mod: dict) -> str:
        parts = []
        if mod.get("author") and mod["author"] != "\u2014":
            parts.append(f"Author: {mod['author']}")
        if mod.get("version"):
            parts.append(f"v{mod['version']}")
        if mod.get("uuid"):
            parts.append(f"UUID: {mod['uuid'][:8]}…")
        return "  ·  ".join(parts)

    @staticmethod
    def _holder_confidence(parent, holder: dict) -> str:
        """Return a human-readable confidence string for the existing holder."""
        from config import get_cached_confidence
        if hasattr(parent, "cfg"):
            conf = get_cached_confidence(
                parent.cfg,
                uuid=holder.get("uuid", ""),
                mod_name=holder.get("mod_name", ""),
            )
            if conf:
                return f"Matched: {conf}"
        return ""

    @staticmethod
    def _trunc(text: str, maxlen: int) -> str:
        return text if len(text) <= maxlen else text[: maxlen - 1] + "…"

    # ── actions ─────────────────────────────────────────────────

    def _do_reassign(self):
        self.choice = self.REASSIGN
        self.destroy()

    def _do_keep_assign(self):
        """Keep the ID with the current holder (same as KEEP)."""
        self.choice = self.KEEP
        self.destroy()

    def _do_keep(self):
        self.choice = self.KEEP
        self.destroy()

    def _do_search(self):
        self.choice = self.SEARCH
        self.destroy()


# ── Match-confirmation dialog ───────────────────────────────────────

class NexusMatchDialog(tk.Toplevel):
    """Modal dialog for confirming a Nexus mod match."""

    def __init__(self, parent, mod: dict, candidates: list[ScoredMatch],
                 current: int = 0, total: int = 0):
        super().__init__(parent)
        self.transient(parent)
        self.grab_set()
        self.configure(bg=BG)
        if current and total:
            self.title(f"Identify Nexus Mod  ({current} / {total})")
        else:
            self.title("Identify Nexus Mod")
        self.resizable(True, True)
        self.minsize(560, 400)
        self.geometry("620x520")

        self._parent = parent
        self._mod = mod
        self.result: NexusSearchResult | None = None
        self.skip_all = False
        self._using_manual = False  # tracks which input mode is active

        # ── Local mod info (top, fixed) ─────────────────────────
        info = tk.Frame(self, bg=BG, padx=14, pady=10)
        info.pack(fill="x", side="top")

        # Progress counter + heading on the same line
        header_frame = tk.Frame(info, bg=BG)
        header_frame.pack(fill="x", anchor="w")
        tk.Label(header_frame, text="Local Mod", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        if current and total:
            tk.Label(header_frame,
                     text=f"{current} of {total}",
                     bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 10)).pack(side="right")
        tk.Label(info, text=mod.get("mod_name", "?"), bg=BG, fg=FG,
                 font=("Segoe UI", 12, "bold"), wraplength=540,
                 justify="left").pack(anchor="w", pady=(2, 0))

        meta_parts = []
        if mod.get("author") and mod["author"] != "\u2014":
            meta_parts.append(f"Author: {mod['author']}")
        if mod.get("version"):
            meta_parts.append(f"Version: {mod['version']}")
        if meta_parts:
            tk.Label(info, text="  \u00b7  ".join(meta_parts), bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 9)).pack(anchor="w")

        # ── Re-search bar ───────────────────────────────────────
        search_frame = tk.Frame(self, bg=BG, padx=14)
        search_frame.pack(fill="x", side="top", pady=(2, 6))

        tk.Label(search_frame, text="Search:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left")

        self._search_var = tk.StringVar(value=mod.get("mod_name", ""))
        self._search_entry = tk.Entry(
            search_frame, textvariable=self._search_var, width=35,
            bg=BG_CARD, fg=FG, insertbackground=FG,
            relief="flat", font=("Segoe UI", 10),
        )
        self._search_entry.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self._search_entry.bind("<Return>", lambda e: self._do_research())

        self._search_btn = tk.Button(
            search_frame, text="\U0001F50D Search",
            command=self._do_research,
            bg=ACCENT, fg=BG, activebackground=ACCENT,
            relief="flat", font=("Segoe UI", 9, "bold"),
            padx=8, cursor="hand2",
        )
        self._search_btn.pack(side="left", padx=(6, 0))

        self._search_status = tk.Label(
            search_frame, text="", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 8),
        )
        self._search_status.pack(side="left", padx=(8, 0))

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=14, pady=4, side="top")

        # ── Buttons (bottom, fixed) ─────────────────────────────
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", side="bottom")

        btn_frame = tk.Frame(bottom, bg=BG, padx=14, pady=10)
        btn_frame.pack(fill="x", side="bottom")

        self._accept_btn = tk.Button(
            btn_frame, text="\u2713  Accept Selection",
            command=self._accept,
            bg=GREEN, fg=BG, activebackground=GREEN,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=12, cursor="hand2",
        )
        self._accept_btn.pack(side="left")
        tk.Button(btn_frame, text="Skip", command=self._skip,
                  bg=BORDER, fg=FG, activebackground=BORDER,
                  relief="flat", font=("Segoe UI", 10),
                  padx=12, cursor="hand2").pack(side="left", padx=(8, 0))
        tk.Button(btn_frame, text="Skip All Remaining", command=self._skip_all,
                  bg=BORDER, fg=FG_DIM, activebackground=BORDER,
                  relief="flat", font=("Segoe UI", 9),
                  padx=10, cursor="hand2").pack(side="right")

        # ── Manual ID entry (bottom, above buttons) ─────────────
        ttk.Separator(bottom, orient="horizontal").pack(fill="x", padx=14, side="bottom")

        manual_frame = tk.Frame(bottom, bg=BG, padx=14, pady=6)
        manual_frame.pack(fill="x", side="bottom")

        self._manual_radio = tk.Radiobutton(
            manual_frame, text="Use manual Nexus ID:",
            variable=tk.IntVar(), value=0,
            bg=BG, fg=FG, selectcolor=BG,
            activebackground=BG, activeforeground=ACCENT,
            highlightthickness=0, font=("Segoe UI", 9, "bold"),
            command=self._switch_to_manual,
        )
        self._manual_radio.pack(side="left")

        self._manual_var = tk.StringVar()
        self._manual_entry = tk.Entry(
            manual_frame, textvariable=self._manual_var, width=10,
            bg=BG_CARD, fg=FG, insertbackground=FG,
            relief="flat", font=("Segoe UI", 10),
        )
        self._manual_entry.pack(side="left", padx=(6, 0))
        self._manual_indicator = tk.Label(
            manual_frame, text="", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 9),
        )
        self._manual_indicator.pack(side="left", padx=(8, 0))

        # When the user types in the manual field, switch to manual mode
        self._manual_var.trace_add("write", lambda *_: self._on_manual_typed())

        ttk.Separator(bottom, orient="horizontal").pack(fill="x", padx=14, pady=4, side="bottom")

        # ── Candidate list (middle, expands) ────────────────────
        self._list_header = tk.Label(
            self, text="Select the matching Nexus mod:", bg=BG, fg=FG,
            font=("Segoe UI", 10), padx=14,
        )
        self._list_header.pack(anchor="w", side="top")

        # Container for rebuilding
        self._list_container = tk.Frame(self, bg=BG)
        self._list_container.pack(fill="both", expand=True, side="top")

        self._build_candidate_list(candidates)

        # Pre-select the top candidate (selection mode by default)
        if candidates:
            self._sel_var.set(0)
        self._switch_to_selection()

        # Centre on parent
        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(px, 0)}+{max(py, 0)}")

        self.protocol("WM_DELETE_WINDOW", self._skip)
        self.wait_window()

    # ── Candidate list builder ──────────────────────────────────

    def _build_candidate_list(self, candidates: list[ScoredMatch]):
        """Build (or rebuild) the scrollable candidate radio list."""
        # Clear existing content
        for child in self._list_container.winfo_children():
            child.destroy()

        self._sel_var = tk.IntVar(value=-1)
        self._candidates = candidates
        self._radio_buttons: list[tk.Radiobutton] = []

        list_frame = tk.Frame(self._list_container, bg=BG, padx=14, pady=4)
        list_frame.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_frame, bg=BG_CARD, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG_CARD)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Stretch the inner frame to match the canvas width so fill="x" works
        canvas.bind("<Configure>",
                    lambda e, wid=win_id: canvas.itemconfigure(wid, width=e.width))

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        game_domain = (self._parent.cfg.get("game_domain", "baldursgate3")
                       if hasattr(self._parent, "cfg") else "baldursgate3")

        for i, sm in enumerate(candidates):
            pct = int(sm.score * 100)
            r = sm.result

            row = tk.Frame(inner, bg=BG_CARD, padx=8, pady=6)
            row.pack(fill="x", pady=1)

            # Top line: radio + score + name
            top_row = tk.Frame(row, bg=BG_CARD)
            top_row.pack(fill="x")

            rb = tk.Radiobutton(
                top_row, variable=self._sel_var, value=i,
                bg=BG_CARD, fg=FG, selectcolor=BG,
                activebackground=BG_CARD, activeforeground=ACCENT,
                highlightthickness=0, relief="flat",
                command=self._switch_to_selection,
            )
            rb.pack(side="left")
            self._radio_buttons.append(rb)

            if pct >= 80:
                badge_fg = GREEN
            elif pct >= 50:
                badge_fg = YELLOW
            else:
                badge_fg = RED

            tk.Label(top_row, text=f"[{pct}%]", bg=BG_CARD, fg=badge_fg,
                     font=("Segoe UI", 10, "bold"), width=5).pack(side="left")
            tk.Label(top_row, text=r.name, bg=BG_CARD, fg=FG,
                     font=("Segoe UI", 10), wraplength=380,
                     justify="left").pack(side="left", padx=(4, 0))

            # Bottom line: author + nexus link
            detail_row = tk.Frame(row, bg=BG_CARD)
            detail_row.pack(fill="x", padx=(30, 0))  # indent past radio button

            tk.Label(detail_row, text=f"by {r.author}",
                     bg=BG_CARD, fg=FG_DIM,
                     font=("Segoe UI", 9)).pack(side="left")

            # Show popularity stats if available
            pop_parts = []
            if r.unique_downloads:
                if r.unique_downloads >= 1_000_000:
                    pop_parts.append(f"{r.unique_downloads / 1_000_000:.1f}M downloads")
                elif r.unique_downloads >= 1_000:
                    pop_parts.append(f"{r.unique_downloads / 1_000:.0f}K downloads")
                else:
                    pop_parts.append(f"{r.unique_downloads} downloads")
            if r.endorsements:
                pop_parts.append(f"♥ {r.endorsements}")
            if pop_parts:
                tk.Label(detail_row, text=f"  ·  {' · '.join(pop_parts)}",
                         bg=BG_CARD, fg=FG_DIM,
                         font=("Segoe UI", 8)).pack(side="left")

            nexus_url = r.url or f"https://www.nexusmods.com/{game_domain}/mods/{r.mod_id}"
            link = tk.Label(detail_row,
                            text=f"\U0001F517 nexusmods.com/.../mods/{r.mod_id}",
                            bg=BG_CARD, fg=ACCENT,
                            font=("Segoe UI", 9, "underline"),
                            cursor="hand2")
            link.pack(side="left", padx=(10, 0))
            link.bind("<Button-1>", lambda e, u=nexus_url: webbrowser.open(u))

    # ── Re-search ──────────────────────────────────────────────

    def _do_research(self):
        """Re-run search with the user-edited query and rebuild the list."""
        query = self._search_var.get().strip()
        if not query:
            return

        api_key = ""
        tavily_key = ""
        if hasattr(self._parent, "cfg"):
            api_key = self._parent.cfg.get("nexus_api_key", "")
            tavily_key = self._parent.cfg.get("tavily_api_key", "")
        if not api_key:
            self._search_status.configure(text="No API key", fg=RED)
            return

        self._search_btn.configure(state="disabled", text="Searching…")
        self._search_status.configure(text="")

        mod = self._mod

        def _worker():
            try:
                from nexus_search import search_all_sources, rank_matches
                results = search_all_sources(
                    query, api_key,
                    author=mod.get("author", ""),
                    local_name=query,
                    local_author=mod.get("author", ""),
                    local_description=mod.get("description", ""),
                    local_version=mod.get("version", ""),
                    tavily_api_key=tavily_key,
                )
                scored = rank_matches(
                    query,
                    mod.get("author", ""),
                    mod.get("description", ""),
                    results,
                    local_version=mod.get("version", ""),
                )
                self.after(0, lambda: self._on_research_done(scored))
            except Exception as exc:
                self.after(0, lambda: self._on_research_error(str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_research_done(self, scored):
        """Callback when re-search completes successfully."""
        self._search_btn.configure(state="normal", text="\U0001F50D Search")
        self._search_status.configure(
            text=f"{len(scored)} result{'s' if len(scored) != 1 else ''}",
            fg=FG_DIM,
        )
        self._build_candidate_list(scored)
        if scored:
            self._sel_var.set(0)
        self._switch_to_selection()

    def _on_research_error(self, msg):
        """Callback when re-search fails."""
        self._search_btn.configure(state="normal", text="\U0001F50D Search")
        self._search_status.configure(text=f"Error: {msg[:40]}", fg=RED)

    # ── Mode switching ──────────────────────────────────────────

    def _switch_to_selection(self):
        """Activate the radio-button selection mode."""
        self._using_manual = False
        self._accept_btn.configure(text="\u2713  Accept Selection")
        self._manual_radio.deselect()
        self._manual_entry.configure(state="normal", bg=BG_CARD)
        self._manual_indicator.configure(text="")
        self._list_header.configure(fg=FG)

    def _switch_to_manual(self):
        """Activate the manual ID entry mode."""
        self._using_manual = True
        self._sel_var.set(-1)  # deselect all radio buttons
        self._accept_btn.configure(text="\u2713  Accept Manual ID")
        self._manual_radio.select()
        self._manual_entry.focus_set()
        self._manual_indicator.configure(text="\u25c0 will use this ID", fg=GREEN)
        self._list_header.configure(fg=FG_DIM)

    def _on_manual_typed(self):
        """Auto-switch to manual mode when the user types an ID."""
        if self._manual_var.get().strip() and not self._using_manual:
            self._switch_to_manual()

    # ── Actions ─────────────────────────────────────────────────

    def _accept(self):
        if self._using_manual:
            manual = self._manual_var.get().strip()
            if manual:
                try:
                    mid = int(manual)
                    self.result = NexusSearchResult(
                        mod_id=mid, name=f"Mod {mid}", url=""
                    )
                    self.destroy()
                    return
                except ValueError:
                    self._manual_indicator.configure(
                        text="\u26a0 enter a number", fg=RED,
                    )
                    return
            # empty manual field — show hint
            self._manual_indicator.configure(
                text="\u26a0 enter an ID or select above", fg=YELLOW,
            )
            return

        # Selection mode
        idx = self._sel_var.get()
        if 0 <= idx < len(self._candidates):
            self.result = self._candidates[idx].result
        self.destroy()

    def _skip(self):
        self.result = None
        self.destroy()

    def _skip_all(self):
        self.result = None
        self.skip_all = True
        self.destroy()


class ModUpdaterApp(tk.Tk):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.title("Nexus Mod Updater – Baldur's Gate 3")
        self.geometry("1100x700")
        self.minsize(900, 500)
        self.configure(bg=BG)

        self.cfg = load_config()
        self.mods: list[dict] = []          # from scanner
        self.results: dict[int, dict] = {}  # mod_id → nexus details
        self._check_thread: threading.Thread | None = None

        self._build_ui()
        self._apply_saved_state()

    # ── UI construction ─────────────────────────────────────────────

    def _build_ui(self):
        # Top bar: directory picker + API key
        top = tk.Frame(self, bg=BG, padx=10, pady=8)
        top.pack(fill="x")

        tk.Label(top, text="Mod Folder:", bg=BG, fg=FG, font=("Segoe UI", 10)).pack(side="left")
        self.dir_var = tk.StringVar()
        dir_entry = tk.Entry(top, textvariable=self.dir_var, width=50,
                             bg=BG_CARD, fg=FG, insertbackground=FG,
                             relief="flat", font=("Segoe UI", 10))
        dir_entry.pack(side="left", padx=(6, 4))
        tk.Button(top, text="Browse…", command=self._browse,
                  bg=ACCENT, fg=BG, activebackground=ACCENT,
                  relief="flat", font=("Segoe UI", 9, "bold"),
                  padx=10, cursor="hand2").pack(side="left")

        tk.Label(top, text="    API Key:", bg=BG, fg=FG, font=("Segoe UI", 10)).pack(side="left")
        self.key_var = tk.StringVar()
        key_entry = tk.Entry(top, textvariable=self.key_var, width=32, show="•",
                             bg=BG_CARD, fg=FG, insertbackground=FG,
                             relief="flat", font=("Segoe UI", 10))
        key_entry.pack(side="left", padx=(6, 4))

        # Second row: Tavily key
        top2 = tk.Frame(self, bg=BG, padx=10, pady=2)
        top2.pack(fill="x")
        tk.Label(top2, text="Tavily Key:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left")
        self.tavily_var = tk.StringVar()
        tavily_entry = tk.Entry(top2, textvariable=self.tavily_var, width=32, show="\u2022",
                                bg=BG_CARD, fg=FG, insertbackground=FG,
                                relief="flat", font=("Segoe UI", 9))
        tavily_entry.pack(side="left", padx=(6, 4))
        tk.Label(top2, text="(optional \u2013 improves web-search fallback, free at tavily.com)",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8)).pack(side="left", padx=(4, 0))

        # Action buttons
        btn_frame = tk.Frame(self, bg=BG, padx=10, pady=4)
        btn_frame.pack(fill="x")

        self.scan_btn = tk.Button(
            btn_frame, text="📂  Scan Mods", command=self._scan_mods,
            bg=ACCENT, fg=BG, activebackground=ACCENT,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=14, pady=4, cursor="hand2",
        )
        self.scan_btn.pack(side="left")

        self.check_btn = tk.Button(
            btn_frame, text="🔄  Check for Updates", command=self._check_updates,
            bg="#74c7ec", fg=BG, activebackground="#74c7ec",
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=14, pady=4, cursor="hand2",
        )
        self.check_btn.pack(side="left", padx=(10, 0))

        self.lookup_btn = tk.Button(
            btn_frame, text="🔍  Look Up Mods", command=self._lookup_mods,
            bg="#cba6f7", fg=BG, activebackground="#cba6f7",
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=14, pady=4, cursor="hand2",
        )
        self.lookup_btn.pack(side="left", padx=(10, 0))

        self.clear_cache_btn = tk.Button(
            btn_frame, text="🗑  Clear Cache", command=self._clear_cache,
            bg=RED, fg=BG, activebackground=RED,
            relief="flat", font=("Segoe UI", 9, "bold"),
            padx=10, pady=4, cursor="hand2",
        )
        self.clear_cache_btn.pack(side="left", padx=(10, 0))

        self.update_all_btn = tk.Button(
            btn_frame, text="⬇  Update All Outdated",
            command=self._update_all_outdated,
            bg=GREEN, fg=BG, activebackground=GREEN,
            relief="flat", font=("Segoe UI", 10, "bold"),
            padx=14, pady=4, cursor="hand2",
        )
        self.update_all_btn.pack(side="left", padx=(10, 0))

        self.status_var = tk.StringVar(value="Ready – point to your mods folder and scan.")
        tk.Label(btn_frame, textvariable=self.status_var, bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="right")

        # Progress bar
        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(0, 4))

        # ── Treeview table ──────────────────────────────────────────
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("Treeview",
                        background=BG_CARD, foreground=FG,
                        fieldbackground=BG_CARD, borderwidth=0,
                        font=("Segoe UI", 10), rowheight=28)
        style.configure("Treeview.Heading",
                        background=BORDER, foreground=FG,
                        font=("Segoe UI", 10, "bold"), borderwidth=1,
                        relief="raised")
        style.map("Treeview.Heading",
                  background=[("active", ACCENT)],
                  foreground=[("active", BG)])
        style.map("Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", BG)])

        columns = ("mod_name", "author", "mod_id", "nexus_link", "local_date", "nexus_date", "version", "status")
        tree_frame = tk.Frame(self, bg=BG)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                 selectmode="browse")
        self.tree.heading("mod_name", text="Mod Name",
                          command=lambda: self._sort_column("mod_name"))
        self.tree.heading("author", text="Author",
                          command=lambda: self._sort_column("author"))
        self.tree.heading("mod_id", text="Nexus ID",
                          command=lambda: self._sort_column("mod_id"))
        self.tree.heading("nexus_link", text="Nexus Page",
                          command=lambda: self._sort_column("nexus_link"))
        self.tree.heading("local_date", text="Local Date",
                          command=lambda: self._sort_column("local_date"))
        self.tree.heading("nexus_date", text="Nexus Updated",
                          command=lambda: self._sort_column("nexus_date"))
        self.tree.heading("version", text="Version",
                          command=lambda: self._sort_column("version"))
        self.tree.heading("status", text="Status",
                          command=lambda: self._sort_column("status"))

        # Sort state: column name → ascending bool
        self._sort_col = ""
        self._sort_asc = True

        self.tree.column("mod_name", width=260, minwidth=160)
        self.tree.column("author", width=110, minwidth=70)
        self.tree.column("mod_id", width=65, anchor="center")
        self.tree.column("nexus_link", width=90, anchor="center")
        self.tree.column("local_date", width=125, anchor="center")
        self.tree.column("nexus_date", width=125, anchor="center")
        self.tree.column("version", width=85, anchor="center")
        self.tree.column("status", width=120, anchor="center")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

        # Tag colours for status
        self.tree.tag_configure("uptodate", foreground=GREEN)
        self.tree.tag_configure("outdated", foreground=YELLOW)
        self.tree.tag_configure("unknown", foreground=RED)
        self.tree.tag_configure("error", foreground=RED)
        self.tree.tag_configure("not_nexus", foreground=FG_DIM)

        # Single-click on the Nexus Link column opens the URL
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        # Double-click on the Nexus ID column to edit inline
        self.tree.bind("<Double-1>", self._on_tree_dblclick)

        # Right-click context menu
        self.tree.bind("<Button-3>", self._on_tree_right_click)

        # Change cursor to hand when hovering over the link column
        self.tree.bind("<Motion>", self._on_tree_motion)

        # Inline-edit state
        self._inline_entry: tk.Entry | None = None
        self._inline_iid: str = ""

        # Bottom info bar
        bottom = tk.Frame(self, bg=BORDER, padx=10, pady=4)
        bottom.pack(fill="x", side="bottom")
        tk.Label(bottom,
                 text="🔍 Look Up Mods to find Nexus IDs by name  •  "
                      "Get your API key at nexusmods.com → My Account → API Access",
                 bg=BORDER, fg=FG_DIM, font=("Segoe UI", 8)).pack(side="left")

    # ── Saved state ─────────────────────────────────────────────────

    def _apply_saved_state(self):
        self.dir_var.set(self.cfg.get("mod_directory", ""))
        self.key_var.set(self.cfg.get("nexus_api_key", ""))
        self.tavily_var.set(self.cfg.get("tavily_api_key", ""))

    def _persist_settings(self):
        self.cfg["mod_directory"] = self.dir_var.get().strip()
        self.cfg["nexus_api_key"] = self.key_var.get().strip()
        self.cfg["tavily_api_key"] = self.tavily_var.get().strip()
        save_config(self.cfg)

    # ── Actions ─────────────────────────────────────────────────────

    def _browse(self):
        path = filedialog.askdirectory(title="Select BG3 Mods Folder")
        if path:
            self.dir_var.set(path)

    def _scan_mods(self):
        self._persist_settings()
        mod_dir = self.dir_var.get().strip()
        if not mod_dir or not os.path.isdir(mod_dir):
            messagebox.showwarning("Invalid folder", "Please select a valid mod directory.")
            return

        self.mods = scan_mod_directory(mod_dir)
        self._populate_tree()
        self.status_var.set(f"Found {len(self.mods)} mod file(s).  Press 'Check for Updates' to compare with Nexus.")

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._iid_to_mod: dict[str, dict] = {}  # map tree iid → mod dict
        for mod in self.mods:
            local_str = mod["local_date"].strftime("%Y-%m-%d %H:%M")
            mid = mod["mod_id"] if mod["mod_id"] is not None else "—"
            version = mod.get("version", "") or "—"
            author = mod.get("author", "") or "—"
            link_text = "🔗 Open" if mod["mod_id"] is not None else "—"

            # Check if permanently tagged as not from Nexus
            not_nexus = is_not_nexus(
                self.cfg, mod.get("uuid", ""), mod.get("mod_name", "")
            )
            if not_nexus:
                status_text = "🚫 Not on Nexus"
                tag = "not_nexus"
            else:
                status_text = "Pending"
                tag = "unknown"

            iid = str(id(mod))
            self.tree.insert("", "end", iid=iid,
                             values=(mod["mod_name"], author, mid, link_text, local_str, "—", version, status_text),
                             tags=(tag,))
            self._iid_to_mod[iid] = mod
        self._update_sort_headings()

    # ── Column sorting ───────────────────────────────────────────

    _COLUMN_LABELS = {
        "mod_name": "Mod Name", "author": "Author", "mod_id": "Nexus ID",
        "nexus_link": "Nexus Page", "local_date": "Local Date",
        "nexus_date": "Nexus Updated", "version": "Version", "status": "Status",
    }

    def _sort_column(self, col: str):
        """Sort the treeview by *col*.  Click again to reverse."""
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True

        col_index = list(self._COLUMN_LABELS.keys()).index(col)

        rows = [
            (self.tree.set(iid, col), iid)
            for iid in self.tree.get_children("")
        ]

        # Decide sort key type
        if col == "mod_id":
            def key(val):
                try:
                    return (0, int(val))
                except (ValueError, TypeError):
                    return (1, 0)  # — sorts last
        elif col in ("local_date", "nexus_date"):
            def key(val):
                if val == "—" or not val:
                    return "9999"  # blanks last
                return val  # ISO format sorts lexically
        else:
            def key(val):
                return val.lower() if isinstance(val, str) else val

        rows.sort(key=lambda r: key(r[0]), reverse=not self._sort_asc)

        for index, (_, iid) in enumerate(rows):
            self.tree.move(iid, "", index)

        self._update_sort_headings()

    def _update_sort_headings(self):
        """Show ▲/▼ indicator on the active sort column."""
        for col, label in self._COLUMN_LABELS.items():
            if col == self._sort_col:
                arrow = " ▲" if self._sort_asc else " ▼"
                self.tree.heading(col, text=label + arrow)
            else:
                self.tree.heading(col, text=label)

    # ── Nexus mod lookup ────────────────────────────────────────

    def _lookup_mods(self):
        """Search Nexus for mods that have no Nexus ID."""
        if self._check_thread and self._check_thread.is_alive():
            messagebox.showinfo("Busy", "An operation is already running.")
            return
        if not self.mods:
            messagebox.showinfo("No mods", "Scan your mod folder first.")
            return

        self._persist_settings()
        api_key = self.key_var.get().strip()
        if not api_key:
            messagebox.showwarning("API Key Required",
                                   "Enter your Nexus Mods API key to search for mods.")
            return

        # Resolve cached mappings first
        self._resolve_cached_ids()

        unmatched = [
            m for m in self.mods
            if m["mod_id"] is None and m.get("mod_name")
            and not is_skipped(self.cfg, m.get("uuid", ""), m.get("mod_name", ""))
            and not is_not_nexus(self.cfg, m.get("uuid", ""), m.get("mod_name", ""))
        ]
        if not unmatched:
            messagebox.showinfo("All matched",
                                "Every mod already has a Nexus ID\n"
                                "(some may be in the skip list or marked as not on Nexus).")
            return

        self._set_buttons_busy(True)
        self.progress["maximum"] = len(unmatched)
        self.progress["value"] = 0
        self.status_var.set(f"Searching Nexus for {len(unmatched)} mod(s)…")

        self._check_thread = threading.Thread(
            target=self._worker_lookup, args=(unmatched, api_key), daemon=True
        )
        self._check_thread.start()

    def _worker_lookup(self, unmatched: list[dict], api_key: str):
        """Background: search Nexus concurrently for each unmatched mod."""
        game = self.cfg.get("game_domain", "baldursgate3")
        tavily_key = self.cfg.get("tavily_api_key", "")
        completed = 0
        total = len(unmatched)
        lock = threading.Lock()
        all_matches: list[tuple[dict, list[ScoredMatch]]] = []

        def _search_one(mod: dict) -> tuple[dict, list[ScoredMatch]] | None:
            nonlocal completed
            name = mod["mod_name"]
            _author = mod.get("author", "")
            _desc = mod.get("description", "")
            _ver = mod.get("version", "")
            self.after(0, lambda n=name: self.status_var.set(
                f"Searching: {n}"
            ))
            try:
                results = search_all_sources(
                    name, api_key, game,
                    author=_author,
                    local_name=name,
                    local_author=_author,
                    local_description=_desc,
                    local_version=_ver,
                    tavily_api_key=tavily_key,
                )
                if results:
                    scored = rank_matches(
                        name, _author, _desc, results,
                        local_version=_ver,
                    )
                    if scored:
                        return (mod, scored)
            except Exception:
                pass
            finally:
                with lock:
                    completed += 1
                self.after(0, lambda v=completed: self.progress.configure(value=v))
                import time
                time.sleep(SEARCH_DELAY)  # per-request cooldown
            return None

        with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
            futures = {pool.submit(_search_one, mod): mod for mod in unmatched}
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    all_matches.append(result)

        self.after(0, lambda: self._process_lookup_results(all_matches))

    def _find_holder(self, nexus_id: int) -> dict | None:
        """Return the mod dict that currently holds *nexus_id*, or None."""
        for m in self.mods:
            if m.get("mod_id") == nexus_id:
                return m
        return None

    def _process_lookup_results(
        self, all_matches: list[tuple[dict, list[ScoredMatch]]]
    ):
        """Main thread: auto-accept confident matches, prompt for the rest."""
        auto_count = 0
        manual_count = 0
        skip_count = 0
        dedup_count = 0
        skip_all = False

        # Collect IDs already in use (filename-extracted or cached)
        used_ids: set[int] = {
            m["mod_id"] for m in self.mods if m["mod_id"] is not None
        }

        # Pass 1 – auto-accept high-confidence matches; queue conflicts
        ambiguous: list[tuple[dict, list[ScoredMatch]]] = []
        conflicts: list[tuple[dict, list[ScoredMatch]]] = []
        for mod, scored in all_matches:
            if scored[0].score >= AUTO_MATCH_THRESHOLD:
                candidate_id = scored[0].result.mod_id
                if candidate_id in used_ids:
                    conflicts.append((mod, scored))
                    dedup_count += 1
                else:
                    mod["mod_id"] = candidate_id
                    used_ids.add(candidate_id)
                    self._cache_mod_mapping(
                        mod, confidence=f"auto-{scored[0].score:.2f}")
                    auto_count += 1
            else:
                ambiguous.append((mod, scored))

        # Pass 1b – resolve duplicate-ID conflicts via dedicated dialog
        for cidx, (mod, scored) in enumerate(conflicts, 1):
            candidate_id = scored[0].result.mod_id
            holder = self._find_holder(candidate_id)
            if holder is None:
                # Holder was evicted by an earlier conflict resolution
                mod["mod_id"] = candidate_id
                used_ids.add(candidate_id)
                self._cache_mod_mapping(
                    mod, confidence=f"auto-{scored[0].score:.2f}")
                auto_count += 1
                dedup_count -= 1
                continue

            cdlg = DuplicateConflictDialog(
                self, mod, scored[0], holder,
                current=cidx, total=len(conflicts),
            )

            if cdlg.choice == DuplicateConflictDialog.REASSIGN:
                self._evict_nexus_id(candidate_id)
                mod["mod_id"] = candidate_id
                used_ids.add(candidate_id)
                self._cache_mod_mapping(mod, confidence=f"conflict-reassign-{scored[0].score:.2f}")
                manual_count += 1
            elif cdlg.choice == DuplicateConflictDialog.SEARCH:
                # Fall through to the normal match dialog
                ambiguous.append((mod, scored))
            else:
                # KEEP – skip this mod for now
                mark_skipped(self.cfg, mod.get("uuid", ""), mod.get("mod_name", ""))
                skip_count += 1

        # Pass 2 – user confirmation dialogs for ambiguous matches
        total_ambiguous = len(ambiguous)
        for idx, (mod, scored) in enumerate(ambiguous, 1):
            if skip_all:
                mark_skipped(self.cfg, mod.get("uuid", ""), mod.get("mod_name", ""))
                skip_count += 1
                continue
            dlg = NexusMatchDialog(self, mod, scored,
                                  current=idx, total=total_ambiguous)
            if dlg.skip_all:
                skip_all = True
                mark_skipped(self.cfg, mod.get("uuid", ""), mod.get("mod_name", ""))
                skip_count += 1
            elif dlg.result is not None:
                candidate_id = dlg.result.mod_id
                if candidate_id in used_ids:
                    # Manual selection is authoritative – evict the old holder
                    self._evict_nexus_id(candidate_id)
                mod["mod_id"] = candidate_id
                used_ids.add(candidate_id)
                self._cache_mod_mapping(mod, confidence="manual")
                manual_count += 1
            else:
                # User clicked Skip
                mark_skipped(self.cfg, mod.get("uuid", ""), mod.get("mod_name", ""))
                skip_count += 1

        matched = auto_count + manual_count
        parts = [f"Lookup done – {matched} matched ({auto_count} auto, {manual_count} manual)"]
        if skip_count:
            parts.append(f"{skip_count} skipped")
        if dedup_count:
            parts.append(f"{dedup_count} duplicate(s) resolved")
        self._populate_tree()
        self.status_var.set(
            ".  ".join(parts) + ".  Press 'Check for Updates' to compare with Nexus."
        )
        self._set_buttons_busy(False)

    # ── Helpers ─────────────────────────────────────────────────

    def _resolve_cached_ids(self):
        """Resolve Nexus IDs from cached UUID and name mappings."""
        for mod in self.mods:
            if mod["mod_id"] is not None:
                continue
            # Try UUID cache first
            if mod.get("uuid"):
                cached = get_cached_nexus_id(self.cfg, mod["uuid"])
                if cached is not None:
                    mod["mod_id"] = cached
                    continue
            # Fall back to name cache
            if mod.get("mod_name"):
                cached = get_cached_nexus_id_by_name(self.cfg, mod["mod_name"])
                if cached is not None:
                    mod["mod_id"] = cached

    def _set_buttons_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.check_btn.configure(state=state)
        self.scan_btn.configure(state=state)
        self.lookup_btn.configure(state=state)
        self.clear_cache_btn.configure(state=state)
        self.update_all_btn.configure(state=state)

    def _evict_nexus_id(self, nexus_id: int):
        """Remove *nexus_id* from whichever mod currently holds it.

        This is used when a manual selection overrides an earlier
        auto-match or cached mapping – the user's choice is
        authoritative.
        """
        for other in self.mods:
            if other.get("mod_id") == nexus_id:
                other["mod_id"] = None
                # Purge stale cache entries for that mod
                if other.get("uuid"):
                    self.cfg.get("uuid_to_nexus_id", {}).pop(other["uuid"], None)
                if other.get("mod_name"):
                    self.cfg.get("name_to_nexus_id", {}).pop(other["mod_name"], None)
                break  # IDs are unique – only one holder

    def _cache_mod_mapping(self, mod: dict, confidence: str = ""):
        """Cache a confirmed mod→Nexus ID mapping via UUID and/or name."""
        nexus_id = mod["mod_id"]
        if nexus_id is None:
            return
        if mod.get("uuid"):
            cache_nexus_id(self.cfg, mod["uuid"], nexus_id, confidence)
        if mod.get("mod_name"):
            cache_nexus_id_by_name(self.cfg, mod["mod_name"], nexus_id, confidence)

    def _clear_cache(self):
        """Clear all cached Nexus ID mappings and skip lists."""
        if messagebox.askyesno(
            "Clear Cache",
            "This will clear all cached Nexus ID mappings and skip lists.\n\n"
            "You will need to re-scan and look up mods again.\nContinue?",
        ):
            clear_cache(self.cfg)
            # Reset mod IDs that came from cache (keep filename-extracted ones)
            for mod in self.mods:
                if mod.get("_id_from_filename"):
                    continue  # keep filename-extracted IDs
                # We can't reliably distinguish, so just clear all
            self.status_var.set("Cache cleared. Re-scan and look up mods again.")

    # ── Update check ───────────────────────────────────────────

    def _check_updates(self):
        if self._check_thread and self._check_thread.is_alive():
            messagebox.showinfo("Busy", "An update check is already running.")
            return

        if not self.mods:
            messagebox.showinfo("No mods", "Scan your mod folder first.")
            return

        self._persist_settings()
        api_key = self.key_var.get().strip()

        # Resolve cached UUID→Nexus ID mappings
        self._resolve_cached_ids()

        # If there are unmatched mods, offer to look them up first
        unmatched = [
            m for m in self.mods
            if m["mod_id"] is None and m.get("mod_name")
            and not is_skipped(self.cfg, m.get("uuid", ""), m.get("mod_name", ""))
            and not is_not_nexus(self.cfg, m.get("uuid", ""), m.get("mod_name", ""))
        ]
        if unmatched and api_key:
            answer = messagebox.askyesno(
                "Search for Nexus IDs",
                f"{len(unmatched)} mod(s) don't have Nexus IDs.\n\n"
                "Would you like to search Nexus Mods to find matches?\n"
                "(You can also do this later with the 'Look Up Mods' button.)",
            )
            if answer:
                # Run lookup, which will call back when done
                self._set_buttons_busy(True)
                self.progress["maximum"] = len(unmatched)
                self.progress["value"] = 0
                self.status_var.set(f"Searching Nexus for {len(unmatched)} mod(s)…")
                self._check_thread = threading.Thread(
                    target=self._worker_lookup_then_check,
                    args=(unmatched, api_key), daemon=True,
                )
                self._check_thread.start()
                return

        self._start_update_check(api_key)

    def _worker_lookup_then_check(self, unmatched, api_key):
        """Background: lookup unknowns concurrently, then continue to update check."""
        game = self.cfg.get("game_domain", "baldursgate3")
        tavily_key = self.cfg.get("tavily_api_key", "")
        completed = 0
        total = len(unmatched)
        lock = threading.Lock()
        all_matches: list[tuple[dict, list[ScoredMatch]]] = []

        def _search_one(mod: dict) -> tuple[dict, list[ScoredMatch]] | None:
            nonlocal completed
            name = mod["mod_name"]
            _author = mod.get("author", "")
            _desc = mod.get("description", "")
            _ver = mod.get("version", "")
            self.after(0, lambda n=name: self.status_var.set(
                f"Searching: {n}"
            ))
            try:
                results = search_all_sources(
                    name, api_key, game,
                    author=_author,
                    local_name=name,
                    local_author=_author,
                    local_description=_desc,
                    local_version=_ver,
                    tavily_api_key=tavily_key,
                )
                if results:
                    scored = rank_matches(
                        name, _author, _desc, results,
                        local_version=_ver,
                    )
                    if scored:
                        return (mod, scored)
            except Exception:
                pass
            finally:
                with lock:
                    completed += 1
                self.after(0, lambda v=completed: self.progress.configure(value=v))
                import time
                time.sleep(SEARCH_DELAY)
            return None

        with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
            futures = {pool.submit(_search_one, mod): mod for mod in unmatched}
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    all_matches.append(result)

        self.after(0, lambda: self._finish_lookup_then_check(all_matches, api_key))

    def _finish_lookup_then_check(self, all_matches, api_key):
        """Main thread: process lookup results then start the update check."""
        auto_count = 0
        manual_count = 0
        skip_count = 0
        dedup_count = 0
        skip_all = False

        # Collect IDs already in use
        used_ids: set[int] = {
            m["mod_id"] for m in self.mods if m["mod_id"] is not None
        }

        ambiguous = []
        conflicts = []
        for mod, scored in all_matches:
            if scored[0].score >= AUTO_MATCH_THRESHOLD:
                candidate_id = scored[0].result.mod_id
                if candidate_id in used_ids:
                    conflicts.append((mod, scored))
                    dedup_count += 1
                else:
                    mod["mod_id"] = candidate_id
                    used_ids.add(candidate_id)
                    self._cache_mod_mapping(
                        mod, confidence=f"auto-{scored[0].score:.2f}")
                    auto_count += 1
            else:
                ambiguous.append((mod, scored))

        # Resolve duplicate-ID conflicts via dedicated dialog
        for cidx, (mod, scored) in enumerate(conflicts, 1):
            candidate_id = scored[0].result.mod_id
            holder = self._find_holder(candidate_id)
            if holder is None:
                mod["mod_id"] = candidate_id
                used_ids.add(candidate_id)
                self._cache_mod_mapping(
                    mod, confidence=f"auto-{scored[0].score:.2f}")
                auto_count += 1
                dedup_count -= 1
                continue

            cdlg = DuplicateConflictDialog(
                self, mod, scored[0], holder,
                current=cidx, total=len(conflicts),
            )

            if cdlg.choice == DuplicateConflictDialog.REASSIGN:
                self._evict_nexus_id(candidate_id)
                mod["mod_id"] = candidate_id
                used_ids.add(candidate_id)
                self._cache_mod_mapping(mod, confidence=f"conflict-reassign-{scored[0].score:.2f}")
                manual_count += 1
            elif cdlg.choice == DuplicateConflictDialog.SEARCH:
                ambiguous.append((mod, scored))
            else:
                mark_skipped(self.cfg, mod.get("uuid", ""), mod.get("mod_name", ""))
                skip_count += 1

        total_ambiguous = len(ambiguous)
        for idx, (mod, scored) in enumerate(ambiguous, 1):
            if skip_all:
                mark_skipped(self.cfg, mod.get("uuid", ""), mod.get("mod_name", ""))
                skip_count += 1
                continue
            dlg = NexusMatchDialog(self, mod, scored,
                                  current=idx, total=total_ambiguous)
            if dlg.skip_all:
                skip_all = True
                mark_skipped(self.cfg, mod.get("uuid", ""), mod.get("mod_name", ""))
                skip_count += 1
            elif dlg.result is not None:
                candidate_id = dlg.result.mod_id
                if candidate_id in used_ids:
                    # Manual selection is authoritative – evict the old holder
                    self._evict_nexus_id(candidate_id)
                mod["mod_id"] = candidate_id
                used_ids.add(candidate_id)
                self._cache_mod_mapping(mod, confidence="manual")
                manual_count += 1
            else:
                mark_skipped(self.cfg, mod.get("uuid", ""), mod.get("mod_name", ""))
                skip_count += 1

        matched = auto_count + manual_count
        if matched:
            self._populate_tree()
            self.status_var.set(
                f"Matched {matched} mod(s). Starting update check…"
            )

        self._set_buttons_busy(False)
        self._start_update_check(api_key)

    def _start_update_check(self, api_key: str):
        """Begin the actual Nexus update check for mods that have IDs."""
        checkable = [m for m in self.mods if m["mod_id"] is not None]
        if not checkable:
            messagebox.showwarning(
                "No Nexus IDs found",
                "None of the scanned mods could be matched to Nexus.\n\n"
                "Try the 'Look Up Mods' button to search by name,\n"
                "or make sure the files haven't been renamed.",
            )
            return

        self._populate_tree()  # refresh to show newly matched IDs
        self.progress["maximum"] = len(checkable)
        self.progress["value"] = 0
        self._set_buttons_busy(True)
        self.status_var.set("Checking updates…")

        self._check_thread = threading.Thread(
            target=self._worker_check, args=(checkable, api_key), daemon=True
        )
        self._check_thread.start()

    def _worker_check(self, checkable: list[dict], api_key: str):
        """Background thread that queries Nexus concurrently for each mod."""
        api = None
        if api_key:
            try:
                api = NexusAPI(api_key, self.cfg.get("game_domain", "baldursgate3"))
                api.validate_key()
            except NexusAPIError as exc:
                self.after(0, lambda: messagebox.showerror("API Error", str(exc)))
                self.after(0, self._worker_done)
                return

        import time
        outdated_count = 0
        completed = 0
        lock = threading.Lock()
        game = self.cfg.get("game_domain", "baldursgate3")

        def _check_one(mod):
            nonlocal outdated_count, completed
            mod_id = mod["mod_id"]
            self.after(0, lambda mi=mod_id: self.status_var.set(
                f"Checking mod ID {mi}…"
            ))
            try:
                if api:
                    details = api.get_mod_details(mod_id)
                else:
                    details = scrape_mod_updated(mod_id, game)

                if details:
                    self.results[mod_id] = details
                    self.after(0, lambda m=mod: self._cache_mod_mapping(m))
                    nexus_dt = details.get("nexus_updated")
                    is_outdated = False
                    if nexus_dt and mod["local_date"]:
                        is_outdated = nexus_dt > mod["local_date"]
                    if is_outdated:
                        with lock:
                            outdated_count += 1
                    self.after(0, lambda m=mod, d=details, o=is_outdated: self._update_row(m, d, o))
                else:
                    self.after(0, lambda m=mod: self._mark_row_error(m, "Not found"))
            except (NexusAPIError, Exception) as exc:
                self.after(0, lambda m=mod, e=str(exc): self._mark_row_error(m, e))
            finally:
                with lock:
                    completed += 1
                self.after(0, lambda v=completed: self.progress.configure(value=v))
                time.sleep(SEARCH_DELAY)

        with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
            futures = [pool.submit(_check_one, mod) for mod in checkable]
            for f in as_completed(futures):
                f.result()  # propagate any unexpected exceptions

        self.after(0, lambda: self.status_var.set(
            f"Done – {outdated_count} mod(s) may need updating out of {len(checkable)} checked."
        ))
        self.after(0, self._worker_done)

    def _worker_done(self):
        self._set_buttons_busy(False)

    def _update_row(self, mod: dict, details: dict, outdated: bool):
        iid = str(id(mod))
        try:
            nexus_str = "—"
            ndt = details.get("nexus_updated")
            if ndt:
                nexus_str = ndt.strftime("%Y-%m-%d %H:%M")

            status = "✅ Up to date" if not outdated else "⚠️ Update available"
            tag = "uptodate" if not outdated else "outdated"
            link_text = "🔗 Open" if mod["mod_id"] is not None else "—"

            self.tree.item(iid, values=(
                mod["mod_name"],
                mod.get("author", "") or details.get("author", "—"),
                mod["mod_id"],
                link_text,
                mod["local_date"].strftime("%Y-%m-%d %H:%M"),
                nexus_str,
                mod.get("version", "") or details.get("version", "?"),
                status,
            ), tags=(tag,))
        except tk.TclError:
            pass

    def _mark_row_error(self, mod: dict, msg: str):
        iid = str(id(mod))
        try:
            self.tree.item(iid, values=(
                mod["mod_name"],
                mod.get("author", "—"),
                mod["mod_id"],
                "🔗 Open" if mod["mod_id"] is not None else "—",
                mod["local_date"].strftime("%Y-%m-%d %H:%M"),
                "—",
                mod.get("version", "—"),
                f"❌ {msg[:30]}",
            ), tags=("error",))
        except tk.TclError:
            pass

    # ── Inline Nexus ID editing ─────────────────────────────────────

    def _on_tree_dblclick(self, event):
        """Start inline editing when the user double-clicks the Nexus ID cell."""
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        # mod_id is the 3rd column → "#3"
        if col != "#3":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self._start_inline_edit(row)

    def _start_inline_edit(self, iid: str):
        """Overlay a small Entry widget on the Nexus ID cell for editing."""
        # Cancel any existing inline edit
        self._cancel_inline_edit()

        # Get cell bounding box: column "#3" = mod_id
        try:
            bbox = self.tree.bbox(iid, column="mod_id")
        except Exception:
            return
        if not bbox:
            return
        x, y, w, h = bbox

        current_values = self.tree.item(iid, "values")
        current_id = current_values[2] if current_values else ""
        if current_id == "—":
            current_id = ""

        self._inline_iid = iid
        self._inline_entry = tk.Entry(
            self.tree, width=8,
            bg="#313244", fg=ACCENT, insertbackground=ACCENT,
            relief="solid", bd=1,
            font=("Segoe UI", 10),
            justify="center",
        )
        self._inline_entry.insert(0, str(current_id))
        self._inline_entry.select_range(0, "end")
        self._inline_entry.place(x=x, y=y, width=w, height=h)
        self._inline_entry.focus_set()

        self._inline_entry.bind("<Return>", lambda e: self._commit_inline_edit())
        self._inline_entry.bind("<Escape>", lambda e: self._cancel_inline_edit())
        self._inline_entry.bind("<FocusOut>", lambda e: self._commit_inline_edit())

    def _commit_inline_edit(self):
        """Save the edited Nexus ID to the mod and cache."""
        if self._inline_entry is None:
            return
        raw = self._inline_entry.get().strip()
        iid = self._inline_iid
        self._destroy_inline_entry()

        mod = getattr(self, "_iid_to_mod", {}).get(iid)
        if mod is None:
            return

        if not raw or raw == "—":
            # Clear the ID
            old_id = mod.get("mod_id")
            mod["mod_id"] = None
            self._update_tree_row(iid, mod)
            if old_id is not None:
                self._evict_nexus_id(old_id)
            return

        try:
            new_id = int(raw)
        except ValueError:
            # Invalid input – revert silently
            return

        old_id = mod.get("mod_id")
        if new_id == old_id:
            return  # no change

        # If another mod holds this ID, evict it
        holder = self._find_holder(new_id)
        if holder is not None and holder is not mod:
            ok = messagebox.askyesno(
                "ID Conflict",
                f"Nexus ID {new_id} is currently assigned to "
                f"\"{holder.get('mod_name', '?')}\"."
                f"\n\nReassign it to \"{mod.get('mod_name', '?')}\"?",
            )
            if not ok:
                return
            self._evict_nexus_id(new_id)
            # Update the holder's row in the tree
            holder_iid = self._mod_to_iid(holder)
            if holder_iid:
                self._update_tree_row(holder_iid, holder)

        mod["mod_id"] = new_id
        self._cache_mod_mapping(mod, confidence="manual-edit")
        self._update_tree_row(iid, mod)

    def _cancel_inline_edit(self):
        """Discard the inline edit."""
        self._destroy_inline_entry()

    def _destroy_inline_entry(self):
        if self._inline_entry is not None:
            self._inline_entry.destroy()
            self._inline_entry = None
            self._inline_iid = ""

    def _mod_to_iid(self, mod: dict) -> str | None:
        """Return the tree IID for a mod dict, or None."""
        for iid, m in getattr(self, "_iid_to_mod", {}).items():
            if m is mod:
                return iid
        return None

    def _update_tree_row(self, iid: str, mod: dict):
        """Refresh a single tree row's Nexus ID and link columns."""
        mid = mod["mod_id"] if mod["mod_id"] is not None else "—"
        link_text = "🔗 Open" if mod["mod_id"] is not None else "—"
        values = list(self.tree.item(iid, "values"))
        if len(values) >= 4:
            values[2] = mid
            values[3] = link_text
            self.tree.item(iid, values=values)

    # ── Tree click handlers ─────────────────────────────────────────

    def _on_tree_click(self, event):
        """Open the Nexus page when the user clicks the link column."""
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        # col is like "#4" — nexus_link is the 4th column
        if col != "#4":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        values = self.tree.item(row, "values")
        if not values:
            return
        mod_id_str = values[2]  # mod_id is the 3rd column (index 2)
        if mod_id_str and mod_id_str != "—":
            try:
                mod_id = int(mod_id_str)
                domain = self.cfg.get("game_domain", "baldursgate3")
                url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
                webbrowser.open(url)
            except ValueError:
                pass

    def _on_tree_motion(self, event):
        """Switch cursor to a hand pointer when hovering over the link column."""
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if col == "#4" and row:
            values = self.tree.item(row, "values")
            if values and values[2] != "—":  # has a mod_id
                self.tree.configure(cursor="hand2")
                return
        self.tree.configure(cursor="")

    # ── Right-click context menu ────────────────────────────────────

    def _on_tree_right_click(self, event):
        """Show a context menu for the clicked row."""
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)

        mod = getattr(self, "_iid_to_mod", {}).get(row)
        if mod is None:
            return

        menu = tk.Menu(self, tearoff=0, bg=BG_CARD, fg=FG,
                       activebackground=ACCENT, activeforeground=BG,
                       font=("Segoe UI", 10))

        # "Not on Nexus" toggle
        tagged = is_not_nexus(
            self.cfg, mod.get("uuid", ""), mod.get("mod_name", "")
        )
        if tagged:
            menu.add_command(
                label="✅  Unmark 'Not on Nexus'",
                command=lambda: self._toggle_not_nexus(row, mod, False),
            )
        else:
            menu.add_command(
                label="🚫  Mark as Not on Nexus",
                command=lambda: self._toggle_not_nexus(row, mod, True),
            )

        menu.add_separator()

        # Update this mod (only if outdated and has an ID)
        has_id = mod.get("mod_id") is not None
        is_outdated = False
        if has_id:
            vals = self.tree.item(row, "values")
            if vals and "Update available" in str(vals[7]):
                is_outdated = True

        if is_outdated:
            menu.add_command(
                label="⬇  Update This Mod",
                command=lambda: self._update_single_mod(row, mod),
            )
        elif has_id:
            menu.add_command(
                label="⬇  Update This Mod", state="disabled",
            )

        # Edit Nexus ID
        menu.add_command(
            label="✏️  Edit Nexus ID",
            command=lambda: self._start_inline_edit(row),
        )

        # Open on Nexus
        if has_id:
            menu.add_command(
                label="🔗  Open on Nexus",
                command=lambda: self._open_nexus_page(mod),
            )

        menu.tk_popup(event.x_root, event.y_root)

    def _open_nexus_page(self, mod: dict):
        """Open the Nexus Mods page for this mod."""
        mod_id = mod.get("mod_id")
        if mod_id is not None:
            domain = self.cfg.get("game_domain", "baldursgate3")
            url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
            webbrowser.open(url)

    def _toggle_not_nexus(self, iid: str, mod: dict, mark: bool):
        """Toggle the 'Not on Nexus' tag for a mod."""
        uuid = mod.get("uuid", "")
        name = mod.get("mod_name", "")
        if mark:
            mark_not_nexus(self.cfg, uuid, name)
            # Clear the mod_id since user says it's not from Nexus
            mod["mod_id"] = None
            self.tree.item(iid, values=(
                mod["mod_name"],
                mod.get("author", "") or "—",
                "—",
                "—",
                mod["local_date"].strftime("%Y-%m-%d %H:%M"),
                "—",
                mod.get("version", "") or "—",
                "🚫 Not on Nexus",
            ), tags=("not_nexus",))
            log.info("Marked '%s' as not on Nexus", name)
        else:
            unmark_not_nexus(self.cfg, uuid, name)
            mid = mod["mod_id"] if mod["mod_id"] is not None else "—"
            link_text = "🔗 Open" if mod["mod_id"] is not None else "—"
            self.tree.item(iid, values=(
                mod["mod_name"],
                mod.get("author", "") or "—",
                mid,
                link_text,
                mod["local_date"].strftime("%Y-%m-%d %H:%M"),
                "—",
                mod.get("version", "") or "—",
                "Pending",
            ), tags=("unknown",))
            log.info("Unmarked '%s' as not on Nexus", name)

    # ── Archive extraction ───────────────────────────────────────────

    _ARCHIVE_EXTS = {".zip", ".7z", ".rar"}

    def _extract_archive(self, archive_path: str, mod_dir: str) -> list[str]:
        """Extract .pak files from an archive into *mod_dir*.

        Returns the list of extracted .pak paths.  Removes the archive
        file after successful extraction.  Supports .zip, .7z, and .rar.
        """
        ext = os.path.splitext(archive_path)[1].lower()
        if ext not in self._ARCHIVE_EXTS:
            return []  # not an archive – nothing to do

        extracted: list[str] = []

        try:
            if ext == ".zip":
                import zipfile
                with zipfile.ZipFile(archive_path, "r") as zf:
                    for name in zf.namelist():
                        if name.lower().endswith(".pak"):
                            # Flatten path – extract to mod_dir root
                            base = os.path.basename(name)
                            dest = os.path.join(mod_dir, base)
                            with zf.open(name) as src, open(dest, "wb") as dst:
                                import shutil
                                shutil.copyfileobj(src, dst)
                            extracted.append(dest)
                            log.info("Extracted %s → %s", name, dest)

            elif ext == ".7z":
                import py7zr
                with py7zr.SevenZipFile(archive_path, "r") as sz:
                    all_names = sz.getnames()
                    pak_names = [n for n in all_names if n.lower().endswith(".pak")]
                    if pak_names:
                        sz.extract(targets=pak_names, path=mod_dir)
                        for name in pak_names:
                            # py7zr preserves directory structure
                            full = os.path.join(mod_dir, name)
                            base_dest = os.path.join(mod_dir, os.path.basename(name))
                            if os.path.isfile(full) and full != base_dest:
                                os.replace(full, base_dest)
                            extracted.append(base_dest)
                            log.info("Extracted %s → %s", name, base_dest)
                        # Clean up leftover empty dirs from nested extraction
                        for name in pak_names:
                            parent = os.path.dirname(os.path.join(mod_dir, name))
                            while parent and parent != mod_dir:
                                try:
                                    os.rmdir(parent)
                                except OSError:
                                    break
                                parent = os.path.dirname(parent)

            elif ext == ".rar":
                import rarfile
                with rarfile.RarFile(archive_path, "r") as rf:
                    for info in rf.infolist():
                        if info.filename.lower().endswith(".pak"):
                            base = os.path.basename(info.filename)
                            dest = os.path.join(mod_dir, base)
                            with rf.open(info) as src, open(dest, "wb") as dst:
                                import shutil
                                shutil.copyfileobj(src, dst)
                            extracted.append(dest)
                            log.info("Extracted %s → %s", info.filename, dest)

        except Exception as exc:
            log.error("Failed to extract %s: %s", archive_path, exc)
            return extracted  # return whatever we managed

        if extracted:
            # Remove the archive now that .pak files are in place
            try:
                os.remove(archive_path)
                log.info("Removed archive after extraction: %s", archive_path)
            except OSError as e:
                log.warning("Could not remove archive %s: %s", archive_path, e)
        else:
            log.warning("No .pak files found inside %s – keeping archive", archive_path)

        return extracted

    # ── Single mod update ───────────────────────────────────────────

    def _update_single_mod(self, iid: str, mod: dict):
        """Download and install the latest version of a single mod."""
        api_key = self.key_var.get().strip()
        if not api_key:
            messagebox.showwarning("API Key Required",
                                   "Enter your Nexus Mods API key to download updates.")
            return

        mod_id = mod["mod_id"]
        if mod_id is None:
            return

        self._set_buttons_busy(True)
        self.status_var.set(f"Downloading update for {mod['mod_name']}…")
        self.progress["maximum"] = 1
        self.progress["value"] = 0

        self._check_thread = threading.Thread(
            target=self._worker_download_mod,
            args=(mod, api_key, True),
            daemon=True,
        )
        self._check_thread.start()

    def _worker_download_mod(self, mod: dict, api_key: str, verify: bool = True):
        """Background: download and install the latest file for a mod.

        If *verify* is True, re-scans and re-checks after install.
        """
        import requests
        import time

        domain = self.cfg.get("game_domain", "baldursgate3")
        mod_id = mod["mod_id"]
        mod_dir = self.dir_var.get().strip()

        try:
            api = NexusAPI(api_key, domain)

            # Find the main file
            self.after(0, lambda: self.status_var.set(
                f"Finding latest file for {mod['mod_name']}…"
            ))
            main_file = api.get_main_file(mod_id)
            if not main_file:
                self.after(0, lambda: messagebox.showwarning(
                    "No Files",
                    f"No downloadable files found for {mod['mod_name']}.",
                ))
                self.after(0, self._worker_done)
                return

            file_id = main_file["file_id"]
            file_name = main_file.get("file_name", f"mod_{mod_id}.pak")

            # Try to get direct download links (requires Premium)
            try:
                links = api.get_download_links(mod_id, file_id)
            except NexusAPIError:
                links = []

            if not links:
                # Non-premium: open the files page in the browser
                url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files"
                self.after(0, lambda u=url: (
                    messagebox.showinfo(
                        "Manual Download Required",
                        f"Direct download requires a Nexus Premium API key.\n\n"
                        f"Opening the mod's files page in your browser.\n"
                        f"Download the file and place it in your mod folder.\n\n"
                        f"File: {file_name}",
                    ),
                    webbrowser.open(u),
                ))
                self.after(0, self._worker_done)
                return

            # Premium: download directly
            download_url = links[0].get("URI", "")
            if not download_url:
                self.after(0, lambda: messagebox.showerror(
                    "Download Error", "No download URL returned from Nexus."
                ))
                self.after(0, self._worker_done)
                return

            self.after(0, lambda fn=file_name: self.status_var.set(
                f"Downloading {fn}…"
            ))

            dest_path = os.path.join(mod_dir, file_name)

            # Download with progress
            resp = requests.get(download_url, stream=True, timeout=120)
            resp.raise_for_status()
            total_size = int(resp.headers.get("content-length", 0))
            if total_size > 0:
                self.after(0, lambda t=total_size: self.progress.configure(
                    maximum=t, value=0
                ))

            downloaded = 0
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    self.after(0, lambda d=downloaded: self.progress.configure(value=d))

            # Remove the old file if different from the new one
            old_path = mod.get("filepath", "")
            if old_path and os.path.isfile(old_path) and os.path.normpath(old_path) != os.path.normpath(dest_path):
                try:
                    os.remove(old_path)
                    log.info("Removed old mod file: %s", old_path)
                except OSError as e:
                    log.warning("Could not remove old file %s: %s", old_path, e)

            log.info("Downloaded %s → %s", file_name, dest_path)

            # Extract .pak files if the download is an archive
            archive_ext = os.path.splitext(dest_path)[1].lower()
            if archive_ext in self._ARCHIVE_EXTS:
                self.after(0, lambda: self.status_var.set(
                    f"Extracting .pak files from {file_name}…"
                ))
                pak_files = self._extract_archive(dest_path, mod_dir)
                if pak_files:
                    log.info("Extracted %d .pak file(s) from %s", len(pak_files), file_name)
                else:
                    log.warning("No .pak files extracted from %s", file_name)

            self.after(0, lambda fn=file_name: self.status_var.set(
                f"✅ Downloaded {fn}. Verifying…"
            ))

            if verify:
                time.sleep(0.5)
                self.after(0, lambda m=mod: self._verify_update(m, api_key))
            else:
                self.after(0, self._worker_done)

        except Exception as exc:
            log.error("Download failed for %s: %s", mod.get("mod_name", "?"), exc)
            self.after(0, lambda e=str(exc): (
                messagebox.showerror("Download Error", f"Failed to download update:\n{e}"),
            ))
            self.after(0, self._worker_done)

    def _verify_update(self, mod: dict, api_key: str):
        """Re-scan the mod directory and re-check this mod to verify the update."""
        self.status_var.set(f"Verifying update for {mod['mod_name']}…")

        mod_dir = self.dir_var.get().strip()
        if not mod_dir or not os.path.isdir(mod_dir):
            self._worker_done()
            return

        # Re-scan to pick up new file dates
        old_mod_id = mod.get("mod_id")
        self.mods = scan_mod_directory(mod_dir)

        # Find the updated mod by its Nexus ID
        updated_mod = None
        for m in self.mods:
            if m.get("mod_id") == old_mod_id:
                updated_mod = m
                break
        if updated_mod is None:
            # Try matching by name
            for m in self.mods:
                if m.get("mod_name") == mod.get("mod_name"):
                    updated_mod = m
                    if updated_mod.get("mod_id") is None:
                        updated_mod["mod_id"] = old_mod_id
                    break

        self._resolve_cached_ids()
        self._populate_tree()

        if updated_mod and old_mod_id and api_key:
            # Re-check just this one mod
            self._check_thread = threading.Thread(
                target=self._worker_verify_single,
                args=(updated_mod, api_key),
                daemon=True,
            )
            self._check_thread.start()
        else:
            self.status_var.set("Update installed – re-scan to verify.")
            self._worker_done()

    def _worker_verify_single(self, mod: dict, api_key: str):
        """Background: re-check a single mod after update install."""
        domain = self.cfg.get("game_domain", "baldursgate3")
        mod_id = mod["mod_id"]
        try:
            api = NexusAPI(api_key, domain)
            details = api.get_mod_details(mod_id)
            if details:
                self.results[mod_id] = details
                nexus_dt = details.get("nexus_updated")
                is_outdated = False
                if nexus_dt and mod["local_date"]:
                    is_outdated = nexus_dt > mod["local_date"]
                self.after(0, lambda m=mod, d=details, o=is_outdated: self._update_row(m, d, o))
                if is_outdated:
                    self.after(0, lambda: self.status_var.set(
                        f"⚠️ {mod['mod_name']} still appears outdated after update."
                    ))
                else:
                    self.after(0, lambda: self.status_var.set(
                        f"✅ {mod['mod_name']} verified – up to date!"
                    ))
        except Exception as exc:
            log.error("Verify failed for mod %s: %s", mod_id, exc)
            self.after(0, lambda: self.status_var.set(
                f"Could not verify update for {mod['mod_name']}."
            ))
        self.after(0, self._worker_done)

    # ── Batch update all outdated ───────────────────────────────────

    def _update_all_outdated(self):
        """Download updates for all mods tagged as outdated."""
        api_key = self.key_var.get().strip()
        if not api_key:
            messagebox.showwarning("API Key Required",
                                   "Enter your Nexus Mods API key to download updates.")
            return

        if self._check_thread and self._check_thread.is_alive():
            messagebox.showinfo("Busy", "An operation is already running.")
            return

        # Collect outdated mods from the tree
        outdated_mods = []
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            if vals and "Update available" in str(vals[7]):
                mod = getattr(self, "_iid_to_mod", {}).get(iid)
                if mod and mod.get("mod_id") is not None:
                    outdated_mods.append(mod)

        if not outdated_mods:
            messagebox.showinfo("No Updates",
                                "No outdated mods found.\n"
                                "Run 'Check for Updates' first.")
            return

        answer = messagebox.askyesno(
            "Update All Outdated",
            f"Download updates for {len(outdated_mods)} mod(s)?\n\n"
            "Note: Direct download requires a Nexus Premium API key.\n"
            "For free users, the mod pages will open in your browser.",
        )
        if not answer:
            return

        self._set_buttons_busy(True)
        self.progress["maximum"] = len(outdated_mods)
        self.progress["value"] = 0
        self.status_var.set(f"Updating {len(outdated_mods)} mod(s)…")

        self._check_thread = threading.Thread(
            target=self._worker_batch_update,
            args=(outdated_mods, api_key),
            daemon=True,
        )
        self._check_thread.start()

    def _worker_batch_update(self, mods: list[dict], api_key: str):
        """Background: download updates for multiple mods sequentially."""
        import requests
        import time

        domain = self.cfg.get("game_domain", "baldursgate3")
        mod_dir = self.dir_var.get().strip()
        api = NexusAPI(api_key, domain)

        success_count = 0
        fail_count = 0
        browser_count = 0

        for idx, mod in enumerate(mods, 1):
            mod_id = mod["mod_id"]
            name = mod.get("mod_name", f"Mod {mod_id}")
            self.after(0, lambda n=name, i=idx, t=len(mods): self.status_var.set(
                f"[{i}/{t}] Updating {n}…"
            ))

            try:
                main_file = api.get_main_file(mod_id)
                if not main_file:
                    log.warning("No files found for %s (ID %s)", name, mod_id)
                    fail_count += 1
                    self.after(0, lambda v=idx: self.progress.configure(value=v))
                    continue

                file_id = main_file["file_id"]
                file_name = main_file.get("file_name", f"mod_{mod_id}.pak")

                try:
                    links = api.get_download_links(mod_id, file_id)
                except NexusAPIError:
                    links = []

                if not links:
                    # Non-premium: open in browser
                    url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files"
                    self.after(0, lambda u=url: webbrowser.open(u))
                    browser_count += 1
                    self.after(0, lambda v=idx: self.progress.configure(value=v))
                    time.sleep(1)  # small delay between opening tabs
                    continue

                # Premium: direct download
                download_url = links[0].get("URI", "")
                if not download_url:
                    fail_count += 1
                    self.after(0, lambda v=idx: self.progress.configure(value=v))
                    continue

                dest_path = os.path.join(mod_dir, file_name)
                resp = requests.get(download_url, stream=True, timeout=120)
                resp.raise_for_status()

                with open(dest_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)

                # Remove old file if different
                old_path = mod.get("filepath", "")
                if old_path and os.path.isfile(old_path) and os.path.normpath(old_path) != os.path.normpath(dest_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass

                # Extract .pak files if the download is an archive
                archive_ext = os.path.splitext(dest_path)[1].lower()
                if archive_ext in self._ARCHIVE_EXTS:
                    pak_files = self._extract_archive(dest_path, mod_dir)
                    if pak_files:
                        log.info("Batch: extracted %d .pak(s) from %s", len(pak_files), file_name)

                log.info("Batch: downloaded %s → %s", file_name, dest_path)
                success_count += 1

            except Exception as exc:
                log.error("Batch download failed for %s: %s", name, exc)
                fail_count += 1

            self.after(0, lambda v=idx: self.progress.configure(value=v))
            time.sleep(SEARCH_DELAY)

        # Summary
        parts = []
        if success_count:
            parts.append(f"{success_count} downloaded")
        if browser_count:
            parts.append(f"{browser_count} opened in browser")
        if fail_count:
            parts.append(f"{fail_count} failed")
        summary = ", ".join(parts) or "No updates processed"

        self.after(0, lambda s=summary: self.status_var.set(f"Batch update: {s}"))
        self.after(0, lambda: messagebox.showinfo(
            "Batch Update Complete",
            f"Results:\n• {summary}\n\n"
            "Running verification scan…",
        ))

        # Verify by re-scanning and re-checking
        self.after(0, lambda: self._batch_verify(api_key))

    def _batch_verify(self, api_key: str):
        """Re-scan mod directory and re-check all mods after batch update."""
        mod_dir = self.dir_var.get().strip()
        if not mod_dir or not os.path.isdir(mod_dir):
            self._worker_done()
            return

        self.mods = scan_mod_directory(mod_dir)
        self._resolve_cached_ids()
        self._populate_tree()
        self.status_var.set("Verification: re-checking all mods…")

        # Start a full update check
        self._start_update_check(api_key)
