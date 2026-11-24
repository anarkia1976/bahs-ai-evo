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
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

SCRIPT_NAME = "04_BAHS_split-original-ALL"
DEFAULT_BUFFER_BEFORE = 3.0
DEFAULT_BUFFER_AFTER = 3.0

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


def disabilita_quick_edit_windows():
    """Evita che un click accidentale sulla console Windows metta in pausa l'output."""
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            h_in = k.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h_in, ctypes.byref(mode)):
                ENABLE_QUICK_EDIT_MODE = 0x0040
                ENABLE_EXTENDED_FLAGS = 0x0080
                new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
                k.SetConsoleMode(h_in, new_mode)
        except Exception:
            pass


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

    if threads <= 4 or ram_avail < 6.0:
        max_workers = 2
        threads_per_worker = max(1, threads // 2)
        preset = "veryfast"
    elif threads <= 8 or ram_avail < 12.0:
        max_workers = min(4, cores)
        threads_per_worker = 2
        preset = "fast"
    else:
        max_workers = min(cores, 8)
        threads_per_worker = min(4, max(2, threads // max_workers))
        preset = "fast"

    return {
        "threads": threads,
        "cores": cores,
        "ram_tot": ram_tot,
        "ram_avail": ram_avail,
        "max_workers": max_workers,
        "threads_per_worker": threads_per_worker,
        "preset": preset,
    }


def sanitize_slug(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in text.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "INCONTRO"


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


def get_video_duration(video_path: Path) -> float | None:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return None


def time_to_seconds(time_str: str) -> float:
    parts = [float(x) for x in time_str.strip().split(":")]
    if len(parts) == 3:
        return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60.0 + parts[1]
    return parts[0]


def load_timestamps(dir_out: Path, tipo_slug: str, fmt_slug: str) -> list[str] | None:
    candidates = [
        dir_out / f"{tipo_slug}_{fmt_slug}_timestamps.txt",
        dir_out / "timestamps.txt",
    ]
    txt_path = next((p for p in candidates if p.is_file()), None)
    if not txt_path:
        return None
    times: list[str] = []
    for line in txt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for p in s.split(","):
            if p.strip():
                times.append(p.strip())
    return times if times else None


def trova_clip_esistenti(dir_out: Path, tipo_slug: str, fmt_slug: str) -> list[Path]:
    uniform_clips = sorted(dir_out.glob(f"{tipo_slug}_{fmt_slug}_clip_*.mp4"))
    if uniform_clips:
        return uniform_clips
    return sorted(dir_out.glob(f"canestro_*_{tipo_slug}_1080p.mp4"))


def risolvi_video_sorgente(dir_out: Path, tipo_slug: str, fmt_slug: str, requested_name: str | None = None) -> Path | None:
    if requested_name:
        cand = dir_out / requested_name
        if cand.is_file():
            return cand
    candidates = [
        dir_out / f"{tipo_slug}_{fmt_slug}_merged_orig.mp4",
        dir_out / f"merged_orig_{tipo_slug}_{fmt_slug}.mp4",
        dir_out / "video_finale.mp4",
    ]
    return next((p for p in candidates if p.is_file()), None)


def chiedi_tempi_taglio(default_before: float = DEFAULT_BUFFER_BEFORE, default_after: float = DEFAULT_BUFFER_AFTER) -> tuple[float, float]:
    print(f"\n{CYAN}--- CONFIGURAZIONE TEMPI DI TAGLIO (Default: {default_before:g}s prima / {default_after:g}s dopo) ---{RESET}", flush=True)
    raw_before = input(f"⏱️  Secondi PRIMA del timestamp [INVIO = {default_before:g}]: ").strip().replace(",", ".")
    raw_after = input(f"⏱️  Secondi DOPO il timestamp  [INVIO = {default_after:g}]: ").strip().replace(",", ".")

    try:
        before_val = float(raw_before) if raw_before else default_before
        if before_val < 0:
            before_val = default_before
    except ValueError:
        before_val = default_before

    try:
        after_val = float(raw_after) if raw_after else default_after
        if after_val <= 0:
            after_val = default_after
    except ValueError:
        after_val = default_after

    return before_val, after_val


def extract_clip(input_video: Path, timestamp: str, out_clip: Path, buffer_before: float, duration: float, res: dict, logger: logging.Logger) -> bool:
    center_sec = time_to_seconds(timestamp)
    start_sec = max(0.0, center_sec - buffer_before)

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-ss", f"{start_sec:.3f}",
        "-i", str(input_video),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264",
        "-preset", res["preset"],
        "-crf", "18",
        "-threads", str(res["threads_per_worker"]),
        "-c:a", "aac",
        "-b:a", "320k",
        "-avoid_negative_ts", "make_zero",
        "-y",
        str(out_clip),
    ]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode == 0 and out_clip.is_file() and out_clip.stat().st_size > 0:
        logger.info(f"[{SCRIPT_NAME}] Clip creata: {out_clip.name} (ts={timestamp}, start={start_sec:.2f}s, dur={duration:.2f}s)")
        return True
    logger.error(f"[{SCRIPT_NAME}] Errore taglio clip {out_clip.name}: {proc.stderr.strip()}")
    return False


def main():
    abilita_ansi()
    disabilita_quick_edit_windows()
    pulisci_console()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Taglio clip originali dai timestamp")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--tipo", default="HIGHLIGHTS")
    parser.add_argument("--sottotitolo", default="CAMPIONATO - TEAM vs TEAM")
    parser.add_argument("--formato", default="1080P")
    parser.add_argument("--before", type=float, default=DEFAULT_BUFFER_BEFORE)
    parser.add_argument("--after", type=float, default=DEFAULT_BUFFER_AFTER)
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    dirs, logger, log_file = setup_dirs_and_logger(base_dir)
    res = calcola_risorse()

    tipo_slug = sanitize_slug(args.tipo)
    fmt_slug = args.formato.strip().upper()
    if fmt_slug not in ("1080P", "REEL"):
        fmt_slug = "1080P"

    print(f"{BOLD}{CYAN}{'=' * 80}{RESET}")
    print(f" {BOLD}{GREEN}INIZIO: {SCRIPT_NAME}{RESET} | Tipo: {BOLD}{args.tipo}{RESET} | Formato: {BOLD}{fmt_slug}{RESET}")
    print(
        f" {YELLOW}[CPU/RAM]{RESET} Thread: {res['threads']} | RAM disp.: {res['ram_avail']}/{res['ram_tot']} GB | "
        f"Worker: {res['max_workers']} | Thread/Worker: {res['threads_per_worker']} | Preset: {res['preset']}"
    )
    print(f"{BOLD}{CYAN}{'=' * 80}{RESET}\n", flush=True)
    logger.info(f"[{SCRIPT_NAME}] Avvio | tipo={args.tipo} | formato={fmt_slug} | auto={args.auto} | risorse={res}")

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print(f"{RED}[X] FFmpeg o FFprobe non trovati nel PATH.{RESET}", flush=True)
        sys.exit(1)

    existing_clips = trova_clip_esistenti(dirs["out"], tipo_slug, fmt_slug)
    if existing_clips:
        print(f"{YELLOW}[!] ATTENZIONE: Attività già svolta! Trovate {len(existing_clips)} clip già presenti in 'OUT/'.{RESET}", flush=True)
        logger.warning(f"[{SCRIPT_NAME}] Trovate {len(existing_clips)} clip già esistenti in OUT.")
        if args.auto:
            print(f"{DIM}   Modalità AUTO: clip già generate presenti, salto taglio per preservare il lavoro esistente.{RESET}", flush=True)
            logger.info(f"[{SCRIPT_NAME}] Modalità AUTO: clip esistenti mantenute, step saltato.")
            return
        print("   1. Sovrascrivi e rigenera tutte le clip")
        print("   2. Salta (mantieni clip esistenti)")
        print("   3. Interrompi script", flush=True)
        while True:
            sc = input("👉 Scelta [INVIO = 2 Salta]: ").strip()
            if sc in ("", "2"):
                print(f"{GREEN}[✔] Mantenute {len(existing_clips)} clip esistenti in 'OUT/'.{RESET}", flush=True)
                return
            if sc == "1":
                break
            if sc == "3":
                print(f"{YELLOW}[!] Elaborazione interrotta dall'utente.{RESET}", flush=True)
                return
            print(f"{RED}[!] Scelta non valida.{RESET}", flush=True)

    default_v = risolvi_video_sorgente(dirs["out"], tipo_slug, fmt_slug)
    if args.auto:
        video_path = default_v
    else:
        default_name = default_v.name if default_v else f"{tipo_slug}_{fmt_slug}_merged_orig.mp4"
        raw_v = input(f"📁 Nome video in 'OUT/' [INVIO = {default_name}]: ").strip()
        video_path = risolvi_video_sorgente(dirs["out"], tipo_slug, fmt_slug, raw_v or None)

    if not video_path or not video_path.is_file():
        print(f"{RED}[X] Video sorgente non trovato in 'OUT/'.{RESET}", flush=True)
        logger.error(f"[{SCRIPT_NAME}] Video sorgente non trovato.")
        sys.exit(1)

    basket_times = load_timestamps(dirs["out"], tipo_slug, fmt_slug)
    if not basket_times:
        print(f"{RED}[X] Nessun timestamp trovato in 'OUT/' ({tipo_slug}_{fmt_slug}_timestamps.txt o timestamps.txt).{RESET}", flush=True)
        logger.error(f"[{SCRIPT_NAME}] Timestamp mancanti in OUT.")
        sys.exit(1)

    vid_dur = get_video_duration(video_path)
    if vid_dur:
        basket_times = [t for t in basket_times if time_to_seconds(t) <= vid_dur]

    if not basket_times:
        print(f"{RED}[X] Nessun timestamp valido rispetto alla durata del video.{RESET}", flush=True)
        sys.exit(1)

    if args.auto:
        buffer_before, buffer_after = float(args.before), float(args.after)
        print(f"\n{CYAN}⏱️  Tempi taglio (AUTO): {buffer_before:g}s prima / {buffer_after:g}s dopo.{RESET}", flush=True)
    else:
        buffer_before, buffer_after = chiedi_tempi_taglio(args.before, args.after)

    clip_duration = buffer_before + buffer_after
    print(f"{CYAN}Taglio parallelo di {len(basket_times)} clip ({buffer_before:g}s prima + {buffer_after:g}s dopo = {clip_duration:.1f}s)...{RESET}", flush=True)
    logger.info(f"[{SCRIPT_NAME}] Avvio taglio {len(basket_times)} clip (prima={buffer_before}s, dopo={buffer_after}s, tot={clip_duration}s) su {res['max_workers']} worker.")

    ok_cnt = err_cnt = 0
    bar_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=res["max_workers"]) as pool:
        futures = {}
        for idx, ts in enumerate(basket_times, start=1):
            ts_slug = ts.replace(":", "-")
            out_clip = dirs["out"] / f"{tipo_slug}_{fmt_slug}_clip_{idx:02d}_{ts_slug}.mp4"
            fut = pool.submit(extract_clip, video_path, ts, out_clip, buffer_before, clip_duration, res, logger)
            futures[fut] = out_clip.name

        with tqdm(total=len(basket_times), desc="Taglio Clip", unit="clip", colour="blue", ncols=78) as bar:
            for fut in as_completed(futures):
                ok = fut.result()
                with bar_lock:
                    if ok:
                        ok_cnt += 1
                    else:
                        err_cnt += 1
                    bar.set_postfix({"ok": ok_cnt, "err": err_cnt})
                    bar.update(1)

    print(f"\n{GREEN}[✔] Taglio completato! Clip generate: {ok_cnt}/{len(basket_times)} (errori: {err_cnt}){RESET}")
    print(f"    Cartella clip : 'OUT/'")
    print(f"    Log salvato   : 'LOG/' ({log_file.name})", flush=True)
    logger.info(f"[{SCRIPT_NAME}] Completato: ok={ok_cnt}, err={err_cnt}")

    if ok_cnt == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()