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

SCRIPT_NAME = "10_BAHS_add-logo-overlay-ALL"
LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
VIDEO_1080P_W, VIDEO_1080P_H = 1920, 1080
REEL_W, REEL_H = 1080, 1920

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


def pulisci_console():
    os.system("cls" if os.name == "nt" else "clear")


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


def parse_video_layers(video_path: Path) -> tuple[bool, bool, bool]:
    name = video_path.name.lower()
    fade = "nofade" not in name
    has_music = "_music" in name
    has_text = "_text" in name
    return fade, has_music, has_text


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


def gestione_file_esistente(out_file: Path, auto: bool, logger: logging.Logger) -> str:
    if not out_file.is_file() or out_file.stat().st_size == 0:
        return "overwrite"
    print(f"\n{YELLOW}[!] ATTENZIONE: Attività già svolta! File esistente{RESET}")
    logger.warning(f"[{SCRIPT_NAME}] File già esistente: {out_file}")
    if auto:
        print(f"{DIM}   Modalità AUTO: logo già applicato, salto passaggio.{RESET}")
        return "skip"
    print("   1. Sovrascrivi e rigenera")
    print("   2. Salta (mantieni file esistente)")
    print("   3. Interrompi script")
    while True:
        sc = input("👉 Scelta [INVIO = 2 Salta]: ").strip()
        if sc in ("", "2"):
            return "skip"
        if sc == "1":
            return "overwrite"
        if sc == "3":
            return "abort"
        print(f"{RED}[!] Scelta non valida.{RESET}")


def find_logo(extra_dir: Path) -> Path | None:
    logos = sorted([
        p for p in extra_dir.iterdir()
        if p.is_file() and p.stem.lower() == "logo" and p.suffix.lower() in LOGO_EXTS and p.stat().st_size > 0
    ])
    return logos[0] if logos else None


def attendi_logo_in_cartella_extra(extra_dir: Path, logger: logging.Logger) -> Path:
    avvisato_log = False
    while True:
        logo_path = find_logo(extra_dir)
        if logo_path:
            return logo_path

        if not avvisato_log:
            logger.warning(f"[{SCRIPT_NAME}] Nessun file 'logo.*' trovato in 'EXTRA/' ({', '.join(LOGO_EXTS)}).")
            avvisato_log = True

        print(f"\n{RED}{BOLD}[X] Nessun file 'logo.*' trovato nella cartella 'EXTRA/' ({', '.join(LOGO_EXTS)}).{RESET}")
        print(f"{YELLOW}[!] Inserisci un file 'logo.*' nella cartella 'EXTRA/' e riavvio lo script.{RESET}")
        sc = input("\n👉 Premi [INVIO] per riavviare lo script (oppure '0' per uscire): ").strip().lower()
        if sc in ("0", "q", "quit", "exit"):
            print(f"{YELLOW}[!] Elaborazione interrotta dall'utente.{RESET}")
            logger.info(f"[{SCRIPT_NAME}] Uscita richiesta dall'utente per mancanza logo in EXTRA/.")
            sys.exit(1)

        pulisci_console()
        script_file = str(Path(__file__).resolve())
        os.execv(sys.executable, [sys.executable, script_file] + sys.argv[1:])


def main():
    abilita_ansi()
    pulisci_console()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Sovraimpressione logo 1080P o REEL")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--tipo", default="HIGHLIGHTS")
    parser.add_argument("--sottotitolo", default="CAMPIONATO - TEAM vs TEAM")
    parser.add_argument("--formato", default="1080P")
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    dirs, logger, log_file = setup_dirs_and_logger(base_dir)
    res = calcola_risorse()

    fmt_slug = args.formato.strip().upper()
    if fmt_slug not in ("1080P", "REEL"):
        fmt_slug = "1080P"

    tipo_slug = sanitize_slug(args.tipo)

    print(f"{BOLD}{CYAN}{'=' * 80}{RESET}")
    print(f" {BOLD}{GREEN}INIZIO: {SCRIPT_NAME}{RESET} | Tipo: {BOLD}{args.tipo}{RESET} | Formato: {BOLD}{fmt_slug}{RESET}")
    print(f" {YELLOW}[CPU/RAM]{RESET} Thread: {res['threads']} | RAM disp.: {res['ram_avail']}/{res['ram_tot']} GB | Preset: {res['preset']}")
    print(f"{BOLD}{CYAN}{'=' * 80}{RESET}\n")
    logger.info(f"[{SCRIPT_NAME}] Avvio | tipo={args.tipo} | formato={fmt_slug} | risorse={res}")

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print(f"{RED}[X] FFmpeg o FFprobe non trovati nel PATH.{RESET}")
        sys.exit(1)

    logo_path = attendi_logo_in_cartella_extra(dirs["extra"], logger)
    print(f"{GREEN}[✔] Logo trovato in 'EXTRA/': '{logo_path.name}'{RESET}")
    logger.info(f"[{SCRIPT_NAME}] Utilizzo logo: {logo_path}")

    if fmt_slug == "REEL":
        preferred = [
            dirs["tmp"] / build_stage_name(tipo_slug, "REEL", fade=True, music=True, text=True),
            dirs["final"] / build_stage_name(tipo_slug, "REEL", fade=True, music=True, text=True),
            dirs["tmp"] / build_stage_name(tipo_slug, "REEL", fade=False, music=True, text=True),
            dirs["final"] / build_stage_name(tipo_slug, "REEL", fade=False, music=True, text=True),
            dirs["tmp"] / build_stage_name(tipo_slug, "REEL", fade=True, music=True, text=False),
            dirs["final"] / build_stage_name(tipo_slug, "REEL", fade=True, music=True, text=False),
            dirs["tmp"] / build_stage_name(tipo_slug, "REEL", fade=True, music=False, text=True),
            dirs["final"] / build_stage_name(tipo_slug, "REEL", fade=True, music=False, text=True),
            dirs["tmp"] / build_stage_name(tipo_slug, "REEL", fade=True, music=False, text=False),
            dirs["final"] / build_stage_name(tipo_slug, "REEL", fade=True, music=False, text=False),
        ]
        tw, th = REEL_W, REEL_H
        lw = int(round(tw * 0.09))
        if lw % 2 != 0:
            lw += 1
        mx, my = 36, 48
        bar_col = "cyan"
    else:
        preferred = [
            dirs["tmp"] / build_stage_name(tipo_slug, "1080P", fade=True, music=True, text=True),
            dirs["final"] / build_stage_name(tipo_slug, "1080P", fade=True, music=True, text=True),
            dirs["tmp"] / build_stage_name(tipo_slug, "1080P", fade=False, music=True, text=True),
            dirs["final"] / build_stage_name(tipo_slug, "1080P", fade=False, music=True, text=True),
            dirs["tmp"] / build_stage_name(tipo_slug, "1080P", fade=True, music=True, text=False),
            dirs["final"] / build_stage_name(tipo_slug, "1080P", fade=True, music=True, text=False),
            dirs["tmp"] / build_stage_name(tipo_slug, "1080P", fade=True, music=False, text=True),
            dirs["final"] / build_stage_name(tipo_slug, "1080P", fade=True, music=False, text=True),
            dirs["tmp"] / build_stage_name(tipo_slug, "1080P", fade=True, music=False, text=False),
            dirs["final"] / build_stage_name(tipo_slug, "1080P", fade=True, music=False, text=False),
        ]
        tw, th = VIDEO_1080P_W, VIDEO_1080P_H
        lw = int(round(tw * 0.065))
        if lw % 2 != 0:
            lw += 1
        mx, my = 40, 36
        bar_col = "green"

    if is_non_highlights_mode(args.tipo):
        preferred.append(dirs["out"] / f"{tipo_slug}_1080P_merged_orig.mp4")

    in_video = next((p for p in preferred if p.is_file()), None)
    if not in_video:
        print(f"{RED}[X] Nessun video {fmt_slug} idoneo trovato in 'FINAL/', 'TMP/' o 'OUT/'.{RESET}")
        logger.error(f"[{SCRIPT_NAME}] Nessun video {fmt_slug} trovato.")
        sys.exit(1)

    fade_flag, has_music_flag, has_text_flag = parse_video_layers(in_video)
    stage_name = build_stage_name(
        tipo_slug,
        fmt_slug,
        fade=fade_flag,
        music=has_music_flag,
        text=has_text_flag,
        logo=True,
    )
    out_file = dirs["final"] / stage_name
    tmp_out_file = dirs["tmp"] / stage_name

    azione = gestione_file_esistente(out_file, args.auto, logger)
    if azione == "abort":
        print(f"{YELLOW}[!] Elaborazione interrotta dall'utente.{RESET}")
        return
    if azione == "skip":
        print(f"{GREEN}[✔] Mantenuto file esistente: 'FINAL/{out_file.name}'{RESET}")
        return

    fc = (
        f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
        f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p[v_base];"
        f"[1:v]format=rgba,colorchannelmixer=aa=0.75,scale={lw}:-2:flags=lanczos[logo_s];"
        f"[v_base][logo_s]overlay=x=W-w-{mx}:y=H-h-{my}:format=auto[v_out]"
    )

    total_ms = get_duration_ms(in_video)
    cmd = ["ffmpeg", "-i", str(in_video), "-i", str(logo_path), "-filter_complex", fc, "-map", "[v_out]"]
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
        with tqdm(total=max(1, total_ms), desc=f"Logo {fmt_slug}", unit="ms", unit_scale=True, colour=bar_col, ncols=78) as bar:
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
            print(f"\n{RED}[X] Errore durante l'overlay logo.{RESET}")
            logger.error(f"[{SCRIPT_NAME}] Errore FFmpeg (rc={rc}): {' '.join(stderr_lines[-5:])}")
            sys.exit(1)

        shutil.move(str(tmp_out_file), str(out_file))
        print(f"\n{GREEN}[✔] Overlay logo completato! Output: 'FINAL/{out_file.name}'{RESET}")
        logger.info(f"[{SCRIPT_NAME}] Completato: {out_file} | Log: {log_file}")
    finally:
        if tmp_out_file.exists():
            tmp_out_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()