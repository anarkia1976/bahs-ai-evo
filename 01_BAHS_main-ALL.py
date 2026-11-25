import os
import sys
import stat
import time
import shutil
import logging
import importlib
import subprocess
from importlib import metadata
from pathlib import Path

from bahs_core import (
    BLUE,
    BOLD,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    NO_MUSIC_TYPES,
    RED,
    REQUIRED_DIRS,
    RESET,
    VIDEO_EXTS,
    YELLOW,
    clear_console,
    compute_system_resources,
    enable_ansi_console,
    ensure_project_dirs,
    has_valid_timestamps,
    normalize_format,
    sanitize_slug,
    setup_step_logger,
)

SCRIPT_NAME = "01_BAHS_main-ALL"
BAR_WIDTH = 82
DIVIDER = "=" * BAR_WIDTH

PROTECTED_CLEAN_DIRS = {"IN", "EXTRA", "MUSIC"}
WORK_CLEAN_DIRS = ["OUT", "FINAL", "TMP"]

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


def stampa_banner(res: dict) -> None:
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


def verifica_e_crea_cartelle(base_dir: Path, logger: logging.Logger) -> None:
    print(f"{BOLD}{CYAN}[1/4] Verifica e creazione cartelle di lavoro...{RESET}")
    existed_before = {d: (base_dir / d).exists() for d in REQUIRED_DIRS}
    dirs = ensure_project_dirs(base_dir)
    created_cnt = sum(1 for d in REQUIRED_DIRS if not existed_before[d])
    for d in REQUIRED_DIRS:
        stato = "esistente" if existed_before[d] else "creata"
        logger.info(f"[{SCRIPT_NAME}] Cartella '{d}': {stato} ({dirs[d.lower()]})")
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


def _rimuovi_elemento_forzato(percorso: Path) -> None:
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


def chiedi_pulizia_cartelle(base_dir: Path, log_file_attuale: Path, logger: logging.Logger) -> None:
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


def costruisci_pipeline(base_dir: Path, tipo_incontro: str, formato: str, logger: logging.Logger | None = None) -> list[dict]:
    tipo_upper = tipo_incontro.strip().upper()
    fmt_upper = normalize_format(formato)

    if tipo_upper != "HIGHLIGHTS":
        steps = [
            {"script": "02_BAHS_merge-originals-ALL.py", "label": "Merge originals (AUTO)", "interactive": False},
        ]
        if tipo_upper not in NO_MUSIC_TYPES:
            steps.append({"script": "07_BAHS_add-music-overlay-ALL.py", "label": "Add music overlay (AUTO)", "interactive": False})
        steps.append(
            {"script": "11_BAHS_add-text-logo-overlay-ALL.py", "label": f"Add text & logo overlay {fmt_upper} (AUTO)", "interactive": False}
        )
        for idx, s in enumerate(steps, start=1):
            s["step"] = idx
        return steps

    timestamps_file = base_dir / "OUT" / "timestamps.txt"
    skip_timestamps = has_valid_timestamps(timestamps_file)
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
            {"script": "11_BAHS_add-text-logo-overlay-ALL.py", "label": "Add text & logo overlay REEL (AUTO)", "interactive": False},
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
        {"script": "11_BAHS_add-text-logo-overlay-ALL.py", "label": "Add text & logo overlay 1080P (AUTO)", "interactive": False},
    ])
    for idx, s in enumerate(raw_steps, start=1):
        s["step"] = idx
    return raw_steps


def trova_output_logo_finale(base_dir: Path, tipo_incontro: str, formato: str) -> Path | None:
    tipo_slug = sanitize_slug(tipo_incontro)
    fmt_slug = normalize_format(formato)
    final_dir = base_dir / "FINAL"
    if not final_dir.is_dir():
        return None
    matches = sorted(
        [p for p in final_dir.glob(f"{tipo_slug}_{fmt_slug}_*logo.mp4") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def stampa_intestazione_step(step_num: int, total_steps: int, step_info: dict) -> None:
    banner = (
        f"\n{CYAN}{DIVIDER}\n"
        f"{BOLD}▶ [STEP {step_num}/{total_steps} IN ESECUZIONE]{RESET}{CYAN} {step_info['label']}\n"
        f"  📄 Modulo: {BOLD}{step_info['script']}{RESET}\n"
        f"{CYAN}{DIVIDER}{RESET}"
    )
    print(banner, flush=True)


def esegui_script_diretto(
    base_dir: Path,
    script_name: str,
    tipo_incontro: str,
    riga_seconda: str,
    formato: str,
    interactive: bool,
    logger: logging.Logger,
) -> tuple[bool, float]:
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


def esegui_step(
    base_dir: Path,
    step_info: dict,
    step_num: int,
    total_steps: int,
    tipo_incontro: str,
    riga_seconda: str,
    formato: str,
    logger: logging.Logger,
) -> tuple[bool, float]:
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


def main() -> None:
    enable_ansi_console()
    clear_console()

    base_dir = Path(__file__).resolve().parent
    res = compute_system_resources()
    dirs = ensure_project_dirs(base_dir)
    logger, log_file = setup_step_logger(dirs["log"], SCRIPT_NAME)
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
    if tipo_incontro.strip().upper() == "HIGHLIGHTS" and has_valid_timestamps(base_dir / "OUT" / "timestamps.txt"):
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
    default_final_name = f"{sanitize_slug(tipo_incontro)}_{normalize_format(formato)}_with_fade_music_text_logo.mp4"
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