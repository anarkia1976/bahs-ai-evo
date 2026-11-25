import os
import re
import sys
import logging
import argparse
import threading
import subprocess
from pathlib import Path
from tqdm import tqdm

from bahs_core import (
    AUDIO_EXTS,
    BOLD,
    NO_MUSIC_TYPES,
    RED,
    RESET,
    YELLOW,
    build_stage_name,
    check_ffmpeg_tools,
    cleanup_temp_paths,
    compute_system_resources,
    enable_ansi_console,
    ensure_project_dirs,
    normalize_format,
    sanitize_slug,
    setup_step_logger,
)

SCRIPT_NAME = "07_BAHS_add-music-overlay-ALL"
MUSIC_VOLUME = 0.6
ORIGINAL_AUDIO_VOLUME = 0.4
MUSIC_FADE_OUT_SEC = 1.0


def is_non_highlights_mode(tipo_raw: str) -> bool:
    return tipo_raw.strip().upper() != "HIGHLIGHTS"


def extract_fade_flag(video_path: Path, default_fade: bool = True) -> bool:
    match = re.search(r"_with_(nofade|fade)(?:_|\.mp4$)", video_path.name, re.IGNORECASE)
    if match:
        return match.group(1).lower() == "fade"
    return default_fade


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


def build_music_filter_complex(total_ms: int, has_orig_audio: bool, apply_fade_out: bool = True) -> tuple[str, str | None]:
    mus_chain = f"[1:a]volume={MUSIC_VOLUME}"
    fade_desc: str | None = None

    if apply_fade_out and total_ms > 100:
        total_sec = total_ms / 1000.0
        fade_dur = min(MUSIC_FADE_OUT_SEC, total_sec * 0.45)
        if fade_dur > 0.05:
            start_sec = max(0.0, total_sec - fade_dur)
            mus_chain += f",afade=t=out:st={start_sec:.3f}:d={fade_dur:.3f}"
            fade_desc = f"st={start_sec:.2f}s,d={fade_dur:.2f}s"

    mus_chain += "[mus]"

    if has_orig_audio:
        fc = (
            f"[0:a]volume={ORIGINAL_AUDIO_VOLUME}[orig];"
            f"{mus_chain};"
            f"[orig][mus]amix=inputs=2:duration=first:normalize=0:dropout_transition=2[aout]"
        )
    else:
        fc = f"{mus_chain.replace('[mus]', '[aout]')}"

    return fc, fade_desc


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


def select_input_video(
    out_dir: Path,
    final_dir: Path,
    tmp_dir: Path,
    tipo_raw: str,
    tipo_slug: str,
    fmt_slug: str,
    auto: bool,
) -> Path | None:
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
    existing: list[Path] = []
    seen: set[Path] = set()
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


def main() -> None:
    enable_ansi_console()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Mix musica di sottofondo sul video unito")
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

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | formato='{fmt_slug}' | "
        f"auto={args.auto} | vol_music={MUSIC_VOLUME} | vol_orig={ORIGINAL_AUDIO_VOLUME} | risorse={res}"
    )

    if args.tipo.strip().upper() in NO_MUSIC_TYPES:
        logger.info(f"[{SCRIPT_NAME}] Tipo '{args.tipo}' in NO_MUSIC_TYPES: step musica saltato.")
        return

    ffmpeg_ok, _ = check_ffmpeg_tools(require_ffprobe=True)
    if not ffmpeg_ok:
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
    filter_complex, fade_desc = build_music_filter_complex(total_ms, has_orig_audio, apply_fade_out=True)
    logger.info(
        f"[{SCRIPT_NAME}] Traccia audio selezionata='{selected_music}' | "
        f"durata_video_ms={total_ms} | orig_has_audio={has_orig_audio} | music_fade_out={fade_desc or 'off'}"
    )

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
        cleanup_temp_paths([tmp_out_file], logger)


if __name__ == "__main__":
    main()