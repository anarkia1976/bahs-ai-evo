import os
import sys
import time
import ctypes
import logging
import argparse
from pathlib import Path
import cv2
from tqdm import tqdm

from bahs_core import (
    RED,
    YELLOW,
    RESET,
    compute_system_resources,
    enable_ansi_console,
    ensure_project_dirs,
    has_valid_timestamps,
    normalize_format,
    read_timestamps_file,
    resolve_merged_orig_path,
    resolve_timestamp_files,
    sanitize_slug,
    seconds_to_time_str,
    setup_step_logger,
    sync_generic_timestamps,
)

SCRIPT_NAME = "03_BAHS_identify-timestamps-ALL"
DEFAULT_SPEED = 1.0
ALLOWED_CHOICES = (1.0, 2.0, 4.0)
LARGE_SKIP_FRAMES_THRESHOLD = 6


def controlla_timestamp_out(out_dir: Path, txt_path: Path, logger: logging.Logger) -> list[str]:
    if not txt_path.is_file():
        logger.info(f"[{SCRIPT_NAME}] File timestamp '{txt_path}' non presente in '{out_dir.name}/'.")
        return []

    saved_times = read_timestamps_file(txt_path, validate_seconds=False)
    if saved_times:
        logger.info(f"[{SCRIPT_NAME}] File '{txt_path}' presente con {len(saved_times)} timestamp: {saved_times}")
    else:
        logger.warning(f"[{SCRIPT_NAME}] File '{txt_path}' presente ma privo di timestamp validi.")
    return saved_times


def put_contrasted_text(
    frame,
    text: str,
    org: tuple[int, int],
    font_scale: float,
    color: tuple[int, int, int],
    thickness: int = 2,
) -> None:
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness + 2, cv2.LINE_8)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness, cv2.LINE_8)


def draw_overlay(
    frame,
    current_sec: float,
    total_sec: float,
    speed: float,
    paused: bool,
    recorded_times: list[str],
    flash_active: bool,
    session_label: str,
):
    h, w, _ = frame.shape
    put_contrasted_text(frame, f"Sessione: {session_label}", (20, 45), 0.9, (0, 255, 255), 2)

    time_str = seconds_to_time_str(current_sec)
    status_text = "PAUSA (Premi P per riprendere)" if paused else f"IN RIPRODUZIONE ({speed:g}x)"
    status_color = (0, 165, 255) if paused else (0, 255, 0)

    put_contrasted_text(frame, f"Tempo: {time_str}", (20, 90), 0.9, (255, 255, 255), 2)
    put_contrasted_text(frame, status_text, (20, 135), 0.7, status_color, 2)
    put_contrasted_text(frame, f"Canestri registrati: {len(recorded_times)}", (20, 185), 0.7, (0, 255, 255), 2)

    start_idx = max(0, len(recorded_times) - 6)
    y_offset = 225
    for i, t in enumerate(recorded_times[start_idx:], start=start_idx + 1):
        put_contrasted_text(frame, f"[{i}] Canestro: {t}", (20, y_offset), 0.65, (255, 255, 0), 2)
        y_offset += 35

    if flash_active:
        put_contrasted_text(frame, "CANESTRO REGISTRATO!", (w // 2 - 320, h // 2), 1.4, (0, 255, 0), 3)

    bar_x, bar_y, bar_w, bar_h = 30, h - 85, w - 60, 16
    progress = min(1.0, max(0.0, current_sec / total_sec)) if total_sec > 0 else 0.0
    filled_w = int(bar_w * progress)

    cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (50, 50, 50), -1)
    cv2.rectangle(frame, (bar_x, bar_y), (bar_x + filled_w, bar_y + bar_h), (0, 200, 0), -1)
    cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (255, 255, 255), 1)

    total_str = seconds_to_time_str(total_sec)
    progress_text = f"Progresso Elaborazione: {time_str} / {total_str} ({int(progress * 100)}%)"
    put_contrasted_text(frame, progress_text, (bar_x, bar_y - 12), 0.6, (255, 255, 255), 2)
    put_contrasted_text(frame, "[BARRA SPAZIO]: Canestro | [P]: Pausa | [F/R]: Avanti/Indietro 5s | [Q]: Salva ed Esci", (30, h - 25), 0.6, (0, 255, 255), 2)
    return frame


def seek_to_seconds(cap, target_sec: float, fps: float, total_frames: int) -> tuple[bool, object, int, float]:
    clamped_sec = max(0.0, target_sec)
    if total_frames > 0:
        max_frame = max(0, total_frames - 1)
        target_frame = min(max_frame, int(round(clamped_sec * fps)))
    else:
        target_frame = int(round(clamped_sec * fps))

    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
    ret, frame = cap.read()
    if not ret or frame is None:
        return False, None, target_frame, clamped_sec
    return True, frame, target_frame, target_frame / fps


def porta_in_primo_piano(window_title: str) -> None:
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, window_title)
            if hwnd:
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
                user32.SetFocus(hwnd)
        except Exception:
            pass


def main() -> None:
    enable_ansi_console()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Identificazione interattiva timestamp canestri")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--tipo", default="HIGHLIGHTS")
    parser.add_argument("--sottotitolo", default="CAMPIONATO - TEAM vs TEAM")
    parser.add_argument("--formato", default="1080P")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="Velocità riproduzione (es. 1.0, 2.0, 4.0)")
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    dirs = ensure_project_dirs(base_dir)
    logger, log_file = setup_step_logger(dirs["log"], SCRIPT_NAME)
    res = compute_system_resources()

    tipo_slug = sanitize_slug(args.tipo)
    fmt_slug = normalize_format(args.formato)

    session_label = f"{tipo_slug}_{fmt_slug}_{sanitize_slug(args.sottotitolo)}"
    speed = float(args.speed) if float(args.speed) in ALLOWED_CHOICES else DEFAULT_SPEED

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | "
        f"sottotitolo='{args.sottotitolo}' | formato='{fmt_slug}' | session='{session_label}' | "
        f"speed={speed:g} | auto={args.auto} | risorse={res}"
    )

    specific_txt, generic_txt = resolve_timestamp_files(dirs["out"], tipo_slug, fmt_slug)
    if tipo_slug.upper() == "HIGHLIGHTS" and fmt_slug == "REEL" and has_valid_timestamps(generic_txt):
        txt_path = generic_txt
    else:
        txt_path = specific_txt

    existing_timestamps = controlla_timestamp_out(dirs["out"], txt_path, logger)
    logger.info(f"[{SCRIPT_NAME}] Sessione impostata: '{session_label}' | target_txt='{txt_path}'")

    overwrite_mode = False
    if txt_path.is_file() and txt_path.stat().st_size > 0:
        if args.auto:
            overwrite_mode = True
            logger.info(f"[{SCRIPT_NAME}] Modalità --auto: sovrascrittura '{txt_path}'.")
        else:
            logger.warning(f"[{SCRIPT_NAME}] File timestamp '{txt_path}' già esistente (voci={len(existing_timestamps)}).")
            print(f"\n{YELLOW}[!] ATTENZIONE: File '{txt_path.name}' già presente in 'OUT/' ({len(existing_timestamps)} timestamp).{RESET}")
            print("   1. Sovrascrivi e registra nuovi timestamp")
            print("   2. Salta (mantieni timestamp esistenti)")
            print("   3. Interrompi script")
            while True:
                sc = input("👉 Scelta [INVIO = 2 Salta]: ").strip()
                if sc in ("", "2"):
                    if existing_timestamps:
                        sync_generic_timestamps(txt_path, dirs["out"], logger, overwrite_generic=False)
                    logger.info(f"[{SCRIPT_NAME}] Completato (SKIP): mantenuto '{txt_path}'.")
                    return
                if sc == "1":
                    overwrite_mode = True
                    logger.info(f"[{SCRIPT_NAME}] Scelta utente: sovrascrittura '{txt_path}'.")
                    break
                if sc == "3":
                    print(f"{YELLOW}[!] [{SCRIPT_NAME}] Elaborazione interrotta dall'utente.{RESET}")
                    logger.info(f"[{SCRIPT_NAME}] Interrotto dall'utente su file esistente.")
                    return
                print(f"{RED}[!] Scelta non valida.{RESET}")

    default_video_name = f"{tipo_slug}_{fmt_slug}_merged_orig.mp4"
    video_path = resolve_merged_orig_path(dirs["out"], tipo_slug, fmt_slug, None)
    if not video_path:
        print(f"{RED}[X] [{SCRIPT_NAME}] Video non trovato in 'OUT/': {default_video_name}{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] Video non trovato in OUT (richiesto='{default_video_name}').")
        sys.exit(1)

    logger.info(f"[{SCRIPT_NAME}] Video sorgente='{video_path}' | velocità={speed:g}x")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"{RED}[X] [{SCRIPT_NAME}] Impossibile aprire il video: {video_path}{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] Impossibile aprire video '{video_path}' con OpenCV.")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 25.0
    total_frames = max(0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    total_sec = (total_frames / fps) if (fps > 0 and total_frames > 0) else 0.0
    logger.info(f"[{SCRIPT_NAME}] Metadati video: fps={fps:.2f} | frames={total_frames} | durata={total_sec:.2f}s")

    ret, current_frame = cap.read()
    if not ret or current_frame is None:
        cap.release()
        cv2.destroyAllWindows()
        print(f"{RED}[X] [{SCRIPT_NAME}] Impossibile leggere il primo fotogramma da '{video_path.name}'.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] Errore lettura primo fotogramma da '{video_path}'.")
        sys.exit(1)

    recorded_times: list[str] = []
    paused = False
    flash_timer = 0.0
    window_title = f"Player Analisi [{SCRIPT_NAME}] - {session_label} ({speed:g}x)"

    cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)
    display_frame = draw_overlay(current_frame.copy(), 0.0, total_sec, speed, paused, recorded_times, False, session_label)
    rendered_frame: object | None = display_frame
    needs_redraw = True

    cv2.imshow(window_title, display_frame)
    cv2.setWindowProperty(window_title, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    cv2.waitKey(50)
    porta_in_primo_piano(window_title)

    current_frame_idx = 0
    current_sec = 0.0
    anchor_wall = time.perf_counter()
    anchor_vid = 0.0
    frame_interval_sec = 1.0 / fps

    with tqdm(total=max(1, total_frames), desc="03 | Analisi Video", unit="frame", colour="blue", ncols=78) as pbar:
        try:
            while cap.isOpened():
                flash_active = time.time() < flash_timer

                if not paused:
                    now = time.perf_counter()
                    expected_sec = anchor_vid + (now - anchor_wall) * speed
                    target_idx = int(expected_sec * fps)

                    if total_frames > 0 and target_idx >= total_frames:
                        break

                    if target_idx > current_frame_idx:
                        frames_to_advance = target_idx - current_frame_idx
                        if frames_to_advance >= LARGE_SKIP_FRAMES_THRESHOLD:
                            ok_seek, frame, actual_idx, actual_sec = seek_to_seconds(
                                cap, expected_sec, fps, total_frames
                            )
                            if not ok_seek or frame is None:
                                break
                            current_frame = frame
                            current_frame_idx = actual_idx
                            current_sec = actual_sec
                            anchor_wall = time.perf_counter()
                            anchor_vid = current_sec
                        else:
                            grabbed_ok = True
                            for _ in range(frames_to_advance - 1):
                                if not cap.grab():
                                    grabbed_ok = False
                                    break
                            if not grabbed_ok or not cap.grab():
                                break
                            ret, frame = cap.retrieve()
                            if not ret or frame is None:
                                break
                            current_frame = frame
                            current_frame_idx = current_frame_idx + frames_to_advance
                            current_sec = current_frame_idx / fps
                        needs_redraw = True

                if total_frames > 0:
                    target_pbar = min(total_frames, current_frame_idx + 1)
                    if target_pbar > pbar.n and (target_pbar - pbar.n >= 15 or target_pbar >= total_frames):
                        pbar.update(target_pbar - pbar.n)

                if needs_redraw or flash_active or rendered_frame is None:
                    rendered_frame = draw_overlay(
                        current_frame.copy(),
                        current_sec,
                        total_sec,
                        speed,
                        paused,
                        recorded_times,
                        flash_active,
                        session_label,
                    )
                    needs_redraw = False

                cv2.imshow(window_title, rendered_frame)

                if paused:
                    wait_ms = 25
                else:
                    next_sec = (current_frame_idx + 1) * frame_interval_sec
                    time_until_next = (next_sec - anchor_vid) / speed - (time.perf_counter() - anchor_wall)
                    wait_ms = 1 if time_until_next <= 0.002 else max(1, min(10, int(time_until_next * 1000)))

                key = cv2.waitKey(wait_ms) & 0xFF
                if key in (ord("q"), ord("Q")):
                    logger.info(f"[{SCRIPT_NAME}] Uscita dal player tramite tasto Q al frame {current_frame_idx} ({seconds_to_time_str(current_sec)}).")
                    break
                elif key in (ord("p"), ord("P")):
                    paused = not paused
                    needs_redraw = True
                    if not paused:
                        anchor_wall = time.perf_counter()
                        anchor_vid = current_sec
                        porta_in_primo_piano(window_title)
                elif key in (ord("f"), ord("F")):
                    ok_seek, frame, actual_idx, actual_sec = seek_to_seconds(
                        cap, current_sec + 5.0, fps, total_frames
                    )
                    if ok_seek and frame is None:
                        break
                    if ok_seek and frame is not None:
                        current_frame = frame
                        current_frame_idx = actual_idx
                        current_sec = actual_sec
                        anchor_wall = time.perf_counter()
                        anchor_vid = current_sec
                        needs_redraw = True
                elif key in (ord("r"), ord("R")):
                    ok_seek, frame, actual_idx, actual_sec = seek_to_seconds(
                        cap, current_sec - 5.0, fps, total_frames
                    )
                    if ok_seek and frame is None:
                        break
                    if ok_seek and frame is not None:
                        current_frame = frame
                        current_frame_idx = actual_idx
                        current_sec = actual_sec
                        anchor_wall = time.perf_counter()
                        anchor_vid = current_sec
                        needs_redraw = True
                elif key == 32:
                    t_str = seconds_to_time_str(current_sec)
                    if t_str not in recorded_times:
                        recorded_times.append(t_str)
                        flash_timer = time.time() + 0.6
                        needs_redraw = True
                        pbar.set_postfix({"canestri": len(recorded_times)})
                        logger.info(f"[{SCRIPT_NAME}] Registrato timestamp: {t_str} (frame={current_frame_idx})")
        finally:
            cap.release()
            cv2.destroyAllWindows()

    if recorded_times:
        content = f"# Sessione: {session_label} | Script: {SCRIPT_NAME}\n" + "\n".join(recorded_times) + "\n"
        txt_path.write_text(content, encoding="utf-8")
        logger.info(f"[{SCRIPT_NAME}] Salvati {len(recorded_times)} timestamp in '{txt_path}': {recorded_times}")
        sync_generic_timestamps(txt_path, dirs["out"], logger, overwrite_generic=overwrite_mode)
    else:
        print(f"{YELLOW}[!] [{SCRIPT_NAME}] Nessun timestamp registrato (log: {log_file.name}).{RESET}", file=sys.stderr)
        logger.warning(f"[{SCRIPT_NAME}] Sessione terminata senza timestamp registrati.")


if __name__ == "__main__":
    main()