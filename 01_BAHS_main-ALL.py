import os
import sys
import stat
import time
import shutil
import ctypes
import logging
import importlib
import subprocess
from importlib import metadata
from datetime import datetime
from pathlib import Path

SCRIPT_NAME = "01_BAHS_main-ALL"
BAR_WIDTH = 82
DIVIDER = "=" * BAR_WIDTH

REQUIRED_DIRS = ["IN", "OUT", "FINAL", "TMP", "LOG", "MUSIC", "EXTRA"]
PROTECTED_CLEAN_DIRS = {"IN", "EXTRA", "MUSIC"}
WORK_CLEAN_DIRS = ["OUT", "FINAL", "TMP"]
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm")
NO_MUSIC_TYPES = {"GAMEDAY", "FRIENDLY MATCH"}

PACKAGE_TO_IMPORT = {
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "opencv-contrib-python": "cv2",
    "tqdm": "tqdm",
    "psutil": "psutil",
    "numpy": "numpy",
    "pillow": "PIL",
}
CORE_IMPORTS = [("opencv-python", "cv2"), ("tqdm", "tqdm")]

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"


def abilita_ansi():
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            h = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h, mode.value | 0x0004)
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
            stat_mem = MEMORYSTATUSEX()
            stat_mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat_mem))
            return round(stat_mem.ullTotalPhys / (1024 ** 3), 1), round(stat_mem.ullAvailPhys / (1024 ** 3), 1)
        except Exception:
            return 8.0, 4.0

    meminfo_path = Path("/proc/meminfo")
    if meminfo_path.is_file():
        total_kb = 0
        avail_kb = 0
        for line in meminfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
        if total_kb > 0:
            return round(total_kb / (1024 ** 2), 1), round((avail_kb or total_kb // 2) / (1024 ** 2), 1)

    return 8.0, 4.0


def calcola_risorse_sistema() -> dict:
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


def setup_logger(log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{SCRIPT_NAME}_{ts}.log"

    logger = logging.getLogger(SCRIPT_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    return logger, log_file


def stampa_banner(res: dict):
    print(f"{BOLD}{CYAN}{DIVIDER}{RESET}")
    print(f" {BOLD}{GREEN}BAHS - BASKETBALL AUTOMATION HIGHLIGHTS SUITE (MAIN ORCHESTRATOR){RESET}")
    print(f" {YELLOW}[Caratteristiche Sistema]{RESET}")
    print(f"   • Thread logici    : {BOLD}{res['threads']}{RESET}")
    print(f"   • Core fisici stim.: {BOLD}{res['cores']}{RESET}")
    print(f"   • RAM disponibile  : {BOLD}{res['ram_avail']} / {res['ram_tot']} GB{RESET}")
    print(f"   • Worker paralleli : {BOLD}{res['max_workers']}{RESET}")
    print(f"   • Thread / Worker  : {BOLD}{res['threads_per_worker']}{RESET}")
    print(f"   • Preset codifica  : {BOLD}{res['preset']}{RESET}")
    print(f"{BOLD}{CYAN}{DIVIDER}{RESET}\n")


def verifica_e_crea_cartelle(base_dir: Path, logger: logging.Logger):
    print(f"{BOLD}{CYAN}[1/4] Verifica e creazione cartelle di lavoro...{RESET}")
    created_cnt = 0
    for d in REQUIRED_DIRS:
        p = base_dir / d
        existed = p.exists()
        p.mkdir(parents=True, exist_ok=True)
        if not existed:
            created_cnt += 1
        logger.info(f"[{SCRIPT_NAME}] Cartella '{d}': {'esistente' if existed else 'creata'} ({p})")
    print(f"   {GREEN}[✔] Cartelle verificate ({len(REQUIRED_DIRS)} totali, {created_cnt} create).{RESET}")
    print(f"{DIM}   Cartelle protette dalla pulizia: {', '.join(sorted(PROTECTED_CLEAN_DIRS))}{RESET}\n")


def lista_video_in(base_dir: Path) -> list[Path]:
    in_dir = base_dir / "IN"
    if not in_dir.is_dir():
        return []
    return sorted([f for f in in_dir.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS])


def attendi_file_in_cartella_in(base_dir: Path, logger: logging.Logger) -> list[Path]:
    avvisato_log = False
    while True:
        videos = lista_video_in(base_dir)
        if videos:
            print(f"   {GREEN}[✔] Trovati {len(videos)} file video validi nella cartella 'IN/'.{RESET}\n")
            logger.info(f"[{SCRIPT_NAME}] File video presenti in IN ({len(videos)}): {[v.name for v in videos]}")
            return videos

        if not avvisato_log:
            logger.warning(f"[{SCRIPT_NAME}] Cartella 'IN/' priva di file video supportati. In attesa inserimento utente.")
            avvisato_log = True

        print(f"\n   {RED}{BOLD}[!] ATTENZIONE: Nessun file video trovato nella cartella 'IN/'.{RESET}")
        print(f"   {YELLOW}👉 Inserisci almeno un file video ({', '.join(VIDEO_EXTS)}) nella cartella 'IN/' prima di procedere.{RESET}")
        scelta = input(f"\n   ⏳ Inserisci i video in 'IN/' e premi [INVIO] per ricontrollare (oppure '0' per uscire): ").strip().lower()
        if scelta in ("0", "q", "quit", "exit"):
            print(f"   {YELLOW}[!] Uscita dallo script richiesta dall'utente.{RESET}")
            logger.info(f"[{SCRIPT_NAME}] Uscita dallo script durante attesa file in IN/.")
            sys.exit(0)


def _estrai_nome_pacchetto(riga: str) -> str:
    s = riga.split("#", 1)[0].strip()
    if not s or s.startswith("-") or s.startswith(("http://", "https://", "git+")):
        return ""
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", ";", "["):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    return s


def _controlla_stato_ambiente(base_dir: Path) -> tuple[bool, list[str], list[str], bool]:
    ffmpeg_ok = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    req_file = base_dir / "requirements.txt"
    missing_core: list[str] = []
    missing_req: list[str] = []

    for pkg_name, mod_name in CORE_IMPORTS:
        try:
            importlib.import_module(mod_name)
        except Exception:
            missing_core.append(pkg_name)

    if req_file.is_file():
        for line in req_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            pkg = _estrai_nome_pacchetto(line)
            if not pkg:
                continue
            installed = False
            try:
                metadata.version(pkg)
                installed = True
            except metadata.PackageNotFoundError:
                mod = PACKAGE_TO_IMPORT.get(pkg.lower())
                if mod:
                    try:
                        importlib.import_module(mod)
                        installed = True
                    except Exception:
                        installed = False
            if not installed and pkg not in missing_req:
                missing_req.append(pkg)

    return ffmpeg_ok, missing_core, missing_req, req_file.is_file()


def verifica_ffmpeg_e_requirements(base_dir: Path, logger: logging.Logger) -> bool:
    print(f"{BOLD}{CYAN}[2/4] Controllo FFmpeg/FFprobe e requirements...{RESET}")
    req_file = base_dir / "requirements.txt"

    while True:
        ffmpeg_ok, missing_core, missing_req, has_req_file = _controlla_stato_ambiente(base_dir)
        all_missing_pkgs = list(dict.fromkeys(missing_core + missing_req))

        if ffmpeg_ok and not all_missing_pkgs:
            print(f"   {GREEN}[✔] FFmpeg e FFprobe disponibili.{RESET}")
            if has_req_file:
                print(f"   {GREEN}[✔] Tutti i requirements in 'requirements.txt' sono installati.{RESET}")
            else:
                print(f"   {GREEN}[✔] Pacchetti Python principali verificati.{RESET}")
            logger.info(f"[{SCRIPT_NAME}] Controllo FFmpeg/FFprobe e requirements superato (has_req_file={has_req_file}).")
            return True

        print(f"\n   {RED}{BOLD}[X] Controllo ambiente non superato!{RESET}")
        if not ffmpeg_ok:
            print(f"   {RED}   • FFmpeg o FFprobe mancanti nel PATH di sistema.{RESET}")
            logger.error(f"[{SCRIPT_NAME}] FFmpeg o FFprobe mancanti nel PATH.")

        if all_missing_pkgs:
            print(f"   {RED}   • Requirements mancanti: {', '.join(all_missing_pkgs)}{RESET}")
            if has_req_file:
                print(f"   {YELLOW}     Installa i requirements eseguendo: pip install -r \"{req_file.name}\"{RESET}")
            else:
                print(f"   {YELLOW}     Installa i pacchetti mancanti eseguendo: pip install {' '.join(all_missing_pkgs)}{RESET}")
            logger.error(f"[{SCRIPT_NAME}] Requirements mancanti: {all_missing_pkgs}")

        print("\n   Cosa vuoi fare?")
        print("     1. Ho installato quanto richiesto -> Ricontrolla ora")
        print("     0. Esci dallo script")
        while True:
            sc = input("👉 Scelta [INVIO = 1 Ricontrolla | 0 = Esci]: ").strip().lower()
            if sc in ("", "1"):
                break
            if sc in ("0", "q", "quit", "exit"):
                print(f"   {YELLOW}[!] Uscita dallo script.{RESET}")
                logger.info(f"[{SCRIPT_NAME}] Uscita dallo script per dipendenze/requirements mancanti.")
                return False
            print(f"   {RED}[!] Scelta non valida. Inserisci 1 oppure 0.{RESET}")


def _rimuovi_elemento_forzato(percorso: Path):
    def _handle_remove_readonly(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            raise exc_info[1]

    if percorso.is_symlink() or percorso.is_file():
        try:
            percorso.unlink()
        except PermissionError:
            os.chmod(percorso, stat.S_IWRITE)
            percorso.unlink()
    elif percorso.is_dir():
        shutil.rmtree(percorso, onerror=_handle_remove_readonly)


def chiedi_pulizia_cartelle(base_dir: Path, log_file_attuale: Path, logger: logging.Logger):
    print(f"{BOLD}{CYAN}[3/4] Pulizia cartelle di lavoro (IN, EXTRA e MUSIC sono SEMPRE protette)...{RESET}")
    print(f"   1. Ripulire ({', '.join(WORK_CLEAN_DIRS)} e vecchi log in 'LOG/')")
    print(f"   2. Non ripulire {DIM}[Default]{RESET}")
    print(f"   0. Esci dallo script")

    while True:
        scelta = input(f"\n👉 Scelta pulizia (1-2, 0 per uscire) [INVIO = 2 Non ripulire]: ").strip().lower()
        if scelta in ("0", "q", "quit", "exit"):
            print(f"   {YELLOW}[!] Uscita dallo script richiesta dall'utente.{RESET}")
            logger.info(f"[{SCRIPT_NAME}] Uscita dallo script durante scelta pulizia cartelle.")
            sys.exit(0)
        if scelta in ("", "2"):
            print(f"   {DIM}Nessuna pulizia effettuata.{RESET}\n")
            logger.info(f"[{SCRIPT_NAME}] Pulizia cartelle saltata dall'utente.")
            return
        if scelta == "1":
            break
        print(f"   {RED}[!] Scelta non valida. Inserisci 1 (Ripulire), 2 (Non ripulire) o 0 per uscire.{RESET}")

    for d_name in WORK_CLEAN_DIRS:
        if d_name.upper() in PROTECTED_CLEAN_DIRS:
            continue
        target_p = base_dir / d_name
        if not target_p.exists():
            print(f"   {DIM}Cartella '{d_name}/' non presente: nulla da ripulire.{RESET}")
            logger.info(f"[{SCRIPT_NAME}] Cartella '{d_name}/' non esistente durante pulizia.")
            continue
        items = list(target_p.iterdir())
        if not items:
            print(f"   {GREEN}[✔] Cartella '{d_name}/' già vuota (0 elementi da rimuovere).{RESET}")
            logger.info(f"[{SCRIPT_NAME}] Cartella '{d_name}/' già vuota.")
            continue
        non_rimossi: list[str] = []
        for item in items:
            try:
                _rimuovi_elemento_forzato(item)
            except Exception as e:
                non_rimossi.append(item.name)
                logger.warning(f"[{SCRIPT_NAME}] Impossibile rimuovere '{item.name}' in '{d_name}/': {e}")

        residui = [p.name for p in target_p.iterdir()] if target_p.exists() else []
        if not residui:
            print(f"   {GREEN}[✔] Cartella '{d_name}/' completamente ripulita ({len(items)} elementi).{RESET}")
            logger.info(f"[{SCRIPT_NAME}] Cartella '{d_name}/' completamente svuotata ({len(items)} elementi).")
        else:
            print(f"   {YELLOW}[!] ATTENZIONE: Residui in '{d_name}/' ({len(residui)}): {', '.join(residui)} - Chiudi eventuali programmi/player che li stanno usando e riprova se necessario.{RESET}")
            logger.warning(f"[{SCRIPT_NAME}] Residui in '{d_name}/': {residui} (errori su: {non_rimossi})")

    log_dir = base_dir / "LOG"
    old_logs = [f for f in log_dir.iterdir() if f.is_file() and f.resolve() != log_file_attuale.resolve()]
    if not old_logs:
        print(f"   {GREEN}[✔] Nessun vecchio log da rimuovere in 'LOG/' (log sessione corrente preservato).{RESET}")
    else:
        for lf in old_logs:
            try:
                _rimuovi_elemento_forzato(lf)
            except Exception as e:
                logger.warning(f"[{SCRIPT_NAME}] Impossibile rimuovere vecchio log '{lf.name}': {e}")
        print(f"   {GREEN}[✔] Vecchi log rimossi in 'LOG/' ({len(old_logs)} file, log sessione corrente preservato).{RESET}")
    logger.info(f"[{SCRIPT_NAME}] Rimossi {len(old_logs)} vecchi file di log in LOG/.")
    print()


def chiedi_parametri_incontro() -> tuple[str, str, str]:
    print(f"{BOLD}{CYAN}[4/4] Configurazione incontro e formato di uscita...{RESET}")
    print("   Tipo incontro (1ª riga sovraimpressione):")
    print("     1. HIGHLIGHTS")
    print("     2. GAMEDAY")
    print("     3. FRIENDLY MATCH")
    print("     4. CUSTOM (inserimento dinamico)")
    print("     0. Esci dallo script")

    tipo_incontro = "HIGHLIGHTS"
    while True:
        raw_t = input("\n👉 Seleziona tipo incontro (1-4, 0 per uscire) [INVIO = 1 HIGHLIGHTS]: ").strip().lower()
        if raw_t in ("", "1"):
            tipo_incontro = "HIGHLIGHTS"
            break
        if raw_t == "2":
            tipo_incontro = "GAMEDAY"
            break
        if raw_t == "3":
            tipo_incontro = "FRIENDLY MATCH"
            break
        if raw_t == "4":
            custom_val = input("✍️  Inserisci il testo personalizzato per la 1ª riga: ").strip()
            if custom_val:
                tipo_incontro = custom_val
                break
            print(f"   {RED}[!] Il testo CUSTOM non può essere vuoto.{RESET}")
        elif raw_t in ("0", "q", "quit", "exit"):
            print(f"   {YELLOW}[!] Uscita dallo script richiesta dall'utente.{RESET}")
            sys.exit(0)
        else:
            print(f"   {RED}[!] Inserisci un numero da 1 a 4, 0 per uscire oppure premi INVIO.{RESET}")

    default_sub = "CAMPIONATO - TEAM vs TEAM"
    raw_sub = input(f"✍️  Seconda riga overlay [{default_sub}] [INVIO = default]: ").strip()
    riga_seconda = raw_sub if raw_sub else default_sub

    if tipo_incontro.upper() == "HIGHLIGHTS":
        print("\n   Formato video per HIGHLIGHTS:")
        print("     1. 1080P (1920x1080 orizzontale) [Default]")
        print("     2. REEL  (1080x1920 verticale 9:16)")
        print("     0. Esci dallo script")
        while True:
            raw_f = input("👉 Scegli formato (1 o 2, 0 per uscire) [INVIO = 1 1080P]: ").strip().lower()
            if raw_f in ("", "1", "1080p", "1080"):
                formato = "1080P"
                break
            if raw_f in ("2", "reel", "reels"):
                formato = "REEL"
                break
            if raw_f in ("0", "q", "quit", "exit"):
                print(f"   {YELLOW}[!] Uscita dallo script richiesta dall'utente.{RESET}")
                sys.exit(0)
            print(f"   {RED}[!] Inserisci 1 (1080P), 2 (REEL) o 0 per uscire.{RESET}")
    else:
        formato = "1080P"
        print(f"   {DIM}Formato impostato automaticamente su 1080P per tipo '{tipo_incontro}'.{RESET}")

    return tipo_incontro, riga_seconda, formato


def ha_timestamp_validi(txt_path: Path) -> bool:
    if not txt_path.is_file() or txt_path.stat().st_size == 0:
        return False
    for line in txt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for part in s.split(","):
            if part.strip():
                return True
    return False


def costruisci_pipeline(base_dir: Path, tipo_incontro: str, formato: str, logger: logging.Logger | None = None) -> list[dict]:
    tipo_upper = tipo_incontro.strip().upper()
    fmt_upper = formato.strip().upper()

    if tipo_upper != "HIGHLIGHTS":
        steps = [
            {"script": "02_BAHS_merge-originals-ALL.py", "label": "Merge originals (AUTO)", "interactive": False},
        ]
        if tipo_upper not in NO_MUSIC_TYPES:
            steps.append({"script": "07_BAHS_add-music-overlay-ALL.py", "label": "Add music overlay (AUTO)", "interactive": False})
        steps.extend([
            {"script": "08_BAHS_add-text-overlay-ALL.py", "label": f"Add text overlay {fmt_upper} (AUTO)", "interactive": False},
            {"script": "10_BAHS_add-logo-overlay-ALL.py", "label": f"Add logo overlay {fmt_upper} (AUTO)", "interactive": False},
        ])
        for idx, s in enumerate(steps, start=1):
            s["step"] = idx
        return steps

    timestamps_file = base_dir / "OUT" / "timestamps.txt"
    skip_timestamps = ha_timestamp_validi(timestamps_file)
    if skip_timestamps and logger:
        logger.info(f"[{SCRIPT_NAME}] Trovato '{timestamps_file}' valido per HIGHLIGHTS ({fmt_upper}): step 03_BAHS_identify-timestamps-ALL.py saltato.")

    if fmt_upper == "REEL":
        raw_steps = [
            {"script": "02_BAHS_merge-originals-ALL.py", "label": "Merge originals (AUTO)", "interactive": False},
        ]
        if not skip_timestamps:
            raw_steps.append({"script": "03_BAHS_identify-timestamps-ALL.py", "label": "Identify timestamps (INPUT UTENTE)", "interactive": True})
        raw_steps.extend([
            {"script": "04_BAHS_split-original-ALL.py", "label": "Split originals (AUTO)", "interactive": False},
            {"script": "05_BAHS_convert-clips-REEL.py", "label": "Convert clips to REEL (AUTO)", "interactive": False},
            {"script": "06_BAHS_merge-clips-ALL.py", "label": "Merge clips REEL (AUTO)", "interactive": False},
            {"script": "07_BAHS_add-music-overlay-ALL.py", "label": "Add music overlay (AUTO)", "interactive": False},
            {"script": "08_BAHS_add-text-overlay-ALL.py", "label": "Add text overlay REEL (AUTO)", "interactive": False},
            {"script": "10_BAHS_add-logo-overlay-ALL.py", "label": "Add logo overlay REEL (AUTO)", "interactive": False},
        ])
        for idx, s in enumerate(raw_steps, start=1):
            s["step"] = idx
        return raw_steps

    raw_steps = [
        {"script": "02_BAHS_merge-originals-ALL.py", "label": "Merge originals (AUTO)", "interactive": False},
    ]
    if not skip_timestamps:
        raw_steps.append({"script": "03_BAHS_identify-timestamps-ALL.py", "label": "Identify timestamps (INPUT UTENTE)", "interactive": True})
    raw_steps.extend([
        {"script": "04_BAHS_split-original-ALL.py", "label": "Split originals (AUTO)", "interactive": False},
        {"script": "06_BAHS_merge-clips-ALL.py", "label": "Merge clips 1080P (AUTO)", "interactive": False},
        {"script": "07_BAHS_add-music-overlay-ALL.py", "label": "Add music overlay (AUTO)", "interactive": False},
        {"script": "08_BAHS_add-text-overlay-ALL.py", "label": "Add text overlay 1080P (AUTO)", "interactive": False},
        {"script": "10_BAHS_add-logo-overlay-ALL.py", "label": "Add logo overlay 1080P (AUTO)", "interactive": False},
    ])
    for idx, s in enumerate(raw_steps, start=1):
        s["step"] = idx
    return raw_steps


def trova_output_logo_finale(base_dir: Path, tipo_incontro: str, formato: str) -> Path | None:
    tipo_slug = sanitize_slug(tipo_incontro)
    fmt_slug = formato.strip().upper()
    final_dir = base_dir / "FINAL"
    if not final_dir.is_dir():
        return None
    matches = sorted(
        [p for p in final_dir.glob(f"{tipo_slug}_{fmt_slug}_*logo.mp4") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def stampa_intestazione_step(step_num: int, total_steps: int, step_info: dict):
    banner = (
        f"\n{CYAN}{DIVIDER}\n"
        f"{BOLD}▶ [STEP {step_num}/{total_steps} IN ESECUZIONE]{RESET}{CYAN} {step_info['label']}\n"
        f"  📄 Modulo: {BOLD}{step_info['script']}{RESET}\n"
        f"{CYAN}{DIVIDER}{RESET}"
    )
    print(banner, flush=True)


def esegui_script_diretto(base_dir: Path, script_name: str, tipo_incontro: str, riga_seconda: str, formato: str, interactive: bool, logger: logging.Logger) -> tuple[bool, float]:
    script_path = base_dir / script_name
    if not script_path.is_file():
        print(f"   {RED}[X] Modulo mancante: {script_path.name}{RESET}", flush=True)
        logger.error(f"[{SCRIPT_NAME}] Modulo mancante: {script_path}")
        return False, 0.0

    cmd = [
        sys.executable,
        str(script_path),
        "--base-dir", str(base_dir),
        "--tipo", tipo_incontro,
        "--sottotitolo", riga_seconda,
        "--formato", formato,
    ]
    if not interactive:
        cmd.append("--auto")

    t0 = time.perf_counter()
    logger.info(f"[{SCRIPT_NAME}] Avvio script '{script_name}' (interactive={interactive}) -> cmd={cmd}")
    proc = subprocess.run(cmd, cwd=str(base_dir))
    elapsed_step = time.perf_counter() - t0
    if proc.returncode == 0:
        logger.info(f"[{SCRIPT_NAME}] Script '{script_name}' completato con successo (rc=0, tempo={elapsed_step:.2f}s).")
        return True, elapsed_step

    logger.error(f"[{SCRIPT_NAME}] Script '{script_name}' terminato con errore (rc={proc.returncode}, tempo={elapsed_step:.2f}s). Verifica il log dedicato in LOG/.")
    return False, elapsed_step


def esegui_step(base_dir: Path, step_info: dict, step_num: int, total_steps: int, tipo_incontro: str, riga_seconda: str, formato: str, logger: logging.Logger) -> tuple[bool, float]:
    script_name = step_info["script"]
    stampa_intestazione_step(step_num, total_steps, step_info)
    ok, elapsed = esegui_script_diretto(
        base_dir,
        script_name,
        tipo_incontro,
        riga_seconda,
        formato,
        step_info["interactive"],
        logger,
    )

    if not ok and script_name == "04_BAHS_split-original-ALL.py":
        while not ok:
            logger.warning(f"[{SCRIPT_NAME}] Fallback interattivo attivato per errore su '{script_name}': richiesto recupero timestamp con 03_BAHS_identify-timestamps-ALL.py.")
            print(f"\n{YELLOW}[!] Riavvio l'attività di analisi del video.{RESET}\n", flush=True)
            print("1. Crea/aggiorna i timestamp ora e riprova split original", flush=True)
            print("0. Esci dallo script", flush=True)
            while True:
                sc = input("\n👉 Scelta [INVIO = 1 Avanti | 0 = Esci]: ").strip().lower()
                if sc in ("", "1"):
                    break
                if sc in ("0", "q", "quit", "exit"):
                    print(f"   {YELLOW}[!] Uscita dallo script richiesta dall'utente.{RESET}", flush=True)
                    logger.info(f"[{SCRIPT_NAME}] Uscita dallo script durante recupero timestamp per 04_BAHS_split-original-ALL.py.")
                    return False, elapsed
                print(f"   {RED}[!] Scelta non valida. Inserisci 1 oppure 0.{RESET}", flush=True)

            esegui_script_diretto(
                base_dir,
                "03_BAHS_identify-timestamps-ALL.py",
                tipo_incontro,
                riga_seconda,
                formato,
                True,
                logger,
            )
            print(f"\n   {BLUE}↻ Nuovo tentativo: {script_name}...{RESET}", flush=True)
            ok, extra_elapsed = esegui_script_diretto(
                base_dir,
                script_name,
                tipo_incontro,
                riga_seconda,
                formato,
                step_info["interactive"],
                logger,
            )
            elapsed += extra_elapsed

    return ok, elapsed


def main():
    abilita_ansi()
    pulisci_console()

    base_dir = Path(__file__).resolve().parent
    res = calcola_risorse_sistema()
    logger, log_file = setup_logger(base_dir / "LOG")
    logger.info(f"[{SCRIPT_NAME}] Avvio orchestratore | base_dir='{base_dir}' | risorse={res}")

    stampa_banner(res)
    verifica_e_crea_cartelle(base_dir, logger)
    if not verifica_ffmpeg_e_requirements(base_dir, logger):
        print(f"{RED}Impossibile proseguire senza FFmpeg/FFprobe o requirements.{RESET}")
        return

    attendi_file_in_cartella_in(base_dir, logger)
    chiedi_pulizia_cartelle(base_dir, log_file, logger)
    tipo_incontro, riga_seconda, formato = chiedi_parametri_incontro()

    pipeline = costruisci_pipeline(base_dir, tipo_incontro, formato, logger)
    total_steps = len(pipeline)
    print(f"\n{BOLD}{MAGENTA}{DIVIDER}{RESET}")
    print(f" {BOLD}{MAGENTA}RIEPILOGO PIANO DI LAVORO{RESET}")
    print(f"  Tipo incontro : {BOLD}{tipo_incontro}{RESET}")
    print(f"  Seconda riga  : {BOLD}{riga_seconda}{RESET}")
    print(f"  Formato target: {BOLD}{formato}{RESET}")
    if tipo_incontro.strip().upper() == "HIGHLIGHTS" and ha_timestamp_validi(base_dir / "OUT" / "timestamps.txt"):
        print(f"  Timestamp {formato}: {GREEN}rilevato 'OUT/timestamps.txt' (step 03 saltato){RESET}")
    print(f"  Step previsti : {total_steps}")
    print(f"{BOLD}{MAGENTA}{DIVIDER}{RESET}\n")
    logger.info(
        f"[{SCRIPT_NAME}] Piano avviato -> tipo='{tipo_incontro}', sottotitolo='{riga_seconda}', "
        f"formato='{formato}', steps={[s['script'] for s in pipeline]}"
    )

    t_start = time.perf_counter()

    for idx, item in enumerate(pipeline, start=1):
        ok, step_elapsed = esegui_step(
            base_dir,
            item,
            idx,
            total_steps,
            tipo_incontro,
            riga_seconda,
            formato,
            logger,
        )
        if not ok:
            print(
                f"\n{RED}[X] Pipeline interrotta allo Step {idx}/{total_steps} ({item['script']}). "
                f"Controlla il file di log '{log_file.name}' nella cartella 'LOG/'.{RESET}",
                flush=True,
            )
            logger.error(f"[{SCRIPT_NAME}] Pipeline interrotta allo Step {idx}/{total_steps} ({item['script']}).")
            return

        print(
            f"{GREEN}✔ [STEP {idx}/{total_steps} COMPLETATO] {item['script']} ({step_elapsed:.1f}s){RESET}",
            flush=True,
        )

    elapsed = time.perf_counter() - t_start
    final_logo_file = trova_output_logo_finale(base_dir, tipo_incontro, formato)
    default_final_name = f"{sanitize_slug(tipo_incontro)}_{formato.strip().upper()}_with_fade_music_text_logo.mp4"
    video_finale_nome = final_logo_file.name if final_logo_file else default_final_name

    print(f"\n{BOLD}{GREEN}{DIVIDER}{RESET}")
    print(f" {BOLD}{GREEN}[✔] PIPELINE COMPLETATA IN {elapsed:.1f}s!{RESET}")
    print(f"- Video Finale        : '{video_finale_nome}'")
    print(f"- Cartella finale     : 'FINAL/'")
    print(f"- Cartella intermedia : 'OUT/'")
    print(f"- Cartella log        : 'LOG/'")
    print(f"{BOLD}{GREEN}{DIVIDER}{RESET}\n")
    logger.info(f"[{SCRIPT_NAME}] Pipeline completata in {elapsed:.2f}s. Video Finale: {final_logo_file or video_finale_nome}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[!] Esecuzione interrotta dall'utente (Ctrl+C).{RESET}")