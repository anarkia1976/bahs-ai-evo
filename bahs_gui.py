import os
import sys
import math
import queue
import shutil
import threading
import subprocess
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import BOTH, EW, LEFT, NS, RIGHT, W, X, Y, DISABLED, NORMAL
    HAS_TTKB = True
except ImportError:
    from tkinter import ttk
    from tkinter.constants import BOTH, EW, LEFT, NS, RIGHT, W, X, Y, DISABLED, NORMAL
    HAS_TTKB = False

try:
    from PIL import Image, ImageDraw, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from bahs_core import (
    DEFAULT_FORMAT,
    DEFAULT_SUB_TEXT,
    DEFAULT_TOP_TEXT,
    LOGO_EXTS,
    NO_MUSIC_TYPES,
    compute_system_resources,
    ensure_project_dirs,
    has_valid_timestamps,
    normalize_format,
)

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}
FONT_EXTS = {".ttf", ".otf", ".ttc", ".woff", ".woff2"}
MUSIC_EXTS = {".mp3", ".wav", ".aac", ".m4a", ".ogg", ".flac"}

# (script_name, badge_code, badge_color, title, description, is_interactive)
ALL_STEP_SCRIPTS = [
    (
        "02_BAHS_merge-originals-ALL.py",
        "02",
        "#3b82f6",
        "Merge Originali",
        "Unisci i file video in IN/ in un unico flusso",
        False,
    ),
    (
        "03_BAHS_identify-timestamps-ALL.py",
        "03",
        "#f59e0b",
        "Identifica Timestamp",
        "Segna dal vivo i momenti canestro/azione",
        True,
    ),
    (
        "04_BAHS_split-original-ALL.py",
        "04",
        "#ef4444",
        "Taglio Clip",
        "Estrai le clip attorno ai timestamp salvati",
        False,
    ),
    (
        "05_BAHS_convert-clips-REEL.py",
        "05",
        "#a855f7",
        "Converti REEL (YOLO)",
        "Inquadratura dinamica 9:16 con tracking AI",
        False,
    ),
    (
        "06_BAHS_merge-clips-ALL.py",
        "06",
        "#10b981",
        "Unisci Clip",
        "Monta le clip in sequenza con transizioni",
        False,
    ),
    (
        "07_BAHS_add-music-overlay-ALL.py",
        "07",
        "#ec4899",
        "Mix Musica",
        "Aggiungi traccia audio e bilancia i volumi",
        False,
    ),
    (
        "11_BAHS_add-text-logo-overlay-ALL.py",
        "11",
        "#06b6d4",
        "Testo + Logo",
        "Applica titolo, sottotitolo e logo in uscita",
        False,
    ),
]

STATUS_STYLES = {
    "pronto": {
        "label": "Pronto",
        "core": "#64748b",
        "light": "#cbd5e1",
        "dark": "#334155",
        "glow": "#475569",
        "pulse": False,
    },
    "in_corso": {
        "label": "In corso",
        "core": "#f59e0b",
        "light": "#fef3c7",
        "dark": "#b45309",
        "glow": "#f59e0b",
        "pulse": True,
    },
    "errore": {
        "label": "Errore",
        "core": "#ef4444",
        "light": "#fee2e2",
        "dark": "#991b1b",
        "glow": "#ef4444",
        "pulse": True,
    },
    "terminato": {
        "label": "Terminato",
        "core": "#22c55e",
        "light": "#dcfce7",
        "dark": "#166534",
        "glow": "#22c55e",
        "pulse": True,
    },
}

BaseWindow = ttk.Window if HAS_TTKB else tk.Tk


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (100, 116, 139)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _blend_hex(c1: str, c2: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


class BahsGuiApp(BaseWindow):
    def __init__(self) -> None:
        if HAS_TTKB:
            super().__init__(themename="darkly")
        else:
            super().__init__()

        self.title("BAHS · Basketball Highlights Automation Suite")
        self.geometry("1120x860")
        self.minsize(720, 480)

        self.base_dir_var = tk.StringVar(value=str(Path(__file__).resolve().parent))
        self.tipo_var = tk.StringVar(value=DEFAULT_TOP_TEXT)
        self.tipo_custom_var = tk.StringVar(value=DEFAULT_TOP_TEXT)
        self.sottotitolo_var = tk.StringVar(value=DEFAULT_SUB_TEXT)
        self.formato_var = tk.StringVar(value=DEFAULT_FORMAT)

        # Parametri opzionali per step specifici
        self.ts_speed_var = tk.StringVar(value="1.0")
        self.before_var = tk.StringVar(value="3.0")
        self.after_var = tk.StringVar(value="3.0")
        self.model_var = tk.StringVar(value="yolo26l.pt")
        self.device_var = tk.StringVar(value="auto")
        self.conf_var = tk.StringVar(value="0.50")
        self.alpha_var = tk.StringVar(value="0.25")
        self.gap_var = tk.StringVar(value="35")
        self.no_fade_var = tk.BooleanVar(value=False)
        self.duration_var = tk.StringVar(value="15.0")

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.status_queue: queue.Queue[tuple[str, int | None, int | None]] = queue.Queue()
        self.current_proc: subprocess.Popen | None = None
        self.worker_thread: threading.Thread | None = None
        self.stop_requested = False

        self.status_state = "pronto"
        self.status_current_step: int | None = None
        self.status_total_steps: int | None = None
        self._pulse_phase = 0.0
        self._blink_after_id: str | None = None

        self._window_icon_img: tk.PhotoImage | None = None
        self._extra_logo_img: tk.PhotoImage | None = None

        self._set_basketball_window_icon()
        self._build_ui()
        self._set_status_state("pronto")
        self._on_tipo_changed()
        self._refresh_system_info()
        self._refresh_in_videos_status()
        self._refresh_extra_logo()
        self._refresh_extra_assets_status()
        self._poll_log_queue()
        self._apply_windows_dark_titlebar()

    def _build_basketball_pil_image(self, size: int = 64):
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        pad = max(2, size // 16)
        cx = cy = size / 2.0
        r = (size - 2 * pad) / 2.0
        line_w = max(2, size // 24)

        draw.ellipse(
            [pad, pad, size - pad, size - pad],
            fill=(249, 115, 22, 255),
            outline=(67, 20, 7, 255),
            width=line_w,
        )

        draw.line([(pad + 1, cy), (size - pad - 1, cy)], fill=(67, 20, 7, 255), width=line_w)
        draw.line([(cx, pad + 1), (cx, size - pad - 1)], fill=(67, 20, 7, 255), width=line_w)

        left_pts = []
        right_pts = []
        for step in range(-16, 17):
            t = step / 16.0
            y = cy + t * (r * 0.88)
            curve_dx = math.cos(t * math.pi / 2.0) * (r * 0.56)
            left_pts.append((cx - curve_dx, y))
            right_pts.append((cx + curve_dx, y))

        draw.line(left_pts, fill=(67, 20, 7, 255), width=line_w)
        draw.line(right_pts, fill=(67, 20, 7, 255), width=line_w)
        return img

    def _set_basketball_window_icon(self) -> None:
        if not HAS_PIL:
            return
        try:
            pil_icon = self._build_basketball_pil_image(64)
            self._window_icon_img = ImageTk.PhotoImage(pil_icon.resize((32, 32), Image.Resampling.LANCZOS))
            self.iconphoto(True, self._window_icon_img)

            if os.name == "nt":
                ico_path = Path(self.base_dir_var.get() or ".").resolve() / "TMP" / "_bahs_ball_icon.ico"
                ico_path.parent.mkdir(parents=True, exist_ok=True)
                pil_icon.save(
                    ico_path,
                    format="ICO",
                    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)],
                )
                self.iconbitmap(str(ico_path))
        except Exception:
            pass

    def _apply_windows_dark_titlebar(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if not hwnd:
                hwnd = self.winfo_id()
            value = ctypes.c_int(1)
            for attr in (20, 19):
                res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    attr,
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )
                if res == 0:
                    break
        except Exception:
            pass

    def _btn(self, parent, text: str, command, bootstyle: str = "secondary", **kwargs):
        if HAS_TTKB:
            return ttk.Button(parent, text=text, command=command, bootstyle=bootstyle, **kwargs)
        return ttk.Button(parent, text=text, command=command, **kwargs)

    def _make_step_badge(self, parent, code: str, color: str) -> tk.Canvas:
        bg_panel = "#22252a" if HAS_TTKB else parent.cget("background")
        canvas = tk.Canvas(
            parent,
            width=34,
            height=34,
            bg=bg_panel,
            highlightthickness=0,
            bd=0,
        )
        canvas.create_oval(3, 3, 31, 31, fill=color, outline=color)
        canvas.create_text(
            17,
            17,
            text=code,
            fill="#ffffff",
            font=("Segoe UI", 9, "bold"),
        )
        return canvas

    def _get_panel_bg(self, widget) -> str:
        if HAS_TTKB:
            try:
                style_bg = self.style.colors.get("bg")
                if style_bg:
                    return style_bg
            except Exception:
                pass
            return "#22252a"
        try:
            bg = widget.cget("background")
            if bg and bg.startswith("#"):
                return bg
        except Exception:
            pass
        return "#f0f0f0"

    def _format_status_label(
        self,
        state: str,
        current_step: int | None = None,
        total_steps: int | None = None,
    ) -> str:
        base_label = STATUS_STYLES.get(state, STATUS_STYLES["pronto"])["label"]
        if current_step is not None and total_steps is not None and total_steps > 0:
            return f"{base_label} · Step {current_step}/{total_steps}"
        if total_steps is not None and total_steps > 0:
            return f"{base_label} · {total_steps} step"
        return base_label

    def _set_status_state(
        self,
        state: str,
        current_step: int | None = None,
        total_steps: int | None = None,
    ) -> None:
        if state not in STATUS_STYLES:
            state = "pronto"
        self.status_state = state
        self.status_current_step = current_step
        self.status_total_steps = total_steps
        self._pulse_phase = 0.0

        if self._blink_after_id is not None:
            try:
                self.after_cancel(self._blink_after_id)
            except Exception:
                pass
            self._blink_after_id = None

        info = STATUS_STYLES[state]
        if hasattr(self, "status_text_lbl"):
            self.status_text_lbl.configure(
                text=self._format_status_label(state, current_step, total_steps)
            )

        self._redraw_status_circle(intensity=1.0)

        if info["pulse"]:
            self._schedule_status_pulse()

    def _schedule_status_pulse(self) -> None:
        info = STATUS_STYLES.get(self.status_state, STATUS_STYLES["pronto"])
        if not info["pulse"]:
            self._redraw_status_circle(intensity=1.0)
            return

        def _animate():
            self._blink_after_id = None
            if not STATUS_STYLES.get(self.status_state, {}).get("pulse", False):
                self._redraw_status_circle(intensity=1.0)
                return
            self._pulse_phase = (self._pulse_phase + 0.34) % (2.0 * math.pi)
            intensity = 0.15 + 0.85 * (0.5 * (1.0 + math.cos(self._pulse_phase)))
            self._redraw_status_circle(intensity=intensity)
            self._schedule_status_pulse()

        self._blink_after_id = self.after(55, _animate)

    def _redraw_status_circle(self, intensity: float = 1.0) -> None:
        if not hasattr(self, "status_canvas"):
            return

        info = STATUS_STYLES.get(self.status_state, STATUS_STYLES["pronto"])
        bg = self._get_panel_bg(self.status_canvas.master)
        self.status_canvas.configure(bg=bg)
        self.status_canvas.delete("all")

        cx, cy = 11.0, 11.0
        bezel_outer = "#0f172a" if HAS_TTKB else "#94a3b8"
        bezel_inner = "#334155" if HAS_TTKB else "#cbd5e1"
        off_base = "#1e293b" if HAS_TTKB else "#cbd5e1"

        if info["pulse"] and intensity > 0.2:
            glow_outer = _blend_hex(bg, info["glow"], 0.24 * intensity)
            glow_mid = _blend_hex(bg, info["glow"], 0.45 * intensity)
            r_glow1 = 10.0 + 0.8 * intensity
            r_glow2 = 9.0 + 0.5 * intensity
            self.status_canvas.create_oval(
                cx - r_glow1, cy - r_glow1, cx + r_glow1, cy + r_glow1,
                fill=glow_outer, outline=""
            )
            self.status_canvas.create_oval(
                cx - r_glow2, cy - r_glow2, cx + r_glow2, cy + r_glow2,
                fill=glow_mid, outline=""
            )

        self.status_canvas.create_oval(
            cx - 8.4, cy - 8.4, cx + 8.4, cy + 8.4,
            fill=bezel_outer, outline=bezel_inner, width=1
        )

        rim_col = _blend_hex(off_base, info["dark"], 0.35 + 0.65 * intensity)
        mid_col = _blend_hex(off_base, info["core"], 0.25 + 0.75 * intensity)
        hot_col = _blend_hex(mid_col, info["light"], 0.45 * intensity)

        self.status_canvas.create_oval(
            cx - 6.7, cy - 6.7, cx + 6.7, cy + 6.7,
            fill=rim_col, outline=""
        )
        self.status_canvas.create_oval(
            cx - 5.5, cy - 5.5, cx + 5.5, cy + 5.5,
            fill=mid_col, outline=""
        )
        self.status_canvas.create_oval(
            cx - 3.8, cy - 3.9, cx + 3.1, cy + 3.0,
            fill=hot_col, outline=""
        )

        spec_col = _blend_hex(hot_col, "#ffffff", 0.35 + 0.50 * intensity)
        self.status_canvas.create_oval(
            cx - 4.0, cy - 4.1, cx - 1.1, cy - 1.4,
            fill=spec_col, outline=""
        )

    def _build_adv_step_header(self, parent: ttk.Frame, code: str, color: str, title: str, desc: str) -> ttk.Frame:
        header = ttk.Frame(parent)
        header.pack(fill=X, pady=(0, 8))

        badge = self._make_step_badge(header, code, color)
        badge.pack(side=LEFT, padx=(0, 10))

        text_col = ttk.Frame(header)
        text_col.pack(side=LEFT, fill=X, expand=True)

        ttk.Label(text_col, text=title, font=("Segoe UI", 10, "bold")).pack(anchor=W)

        desc_color = "#9aa5b4" if HAS_TTKB else "#666666"
        tk.Label(
            text_col,
            text=desc,
            font=("Segoe UI", 8),
            fg=desc_color,
            bg="#22252a" if HAS_TTKB else text_col.cget("background"),
            anchor=W,
        ).pack(anchor=W)

        return header

    def _list_in_videos(self, base_dir: Path) -> list[Path]:
        in_dir = base_dir / "IN"
        if not in_dir.is_dir():
            return []
        return sorted([
            p for p in in_dir.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and p.stat().st_size > 0
        ])

    def _refresh_in_videos_status(self) -> None:
        root = Path(self.base_dir_var.get().strip() or ".").resolve()
        videos = self._list_in_videos(root)
        if not hasattr(self, "in_status_lbl"):
            return
        if videos:
            names = ", ".join(p.name for p in videos[:3])
            extra = f" (+{len(videos) - 3})" if len(videos) > 3 else ""
            self.in_status_lbl.configure(
                text=f"🎬 Cartella IN/: {len(videos)} video pronti ({names}{extra})"
            )
        else:
            self.in_status_lbl.configure(
                text="⚠️ Cartella IN/: nessun file video presente"
            )

    def _upload_videos_to_in(self) -> None:
        root = Path(self.base_dir_var.get().strip() or ".").resolve()
        ensure_project_dirs(root, include_model_dir=True)
        in_dir = root / "IN"
        in_dir.mkdir(parents=True, exist_ok=True)

        filetypes = [
            ("File Video", "*.mp4 *.mov *.avi *.mkv *.m4v *.webm"),
            ("Tutti i file", "*.*"),
        ]
        chosen = filedialog.askopenfilenames(title="Seleziona video da caricare in IN/", filetypes=filetypes)
        if not chosen:
            return

        copied: list[str] = []
        for src_str in chosen:
            src = Path(src_str)
            if not src.is_file():
                continue
            dest = in_dir / src.name
            shutil.copy2(str(src), str(dest))
            copied.append(dest.name)

        self._refresh_in_videos_status()
        if copied:
            self._append_log(f"[IN] Caricati in IN/: {', '.join(copied)}\n")

    def _clear_in_folder(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning(
                "Elaborazione in corso",
                "Attendi o interrompi l'esecuzione corrente prima di svuotare IN/.",
            )
            return

        root = Path(self.base_dir_var.get().strip() or ".").resolve()
        in_dir = root / "IN"
        in_dir.mkdir(parents=True, exist_ok=True)

        items = list(in_dir.iterdir())
        if not items:
            self._refresh_in_videos_status()
            self._append_log("[IN] La cartella IN/ è già vuota.\n")
            return

        confirm = messagebox.askyesno(
            "Pulizia cartella IN/",
            f"Vuoi rimuovere tutti i file e sottocartelle ({len(items)}) presenti in:\n{in_dir}?",
        )
        if not confirm:
            return

        removed = 0
        for item in items:
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                removed += 1
            except Exception as exc:
                self._append_log(f"[IN][WARN] Impossibile rimuovere {item.name}: {exc}\n")

        self._refresh_in_videos_status()
        self._append_log(f"[IN] Cartella IN/ svuotata ({removed} elementi rimossi).\n")

    def _clean_workspace_dirs(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning(
                "Elaborazione in corso",
                "Attendi o interrompi l'esecuzione corrente prima di pulire le cartelle.",
            )
            return

        root = Path(self.base_dir_var.get().strip() or ".").resolve()
        ensure_project_dirs(root, include_model_dir=True)

        confirm = messagebox.askyesno(
            "Pulizia cartelle",
            f"Vuoi svuotare le cartelle di lavoro (TMP/, OUT/, FINAL/, LOG/) in:\n{root}\n\n"
            "I file in IN/ e EXTRA/ verranno mantenuti.",
        )
        if not confirm:
            return

        targets = ["TMP", "OUT", "FINAL", "LOG"]
        cleaned_count = 0
        for sub in targets:
            d = root / sub
            if not d.is_dir():
                d.mkdir(parents=True, exist_ok=True)
                continue
            for item in d.iterdir():
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    cleaned_count += 1
                except Exception as exc:
                    self._append_log(f"[CLEAN][WARN] Impossibile rimuovere {item.name}: {exc}\n")
            d.mkdir(parents=True, exist_ok=True)

        self._append_log(f"[CLEAN] Pulizia completata ({cleaned_count} elementi rimossi da TMP/, OUT/, FINAL/, LOG/).\n")

    def _find_extra_logo(self, base_dir: Path) -> Path | None:
        extra_dir = base_dir / "EXTRA"
        if not extra_dir.is_dir():
            return None
        preferred = extra_dir / "logo.png"
        if preferred.is_file() and preferred.stat().st_size > 0:
            return preferred
        logos = sorted([
            p for p in extra_dir.iterdir()
            if p.is_file()
            and p.stem.lower() == "logo"
            and p.suffix.lower() in LOGO_EXTS
            and p.stat().st_size > 0
        ])
        return logos[0] if logos else None

    def _find_extra_font(self, extra_dir: Path) -> Path | None:
        if not extra_dir.is_dir():
            return None
        fonts = sorted([
            p for p in extra_dir.iterdir()
            if p.is_file() and p.suffix.lower() in FONT_EXTS and p.stat().st_size > 0
        ])
        return fonts[0] if fonts else None

    def _find_extra_music(self, extra_dir: Path) -> Path | None:
        if not extra_dir.is_dir():
            return None
        tracks = sorted([
            p for p in extra_dir.iterdir()
            if p.is_file() and p.suffix.lower() in MUSIC_EXTS and p.stat().st_size > 0
        ])
        return tracks[0] if tracks else None

    def _refresh_extra_assets_status(self) -> None:
        root = Path(self.base_dir_var.get().strip() or ".").resolve()
        extra_dir = root / "EXTRA"

        logo_file = self._find_extra_logo(root)
        font_file = self._find_extra_font(extra_dir)
        music_file = self._find_extra_music(extra_dir)

        if hasattr(self, "asset_logo_lbl"):
            self.asset_logo_lbl.configure(text=f"Attuale: {logo_file.name}" if logo_file else "Attuale: nessuno")
        if hasattr(self, "asset_font_lbl"):
            self.asset_font_lbl.configure(text=f"Attuale: {font_file.name}" if font_file else "Attuale: nessuno")
        if hasattr(self, "asset_music_lbl"):
            self.asset_music_lbl.configure(text=f"Attuale: {music_file.name}" if music_file else "Attuale: nessuno")

    def _backup_existing_files(self, backup_dir: Path, files_to_backup: list[Path], stamp: str) -> list[str]:
        moved_names: list[str] = []
        seen: set[Path] = set()
        for src in files_to_backup:
            if src in seen or not src.is_file():
                continue
            seen.add(src)
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest_name = f"{src.stem}_{stamp}{src.suffix}"
            dest = backup_dir / dest_name
            counter = 1
            while dest.exists():
                dest = backup_dir / f"{src.stem}_{stamp}_{counter}{src.suffix}"
                counter += 1
            shutil.move(str(src), str(dest))
            moved_names.append(dest.name)
        return moved_names

    def _install_custom_asset(self, kind: str) -> None:
        root = Path(self.base_dir_var.get().strip() or ".").resolve()
        ensure_project_dirs(root, include_model_dir=True)
        extra_dir = root / "EXTRA"
        backup_dir = extra_dir / "BACKUP"
        extra_dir.mkdir(parents=True, exist_ok=True)

        if kind == "logo":
            filetypes = [
                ("Immagini Logo", "*.png *.jpg *.jpeg *.webp *.bmp"),
                ("Tutti i file", "*.*"),
            ]
            title_dlg = "Seleziona Logo"
        elif kind == "font":
            filetypes = [
                ("Font", "*.ttf *.otf *.ttc *.woff *.woff2"),
                ("Tutti i file", "*.*"),
            ]
            title_dlg = "Seleziona Font"
        else:
            filetypes = [
                ("File Audio", "*.mp3 *.wav *.aac *.m4a *.ogg *.flac"),
                ("Tutti i file", "*.*"),
            ]
            title_dlg = "Seleziona Musica"

        chosen_str = filedialog.askopenfilename(title=title_dlg, filetypes=filetypes)
        if not chosen_str:
            return

        src_path = Path(chosen_str)
        if not src_path.is_file():
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if kind == "logo":
            existing_logos = [
                p for p in extra_dir.iterdir()
                if p.is_file() and (p.stem.lower() == "logo" or p.suffix.lower() in LOGO_EXTS)
            ]
            moved = self._backup_existing_files(backup_dir, existing_logos, stamp)
            ext = src_path.suffix.lower() if src_path.suffix.lower() in LOGO_EXTS else ".png"
            target_path = extra_dir / f"logo{ext}"
            shutil.copy2(str(src_path), str(target_path))
            self._refresh_extra_logo()
        elif kind == "font":
            existing_fonts = [
                p for p in extra_dir.iterdir()
                if p.is_file() and p.suffix.lower() in FONT_EXTS
            ]
            moved = self._backup_existing_files(backup_dir, existing_fonts, stamp)
            target_path = extra_dir / src_path.name
            shutil.copy2(str(src_path), str(target_path))
        else:
            existing_music = [
                p for p in extra_dir.iterdir()
                if p.is_file() and p.suffix.lower() in MUSIC_EXTS
            ]
            moved = self._backup_existing_files(backup_dir, existing_music, stamp)
            target_path = extra_dir / src_path.name
            shutil.copy2(str(src_path), str(target_path))

        self._refresh_extra_assets_status()
        if moved:
            self._append_log(f"[EXTRA] Backup in EXTRA/BACKUP/: {', '.join(moved)}\n")
        self._append_log(f"[EXTRA] Caricato {kind}: {target_path.name}\n")

    def _refresh_extra_logo(self) -> None:
        root = Path(self.base_dir_var.get().strip() or ".").resolve()
        logo_path = self._find_extra_logo(root)
        max_w, max_h = 170, 96

        if not logo_path:
            self._extra_logo_img = None
            self.logo_preview_lbl.configure(image="", text="No logo.png", width=16, height=4)
            return

        try:
            if HAS_PIL:
                pil_img = Image.open(logo_path)
                pil_img.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
                self._extra_logo_img = ImageTk.PhotoImage(pil_img)
                self.logo_preview_lbl.configure(
                    image=self._extra_logo_img,
                    text="",
                    width=pil_img.width,
                    height=pil_img.height,
                )
            else:
                tk_img = tk.PhotoImage(file=str(logo_path))
                w, h = tk_img.width(), tk_img.height()
                scale = max(1, math.ceil(max(w / max_w, h / max_h)))
                if scale > 1:
                    tk_img = tk_img.subsample(scale, scale)
                self._extra_logo_img = tk_img
                self.logo_preview_lbl.configure(
                    image=self._extra_logo_img,
                    text="",
                    width=tk_img.width(),
                    height=tk_img.height(),
                )
        except Exception:
            self._extra_logo_img = None
            self.logo_preview_lbl.configure(image="", text="Logo err", width=16, height=4)

    def _build_scrollable_container(self, parent: ttk.Frame) -> ttk.Frame:
        panel_bg = self._get_panel_bg(parent)
        wrapper = ttk.Frame(parent)
        wrapper.pack(fill=BOTH, expand=True)

        canvas = tk.Canvas(wrapper, bg=panel_bg, highlightthickness=0, bd=0)
        v_scroll = ttk.Scrollbar(wrapper, orient=tk.VERTICAL, command=canvas.yview)
        h_scroll = ttk.Scrollbar(wrapper, orient=tk.HORIZONTAL, command=canvas.xview)
        canvas.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)

        canvas.grid(row=0, column=0, sticky="nsew")
        wrapper.rowconfigure(0, weight=1)
        wrapper.columnconfigure(0, weight=1)

        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        scroll_state = {"needs_v": False, "needs_h": False}

        def _sync_scrollbars(_event=None):
            req_w = inner.winfo_reqwidth()
            req_h = inner.winfo_reqheight()
            vis_w = max(1, canvas.winfo_width())
            vis_h = max(1, canvas.winfo_height())

            target_w = max(vis_w, req_w)
            target_h = max(vis_h, req_h)
            canvas.itemconfigure(win_id, width=target_w, height=target_h)
            canvas.configure(scrollregion=(0, 0, target_w, target_h))

            needs_v = req_h > vis_h + 1
            needs_h = req_w > vis_w + 1
            scroll_state["needs_v"] = needs_v
            scroll_state["needs_h"] = needs_h

            if needs_v:
                v_scroll.grid(row=0, column=1, sticky=NS)
            else:
                v_scroll.grid_remove()
                canvas.yview_moveto(0.0)

            if needs_h:
                h_scroll.grid(row=1, column=0, sticky=EW)
            else:
                h_scroll.grid_remove()
                canvas.xview_moveto(0.0)

        inner.bind("<Configure>", _sync_scrollbars)
        canvas.bind("<Configure>", _sync_scrollbars)

        def _is_inside_log_widget(widget) -> bool:
            if not hasattr(self, "log_text"):
                return False
            cur = widget
            while cur is not None:
                if cur == self.log_text:
                    return True
                cur = getattr(cur, "master", None)
            return False

        def _on_mousewheel(event):
            if _is_inside_log_widget(event.widget):
                return
            delta = -1 if event.delta > 0 else 1
            if event.state & 0x0001:
                if scroll_state["needs_h"]:
                    canvas.xview_scroll(delta, "units")
                    return "break"
            else:
                if scroll_state["needs_v"]:
                    canvas.yview_scroll(delta, "units")
                    return "break"

        def _bind_wheel(_e):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_wheel(_e):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        inner.bind("<Enter>", _bind_wheel)
        inner.bind("<Leave>", _unbind_wheel)

        return inner

    def _build_ui(self) -> None:
        root_scroll = self._build_scrollable_container(self)
        main_pad = ttk.Frame(root_scroll, padding=14)
        main_pad.pack(fill=BOTH, expand=True)

        # --- Cartella Progetto, Risorse & Logo EXTRA a destra ---
        top_box = ttk.LabelFrame(main_pad, text="  📁 Progetto & Risorse di Sistema  ", padding=12)
        top_box.pack(fill=X, pady=(0, 10))

        top_grid = ttk.Frame(top_box)
        top_grid.pack(fill=X)
        top_grid.columnconfigure(0, weight=1)

        left_info = ttk.Frame(top_grid)
        left_info.grid(row=0, column=0, sticky=EW, padx=(0, 16))

        row_dir = ttk.Frame(left_info)
        row_dir.pack(fill=X)
        ttk.Label(row_dir, text="Cartella Base:").pack(side=LEFT, padx=(0, 8))
        ttk.Entry(row_dir, textvariable=self.base_dir_var).pack(side=LEFT, fill=X, expand=True, padx=(0, 8))
        self._btn(row_dir, "Sfoglia...", self._choose_base_dir, bootstyle="info-outline").pack(side=LEFT, padx=(0, 6))
        self._btn(row_dir, "Crea Cartelle", self._init_dirs, bootstyle="secondary-outline").pack(side=LEFT)

        self.sys_label = ttk.Label(left_info, text="")
        self.sys_label.pack(anchor=W, pady=(8, 0))

        row_in = ttk.Frame(left_info)
        row_in.pack(fill=X, pady=(6, 0))
        self.in_status_lbl = ttk.Label(row_in, text="🎬 Cartella IN/: -")
        self.in_status_lbl.pack(side=LEFT, fill=X, expand=True)
        self._btn(row_in, "Carica Video in IN...", self._upload_videos_to_in, bootstyle="success-outline").pack(side=LEFT, padx=(0, 6))
        self._btn(row_in, "Pulisci IN/", self._clear_in_folder, bootstyle="danger-outline").pack(side=LEFT, padx=(0, 6))
        self._btn(row_in, "Aggiorna", self._refresh_in_videos_status, bootstyle="secondary-outline").pack(side=LEFT)

        bg_logo = "#181b20" if HAS_TTKB else top_box.cget("background")
        self.logo_preview_lbl = tk.Label(
            top_grid,
            text="Logo",
            bg=bg_logo,
            fg="#9aa5b4" if HAS_TTKB else "#555555",
            relief=tk.FLAT,
            bd=0,
        )
        self.logo_preview_lbl.grid(row=0, column=1, sticky=NS)

        # --- Notebook ---
        notebook = ttk.Notebook(main_pad)
        notebook.pack(fill=BOTH, expand=True, pady=(0, 10))

        tab_pipe = ttk.Frame(notebook, padding=12)
        tab_adv = ttk.Frame(notebook, padding=12)
        tab_assets = ttk.Frame(notebook, padding=12)
        notebook.add(tab_pipe, text="  🏀 Pipeline & Step  ")
        notebook.add(tab_adv, text="  ⚙️ Parametri Avanzati  ")
        notebook.add(tab_assets, text="  🎨 Risorse Personalizzate  ")

        self._build_pipeline_tab(tab_pipe)
        self._build_advanced_tab(tab_adv)
        self._build_assets_tab(tab_assets)

        # --- Pulsanti Azione ---
        action_bar = ttk.Frame(main_pad)
        action_bar.pack(fill=X, pady=(0, 10))

        self.btn_run_all = self._btn(
            action_bar,
            "▶  Avvia Pipeline Completa",
            self._start_full_pipeline,
            bootstyle="success",
        )
        self.btn_run_all.pack(side=LEFT, padx=(0, 8))

        self.btn_stop = self._btn(
            action_bar,
            "■  Interrompi",
            self._stop_process,
            bootstyle="danger",
        )
        self.btn_stop.configure(state=DISABLED)
        self.btn_stop.pack(side=LEFT, padx=(0, 16))

        self._btn(action_bar, "📂 FINAL/", lambda: self._open_folder("FINAL"), bootstyle="info-outline").pack(side=LEFT, padx=(0, 6))
        self._btn(action_bar, "📂 OUT/", lambda: self._open_folder("OUT"), bootstyle="secondary-outline").pack(side=LEFT, padx=(0, 6))
        self._btn(action_bar, "📂 LOG/", lambda: self._open_folder("LOG"), bootstyle="secondary-outline").pack(side=LEFT, padx=(0, 6))
        self._btn(action_bar, "🧹 Pulisci Log", self._clear_log, bootstyle="warning-outline").pack(side=LEFT, padx=(0, 6))
        self._btn(action_bar, "🧹 Pulisci Cartelle", self._clean_workspace_dirs, bootstyle="warning-outline").pack(side=LEFT)

        # Pallino LED e testo di stato (con step corrente / totale) a destra
        panel_bg = self._get_panel_bg(action_bar)
        status_holder = tk.Frame(action_bar, bg=panel_bg, bd=0, highlightthickness=0)
        status_holder.pack(side=RIGHT, padx=(8, 2))

        self.status_canvas = tk.Canvas(
            status_holder,
            width=22,
            height=22,
            bg=panel_bg,
            highlightthickness=0,
            bd=0,
        )
        self.status_canvas.pack(side=LEFT, padx=(0, 6))

        self.status_text_lbl = tk.Label(
            status_holder,
            text="Pronto",
            font=("Segoe UI", 9, "bold"),
            fg="#e6edf3" if HAS_TTKB else "#222222",
            bg=panel_bg,
        )
        self.status_text_lbl.pack(side=LEFT)

        # --- Console Output Live ---
        log_box = ttk.LabelFrame(main_pad, text="  🖥️ Output Elaborazione  ", padding=10)
        log_box.pack(fill=BOTH, expand=True)

        bg_log = "#0d1117" if HAS_TTKB else "#111111"
        fg_log = "#e6edf3" if HAS_TTKB else "#f0f0f0"

        self.log_text = tk.Text(
            log_box,
            height=11,
            wrap=tk.NONE,
            font=("Consolas", 9),
            bg=bg_log,
            fg=fg_log,
            insertbackground=fg_log,
            relief=tk.FLAT,
            state=DISABLED,
        )
        y_scroll = ttk.Scrollbar(log_box, orient=tk.VERTICAL, command=self.log_text.yview)
        x_scroll = ttk.Scrollbar(log_box, orient=tk.HORIZONTAL, command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.log_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky=NS)
        x_scroll.grid(row=1, column=0, sticky=EW)
        log_box.rowconfigure(0, weight=1)
        log_box.columnconfigure(0, weight=1)

    def _build_pipeline_tab(self, parent: ttk.Frame) -> None:
        cfg = ttk.LabelFrame(parent, text="  🎯 Parametri Incontro  ", padding=12)
        cfg.pack(fill=X, pady=(0, 12))

        cfg.columnconfigure(1, weight=1)
        cfg.columnconfigure(3, weight=1)

        ttk.Label(cfg, text="Tipo Incontro:").grid(row=0, column=0, sticky=W, pady=6)
        tipo_combo = ttk.Combobox(
            cfg,
            textvariable=self.tipo_var,
            values=["HIGHLIGHTS", "GAMEDAY", "FRIENDLY MATCH", "CUSTOM"],
            state="readonly",
            width=22,
        )
        tipo_combo.grid(row=0, column=1, sticky=W, padx=(8, 20), pady=6)
        tipo_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_tipo_changed())

        ttk.Label(cfg, text="Titolo overlay:").grid(row=0, column=2, sticky=W, pady=6)
        self.custom_entry = ttk.Entry(cfg, textvariable=self.tipo_custom_var, state=DISABLED)
        self.custom_entry.grid(row=0, column=3, sticky=EW, padx=(8, 0), pady=6)

        ttk.Label(cfg, text="Formato Output:").grid(row=1, column=0, sticky=W, pady=6)
        fmt_row = ttk.Frame(cfg)
        fmt_row.grid(row=1, column=1, sticky=W, padx=(8, 20), pady=6)

        self.fmt_combo = ttk.Combobox(
            fmt_row,
            textvariable=self.formato_var,
            values=["1080P", "REEL"],
            state="readonly",
            width=14,
        )
        self.fmt_combo.pack(side=LEFT)

        self.fmt_hint_label = ttk.Label(fmt_row, text="")
        self.fmt_hint_label.pack(side=LEFT, padx=(8, 0))

        ttk.Label(cfg, text="Sottotitolo overlay:").grid(row=1, column=2, sticky=W, pady=6)
        ttk.Entry(cfg, textvariable=self.sottotitolo_var).grid(row=1, column=3, sticky=EW, padx=(8, 0), pady=6)

        steps_box = ttk.LabelFrame(parent, text="  ⚡ Esecuzione Singolo Step  ", padding=10)
        steps_box.pack(fill=BOTH, expand=True)

        for idx, (script_name, code, color, title, desc, _is_interactive) in enumerate(ALL_STEP_SCRIPTS):
            r, c = divmod(idx, 2)
            row_frame = ttk.Frame(steps_box, padding=(6, 4))
            row_frame.grid(row=r, column=c, sticky=EW, padx=6, pady=3)
            row_frame.columnconfigure(1, weight=1)

            badge = self._make_step_badge(row_frame, code, color)
            badge.grid(row=0, column=0, rowspan=2, sticky=W, padx=(0, 10))

            title_lbl = ttk.Label(row_frame, text=title, font=("Segoe UI", 10, "bold"))
            title_lbl.grid(row=0, column=1, sticky=W)

            desc_color = "#9aa5b4" if HAS_TTKB else "#666666"
            desc_lbl = tk.Label(
                row_frame,
                text=desc,
                font=("Segoe UI", 8),
                fg=desc_color,
                bg="#22252a" if HAS_TTKB else row_frame.cget("background"),
                anchor=W,
            )
            desc_lbl.grid(row=1, column=1, sticky=EW)

            self._btn(
                row_frame,
                "Esegui",
                command=lambda s=script_name: self._start_single_step(s),
                bootstyle="primary-outline",
            ).grid(row=0, column=2, rowspan=2, padx=(10, 0))

        steps_box.columnconfigure(0, weight=1)
        steps_box.columnconfigure(1, weight=1)

    def _build_advanced_tab(self, parent: ttk.Frame) -> None:
        ts_box = ttk.LabelFrame(parent, padding=10)
        ts_box.pack(fill=X, pady=(0, 10))
        self._build_adv_step_header(
            ts_box,
            "03",
            "#f59e0b",
            "Identifica Timestamp",
            "Velocità di riproduzione durante la marcatura dal vivo (sessione da sottotitolo e video da tipo)",
        )
        ts_ctrls = ttk.Frame(ts_box)
        ts_ctrls.pack(fill=X, padx=(44, 0))
        ttk.Label(ts_ctrls, text="Velocità riproduzione:").pack(side=LEFT)
        ttk.Combobox(
            ts_ctrls,
            textvariable=self.ts_speed_var,
            values=["1.0", "2.0", "4.0"],
            state="readonly",
            width=8,
        ).pack(side=LEFT, padx=(6, 0))

        split_box = ttk.LabelFrame(parent, padding=10)
        split_box.pack(fill=X, pady=(0, 10))
        self._build_adv_step_header(
            split_box,
            "04",
            "#ef4444",
            "Taglio Clip",
            "Margine in secondi prima e dopo ogni timestamp salvato",
        )
        split_ctrls = ttk.Frame(split_box)
        split_ctrls.pack(fill=X, padx=(44, 0))
        ttk.Label(split_ctrls, text="Prima (s):").pack(side=LEFT)
        ttk.Entry(split_ctrls, textvariable=self.before_var, width=8).pack(side=LEFT, padx=(6, 18))
        ttk.Label(split_ctrls, text="Dopo (s):").pack(side=LEFT)
        ttk.Entry(split_ctrls, textvariable=self.after_var, width=8).pack(side=LEFT, padx=(6, 0))

        yolo_box = ttk.LabelFrame(parent, padding=10)
        yolo_box.pack(fill=X, pady=(0, 10))
        self._build_adv_step_header(
            yolo_box,
            "05",
            "#a855f7",
            "Converti REEL (YOLO)",
            "Modello, dispositivo, soglie e smoothing per il crop dinamico 9:16",
        )
        yolo_ctrls = ttk.Frame(yolo_box)
        yolo_ctrls.pack(fill=X, padx=(44, 0))
        ttk.Label(yolo_ctrls, text="Modello:").pack(side=LEFT)
        ttk.Entry(yolo_ctrls, textvariable=self.model_var, width=16).pack(side=LEFT, padx=(6, 14))
        ttk.Label(yolo_ctrls, text="Device:").pack(side=LEFT)
        ttk.Entry(yolo_ctrls, textvariable=self.device_var, width=10).pack(side=LEFT, padx=(6, 14))
        ttk.Label(yolo_ctrls, text="Conf:").pack(side=LEFT)
        ttk.Entry(yolo_ctrls, textvariable=self.conf_var, width=6).pack(side=LEFT, padx=(6, 14))
        ttk.Label(yolo_ctrls, text="Alpha:").pack(side=LEFT)
        ttk.Entry(yolo_ctrls, textvariable=self.alpha_var, width=6).pack(side=LEFT, padx=(6, 14))
        ttk.Label(yolo_ctrls, text="Gap:").pack(side=LEFT)
        ttk.Entry(yolo_ctrls, textvariable=self.gap_var, width=6).pack(side=LEFT, padx=(6, 0))

        merge_box = ttk.LabelFrame(parent, padding=10)
        merge_box.pack(fill=X, pady=(0, 10))
        self._build_adv_step_header(
            merge_box,
            "06",
            "#10b981",
            "Unisci Clip",
            "Gestione delle dissolvenze incrociate tra clip consecutive",
        )
        merge_ctrls = ttk.Frame(merge_box)
        merge_ctrls.pack(fill=X, padx=(44, 0))
        if HAS_TTKB:
            ttk.Checkbutton(
                merge_ctrls,
                text="Disattiva fade tra clip (06 --no-fade)",
                variable=self.no_fade_var,
                bootstyle="round-toggle",
            ).pack(side=LEFT)
        else:
            ttk.Checkbutton(
                merge_ctrls,
                text="Disattiva fade tra clip (06 --no-fade)",
                variable=self.no_fade_var,
            ).pack(side=LEFT)

        overlay_box = ttk.LabelFrame(parent, padding=10)
        overlay_box.pack(fill=X)
        self._build_adv_step_header(
            overlay_box,
            "11",
            "#06b6d4",
            "Testo + Logo",
            "Durata iniziale di permanenza del banner testuale sul video finale",
        )
        overlay_ctrls = ttk.Frame(overlay_box)
        overlay_ctrls.pack(fill=X, padx=(44, 0))
        ttk.Label(overlay_ctrls, text="Durata testo (s):").pack(side=LEFT)
        ttk.Entry(overlay_ctrls, textvariable=self.duration_var, width=8).pack(side=LEFT, padx=(6, 0))

    def _build_assets_tab(self, parent: ttk.Frame) -> None:
        specs = [
            (
                "logo",
                "LG",
                "#06b6d4",
                "Logo Squadra / Evento",
                "Salva in EXTRA/logo.<ext> e sposta i logo precedenti in EXTRA/BACKUP/ con data_ora",
                "asset_logo_lbl",
            ),
            (
                "font",
                "FN",
                "#a855f7",
                "Font Overlay Testo",
                "Copia il font in EXTRA/ e sposta i font precedenti in EXTRA/BACKUP/ con data_ora",
                "asset_font_lbl",
            ),
            (
                "music",
                "MS",
                "#ec4899",
                "Musica di Sottofondo",
                "Copia la traccia audio in EXTRA/ e sposta le tracce precedenti in EXTRA/BACKUP/ con data_ora",
                "asset_music_lbl",
            ),
        ]

        for kind, code, color, title, desc, attr_name in specs:
            box = ttk.LabelFrame(parent, padding=10)
            box.pack(fill=X, pady=(0, 10))
            self._build_adv_step_header(box, code, color, title, desc)

            row = ttk.Frame(box)
            row.pack(fill=X, padx=(44, 0))

            status_lbl = ttk.Label(row, text="Attuale: -", font=("Segoe UI", 9))
            status_lbl.pack(side=LEFT, fill=X, expand=True)
            setattr(self, attr_name, status_lbl)

            self._btn(
                row,
                "Carica...",
                command=lambda k=kind: self._install_custom_asset(k),
                bootstyle="info-outline",
            ).pack(side=RIGHT)

        self._refresh_extra_assets_status()

    def _on_tipo_changed(self) -> None:
        tipo = self.tipo_var.get()
        is_highlights = tipo == "HIGHLIGHTS"

        self.tipo_custom_var.set(tipo)
        if tipo == "CUSTOM":
            self.custom_entry.configure(state=NORMAL)
        else:
            self.custom_entry.configure(state=DISABLED)

        if is_highlights:
            self.fmt_combo.configure(state="readonly")
            self.fmt_hint_label.configure(text="")
        else:
            self.formato_var.set("1080P")
            self.fmt_combo.configure(state=DISABLED)
            self.fmt_hint_label.configure(text="(fisso 1080P)")

    def _effective_tipo(self) -> str:
        if self.tipo_var.get() == "CUSTOM":
            val = self.tipo_custom_var.get().strip()
            return val if val else DEFAULT_TOP_TEXT
        return self.tipo_var.get().strip() or DEFAULT_TOP_TEXT

    def _effective_formato(self) -> str:
        if self.tipo_var.get() != "HIGHLIGHTS":
            return "1080P"
        return normalize_format(self.formato_var.get())

    def _choose_base_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.base_dir_var.get() or ".")
        if chosen:
            self.base_dir_var.set(chosen)
            self._refresh_system_info()
            self._refresh_in_videos_status()
            self._refresh_extra_logo()
            self._refresh_extra_assets_status()

    def _init_dirs(self) -> None:
        root = Path(self.base_dir_var.get().strip() or ".").resolve()
        ensure_project_dirs(root, include_model_dir=True)
        (root / "EXTRA" / "BACKUP").mkdir(parents=True, exist_ok=True)
        self._refresh_in_videos_status()
        self._refresh_extra_logo()
        self._refresh_extra_assets_status()
        self._append_log(f"[GUI] Cartelle inizializzate in: {root}\n")

    def _refresh_system_info(self) -> None:
        res = compute_system_resources()
        self.sys_label.configure(
            text=(
                f"💻 CPU: {res['threads']} thread ({res['cores']} core)   ·   "
                f"🧠 RAM disp.: {res['ram_avail']}/{res['ram_tot']} GB   ·   "
                f"⚡ Worker: {res['max_workers']}   ·   "
                f"🎛️ Preset: {res['preset']}"
            )
        )

    def _open_folder(self, sub: str) -> None:
        root = Path(self.base_dir_var.get().strip() or ".").resolve()
        target = root / sub
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]

    def _clear_log(self) -> None:
        self.log_text.configure(state=NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=DISABLED)
        if not (self.worker_thread and self.worker_thread.is_alive()):
            self._set_status_state("pronto")

    def _append_log(self, text: str) -> None:
        self.log_queue.put(text)

    def _set_status_from_worker(
        self,
        state: str,
        current_step: int | None = None,
        total_steps: int | None = None,
    ) -> None:
        self.status_queue.put((state, current_step, total_steps))

    def _poll_log_queue(self) -> None:
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state=NORMAL)
            self.log_text.insert(tk.END, msg)
            self.log_text.see(tk.END)
            self.log_text.configure(state=DISABLED)

        while True:
            try:
                next_state, cur_step, tot_steps = self.status_queue.get_nowait()
            except queue.Empty:
                break
            self._set_status_state(next_state, current_step=cur_step, total_steps=tot_steps)

        self.after(80, self._poll_log_queue)

    def _build_script_cmd(self, base_dir: Path, script_name: str, auto: bool) -> list[str]:
        tipo = self._effective_tipo()
        sub = self.sottotitolo_var.get().strip() or DEFAULT_SUB_TEXT
        fmt = self._effective_formato()

        cmd = [
            sys.executable,
            str(base_dir / script_name),
            "--base-dir", str(base_dir),
            "--tipo", tipo,
            "--sottotitolo", sub,
            "--formato", fmt,
        ]

        if script_name == "03_BAHS_identify-timestamps-ALL.py":
            cmd.extend(["--speed", self.ts_speed_var.get().strip() or "1.0"])
        elif script_name == "04_BAHS_split-original-ALL.py":
            cmd.extend(["--before", self.before_var.get().strip() or "3.0", "--after", self.after_var.get().strip() or "3.0"])
        elif script_name == "05_BAHS_convert-clips-REEL.py":
            cmd.extend([
                "--model", self.model_var.get().strip() or "yolo26l.pt",
                "--device", self.device_var.get().strip() or "auto",
                "--conf", self.conf_var.get().strip() or "0.50",
                "--alpha", self.alpha_var.get().strip() or "0.25",
                "--gap-frames", self.gap_var.get().strip() or "35",
            ])
        elif script_name == "06_BAHS_merge-clips-ALL.py" and self.no_fade_var.get():
            cmd.append("--no-fade")
        elif script_name == "11_BAHS_add-text-logo-overlay-ALL.py":
            cmd.extend(["--duration", self.duration_var.get().strip() or "15.0"])

        if auto:
            cmd.append("--auto")
        return cmd

    def _plan_pipeline(self, base_dir: Path) -> list[tuple[str, bool]]:
        tipo_raw = self._effective_tipo()
        tipo_upper = tipo_raw.strip().upper()
        fmt = self._effective_formato()

        if tipo_upper != "HIGHLIGHTS":
            plan = [("02_BAHS_merge-originals-ALL.py", True)]
            if tipo_upper not in NO_MUSIC_TYPES:
                plan.append(("07_BAHS_add-music-overlay-ALL.py", True))
            plan.append(("11_BAHS_add-text-logo-overlay-ALL.py", True))
            return plan

        skip_ts = has_valid_timestamps(base_dir / "OUT" / "timestamps.txt")
        plan: list[tuple[str, bool]] = [("02_BAHS_merge-originals-ALL.py", True)]
        if not skip_ts:
            plan.append(("03_BAHS_identify-timestamps-ALL.py", False))
        plan.append(("04_BAHS_split-original-ALL.py", True))
        if fmt == "REEL":
            plan.append(("05_BAHS_convert-clips-REEL.py", True))
        plan.extend([
            ("06_BAHS_merge-clips-ALL.py", True),
            ("07_BAHS_add-music-overlay-ALL.py", True),
            ("11_BAHS_add-text-logo-overlay-ALL.py", True),
        ])
        return plan

    def _run_commands_worker(self, base_dir: Path, commands: list[tuple[str, list[str]]]) -> None:
        self.stop_requested = False
        total_steps = len(commands)
        final_state = "terminato"
        final_step: int | None = total_steps if total_steps > 0 else None
        try:
            for idx, (script_name, cmd) in enumerate(commands, start=1):
                if self.stop_requested:
                    self._append_log("\n[GUI] Esecuzione interrotta dall'utente.\n")
                    final_state = "errore"
                    final_step = idx
                    break

                self._set_status_from_worker("in_corso", current_step=idx, total_steps=total_steps)

                script_path = base_dir / script_name
                if not script_path.is_file():
                    self._append_log(f"\n[GUI][ERRORE] Script non trovato: {script_path}\n")
                    final_state = "errore"
                    final_step = idx
                    break

                self._append_log(f"\n=== Avvio {script_name} (Step {idx}/{total_steps}) ===\n")

                is_interactive_03 = script_name == "03_BAHS_identify-timestamps-ALL.py"
                if is_interactive_03 and os.name == "nt":
                    self._append_log("[GUI] Apertura console interattiva per identificazione timestamp...\n")
                    self.current_proc = subprocess.Popen(
                        cmd,
                        cwd=str(base_dir),
                        creationflags=subprocess.CREATE_NEW_CONSOLE,
                    )
                    rc = self.current_proc.wait()
                    self.current_proc = None
                else:
                    self.current_proc = subprocess.Popen(
                        cmd,
                        cwd=str(base_dir),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    assert self.current_proc.stdout is not None
                    for line in self.current_proc.stdout:
                        self._append_log(line)

                    rc = self.current_proc.wait()
                    self.current_proc = None

                if self.stop_requested:
                    self._append_log("\n[GUI] Processo terminato su richiesta.\n")
                    final_state = "errore"
                    final_step = idx
                    break
                if rc != 0:
                    self._append_log(f"\n[GUI][ERRORE] {script_name} terminato con codice {rc}. Pipeline fermata.\n")
                    final_state = "errore"
                    final_step = idx
                    break
                self._append_log(f"=== Completato {script_name} (Step {idx}/{total_steps}) ===\n")
        except Exception as exc:
            self._append_log(f"\n[GUI][ERRORE] Eccezione imprevista: {exc}\n")
            final_state = "errore"
            final_step = self.status_current_step
        finally:
            self.current_proc = None
            self._set_status_from_worker(final_state, current_step=final_step, total_steps=total_steps)
            self.after(0, self._on_worker_finished)

    def _on_worker_finished(self) -> None:
        self.btn_run_all.configure(state=NORMAL)
        self.btn_stop.configure(state=DISABLED)
        self._refresh_in_videos_status()

    def _begin_worker(self, commands: list[tuple[str, list[str]]]) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Elaborazione in corso", "Attendi o interrompi l'esecuzione corrente prima di avviare un nuovo comando.")
            return

        base_dir = Path(self.base_dir_var.get().strip() or ".").resolve()
        ensure_project_dirs(base_dir, include_model_dir=True)

        total_steps = len(commands)
        self.btn_run_all.configure(state=DISABLED)
        self.btn_stop.configure(state=NORMAL)
        self._set_status_state("in_corso", current_step=1 if total_steps > 0 else None, total_steps=total_steps)
        self.worker_thread = threading.Thread(target=self._run_commands_worker, args=(base_dir, commands), daemon=True)
        self.worker_thread.start()

    def _start_full_pipeline(self) -> None:
        base_dir = Path(self.base_dir_var.get().strip() or ".").resolve()
        in_videos = self._list_in_videos(base_dir)
        self._refresh_in_videos_status()
        if not in_videos:
            load_now = messagebox.askyesno(
                "Cartella IN vuota",
                "Non ci sono file video nella cartella IN/.\nVuoi caricarli ora prima di avviare la pipeline?",
            )
            if load_now:
                self._upload_videos_to_in()
                in_videos = self._list_in_videos(base_dir)
            if not in_videos:
                return

        plan = self._plan_pipeline(base_dir)
        commands = [(script, self._build_script_cmd(base_dir, script, auto=auto)) for script, auto in plan]
        self._begin_worker(commands)

    def _start_single_step(self, script_name: str) -> None:
        base_dir = Path(self.base_dir_var.get().strip() or ".").resolve()
        is_interactive = script_name == "03_BAHS_identify-timestamps-ALL.py"
        cmd = self._build_script_cmd(base_dir, script_name, auto=not is_interactive)
        self._begin_worker([(script_name, cmd)])

    def _stop_process(self) -> None:
        self.stop_requested = True
        proc = self.current_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass


def main() -> None:
    app = BahsGuiApp()
    app.mainloop()


if __name__ == "__main__":
    main()