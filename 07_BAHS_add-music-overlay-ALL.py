import os
import re
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

SCRIPT_NAME = "07_BAHS_add-music-overlay-ALL"
MUSIC_VOLUME = 0.6
ORIGINAL_AUDIO_VOLUME = 0.4
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")

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
    return {
        "threads": threads,
        "cores": cores,
        "ram_tot": ram_tot,
        "ram_avail": ram_avail,
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


def extract_fade_flag(video_path: Path, default_fade: bool = True) -> bool:
    m = re.search(r"_with_(nofade|fade)(?:_|\.mp4$)", video_path.name, re.IGNORECASE)
    if m:
        return m.group(1).lower() == "fade"
    return default_fade


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


def probe_media_info(video_path: Path) -> tuple[int, bool]:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "default=noprint_wrappers=1",
        str(video_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    duration_ms = 0
    has_audio = False
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("duration="):
            try:
                duration_ms = max(0, int(round(float(line.split("=", 1)[1]) * 1000.0)))
            except ValueError:
                duration_ms = 0
        elif line == "codec_type=audio":
            has_audio = True
    return duration_ms, has_audio


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
    if is_non_highlights_mode(tipo_raw):
        candidates_non_hl = [
            out_dir / f"{tipo_slug}_{fmt_slug}_merged_orig.mp4",
            out_dir / f"{tipo_slug}_1080P_merged_orig.mp4",
        ]
        for cand in candidates_non_hl:
            if cand.is_file():
                return cand
        return None

    candidates = [
        tmp_dir / build_stage_name(tipo_slug, fmt_slug, fade=True),
        final_dir / build_stage_name(tipo_slug, fmt_slug, fade=True),
        tmp_dir / build_stage_name(tipo_slug, fmt_slug, fade=False),
        final_dir / build_stage_name(tipo_slug, fmt_slug, fade=False),
    ]
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


def attendi_brani_in_cartella_music(music_dir: Path, logger: logging.Logger) -> list[Path]:
    avvisato_log = False
    while True:
        music_files = sorted([f for f in music_dir.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS])
        if music_files:
            return music_files

        if not avvisato_log:
            logger.warning(f"[{SCRIPT_NAME}] Nessun file audio trovato in 'MUSIC/' ({', '.join(AUDIO_EXTS)}).")
            avvisato_log = True

        print(f"\n{RED}{BOLD}[X] [{SCRIPT_NAME}] Nessun brano trovato nella cartella 'MUSIC/' ({', '.join(AUDIO_EXTS)}).{RESET}", file=sys.stderr)
        print(f"{YELLOW}[!] Inserisci almeno un file audio nella cartella 'MUSIC/' prima di proseguire.{RESET}")
        sc = input("\n👉 Premi [INVIO] per ricontrollare 'MUSIC/' (oppure '0' per uscire): ").strip().lower()
        if sc in ("0", "q", "quit", "exit"):
            print(f"{YELLOW}[!] [{SCRIPT_NAME}] Elaborazione interrotta dall'utente.{RESET}")
            logger.info(f"[{SCRIPT_NAME}] Uscita richiesta dall'utente per mancanza brani in MUSIC/.")
            sys.exit(1)


def main():
    abilita_ansi()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Mix musica di sottofondo sul video unito")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--tipo", default="HIGHLIGHTS")
    parser.add_argument("--sottotitolo", default="CAMPIONATO - TEAM vs TEAM")
    parser.add_argument("--formato", default="1080P")
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    dirs, logger, log_file = setup_dirs_and_logger(base_dir)
    res = calcola_risorse()

    tipo_slug = sanitize_slug(args.tipo)
    fmt_slug = args.formato.strip().upper()
    if fmt_slug not in ("1080P", "REEL"):
        fmt_slug = "1080P"

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | formato='{fmt_slug}' | "
        f"auto={args.auto} | vol_music={MUSIC_VOLUME} | vol_orig={ORIGINAL_AUDIO_VOLUME} | risorse={res}"
    )

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print(f"{RED}[X] [{SCRIPT_NAME}] FFmpeg o FFprobe non trovati nel PATH.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] FFmpeg o FFprobe non trovati nel PATH.")
        sys.exit(1)

    in_video = select_input_video(dirs["out"], dirs["final"], dirs["tmp"], args.tipo, tipo_slug, fmt_slug, args.auto)
    if not in_video or not in_video.is_file():
        if is_non_highlights_mode(args.tipo):
            msg = f"Video sorgente per '{tipo_slug}' ({fmt_slug}) non trovato in 'OUT/'."
        else:
            msg = "Nessun video unito trovato in 'FINAL/' o 'TMP/'."
        print(f"{RED}[X] [{SCRIPT_NAME}] {msg}{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] {msg}")
        sys.exit(1)

    fade_flag = extract_fade_flag(in_video, default_fade=True)
    stage_name = build_stage_name(tipo_slug, fmt_slug, fade=fade_flag, music=True)
    out_file = dirs["final"] / stage_name
    tmp_out_file = dirs["tmp"] / f"{out_file.stem}_{os.getpid()}.part.mp4"

    logger.info(f"[{SCRIPT_NAME}] Video input='{in_video}' | fade={fade_flag} | out_file='{out_file}'")

    azione = gestione_file_esistente(out_file, args.auto, logger)
    if azione == "abort":
        print(f"{YELLOW}[!] [{SCRIPT_NAME}] Elaborazione interrotta dall'utente.{RESET}")
        return
    if azione == "skip":
        logger.info(f"[{SCRIPT_NAME}] Completato (SKIP): mantenuto '{out_file}'.")
        return

    music_files = attendi_brani_in_cartella_music(dirs["music"], logger)
    selected_music = music_files[0]
    total_ms, has_orig_audio = probe_media_info(in_video)
    logger.info(
        f"[{SCRIPT_NAME}] Traccia audio selezionata='{selected_music}' | "
        f"durata_video_ms={total_ms} | orig_has_audio={has_orig_audio}"
    )

    if has_orig_audio:
        filter_complex = (
            f"[0:a]volume={ORIGINAL_AUDIO_VOLUME}[orig];"
            f"[1:a]volume={MUSIC_VOLUME}[mus];"
            f"[orig][mus]amix=inputs=2:duration=first:normalize=0:dropout_transition=2[aout]"
        )
    else:
        filter_complex = f"[1:a]volume={MUSIC_VOLUME}[aout]"

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-i", str(in_video),
        "-stream_loop", "-1",
        "-i", str(selected_music),
        "-filter_complex", filter_complex,
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",
        "-threads", str(res["threads"]),
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        "-progress", "pipe:1",
        "-nostats",
        "-y",
        str(tmp_out_file),
    ]
    logger.info(f"[{SCRIPT_NAME}] Esecuzione FFmpeg: {cmd}")

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
        with tqdm(total=max(1, total_ms), desc="07 | Mix Musica", unit="ms", unit_scale=True, colour="green", ncols=78) as bar:
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
            print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore durante il mix audio con FFmpeg (log: {log_file.name}).{RESET}", file=sys.stderr)
            logger.error(f"[{SCRIPT_NAME}] Errore FFmpeg (rc={rc}): {err_msg}")
            sys.exit(1)

        os.replace(tmp_out_file, out_file)
        size_mb = out_file.stat().st_size / (1024 * 1024)
        logger.info(f"[{SCRIPT_NAME}] Completato con successo: '{out_file}' ({size_mb:.2f} MB, musica='{selected_music.name}')")
    finally:
        if tmp_out_file.exists():
            tmp_out_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()