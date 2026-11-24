import os
import sys
import time
import ctypes
import shutil
import logging
import argparse
from datetime import datetime
from pathlib import Path
import cv2
from tqdm import tqdm

SCRIPT_NAME = "03_BAHS_identify-timestamps-ALL"
DEFAULT_SPEED = 1.0
ALLOWED_CHOICES = (2.0, 4.0)

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"


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
    return cleaned.strip("_") or "SESSIONE"


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


def leggi_timestamp_salvati(txt_path: Path) -> list[str]:
    if not txt_path.is_file():
        return []
    timestamps: list[str] = []
    for line in txt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for part in s.split(","):
            t = part.strip()
            if t:
                timestamps.append(t)
    return timestamps


def risolvi_file_timestamp(out_dir: Path, tipo_slug: str, fmt_slug: str) -> Path:
    return out_dir / f"{tipo_slug}_{fmt_slug}_timestamps.txt"


def risolvi_file_timestamp_generico(out_dir: Path) -> Path:
    return out_dir / "timestamps.txt"


def sincronizza_timestamp_generico(src_txt: Path, out_dir: Path, logger: logging.Logger) -> Path:
    dst_txt = risolvi_file_timestamp_generico(out_dir)
    if src_txt.resolve() == dst_txt.resolve():
        return dst_txt
    if dst_txt.is_file() and dst_txt.stat().st_size > 0 and leggi_timestamp_salvati(dst_txt):
        print(f"{GREEN}[✔] Mantenuto file timestamp esistente: '{dst_txt}'{RESET}")
        logger.info(f"[{SCRIPT_NAME}] Mantenuto file timestamp esistente '{dst_txt}'.")
        return dst_txt
    if src_txt.is_file() and src_txt.stat().st_size > 0:
        shutil.copy2(src_txt, dst_txt)
        print(f"{GREEN}[✔] Sincronizzato file timestamp generico per le clip: '{dst_txt}'{RESET}")
        logger.info(f"[{SCRIPT_NAME}] Copiato '{src_txt}' su '{dst_txt}'.")
    return dst_txt


def controlla_e_stampa_timestamp_out(out_dir: Path, txt_path: Path, logger: logging.Logger) -> list[str]:
    print(f"{CYAN}🔎 Controllo file e timestamp nella cartella '{out_dir.name}/'...{RESET}")
    if not txt_path.is_file():
        print(f"   {RED}[X] File timestamp '{txt_path.name}' NON PRESENTE nella cartella '{out_dir.name}/'.{RESET}")
        print(f"   {YELLOW}[!] Timestamp salvati: NESSUNO (file assente).{RESET}\n")
        logger.warning(f"[{SCRIPT_NAME}] File '{txt_path}' non presente nella cartella '{out_dir.name}/'.")
        return []

    saved_times = leggi_timestamp_salvati(txt_path)
    print(f"   {GREEN}[✔] File timestamp '{txt_path.name}' PRESENTE nella cartella '{out_dir.name}/'.{RESET}")
    if saved_times:
        print(f"   {GREEN}[✔] Timestamp salvati ({len(saved_times)}): {', '.join(saved_times)}{RESET}\n")
        logger.info(f"[{SCRIPT_NAME}] File '{txt_path}' presente con {len(saved_times)} timestamp: {saved_times}")
    else:
        print(f"   {YELLOW}[!] Nessun timestamp salvato all'interno di '{txt_path.name}' (file vuoto o senza voci valide).{RESET}\n")
        logger.warning(f"[{SCRIPT_NAME}] File '{txt_path}' presente ma senza timestamp salvati.")
    return saved_times


def risolvi_video_out(out_dir: Path, tipo_slug: str, fmt_slug: str, requested_name: str | None = None) -> Path | None:
    if requested_name:
        cand = out_dir / requested_name
        if cand.is_file():
            return cand
        return None
    primary = out_dir / f"{tipo_slug}_{fmt_slug}_merged_orig.mp4"
    if primary.is_file():
        return primary
    fallback = out_dir / f"merged_orig_{tipo_slug}_{fmt_slug}.mp4"
    if fallback.is_file():
        return fallback
    return None


def seconds_to_time_str(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    if mins >= 60:
        hours = mins // 60
        mins = mins % 60
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def chiedi_velocita(default_speed: float = DEFAULT_SPEED) -> float:
    while True:
        raw = input(f"🚀 Velocità riproduzione [INVIO = {default_speed:g}x | scelta: 2x, 4x]: ").strip()
        if not raw:
            return float(default_speed)
        norm = raw.lower().rstrip("x").strip().replace(",", ".")
        try:
            val = float(norm)
            if val in ALLOWED_CHOICES:
                return val
        except ValueError:
            pass
        print(f"{RED}[!] Scelta non valida. Premi INVIO per {default_speed:g}x oppure inserisci 2x o 4x.{RESET}")


def put_contrasted_text(frame, text: str, org: tuple[int, int], font_scale: float, color: tuple[int, int, int], thickness: int = 2):
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness, cv2.LINE_AA)


def draw_overlay(frame, current_sec: float, total_sec: float, speed: float, paused: bool, recorded_times: list[str], flash_timer: float, session_label: str):
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

    if time.time() < flash_timer:
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


def porta_in_primo_piano(window_title: str):
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, window_title)
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                user32.SetFocus(hwnd)
        except Exception:
            pass


def main():
    abilita_ansi()
    pulisci_console()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Identificazione interattiva timestamp canestri")
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

    default_session = f"{tipo_slug}_{fmt_slug}_{sanitize_slug(args.sottotitolo)}"

    print(f"{BOLD}{CYAN}{'=' * 80}{RESET}")
    print(f" {BOLD}{GREEN}INIZIO: {SCRIPT_NAME}{RESET} | Tipo: {BOLD}{args.tipo}{RESET} | Formato: {BOLD}{fmt_slug}{RESET}")
    print(f" {YELLOW}[CPU/RAM]{RESET} Thread: {res['threads']} | RAM disp.: {res['ram_avail']}/{res['ram_tot']} GB | Worker: {res['max_workers']}")
    print(f"{BOLD}{CYAN}{'=' * 80}{RESET}\n")
    logger.info(f"[{SCRIPT_NAME}] Avvio | tipo={args.tipo} | sottotitolo={args.sottotitolo} | formato={fmt_slug} | risorse={res}")

    generic_txt = risolvi_file_timestamp_generico(dirs["out"])
    if tipo_slug.upper() == "HIGHLIGHTS" and fmt_slug == "REEL" and generic_txt.is_file() and leggi_timestamp_salvati(generic_txt):
        txt_path = generic_txt
    else:
        txt_path = risolvi_file_timestamp(dirs["out"], tipo_slug, fmt_slug)

    existing_timestamps = controlla_e_stampa_timestamp_out(dirs["out"], txt_path, logger)

    session_raw = input(f"🏷️  Etichetta sessione [INVIO = {default_session}]: ").strip()
    session_label = session_raw if session_raw else default_session

    if txt_path.is_file() and txt_path.stat().st_size > 0:
        print(f"\n{YELLOW}[!] ATTENZIONE: Attività già svolta! File '{txt_path.name}' già presente in 'OUT/'.{RESET}")
        if existing_timestamps:
            print(f"{GREEN}    Timestamp attualmente salvati ({len(existing_timestamps)}): {', '.join(existing_timestamps)}{RESET}")
        else:
            print(f"{YELLOW}    Nessun timestamp valido attualmente salvato in '{txt_path.name}'.{RESET}")
        logger.warning(f"[{SCRIPT_NAME}] File '{txt_path}' già esistente.")
        print("   1. Sovrascrivi e registra nuovi timestamp")
        print("   2. Salta (mantieni timestamp esistenti)")
        print("   3. Interrompi script")
        while True:
            sc = input("👉 Scelta [INVIO = 2 Salta]: ").strip()
            if sc in ("", "2"):
                if existing_timestamps:
                    sincronizza_timestamp_generico(txt_path, dirs["out"], logger)
                    print(f"{GREEN}[✔] Mantenuto file timestamp esistente: '{txt_path}' | Timestamp: {', '.join(existing_timestamps)}{RESET}")
                else:
                    print(f"{YELLOW}[!] Mantenuto file timestamp esistente: '{txt_path}' (nessun timestamp salvato).{RESET}")
                return
            if sc == "1":
                break
            if sc == "3":
                print(f"{YELLOW}[!] Elaborazione interrotta dall'utente.{RESET}")
                return
            print(f"{RED}[!] Scelta non valida.{RESET}")

    default_video_name = f"{tipo_slug}_{fmt_slug}_merged_orig.mp4"
    video_name = input(f"📁 Nome video in 'OUT/' [INVIO = {default_video_name}]: ").strip()
    video_path = risolvi_video_out(dirs["out"], tipo_slug, fmt_slug, video_name or None)
    if not video_path:
        print(f"{RED}[X] Video non trovato in 'OUT/': {video_name or default_video_name}{RESET}")
        logger.error(f"[{SCRIPT_NAME}] Video non trovato in OUT.")
        sys.exit(1)

    speed = chiedi_velocita(default_speed=DEFAULT_SPEED)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        print(f"{RED}[X] Impossibile aprire il video: {video_path}{RESET}")
        logger.error(f"[{SCRIPT_NAME}] Impossibile aprire {video_path}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    total_sec = total_frames / fps if fps > 0 else 0.0

    print(f"\n{CYAN}🎮 CONTROLLI TASTIERA:{RESET}")
    print("   [BARRA SPAZIO] -> Registra canestro | [P] -> Pausa/Riprendi | [F/R] -> ±5s | [Q] -> Salva ed Esci")
    input(f"\n{BOLD}{GREEN}👉 Premi [INVIO] per aprire il video a schermo intero... {RESET}")

    ret, current_frame = cap.read()
    if not ret or current_frame is None:
        cap.release()
        cv2.destroyAllWindows()
        print(f"{RED}[X] Impossibile leggere il primo fotogramma.{RESET}")
        sys.exit(1)

    recorded_times: list[str] = []
    paused = False
    flash_timer = 0.0
    window_title = f"Player Analisi [{SCRIPT_NAME}] - {session_label} ({speed:g}x)"

    cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)
    display_frame = draw_overlay(current_frame.copy(), 0.0, total_sec, speed, paused, recorded_times, flash_timer, session_label)
    cv2.imshow(window_title, display_frame)
    cv2.setWindowProperty(window_title, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    cv2.waitKey(50)
    porta_in_primo_piano(window_title)

    current_frame_idx = 0
    current_sec = 0.0
    anchor_wall = time.perf_counter()
    anchor_vid = 0.0

    with tqdm(total=max(1, total_frames), desc="Analisi Video", unit="frame", colour="blue", ncols=78) as pbar:
        try:
            while cap.isOpened():
                if not paused:
                    now = time.perf_counter()
                    expected_sec = anchor_vid + (now - anchor_wall) * speed
                    target_idx = int(expected_sec * fps)

                    if total_frames > 0 and target_idx >= total_frames:
                        break

                    frames_to_advance = target_idx - current_frame_idx
                    if frames_to_advance > 0:
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
                        current_frame_idx = target_idx
                        current_sec = current_frame_idx / fps

                if total_frames > 0:
                    target_pbar = min(total_frames, current_frame_idx + 1)
                    if target_pbar > pbar.n:
                        pbar.update(target_pbar - pbar.n)

                display_frame = draw_overlay(current_frame.copy(), current_sec, total_sec, speed, paused, recorded_times, flash_timer, session_label)
                cv2.imshow(window_title, display_frame)

                if paused:
                    wait_ms = 30
                else:
                    next_sec = (current_frame_idx + 1) / fps
                    time_until_next = (next_sec - anchor_vid) / speed - (time.perf_counter() - anchor_wall)
                    wait_ms = max(10, int(time_until_next * 1000))

                key = cv2.waitKey(wait_ms) & 0xFF
                if key in (ord("q"), ord("Q")):
                    break
                elif key in (ord("p"), ord("P")):
                    paused = not paused
                    if not paused:
                        anchor_wall = time.perf_counter()
                        anchor_vid = current_sec
                        porta_in_primo_piano(window_title)
                elif key in (ord("f"), ord("F")):
                    target_sec = min(total_sec, current_sec + 5.0)
                    target_frame = min(max(0, total_frames - 1), max(0, int(round(target_sec * fps))))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                    ret, frame = cap.read()
                    if ret and frame is None:
                        continue
                    if ret and frame is not None:
                        current_frame = frame
                        current_frame_idx = target_frame
                        current_sec = current_frame_idx / fps
                        anchor_wall = time.perf_counter()
                        anchor_vid = current_sec
                elif key in (ord("r"), ord("R")):
                    target_sec = max(0.0, current_sec - 5.0)
                    target_frame = max(0, int(round(target_sec * fps)))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        current_frame = frame
                        current_frame_idx = target_frame
                        current_sec = current_frame_idx / fps
                        anchor_wall = time.perf_counter()
                        anchor_vid = current_sec
                elif key == 32:
                    t_str = seconds_to_time_str(current_sec)
                    if t_str not in recorded_times:
                        recorded_times.append(t_str)
                        flash_timer = time.time() + 0.6
                        pbar.set_postfix({"canestri": len(recorded_times)})
                        logger.info(f"[{SCRIPT_NAME}] Registrato timestamp: {t_str}")
        finally:
            cap.release()
            cv2.destroyAllWindows()

    if recorded_times:
        content = f"# Sessione: {session_label} | Script: {SCRIPT_NAME}\n" + "\n".join(recorded_times) + "\n"
        txt_path.write_text(content, encoding="utf-8")
        print(f"\n{GREEN}[✔] Salvati {len(recorded_times)} timestamp in '{txt_path}': {', '.join(recorded_times)}{RESET}")
        logger.info(f"[{SCRIPT_NAME}] Salvati {len(recorded_times)} timestamp in '{txt_path}': {recorded_times}")
        sincronizza_timestamp_generico(txt_path, dirs["out"], logger)
    else:
        print(f"\n{YELLOW}[!] Nessun timestamp registrato.{RESET}")
        logger.warning(f"[{SCRIPT_NAME}] Nessun timestamp registrato.")


if __name__ == "__main__":
    main()