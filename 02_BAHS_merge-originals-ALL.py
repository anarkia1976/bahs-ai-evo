import os
import sys
import ctypes
import shutil
import logging
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from tqdm import tqdm

SCRIPT_NAME = "02_BAHS_merge-originals-ALL"
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm")

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
    max_workers = 2 if (threads <= 4 or ram_avail < 6.0) else min(cores, 8)
    threads_per_worker = max(1, min(4, threads // max_workers))
    return {
        "threads": threads,
        "cores": cores,
        "ram_tot": ram_tot,
        "ram_avail": ram_avail,
        "max_workers": max_workers,
        "threads_per_worker": threads_per_worker,
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


def gestione_file_esistente(out_file: Path, auto: bool, logger: logging.Logger) -> str:
    if not out_file.is_file() or out_file.stat().st_size == 0:
        return "overwrite"

    size_mb = out_file.stat().st_size / (1024 * 1024)
    logger.warning(f"[{SCRIPT_NAME}] File di output già esistente: '{out_file}' ({size_mb:.1f} MB)")

    if auto:
        logger.info(f"[{SCRIPT_NAME}] Modalità AUTO: file già presente, step saltato.")
        return "skip"

    print(f"\n{YELLOW}[!] ATTENZIONE: File di output già esistente: '{out_file.name}' ({size_mb:.1f} MB){RESET}")
    print("   1. Sovrascrivi e rigenera")
    print("   2. Salta (mantieni file esistente e prosegui)")
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
        print(f"{RED}[!] Scelta non valida (1, 2 o 3).{RESET}")


def main():
    abilita_ansi()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Unione video originali da IN a OUT")
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

    out_file = dirs["out"] / f"{tipo_slug}_{fmt_slug}_merged_orig.mp4"
    legacy_named = dirs["out"] / f"merged_orig_{tipo_slug}_{fmt_slug}.mp4"
    list_file = dirs["tmp"] / f"concat_orig_{tipo_slug}_{fmt_slug}.txt"

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | formato='{fmt_slug}' | "
        f"auto={args.auto} | out_file='{out_file}' | risorse={res}"
    )

    if shutil.which("ffmpeg") is None:
        print(f"{RED}[X] [{SCRIPT_NAME}] FFmpeg non trovato nel PATH.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] FFmpeg non trovato nel PATH.")
        sys.exit(1)

    target_check = out_file if out_file.is_file() else (legacy_named if legacy_named.is_file() else out_file)
    azione = gestione_file_esistente(target_check, args.auto, logger)
    if azione == "abort":
        print(f"{YELLOW}[!] [{SCRIPT_NAME}] Elaborazione interrotta dall'utente.{RESET}")
        return
    if azione == "skip":
        if not out_file.is_file() and target_check.is_file():
            shutil.copy2(target_check, out_file)
            logger.info(f"[{SCRIPT_NAME}] Copiato file legacy '{target_check}' in '{out_file}'.")
        logger.info(f"[{SCRIPT_NAME}] Completato (SKIP): mantenuto '{out_file}'.")
        return

    videos = sorted([p for p in dirs["in"].iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS])
    if not videos:
        print(f"{RED}[X] [{SCRIPT_NAME}] Nessun video trovato in '{dirs['in']}'.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] Nessun video trovato in '{dirs['in']}'.")
        sys.exit(1)

    logger.info(f"[{SCRIPT_NAME}] Trovati {len(videos)} video originali in IN: {[v.name for v in videos]}")

    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for v in videos:
                f.write(f"file '{v.resolve().as_posix()}'\n")
        logger.info(f"[{SCRIPT_NAME}] Generata lista concat in '{list_file}' ({len(videos)} file).")

        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-threads", str(res["threads"]),
            "-c", "copy",
            str(out_file),
        ]
        logger.info(f"[{SCRIPT_NAME}] Esecuzione comando FFmpeg: {cmd}")

        with tqdm(total=100, desc="02 | Unione Originali", unit="%", colour="green", ncols=78) as bar:
            bar.update(20)
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            bar.update(80)

        if proc.returncode != 0 or not out_file.is_file() or out_file.stat().st_size == 0:
            err_msg = proc.stderr.strip() or f"returncode={proc.returncode}"
            print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore durante l'unione con FFmpeg (log: {log_file.name}).{RESET}", file=sys.stderr)
            logger.error(f"[{SCRIPT_NAME}] Errore FFmpeg (rc={proc.returncode}): {err_msg}")
            sys.exit(1)

        size_mb = out_file.stat().st_size / (1024 * 1024)
        logger.info(f"[{SCRIPT_NAME}] Completato con successo: '{out_file}' ({size_mb:.2f} MB)")

    finally:
        if list_file.exists():
            try:
                list_file.unlink()
            except Exception as e:
                logger.warning(f"[{SCRIPT_NAME}] Impossibile rimuovere file temporaneo '{list_file}': {e}")


if __name__ == "__main__":
    main()
