import os
import sys
import shutil
import logging
import argparse
import subprocess
from pathlib import Path
from tqdm import tqdm

from bahs_core import (
    VIDEO_EXTS,
    RED,
    YELLOW,
    RESET,
    check_ffmpeg_tools,
    cleanup_temp_paths,
    compute_system_resources,
    enable_ansi_console,
    ensure_project_dirs,
    normalize_format,
    sanitize_slug,
    setup_step_logger,
)

SCRIPT_NAME = "02_BAHS_merge-originals-ALL"


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


def main() -> None:
    enable_ansi_console()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Preparazione o unione video originali da IN a OUT")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--tipo", default="HIGHLIGHTS")
    parser.add_argument("--sottotitolo", default="CAMPIONATO - TEAM vs TEAM")
    parser.add_argument("--formato", default="1080P")
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    dirs = ensure_project_dirs(base_dir)
    logger, log_file = setup_step_logger(dirs["log"], SCRIPT_NAME)
    res = compute_system_resources()

    tipo_slug = sanitize_slug(args.tipo)
    fmt_slug = normalize_format(args.formato)

    pid = os.getpid()
    out_file = dirs["out"] / f"{tipo_slug}_{fmt_slug}_merged_orig.mp4"
    legacy_named = dirs["out"] / f"merged_orig_{tipo_slug}_{fmt_slug}.mp4"
    tmp_out_file = dirs["tmp"] / f"{out_file.stem}_{pid}.part.mp4"
    list_file = dirs["tmp"] / f"concat_orig_{tipo_slug}_{fmt_slug}_{pid}.txt"

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | formato='{fmt_slug}' | "
        f"auto={args.auto} | out_file='{out_file}' | risorse={res}"
    )

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
        if len(videos) == 1:
            single_vid = videos[0]
            if single_vid.suffix.lower() == ".mp4":
                logger.info(f"[{SCRIPT_NAME}] Singolo video MP4 rilevato: copia diretta '{single_vid.name}' -> '{out_file.name}'.")
                with tqdm(total=100, desc="02 | Copia Originale", unit="%", colour="green", ncols=78) as bar:
                    shutil.copy2(single_vid, tmp_out_file)
                    bar.update(100)
            else:
                ffmpeg_ok, _ = check_ffmpeg_tools(require_ffprobe=False)
                if not ffmpeg_ok:
                    print(f"{RED}[X] [{SCRIPT_NAME}] FFmpeg non trovato nel PATH.{RESET}", file=sys.stderr)
                    logger.error(f"[{SCRIPT_NAME}] FFmpeg non trovato nel PATH.")
                    sys.exit(1)
                cmd = [
                    "ffmpeg",
                    "-loglevel", "error",
                    "-y",
                    "-i", str(single_vid),
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(tmp_out_file),
                ]
                logger.info(f"[{SCRIPT_NAME}] Singolo video non-MP4: remux diretto '{single_vid.name}' -> cmd={cmd}")
                with tqdm(total=100, desc="02 | Remux Originale", unit="%", colour="green", ncols=78) as bar:
                    bar.update(25)
                    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    bar.update(75)
                if proc.returncode != 0 or not tmp_out_file.is_file() or tmp_out_file.stat().st_size == 0:
                    err_msg = proc.stderr.strip() or f"returncode={proc.returncode}"
                    print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore durante il remux con FFmpeg (log: {log_file.name}).{RESET}", file=sys.stderr)
                    logger.error(f"[{SCRIPT_NAME}] Errore FFmpeg remux (rc={proc.returncode}): {err_msg}")
                    sys.exit(1)
        else:
            ffmpeg_ok, _ = check_ffmpeg_tools(require_ffprobe=False)
            if not ffmpeg_ok:
                print(f"{RED}[X] [{SCRIPT_NAME}] FFmpeg non trovato nel PATH.{RESET}", file=sys.stderr)
                logger.error(f"[{SCRIPT_NAME}] FFmpeg non trovato nel PATH.")
                sys.exit(1)

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
                "-c", "copy",
                "-movflags", "+faststart",
                str(tmp_out_file),
            ]
            logger.info(f"[{SCRIPT_NAME}] Esecuzione comando FFmpeg: {cmd}")

            with tqdm(total=100, desc="02 | Unione Originali", unit="%", colour="green", ncols=78) as bar:
                bar.update(20)
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                bar.update(80)

            if proc.returncode != 0 or not tmp_out_file.is_file() or tmp_out_file.stat().st_size == 0:
                err_msg = proc.stderr.strip() or f"returncode={proc.returncode}"
                print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore durante l'unione con FFmpeg (log: {log_file.name}).{RESET}", file=sys.stderr)
                logger.error(f"[{SCRIPT_NAME}] Errore FFmpeg concat (rc={proc.returncode}): {err_msg}")
                sys.exit(1)

        if not tmp_out_file.is_file() or tmp_out_file.stat().st_size == 0:
            raise RuntimeError(f"File temporaneo non valido: '{tmp_out_file}'")

        os.replace(tmp_out_file, out_file)
        size_mb = out_file.stat().st_size / (1024 * 1024)
        logger.info(f"[{SCRIPT_NAME}] Completato con successo: '{out_file}' ({size_mb:.2f} MB)")

    except Exception as e:
        if not isinstance(e, SystemExit):
            print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore durante la preparazione del video in OUT (log: {log_file.name}).{RESET}", file=sys.stderr)
            logger.exception(f"[{SCRIPT_NAME}] Errore imprevisto: {e}")
            sys.exit(1)
        raise

    finally:
        cleanup_temp_paths([list_file, tmp_out_file], logger)


if __name__ == "__main__":
    main()