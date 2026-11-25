import os
import sys
import logging
import argparse
import threading
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from bahs_core import (
    RED,
    YELLOW,
    RESET,
    check_ffmpeg_tools,
    compute_system_resources,
    disable_windows_quick_edit,
    enable_ansi_console,
    ensure_project_dirs,
    normalize_format,
    parse_time_to_seconds,
    read_timestamps_file,
    resolve_merged_orig_path,
    resolve_timestamp_files,
    sanitize_slug,
    setup_step_logger,
)

SCRIPT_NAME = "04_BAHS_split-original-ALL"
DEFAULT_BUFFER_BEFORE = 3.0
DEFAULT_BUFFER_AFTER = 3.0


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


def load_timestamps(dir_out: Path, tipo_slug: str, fmt_slug: str) -> tuple[list[str] | None, Path | None]:
    specific_txt, generic_txt = resolve_timestamp_files(dir_out, tipo_slug, fmt_slug)
    txt_path = next((p for p in (specific_txt, generic_txt) if p.is_file()), None)
    if not txt_path:
        return None, None
    times = read_timestamps_file(txt_path, validate_seconds=True)
    return (times if times else None), txt_path


def trova_clip_esistenti(dir_out: Path, tipo_slug: str, fmt_slug: str) -> list[Path]:
    return sorted(dir_out.glob(f"{tipo_slug}_{fmt_slug}_clip_*.mp4"))


def chiedi_tempi_taglio(default_before: float = DEFAULT_BUFFER_BEFORE, default_after: float = DEFAULT_BUFFER_AFTER) -> tuple[float, float]:
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


def extract_clip(
    input_video: Path,
    timestamp: str,
    out_clip: Path,
    tmp_dir: Path,
    buffer_before: float,
    duration: float,
    res: dict,
    logger: logging.Logger,
) -> tuple[bool, str]:
    center_sec = parse_time_to_seconds(timestamp)
    if center_sec is None:
        return False, f"{out_clip.name}: timestamp non valido ('{timestamp}')"

    start_sec = max(0.0, center_sec - buffer_before)
    tmp_clip = tmp_dir / f"{out_clip.stem}_{os.getpid()}_{threading.get_ident()}.part.mp4"

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
        "-pix_fmt", "yuv420p",
        "-threads", str(res["threads_per_worker"]),
        "-c:a", "aac",
        "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        "-y",
        str(tmp_clip),
    ]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode == 0 and tmp_clip.is_file() and tmp_clip.stat().st_size > 0:
            os.replace(tmp_clip, out_clip)
            logger.info(f"[{SCRIPT_NAME}] Clip creata: '{out_clip.name}' (ts={timestamp}, start={start_sec:.2f}s, dur={duration:.2f}s)")
            return True, ""
        err_detail = proc.stderr.strip() or f"returncode={proc.returncode}"
        logger.error(f"[{SCRIPT_NAME}] Errore taglio clip '{out_clip.name}' (ts={timestamp}, rc={proc.returncode}): {err_detail}")
        return False, f"{out_clip.name}: {err_detail}"
    finally:
        if tmp_clip.exists():
            try:
                tmp_clip.unlink()
            except Exception:
                pass


def esegui_taglio_parallelo(
    video_path: Path,
    basket_times: list[str],
    dirs: dict[str, Path],
    tipo_slug: str,
    fmt_slug: str,
    buffer_before: float,
    clip_duration: float,
    res: dict,
    logger: logging.Logger,
) -> tuple[int, int, list[str]]:
    ok_cnt = err_cnt = 0
    errors_list: list[str] = []
    bar_lock = threading.Lock()
    workers = res["norm_workers"]

    sys.stdout.flush()
    sys.stderr.flush()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for idx, ts in enumerate(basket_times, start=1):
            ts_slug = ts.replace(":", "-")
            out_clip = dirs["out"] / f"{tipo_slug}_{fmt_slug}_clip_{idx:02d}_{ts_slug}.mp4"
            fut = pool.submit(
                extract_clip,
                video_path,
                ts,
                out_clip,
                dirs["tmp"],
                buffer_before,
                clip_duration,
                res,
                logger,
            )
            futures[fut] = out_clip.name

        with tqdm(
            total=len(basket_times),
            desc="04 | Taglio Clip",
            unit="clip",
            colour="blue",
            ncols=78,
            file=sys.stdout,
        ) as bar:
            for fut in as_completed(futures):
                ok, err_msg = fut.result()
                with bar_lock:
                    if ok:
                        ok_cnt += 1
                    else:
                        err_cnt += 1
                        errors_list.append(err_msg)
                    bar.set_postfix({"ok": ok_cnt, "err": err_cnt})
                    bar.update(1)

    sys.stdout.flush()
    return ok_cnt, err_cnt, errors_list


def main() -> None:
    enable_ansi_console()
    disable_windows_quick_edit()

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
    dirs = ensure_project_dirs(base_dir)
    logger, log_file = setup_step_logger(dirs["log"], SCRIPT_NAME)
    res = compute_system_resources()

    tipo_slug = sanitize_slug(args.tipo)
    fmt_slug = normalize_format(args.formato)

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | formato='{fmt_slug}' | "
        f"before={args.before} | after={args.after} | auto={args.auto} | risorse={res}"
    )

    ffmpeg_ok, _ = check_ffmpeg_tools(require_ffprobe=True)
    if not ffmpeg_ok:
        print(f"\n{RED}[X] [{SCRIPT_NAME}] FFmpeg o FFprobe non trovati nel PATH.{RESET}", file=sys.stderr, flush=True)
        logger.error(f"[{SCRIPT_NAME}] FFmpeg o FFprobe non trovati nel PATH.")
        sys.exit(1)

    existing_clips = trova_clip_esistenti(dirs["out"], tipo_slug, fmt_slug)
    if existing_clips:
        logger.warning(f"[{SCRIPT_NAME}] Trovate {len(existing_clips)} clip già esistenti in OUT.")
        if args.auto:
            logger.info(f"[{SCRIPT_NAME}] Modalità AUTO: clip esistenti mantenute, step saltato.")
            return
        print(f"\n{YELLOW}[!] ATTENZIONE: Trovate {len(existing_clips)} clip già presenti in 'OUT/'.{RESET}", flush=True)
        print("   1. Sovrascrivi e rigenera tutte le clip")
        print("   2. Salta (mantieni clip esistenti)")
        print("   3. Interrompi script")
        while True:
            sc = input("👉 Scelta [INVIO = 2 Salta]: ").strip()
            if sc in ("", "2"):
                logger.info(f"[{SCRIPT_NAME}] Scelta utente: mantieni {len(existing_clips)} clip esistenti.")
                return
            if sc == "1":
                logger.info(f"[{SCRIPT_NAME}] Scelta utente: rigenerazione clip (rimozione {len(existing_clips)} clip precedenti).")
                for old_clip in existing_clips:
                    old_clip.unlink(missing_ok=True)
                break
            if sc == "3":
                print(f"{YELLOW}[!] [{SCRIPT_NAME}] Elaborazione interrotta dall'utente.{RESET}", flush=True)
                logger.info(f"[{SCRIPT_NAME}] Interrotto dall'utente.")
                return
            print(f"{RED}[!] Scelta non valida.{RESET}", flush=True)

    default_v = resolve_merged_orig_path(dirs["out"], tipo_slug, fmt_slug)
    if args.auto:
        video_path = default_v
    else:
        default_name = default_v.name if default_v else f"{tipo_slug}_{fmt_slug}_merged_orig.mp4"
        raw_v = input(f"📁 Nome video in 'OUT/' [INVIO = {default_name}]: ").strip()
        video_path = resolve_merged_orig_path(dirs["out"], tipo_slug, fmt_slug, raw_v or None)

    if not video_path or not video_path.is_file():
        print(f"\n{RED}[X] [{SCRIPT_NAME}] Video sorgente non trovato in 'OUT/'.{RESET}", file=sys.stderr, flush=True)
        logger.error(f"[{SCRIPT_NAME}] Video sorgente non trovato in '{dirs['out']}'.")
        sys.exit(1)

    basket_times, ts_source_path = load_timestamps(dirs["out"], tipo_slug, fmt_slug)
    if not basket_times:
        print(f"\n{RED}[X] [{SCRIPT_NAME}] Nessun timestamp valido trovato in 'OUT/' ({tipo_slug}_{fmt_slug}_timestamps.txt o timestamps.txt).{RESET}", file=sys.stderr, flush=True)
        logger.error(f"[{SCRIPT_NAME}] Timestamp mancanti o non validi in '{dirs['out']}'.")
        sys.exit(1)

    vid_dur = get_video_duration(video_path)
    logger.info(f"[{SCRIPT_NAME}] Video sorgente='{video_path}' (durata={vid_dur}) | file_ts='{ts_source_path}' (voci={len(basket_times)})")
    if vid_dur:
        valid_times = [t for t in basket_times if (parse_time_to_seconds(t) or 0.0) <= vid_dur]
        if len(valid_times) != len(basket_times):
            logger.warning(f"[{SCRIPT_NAME}] Scartati {len(basket_times) - len(valid_times)} timestamp oltre durata video ({vid_dur:.2f}s).")
        basket_times = valid_times

    if not basket_times:
        print(f"\n{RED}[X] [{SCRIPT_NAME}] Nessun timestamp valido rispetto alla durata del video.{RESET}", file=sys.stderr, flush=True)
        logger.error(f"[{SCRIPT_NAME}] Nessun timestamp valido rispetto alla durata video ({vid_dur}).")
        sys.exit(1)

    if args.auto:
        buffer_before, buffer_after = float(args.before), float(args.after)
    else:
        buffer_before, buffer_after = chiedi_tempi_taglio(args.before, args.after)

    clip_duration = buffer_before + buffer_after
    logger.info(
        f"[{SCRIPT_NAME}] Avvio taglio {len(basket_times)} clip (prima={buffer_before}s, dopo={buffer_after}s, tot={clip_duration}s) "
        f"su {res['norm_workers']} worker (threads_per_worker={res['threads_per_worker']})."
    )

    ok_cnt, err_cnt, errors_list = esegui_taglio_parallelo(
        video_path=video_path,
        basket_times=basket_times,
        dirs=dirs,
        tipo_slug=tipo_slug,
        fmt_slug=fmt_slug,
        buffer_before=buffer_before,
        clip_duration=clip_duration,
        res=res,
        logger=logger,
    )

    logger.info(f"[{SCRIPT_NAME}] Completato: ok={ok_cnt}/{len(basket_times)}, err={err_cnt}")

    if err_cnt > 0:
        print(f"\n{RED}[X] [{SCRIPT_NAME}] Errori durante il taglio di {err_cnt}/{len(basket_times)} clip (log: {log_file.name}):{RESET}", file=sys.stderr, flush=True)
        for e in errors_list[:5]:
            print(f"{RED}   • {e}{RESET}", file=sys.stderr, flush=True)

    if ok_cnt == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()