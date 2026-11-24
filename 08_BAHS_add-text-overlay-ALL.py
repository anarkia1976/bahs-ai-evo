import os
import sys
import ctypes
import shutil
import logging
import argparse
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from tqdm import tqdm

SCRIPT_NAME = "08_BAHS_add-text-overlay-ALL"
FONT_EXTS = (".ttf",)
LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")

# Formato 1080P (1920x1080)
FHD_WIDTH = 1920
FHD_HEIGHT = 1080
FHD_LOGO_WIDTH = 125
FHD_LOGO_MARGIN_X = 40
FHD_LOGO_MARGIN_Y = 36

# Formato REEL (1080x1920 verticale 9:16)
REEL_WIDTH = 1080
REEL_HEIGHT = 1920
REEL_LOGO_WIDTH = 98
REEL_LOGO_MARGIN_X = 36
REEL_LOGO_MARGIN_Y = 48

DEFAULT_TOP_TEXT = "HIGHLIGHTS"
DEFAULT_SUB_TEXT = "CAMPIONATO - TEAM vs TEAM"
TEXT_DISPLAY_DURATION_SEC = 15.0

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"


def abilita_ansi():
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            m = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(m)):
                k.SetConsoleMode(h, m.value | 0x0004)
        except Exception:
            os.system("")


def rileva_ram_gb() -> tuple[float, float]:
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        return round(vm.total / (1024 ** 3), 1), round(vm.available / (1024 ** 3), 1)
    except Exception:
        pass
    if os.name == "nt":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return round(st.ullTotalPhys / (1024 ** 3), 1), round(st.ullAvailPhys / (1024 ** 3), 1)
        except Exception:
            return 8.0, 4.0
    p = Path("/proc/meminfo")
    if p.is_file():
        tot = avail = 0
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                tot = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1])
        if tot > 0:
            return round(tot / (1024 ** 2), 1), round((avail or tot // 2) / (1024 ** 2), 1)
    return 8.0, 4.0


def calcola_risorse() -> dict:
    threads = os.cpu_count() or 4
    cores = max(2, threads // 2)
    ram_tot, ram_avail = rileva_ram_gb()
    preset = "veryfast" if (threads <= 4 or ram_avail < 6.0) else "fast"
    return {
        "threads": threads,
        "cores": cores,
        "ram_tot": ram_tot,
        "ram_avail": ram_avail,
        "preset": preset,
    }


def sanitize_slug(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in text.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "INCONTRO"


def is_non_highlights_mode(tipo_raw: str) -> bool:
    return tipo_raw.strip().upper() != "HIGHLIGHTS"


def build_stage_name(tipo_slug: str, fmt_slug: str, fade: bool = True, music: bool = False, text: bool = False, logo: bool = False) -> str:
    parts = [tipo_slug, fmt_slug, "with", "fade" if fade else "nofade"]
    if music:
        parts.append("music")
    if text:
        parts.append("text")
    if logo:
        parts.append("logo")
    return "_".join(parts) + ".mp4"


def parse_video_flags(video_path: Path) -> tuple[bool, bool]:
    name = video_path.name.lower()
    fade = "nofade" not in name
    has_music = "_music" in name
    return fade, has_music


def escape_drawtext(text: str) -> str:
    """Effettua l'escape dei caratteri speciali per il filtro drawtext di FFmpeg."""
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "’")
    escaped = escaped.replace("%", "\\%")
    return escaped


def escape_drawtext_fontfile(font_path: Path) -> str:
    posix_path = font_path.resolve().as_posix()
    return posix_path.replace(":", "\\:").replace("'", "\\'")


def setup_dirs_and_logger(base_dir: Path) -> tuple[dict[str, Path], logging.Logger, Path]:
    dirs = {
        "in": base_dir / "IN",
        "out": base_dir / "OUT",
        "final": base_dir / "FINAL",
        "tmp": base_dir / "TMP",
        "log": base_dir / "LOG",
        "music": base_dir / "MUSIC",
        "extra": base_dir / "EXTRA",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = dirs["log"] / f"{SCRIPT_NAME}_{ts}.log"

    logger = logging.getLogger(SCRIPT_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    return dirs, logger, log_file


def get_duration_ms(video_path: Path) -> int:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return max(0, int(round(float(res.stdout.strip()) * 1000.0)))
    except ValueError:
        return 0


def has_audio(video_path: Path) -> bool:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(video_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return bool(res.stdout.strip())


def get_logo_dimensions(extra_dir: Path, target_w: int) -> tuple[int, int]:
    """Restituisce (larghezza, altezza) scalate del logo in EXTRA/logo.* per il formato indicato."""
    if extra_dir.is_dir():
        for entry in sorted(extra_dir.iterdir()):
            if entry.is_file() and entry.stem.lower() == "logo" and entry.suffix.lower() in LOGO_EXTS:
                cmd = [
                    "ffprobe",
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=p=0:s=x",
                    str(entry),
                ]
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                parts = res.stdout.strip().split("x")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    orig_w, orig_h = int(parts[0]), int(parts[1])
                    if orig_w > 0 and orig_h > 0:
                        scaled_h = max(2, int(round((orig_h * target_w) / orig_w)))
                        if scaled_h % 2 != 0:
                            scaled_h += 1
                        return target_w, scaled_h

    return target_w, target_w


def parse_progress_ms(line: str) -> int | None:
    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
        v = line.split("=", 1)[1].strip()
        if v.lstrip("-").isdigit():
            return max(0, int(v) // 1000)
    elif line.startswith("out_time="):
        val = line.split("=", 1)[1].strip()
        if val and val != "N/A":
            try:
                h, m, s = val.split(":")
                return int(round((float(h) * 3600.0 + float(m) * 60.0 + float(s)) * 1000.0))
            except ValueError:
                return None
    return None


def attendi_font_in_cartella_extra(extra_dir: Path, logger: logging.Logger, required: bool = True) -> Path | None:
    """Controlla 'EXTRA/' per font.ttf (o altro .ttf); se richiesto e assente, avvisa e riavvia lo script."""
    while True:
        preferred_font = extra_dir / "font.ttf"
        if preferred_font.is_file() and preferred_font.stat().st_size > 0:
            return preferred_font

        ttf_files = sorted([
            f for f in extra_dir.iterdir()
            if f.is_file() and f.suffix.lower() in FONT_EXTS and f.stat().st_size > 0
        ])
        if ttf_files:
            return ttf_files[0]

        if not required:
            return None

        logger.warning(f"[{SCRIPT_NAME}] Nessun file 'font.ttf' (o altro .ttf) trovato in 'EXTRA/'.")
        print(f"\n{RED}{BOLD}[X] [{SCRIPT_NAME}] Nessun file 'font.ttf' trovato nella cartella 'EXTRA/'.{RESET}", file=sys.stderr)
        print(f"{YELLOW}[!] Inserisci 'font.ttf' nella cartella 'EXTRA/' prima di proseguire.{RESET}")
        sc = input("\n👉 Premi [INVIO] per riavviare lo script (oppure '0' per uscire): ").strip().lower()
        if sc in ("0", "q", "quit", "exit"):
            print(f"{YELLOW}[!] [{SCRIPT_NAME}] Elaborazione interrotta dall'utente.{RESET}")
            logger.info(f"[{SCRIPT_NAME}] Uscita richiesta dall'utente per mancanza font in EXTRA/.")
            sys.exit(1)

        script_file = str(Path(__file__).resolve())
        os.execv(sys.executable, [sys.executable, script_file] + sys.argv[1:])


def gestione_file_esistente(out_file: Path, auto: bool, logger: logging.Logger) -> str:
    if not out_file.is_file() or out_file.stat().st_size == 0:
        return "overwrite"
    size_mb = out_file.stat().st_size / (1024 * 1024)
    logger.warning(f"[{SCRIPT_NAME}] File già esistente: '{out_file}' ({size_mb:.1f} MB)")
    if auto:
        logger.info(f"[{SCRIPT_NAME}] Modalità AUTO: file esistente mantenuto, step saltato.")
        return "skip"
    print(f"\n{YELLOW}[!] ATTENZIONE: File esistente: 'FINAL/{out_file.name}' ({size_mb:.1f} MB){RESET}")
    print("   1. Sovrascrivi e rigenera")
    print("   2. Salta (mantieni file esistente)")
    print("   3. Interrompi script")
    while True:
        sc = input("👉 Scelta [INVIO = 2 Salta]: ").strip()
        if sc in ("", "2"):
            logger.info(f"[{SCRIPT_NAME}] Scelta utente: mantieni file esistente.")
            return "skip"
        if sc == "1":
            logger.info(f"[{SCRIPT_NAME}] Scelta utente: sovrascrivi file esistente.")
            return "overwrite"
        if sc == "3":
            logger.info(f"[{SCRIPT_NAME}] Scelta utente: interrompi script.")
            return "abort"
        print(f"{RED}[!] Scelta non valida.{RESET}")


def select_input_video(out_dir: Path, final_dir: Path, tmp_dir: Path, tipo_raw: str, tipo_slug: str, fmt_slug: str, auto: bool) -> Path | None:
    candidates = [
        tmp_dir / build_stage_name(tipo_slug, fmt_slug, fade=True, music=True),
        final_dir / build_stage_name(tipo_slug, fmt_slug, fade=True, music=True),
        tmp_dir / build_stage_name(tipo_slug, fmt_slug, fade=False, music=True),
        final_dir / build_stage_name(tipo_slug, fmt_slug, fade=False, music=True),
        tmp_dir / build_stage_name(tipo_slug, fmt_slug, fade=True, music=False),
        final_dir / build_stage_name(tipo_slug, fmt_slug, fade=True, music=False),
        tmp_dir / build_stage_name(tipo_slug, fmt_slug, fade=False, music=False),
        final_dir / build_stage_name(tipo_slug, fmt_slug, fade=False, music=False),
    ]
    if is_non_highlights_mode(tipo_raw):
        candidates.append(out_dir / f"{tipo_slug}_1080P_merged_orig.mp4")

    existing = []
    seen = set()
    for p in candidates:
        if p.is_file() and p.resolve() not in seen:
            existing.append(p)
            seen.add(p.resolve())

    if not existing:
        return None
    if len(existing) == 1 or auto:
        return existing[0]

    print("\n--- SELEZIONE VIDEO SORGENTE ---")
    for idx, p in enumerate(existing, start=1):
        print(f"  {idx}. {p.parent.name}/{p.name}")
    while True:
        sc = input(f"👉 Scegli video (1-{len(existing)}) [INVIO = 1]: ").strip()
        if sc == "":
            return existing[0]
        if sc.isdigit() and 1 <= int(sc) <= len(existing):
            return existing[int(sc) - 1]
        print(f"{RED}[!] Scelta non valida.{RESET}")


def build_cmd_1080p(
    in_video: Path,
    tmp_out_file: Path,
    tmp_font: Path | None,
    extra_dir: Path,
    top_text: str,
    dynamic_text: str,
    duration_sec: float,
    res: dict,
    logger: logging.Logger,
) -> list[str]:
    font_clause = ""
    if tmp_font and tmp_font.is_file():
        font_clause = f"fontfile='{escape_drawtext_fontfile(tmp_font)}':"

    safe_top_text = escape_drawtext(top_text)
    safe_dynamic_text = escape_drawtext(dynamic_text)

    _, logo_scaled_h = get_logo_dimensions(extra_dir, FHD_LOGO_WIDTH)
    logo_top_y = FHD_HEIGHT - FHD_LOGO_MARGIN_Y - logo_scaled_h
    logo_center_y = int(round(logo_top_y + (logo_scaled_h / 2.0)))

    base_x = 60
    line_height = 4
    line_y = logo_center_y - (line_height // 2)
    base_y = line_y - 72
    dynamic_y = line_y + 24
    line_width = 750

    trans_time = min(1.5, duration_sec / 3.0)
    start_x = -1600
    target_x = base_x
    hold_end = max(trans_time, duration_sec - trans_time)

    logger.info(
        f"[{SCRIPT_NAME}] Layout testo 1080P: logo_center_y={logo_center_y}, line_y={line_y}, "
        f"base_y={base_y}, dynamic_y={dynamic_y} | top_text='{top_text}' | "
        f"dynamic_text='{dynamic_text}' | end_sec={duration_sec}"
    )

    x_expr = (
        f"if(lt(t\\,{trans_time})\\,"
        f"{start_x}+({target_x}-({start_x}))*sin((t/{trans_time})*1.570796)\\,"
        f"if(lt(t\\,{hold_end})\\,"
        f"{target_x}\\,"
        f"{target_x}+({start_x}-{target_x})*sin(((t-{hold_end})/{trans_time})*1.570796)))"
    )

    alpha_expr = (
        f"if(lt(t\\,{trans_time})\\,t/{trans_time}\\,"
        f"if(lt(t\\,{hold_end})\\,1\\,"
        f"({duration_sec}-t)/{trans_time}))"
    )

    filter_complex = (
        f"[0:v]scale={FHD_WIDTH}:{FHD_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={FHD_WIDTH}:{FHD_HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=yuv420p[v_norm];"
        f"[v_norm]drawtext={font_clause}text='{safe_top_text}':"
        f"fontcolor=white:fontsize=64:borderw=3:bordercolor=black:"
        f"x={base_x}:y={base_y}:alpha='{alpha_expr}':"
        f"enable='between(t,0,{duration_sec})'[txt_top];"
        f"[txt_top]drawbox=x={base_x}:y={line_y}:w={line_width}:h={line_height}:color=0x00FF66:t=fill:"
        f"enable='between(t,0,{duration_sec})'[line_green];"
        f"[line_green]drawtext={font_clause}text='{safe_dynamic_text}':"
        f"fontcolor=white:fontsize=64:borderw=3:bordercolor=black:"
        f"x='{x_expr}':y={dynamic_y}:alpha='{alpha_expr}':"
        f"enable='between(t,0,{duration_sec})'[v_final]"
    )

    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", str(in_video),
        "-filter_complex", filter_complex,
        "-map", "[v_final]",
    ]
    if has_audio(in_video):
        cmd.extend(["-map", "0:a?", "-c:a", "aac", "-b:a", "192k"])
    cmd.extend([
        "-c:v", "libx264",
        "-preset", res["preset"],
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-threads", str(res["threads"]),
        "-progress", "pipe:1",
        "-nostats",
        "-y",
        str(tmp_out_file),
    ])
    return cmd


def build_cmd_reel(
    in_video: Path,
    tmp_out_file: Path,
    tmp_font: Path | None,
    extra_dir: Path,
    top_text: str,
    dynamic_text: str,
    duration: float,
    res: dict,
    logger: logging.Logger,
) -> list[str]:
    font_clause = ""
    if tmp_font and tmp_font.is_file():
        font_clause = f"fontfile='{escape_drawtext_fontfile(tmp_font)}':"

    safe_top_text = escape_drawtext(top_text)
    safe_dynamic_text = escape_drawtext(dynamic_text)

    base_x = 48
    line_height = 5
    line_y = 1380
    base_y = line_y - 70
    dynamic_y = line_y + 22
    line_width = 720

    trans_time = min(1.2, duration / 3.0)
    start_x = -1120
    target_x = base_x
    hold_end = max(trans_time, duration - trans_time)

    logger.info(
        f"[{SCRIPT_NAME}] Layout testo REEL (durata {duration}s): "
        f"line_y={line_y}, base_y={base_y}, dynamic_y={dynamic_y}, line_width={line_width} | "
        f"top_text='{top_text}' | dynamic_text='{dynamic_text}' | end_sec={duration}"
    )

    x_expr = (
        f"if(lt(t\\,{trans_time})\\,"
        f"{start_x}+({target_x}-({start_x}))*sin((t/{trans_time})*1.570796)\\,"
        f"if(lt(t\\,{hold_end})\\,"
        f"{target_x}\\,"
        f"{target_x}+({start_x}-{target_x})*sin(((t-{hold_end})/{trans_time})*1.570796)))"
    )

    alpha_expr = (
        f"if(lt(t\\,{trans_time})\\,t/{trans_time}\\,"
        f"if(lt(t\\,{hold_end})\\,1\\,"
        f"({duration}-t)/{trans_time}))"
    )

    filter_complex = (
        f"[0:v]scale={REEL_WIDTH}:{REEL_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={REEL_WIDTH}:{REEL_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,format=yuv420p[v_norm];"
        f"[v_norm]drawtext={font_clause}text='{safe_top_text}':"
        f"fontcolor=white:fontsize=56:borderw=3:bordercolor=black@0.85:"
        f"x={base_x}:y={base_y}:alpha='{alpha_expr}':"
        f"enable='between(t,0,{duration})'[txt_top];"
        f"[txt_top]drawbox=x={base_x}:y={line_y}:w={line_width}:h={line_height}:color=0x00FF66:t=fill:"
        f"enable='between(t,0,{duration})'[line_green];"
        f"[line_green]drawtext={font_clause}text='{safe_dynamic_text}':"
        f"fontcolor=white:fontsize=44:borderw=3:bordercolor=black@0.85:"
        f"x='{x_expr}':y={dynamic_y}:alpha='{alpha_expr}':"
        f"enable='between(t,0,{duration})'[v_final]"
    )

    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", str(in_video),
        "-filter_complex", filter_complex,
        "-map", "[v_final]",
    ]
    if has_audio(in_video):
        cmd.extend(["-map", "0:a?", "-c:a", "aac", "-b:a", "192k"])
    cmd.extend([
        "-c:v", "libx264",
        "-preset", res["preset"],
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-threads", str(res["threads"]),
        "-progress", "pipe:1",
        "-nostats",
        "-y",
        str(tmp_out_file),
    ])
    return cmd


def main():
    abilita_ansi()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Sovraimpressione testo 1080P o REEL")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--tipo", default=DEFAULT_TOP_TEXT)
    parser.add_argument("--sottotitolo", default=DEFAULT_SUB_TEXT)
    parser.add_argument("--formato", default="1080P")
    parser.add_argument("--duration", type=float, default=TEXT_DISPLAY_DURATION_SEC)
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    dirs, logger, log_file = setup_dirs_and_logger(base_dir)
    res = calcola_risorse()

    fmt_slug = args.formato.strip().upper()
    if fmt_slug not in ("1080P", "REEL"):
        fmt_slug = "1080P"

    tipo_slug = sanitize_slug(args.tipo)

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | "
        f"sottotitolo='{args.sottotitolo}' | formato='{fmt_slug}' | duration={args.duration} | "
        f"auto={args.auto} | risorse={res}"
    )

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print(f"{RED}[X] [{SCRIPT_NAME}] FFmpeg o FFprobe non trovati nel PATH.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] FFmpeg o FFprobe non trovati nel PATH.")
        sys.exit(1)

    font_path = attendi_font_in_cartella_extra(dirs["extra"], logger, required=(fmt_slug == "1080P"))
    if font_path:
        logger.info(f"[{SCRIPT_NAME}] Utilizzo font: '{font_path}'")
    else:
        logger.info(f"[{SCRIPT_NAME}] Nessun font custom trovato (opzionale per {fmt_slug}): uso font di default FFmpeg.")

    in_video = select_input_video(dirs["out"], dirs["final"], dirs["tmp"], args.tipo, tipo_slug, fmt_slug, args.auto)
    if not in_video or not in_video.is_file():
        print(f"{RED}[X] [{SCRIPT_NAME}] Nessun video {fmt_slug} di input trovato in 'FINAL/', 'TMP/' o 'OUT/'.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] Nessun video {fmt_slug} di input trovato.")
        sys.exit(1)

    fade_flag, has_music_flag = parse_video_flags(in_video)
    stage_name = build_stage_name(tipo_slug, fmt_slug, fade=fade_flag, music=has_music_flag, text=True)
    out_file = dirs["final"] / stage_name
    tmp_out_file = dirs["tmp"] / stage_name

    logger.info(f"[{SCRIPT_NAME}] Video input='{in_video}' | fade={fade_flag} | music={has_music_flag} | out_file='{out_file}'")

    azione = gestione_file_esistente(out_file, args.auto, logger)
    if azione == "abort":
        print(f"{YELLOW}[!] [{SCRIPT_NAME}] Elaborazione interrotta dall'utente.{RESET}")
        return
    if azione == "skip":
        logger.info(f"[{SCRIPT_NAME}] Completato (SKIP): mantenuto '{out_file}'.")
        return

    top_text = args.tipo.strip() or DEFAULT_TOP_TEXT
    dynamic_text = args.sottotitolo.strip() or DEFAULT_SUB_TEXT

    if not args.auto and fmt_slug == "REEL":
        raw_top = input(f"🔝 Prima riga overlay [INVIO = {top_text}]: ").strip()
        if raw_top:
            top_text = raw_top
        raw_sub = input(f"📝 Seconda riga overlay [INVIO = {dynamic_text}]: ").strip()
        if raw_sub:
            dynamic_text = raw_sub

    tmp_font: Path | None = None
    if font_path and font_path.is_file():
        tmp_font = dirs["tmp"] / "font.ttf"
        shutil.copy2(font_path, tmp_font)
        logger.info(f"[{SCRIPT_NAME}] Copiato '{font_path.name}' in TMP come '{tmp_font.name}'.")

    total_ms = get_duration_ms(in_video)
    overlay_end_sec = float(args.duration) if args.duration and args.duration > 0 else TEXT_DISPLAY_DURATION_SEC

    if fmt_slug == "REEL":
        cmd = build_cmd_reel(
            in_video, tmp_out_file, tmp_font, dirs["extra"],
            top_text, dynamic_text, overlay_end_sec, res, logger
        )
        bar_desc = "08 | Overlay Testo REEL"
        bar_col = "cyan"
    else:
        cmd = build_cmd_1080p(
            in_video, tmp_out_file, tmp_font, dirs["extra"],
            top_text, dynamic_text, overlay_end_sec, res, logger
        )
        bar_desc = "08 | Overlay Testo 1080P"
        bar_col = "green"

    logger.info(f"[{SCRIPT_NAME}] Esecuzione FFmpeg (durata_video_ms={total_ms}): {cmd}")

    stderr_lines: list[str] = []

    def _drain(pipe):
        try:
            for l in iter(pipe.readline, ""):
                if l:
                    stderr_lines.append(l.rstrip())
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        t_err = threading.Thread(target=_drain, args=(proc.stderr,), daemon=True)
        if proc.stderr:
            t_err.start()

        last_ms = 0
        with tqdm(total=max(1, total_ms), desc=bar_desc, unit="ms", unit_scale=True, colour=bar_col, ncols=78) as bar:
            if proc.stdout:
                for raw in iter(proc.stdout.readline, ""):
                    line = raw.strip()
                    cur_ms = parse_progress_ms(line)
                    if cur_ms is not None and total_ms > 0:
                        clamped = min(total_ms, cur_ms)
                        if clamped > last_ms:
                            bar.update(clamped - last_ms)
                            last_ms = clamped
                    elif line == "progress=end" and total_ms > 0 and last_ms < total_ms:
                        bar.update(total_ms - last_ms)
                        last_ms = total_ms

        rc = proc.wait()
        t_err.join(timeout=5.0)

        if rc != 0 or not tmp_out_file.is_file() or tmp_out_file.stat().st_size == 0:
            err_msg = " ".join(stderr_lines[-8:]) or f"returncode={rc}"
            print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore durante l'aggiunta del testo ({fmt_slug}) con FFmpeg (log: {log_file.name}).{RESET}", file=sys.stderr)
            logger.error(f"[{SCRIPT_NAME}] Errore FFmpeg (rc={rc}): {err_msg}")
            sys.exit(1)

        shutil.move(str(tmp_out_file), str(out_file))
        size_mb = out_file.stat().st_size / (1024 * 1024)
        logger.info(f"[{SCRIPT_NAME}] Completato con successo: '{out_file}' ({size_mb:.2f} MB)")
    finally:
        if tmp_font and tmp_font.exists():
            try:
                tmp_font.unlink()
            except Exception:
                pass
        if tmp_out_file.exists():
            tmp_out_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
