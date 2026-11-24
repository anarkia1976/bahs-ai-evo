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

SCRIPT_NAME = "06_BAHS_merge-clips-ALL"
FADE_DURATION = 1.0
MAX_LINE_WIDTH = 80

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

    if threads <= 4 or ram_avail < 6.0:
        preset = "veryfast"
    elif threads <= 8 or ram_avail < 12.0:
        preset = "fast"
    else:
        preset = "medium"

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


def format_clip_label(clip_path: Path) -> str:
    stem = clip_path.stem
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].replace("-", "").isdigit() and "-" in parts[1]:
        return parts[0]
    return stem


def build_stage_name(tipo_slug: str, fmt_slug: str, fade: bool = True, music: bool = False, text: bool = False, logo: bool = False) -> str:
    parts = [tipo_slug, fmt_slug, "with", "fade" if fade else "nofade"]
    if music:
        parts.append("music")
    if text:
        parts.append("text")
    if logo:
        parts.append("logo")
    return "_".join(parts) + ".mp4"


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


def get_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        return max(0.0, float(res.stdout.strip()))
    except ValueError:
        return 0.0


def trova_clip_formato(out_dir: Path, tipo_slug: str, fmt_slug: str) -> list[Path]:
    uniform_prefix = f"{tipo_slug}_{fmt_slug}_clip_"
    uniform_clips = sorted([
        p for p in out_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".mp4" and p.name.startswith(uniform_prefix)
    ])
    if uniform_clips:
        return uniform_clips

    all_legacy = sorted([
        p for p in out_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".mp4" and p.stem.lower().startswith("canestro_")
    ])
    if fmt_slug == "REEL":
        return [p for p in all_legacy if p.stem.lower().endswith("_reel")]
    return [p for p in all_legacy if not p.stem.lower().endswith("_reel")]


def normalize_clip(src: Path, dst: Path, is_first: bool, is_last: bool, duration: float, use_fade: bool, res: dict, logger: logging.Logger) -> tuple[bool, str]:
    vf = "fps=30,format=yuv420p"
    af = "aresample=48000,aformat=channel_layouts=stereo"
    if use_fade and is_first and duration > FADE_DURATION:
        vf += f",fade=t=in:st=0:d={FADE_DURATION}"
        af += f",afade=t=in:st=0:d={FADE_DURATION}"
    if use_fade and is_last and duration > FADE_DURATION:
        st_out = max(0.0, duration - FADE_DURATION)
        vf += f",fade=t=out:st={st_out}:d={FADE_DURATION}"
        af += f",afade=t=out:st={st_out}:d={FADE_DURATION}"

    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-i", str(src),
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", res["preset"],
        "-threads", str(res["threads"]),
        "-c:a", "aac",
        "-b:a", "192k",
        str(dst),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
        logger.info(f"[{SCRIPT_NAME}] Normalizzata clip '{src.name}' -> '{dst.name}' (dur={duration:.2f}s)")
        return True, ""
    err_detail = proc.stderr.strip() or f"returncode={proc.returncode}"
    logger.error(f"[{SCRIPT_NAME}] Errore normalizzazione '{src.name}' (rc={proc.returncode}): {err_detail}")
    return False, err_detail


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


def main():
    abilita_ansi()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Unione clip 1080P o REEL con transizioni")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--tipo", default="HIGHLIGHTS")
    parser.add_argument("--sottotitolo", default="CAMPIONATO - TEAM vs TEAM")
    parser.add_argument("--formato", default="1080P")
    parser.add_argument("--no-fade", action="store_true")
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    dirs, logger, log_file = setup_dirs_and_logger(base_dir)
    res = calcola_risorse()

    fmt_slug = args.formato.strip().upper()
    if fmt_slug not in ("1080P", "REEL"):
        fmt_slug = "1080P"

    if not args.auto:
        print("Formato clip da unire:")
        print("  1. 1080P [Default]")
        print("  2. REEL")
        sc_f = input("👉 Scelta [INVIO = mantieni/1080P]: ").strip().lower()
        if sc_f in ("2", "reel"):
            fmt_slug = "REEL"
        elif sc_f in ("1", "1080p"):
            fmt_slug = "1080P"

    use_fade = not args.no_fade
    tipo_slug = sanitize_slug(args.tipo)
    stage_name = build_stage_name(tipo_slug, fmt_slug, fade=use_fade)
    out_file = dirs["final"] / stage_name
    tmp_out_file = dirs["tmp"] / stage_name

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | formato='{fmt_slug}' | "
        f"fade={use_fade} | auto={args.auto} | out_file='{out_file}' | risorse={res}"
    )

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print(f"{RED}[X] [{SCRIPT_NAME}] FFmpeg o FFprobe non trovati nel PATH.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] FFmpeg o FFprobe non trovati nel PATH.")
        sys.exit(1)

    azione = gestione_file_esistente(out_file, args.auto, logger)
    if azione == "abort":
        print(f"{YELLOW}[!] [{SCRIPT_NAME}] Elaborazione interrotta dall'utente.{RESET}")
        return
    if azione == "skip":
        logger.info(f"[{SCRIPT_NAME}] Completato (SKIP): mantenuto '{out_file}'.")
        return

    selected_clips = trova_clip_formato(dirs["out"], tipo_slug, fmt_slug)
    if not selected_clips:
        print(f"{RED}[X] [{SCRIPT_NAME}] Nessuna clip {fmt_slug} per '{tipo_slug}' trovata in 'OUT/'.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] Nessuna clip {fmt_slug} per '{tipo_slug}' trovata in '{dirs['out']}'.")
        sys.exit(1)

    logger.info(f"[{SCRIPT_NAME}] Trovate {len(selected_clips)} clip {fmt_slug} da unire: {[c.name for c in selected_clips]}")

    step_tmp_dir = dirs["tmp"] / f"merge_{tipo_slug}_{fmt_slug}"
    step_tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        norm_items: list[dict] = []
        total_n = len(selected_clips)
        total_steps = total_n + 1

        with tqdm(
            total=total_steps,
            desc=f"06 | Unione Clip {fmt_slug}",
            unit="step",
            colour="green",
            ncols=MAX_LINE_WIDTH,
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} - {postfix}",
        ) as bar:
            for idx, clip_p in enumerate(selected_clips):
                bar.set_postfix_str(format_clip_label(clip_p))
                dst_p = step_tmp_dir / f"norm_{idx:03d}.mp4"
                dur = get_duration(clip_p)
                ok_norm, err_norm = normalize_clip(clip_p, dst_p, idx == 0, idx == total_n - 1, dur, use_fade, res, logger)
                if not ok_norm:
                    print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore normalizzazione clip '{clip_p.name}' (log: {log_file.name}).{RESET}", file=sys.stderr)
                    sys.exit(1)
                norm_items.append({"path": dst_p, "duration": get_duration(dst_p)})
                bar.update(1)

            bar.set_postfix_str(Path(tmp_out_file.name).stem)
            if len(norm_items) == 1:
                shutil.copy2(norm_items[0]["path"], tmp_out_file)
                logger.info(f"[{SCRIPT_NAME}] Singola clip copiata direttamente in '{tmp_out_file}'.")
            elif not use_fade:
                concat_txt = step_tmp_dir / "concat_nofade.txt"
                with open(concat_txt, "w", encoding="utf-8") as f:
                    for it in norm_items:
                        f.write(f"file '{it['path'].resolve().as_posix()}'\n")
                cmd = [
                    "ffmpeg",
                    "-loglevel", "error",
                    "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_txt),
                    "-c:v", "libx264",
                    "-crf", "18",
                    "-preset", res["preset"],
                    "-threads", str(res["threads"]),
                    "-c:a", "aac",
                    "-b:a", "192k",
                    str(tmp_out_file),
                ]
                logger.info(f"[{SCRIPT_NAME}] Esecuzione FFmpeg nofade: {cmd}")
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if proc.returncode != 0 or not tmp_out_file.is_file() or tmp_out_file.stat().st_size == 0:
                    print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore durante l'unione senza fade con FFmpeg (log: {log_file.name}).{RESET}", file=sys.stderr)
                    logger.error(f"[{SCRIPT_NAME}] Errore FFmpeg nofade (rc={proc.returncode}): {proc.stderr.strip()}")
                    sys.exit(1)
            else:
                cmd = ["ffmpeg", "-loglevel", "error", "-y"]
                for item in norm_items:
                    cmd.extend(["-i", str(item["path"])])

                fc_parts = []
                cur_dur = norm_items[0]["duration"]
                for i in range(len(norm_items) - 1):
                    nxt_dur = norm_items[i + 1]["duration"]
                    offset = max(0.0, cur_dur - FADE_DURATION)
                    v1 = f"[{i}:v]" if i == 0 else f"[v{i}]"
                    v2 = f"[{i + 1}:v]"
                    vo = f"[v{i + 1}]" if i < len(norm_items) - 2 else "[vout]"
                    a1 = f"[{i}:a]" if i == 0 else f"[a{i}]"
                    a2 = f"[{i + 1}:a]"
                    ao = f"[a{i + 1}]" if i < len(norm_items) - 2 else "[aout]"
                    fc_parts.append(f"{v1}{v2}xfade=transition=fade:duration={FADE_DURATION}:offset={offset:.3f}{vo}")
                    fc_parts.append(f"{a1}{a2}acrossfade=d={FADE_DURATION}{ao}")
                    cur_dur = offset + nxt_dur

                cmd.extend([
                    "-filter_complex", ";".join(fc_parts),
                    "-map", "[vout]",
                    "-map", "[aout]",
                    "-c:v", "libx264",
                    "-crf", "18",
                    "-preset", res["preset"],
                    "-threads", str(res["threads"]),
                    "-c:a", "aac",
                    "-b:a", "192k",
                    str(tmp_out_file),
                ])
                logger.info(f"[{SCRIPT_NAME}] Esecuzione FFmpeg xfade ({len(norm_items)} clip).")

                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if proc.returncode != 0 or not tmp_out_file.is_file() or tmp_out_file.stat().st_size == 0:
                    print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore durante l'unione finale con FFmpeg (log: {log_file.name}).{RESET}", file=sys.stderr)
                    logger.error(f"[{SCRIPT_NAME}] Errore FFmpeg xfade (rc={proc.returncode}): {proc.stderr.strip()}")
                    sys.exit(1)

            bar.update(1)

        shutil.move(str(tmp_out_file), str(out_file))
        size_mb = out_file.stat().st_size / (1024 * 1024)
        logger.info(f"[{SCRIPT_NAME}] Completato con successo: '{out_file}' ({size_mb:.2f} MB)")

    finally:
        if tmp_out_file.exists():
            tmp_out_file.unlink(missing_ok=True)
        if step_tmp_dir.exists():
            shutil.rmtree(step_tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()