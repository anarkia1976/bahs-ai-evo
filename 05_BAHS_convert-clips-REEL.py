import os
import re
import sys
import ctypes
import shutil
import logging
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
import cv2
from tqdm import tqdm
from ultralytics import YOLO

SCRIPT_NAME = "05_BAHS_convert-clips-REEL"
COCO_SPORTS_BALL_CLASS = 32
BALL_KEYWORDS = ("sports ball", "basketball", "ball")
MODEL_EXTS = {".pt", ".onnx", ".engine", ".tflite", ".pb"}
UNIFORM_CLIP_RE = re.compile(r"^.+_(?:1080P|REEL)_clip_\d{2}_(?:\d{1,2}-)?\d{1,2}-\d{2}\.mp4$", re.IGNORECASE)
LEGACY_CLIP_RE = re.compile(r"^canestro_\d{2}_(?:\d{1,2}-)?\d{1,2}-\d{2}(?:_[A-Za-z0-9_-]+)?\.mp4$", re.IGNORECASE)
TIMESTAMP_SUFFIX_RE = re.compile(r"_(?:\d{1,2}-)?\d{1,2}-\d{2}(?:_[A-Za-z0-9_-]+)?$", re.IGNORECASE)

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
        imgsz_default = 640
        preset = "veryfast"
    elif ram_avail < 12.0:
        imgsz_default = 960
        preset = "fast"
    else:
        imgsz_default = None
        preset = "fast"

    return {
        "threads": threads,
        "cores": cores,
        "ram_tot": ram_tot,
        "ram_avail": ram_avail,
        "imgsz_default": imgsz_default,
        "preset": preset,
    }


def sanitize_slug(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in text.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_") or "INCONTRO"


def setup_dirs_and_logger(base_dir: Path) -> tuple[dict[str, Path], logging.Logger, Path]:
    extra_dir = base_dir / "EXTRA"
    dirs = {
        "in": base_dir / "IN",
        "out": base_dir / "OUT",
        "final": base_dir / "FINAL",
        "tmp": base_dir / "TMP",
        "log": base_dir / "LOG",
        "music": base_dir / "MUSIC",
        "extra": extra_dir,
        "model": extra_dir / "MODEL",
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


def build_reel_clip_name(clip_path: Path, tipo_slug: str) -> str:
    m = re.match(r"^.+_(?:1080P|REEL)_(clip_\d{2}_.+)$", clip_path.stem, re.IGNORECASE)
    if m:
        return f"{tipo_slug}_REEL_{m.group(1)}.mp4"
    return f"{tipo_slug}_REEL_{clip_path.stem}.mp4"


def short_clip_name(clip_path: Path, tipo_slug: str) -> str:
    reel_stem = Path(build_reel_clip_name(clip_path, tipo_slug)).stem
    return TIMESTAMP_SUFFIX_RE.sub("", reel_stem)


def trova_clip_sorgente(out_dir: Path, tipo_slug: str) -> list[Path]:
    prefixes = (f"{tipo_slug}_1080P_clip_", f"{tipo_slug}_REEL_clip_")
    uniform_matches = sorted([
        p for p in out_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() == ".mp4"
        and p.name.startswith(prefixes)
        and UNIFORM_CLIP_RE.match(p.name)
    ])
    if uniform_matches:
        return uniform_matches

    return sorted([
        p for p in out_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() == ".mp4"
        and tipo_slug.lower() in p.stem.lower()
        and LEGACY_CLIP_RE.match(p.name)
    ])


def log_model_info(model: YOLO, model_path: Path, logger: logging.Logger):
    size_mb = model_path.stat().st_size / (1024 * 1024) if model_path.is_file() else 0.0
    names = model.names if isinstance(model.names, dict) else {}
    num_classes = len(names)
    sample_classes = ", ".join(f"{k}:{v}" for k, v in list(names.items())[:10])
    logger.info(
        f"[{SCRIPT_NAME}][YOLO-MODEL] file='{model_path.name}' | path='{model_path}' | "
        f"size={size_mb:.2f} MB | classes={num_classes} | sample=[{sample_classes}]"
    )


def resolve_model(model_arg: str, model_dir: Path, base_dir: Path, logger: logging.Logger) -> tuple[YOLO, Path]:
    model_dir.mkdir(parents=True, exist_ok=True)
    p = Path(model_arg)
    norm_name = model_arg if (p.is_file() or p.suffix.lower() in MODEL_EXTS) else f"{model_arg.lower()}.pt"
    model_filename = Path(norm_name).name
    target = model_dir / model_filename

    if target.is_file() and target.stat().st_size > 0:
        logger.info(f"[{SCRIPT_NAME}][YOLO-MODEL] Caricamento modello locale da '{target}'.")
        model = YOLO(str(target))
        log_model_info(model, target, logger)
        return model, target

    src_candidates = [
        p if p.is_file() else None,
        base_dir / norm_name,
        base_dir / model_filename,
        Path.cwd() / model_filename,
    ]
    for cand in src_candidates:
        if cand and cand.is_file() and cand.stat().st_size > 0 and cand.resolve() != target.resolve():
            shutil.copy2(cand, target)
            logger.info(f"[{SCRIPT_NAME}][YOLO-MODEL] Copiato modello '{cand}' in '{target}'.")
            model = YOLO(str(target))
            log_model_info(model, target, logger)
            return model, target

    logger.info(f"[{SCRIPT_NAME}][YOLO-MODEL] Download modello '{model_filename}' in '{model_dir}'...")

    prev_cwd = Path.cwd()
    try:
        os.chdir(model_dir)
        YOLO(model_filename)
    finally:
        os.chdir(prev_cwd)

    for possible_dl in (model_dir / model_filename, base_dir / model_filename, Path.cwd() / model_filename):
        if possible_dl.is_file() and possible_dl.stat().st_size > 0 and possible_dl.resolve() != target.resolve():
            shutil.move(str(possible_dl), str(target))
            break

    if not target.is_file() or target.stat().st_size == 0:
        raise FileNotFoundError(f"Modello '{model_filename}' non disponibile in '{model_dir}' dopo il download.")

    logger.info(f"[{SCRIPT_NAME}][YOLO-MODEL] Modello scaricato e pronto in '{target}'.")
    model = YOLO(str(target))
    log_model_info(model, target, logger)
    return model, target


def detect_ball_cls(model: YOLO, logger: logging.Logger) -> tuple[int, str]:
    names = model.names if isinstance(model.names, dict) else {}
    for cid, lbl in names.items():
        if str(lbl).lower().strip() in BALL_KEYWORDS:
            logger.info(f"[{SCRIPT_NAME}][YOLO-CLASS] Classe target rilevata: ID={int(cid)} ('{lbl}')")
            return int(cid), str(lbl)
    fallback_lbl = str(names.get(COCO_SPORTS_BALL_CLASS, "sports ball (default)"))
    logger.info(f"[{SCRIPT_NAME}][YOLO-CLASS] Uso classe default: ID={COCO_SPORTS_BALL_CLASS} ('{fallback_lbl}')")
    return COCO_SPORTS_BALL_CLASS, fallback_lbl


def fill_gaps(centers_x: list[float | None], fallback_x: float, max_gap: int = 15) -> list[float]:
    n = len(centers_x)
    if n == 0:
        return []
    known = [i for i, v in enumerate(centers_x) if v is not None]
    if not known:
        return [fallback_x] * n

    out = [0.0] * n
    for k in known:
        out[k] = float(centers_x[k])  # type: ignore[arg-type]

    for i in range(n):
        if centers_x[i] is not None:
            continue
        prev_k = max([k for k in known if k < i], default=None)
        next_k = min([k for k in known if k > i], default=None)
        if prev_k is not None and next_k is not None and (next_k - prev_k - 1) <= max_gap:
            r = (i - prev_k) / (next_k - prev_k)
            out[i] = (1.0 - r) * float(centers_x[prev_k]) + r * float(centers_x[next_k])  # type: ignore[index]
        elif prev_k is not None:
            out[i] = float(centers_x[prev_k])  # type: ignore[index]
        elif next_k is not None:
            out[i] = float(centers_x[next_k])  # type: ignore[index]
        else:
            out[i] = fallback_x
    return out


def smooth_ema(vals: list[float], alpha: float = 0.12) -> list[float]:
    if not vals:
        return []
    sm = [vals[0]]
    for v in vals[1:]:
        sm.append(alpha * v + (1.0 - alpha) * sm[-1])
    return sm


def convert_single_clip(
    input_path: Path,
    output_path: Path,
    tmp_dir: Path,
    model: YOLO,
    model_path: Path,
    ball_cls_id: int,
    ball_cls_name: str,
    conf: float,
    imgsz: int | None,
    alpha: float,
    gap_frames: int,
    res: dict,
    logger: logging.Logger,
    out_w: int = 1080,
    out_h: int = 1920,
) -> dict:
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"Impossibile aprire {input_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    logger.info(
        f"[{SCRIPT_NAME}][YOLO-TRACK-START] clip='{input_path.name}' | model='{model_path.name}' | "
        f"cls={ball_cls_id}('{ball_cls_name}') | conf={conf} | imgsz={imgsz or 'native'} | "
        f"res={src_w}x{src_h}@{fps:.2f}fps | frames_meta={total_frames}"
    )

    raw_x: list[float | None] = []
    match_confs: list[float] = []
    total_boxes_all_classes = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        results = model.predict(frame, conf=conf, imgsz=imgsz, verbose=False) if imgsz else model.predict(frame, conf=conf, verbose=False)
        best_x = None
        best_c = 0.0
        for r in results:
            if r.boxes is None:
                continue
            total_boxes_all_classes += len(r.boxes)
            for box in r.boxes:
                cid = int(box.cls[0].item())
                c = float(box.conf[0].item())
                if cid == ball_cls_id and c > best_c:
                    x1, _, x2, _ = box.xyxy[0].tolist()
                    best_x = (x1 + x2) / 2.0
                    best_c = c
        if best_x is not None:
            match_confs.append(best_c)
        raw_x.append(best_x)
    cap.release()

    analyzed_frames = len(raw_x)
    matched_frames = len(match_confs)
    unmatched_frames = analyzed_frames - matched_frames
    match_ratio = (matched_frames / analyzed_frames * 100.0) if analyzed_frames > 0 else 0.0
    avg_conf = (sum(match_confs) / matched_frames) if matched_frames > 0 else 0.0
    max_conf = max(match_confs) if match_confs else 0.0
    min_conf = min(match_confs) if match_confs else 0.0

    filled_x = fill_gaps(raw_x, fallback_x=src_w / 2.0, max_gap=gap_frames)
    smooth_x = smooth_ema(filled_x, alpha=alpha)

    logger.info(
        f"[{SCRIPT_NAME}][YOLO-STATS] clip='{input_path.name}' | model='{model_path.name}' | "
        f"frames={analyzed_frames}/{total_frames} | matches={matched_frames} ({match_ratio:.1f}%) | "
        f"no_match={unmatched_frames} | boxes_tot={total_boxes_all_classes} | "
        f"conf_avg={avg_conf:.3f} (min={min_conf:.3f}, max={max_conf:.3f}) | alpha={alpha} | gap_max={gap_frames}"
    )

    crop_w = min(src_w, int(round(src_h * (out_w / out_h))))
    crop_h = src_h
    half_w = crop_w / 2.0

    tmp_silent = tmp_dir / f"{input_path.stem}_silent.mp4"
    tmp_mux = tmp_dir / f"{input_path.stem}_reel_mux.mp4"

    cap2 = cv2.VideoCapture(str(input_path))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp_silent), fourcc, fps, (out_w, out_h))

    idx = 0
    while True:
        ret, frame = cap2.read()
        if not ret:
            break
        cx = smooth_x[idx] if idx < len(smooth_x) else src_w / 2.0
        x1 = max(0, min(int(round(cx - half_w)), src_w - crop_w))
        cropped = frame[0:crop_h, x1:x1 + crop_w]
        resized = cv2.resize(cropped, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)
        writer.write(resized)
        idx += 1

    cap2.release()
    writer.release()

    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-i", str(tmp_silent),
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "1:a?",
        "-c:v", "libx264",
        "-preset", res["preset"],
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-threads", str(res["threads"]),
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(tmp_mux),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if tmp_silent.exists():
            tmp_silent.unlink(missing_ok=True)
        if tmp_mux.exists():
            tmp_mux.unlink(missing_ok=True)
        raise RuntimeError(f"Errore FFmpeg mux REEL su {input_path.name} (rc={proc.returncode}): {proc.stderr.strip()}")

    shutil.move(str(tmp_mux), str(output_path))
    if tmp_silent.exists():
        tmp_silent.unlink()
    logger.info(f"[{SCRIPT_NAME}] Convertita clip REEL: '{output_path.name}'")

    return {
        "clip": input_path.name,
        "frames": analyzed_frames,
        "matches": matched_frames,
        "boxes": total_boxes_all_classes,
        "conf_sum": sum(match_confs),
        "conf_max": max_conf,
    }


def main():
    abilita_ansi()

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Conversione clip in formato REEL 9:16")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--tipo", default="HIGHLIGHTS")
    parser.add_argument("--sottotitolo", default="CAMPIONATO - TEAM vs TEAM")
    parser.add_argument("--formato", default="REEL")
    parser.add_argument("--model", default="yolo26l.pt")
    parser.add_argument("--conf", type=float, default=0.50)
    parser.add_argument("--alpha", type=float, default=0.12)
    parser.add_argument("--gap-frames", type=int, default=15)
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    dirs, logger, log_file = setup_dirs_and_logger(base_dir)
    res = calcola_risorse()
    imgsz = res["imgsz_default"]
    tipo_slug = sanitize_slug(args.tipo)

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | model='{args.model}' | "
        f"imgsz={imgsz} | conf={args.conf} | alpha={args.alpha} | gap_frames={args.gap_frames} | "
        f"auto={args.auto} | risorse={res}"
    )

    if shutil.which("ffmpeg") is None:
        print(f"{RED}[X] [{SCRIPT_NAME}] FFmpeg non trovato nel PATH.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] FFmpeg non trovato nel PATH.")
        sys.exit(1)

    clips = trova_clip_sorgente(dirs["out"], tipo_slug)
    if not clips:
        print(f"{RED}[X] [{SCRIPT_NAME}] Nessuna clip sorgente per '{tipo_slug}' trovata in 'OUT/'.{RESET}", file=sys.stderr)
        logger.error(f"[{SCRIPT_NAME}] Nessuna clip sorgente per '{tipo_slug}' trovata in '{dirs['out']}'.")
        sys.exit(1)

    logger.info(f"[{SCRIPT_NAME}] Trovate {len(clips)} clip sorgente: {[c.name for c in clips]}")

    try:
        model, model_path = resolve_model(args.model, dirs["model"], base_dir, logger)
    except Exception as e:
        print(f"{RED}[X] [{SCRIPT_NAME}] Impossibile preparare/caricare il modello '{args.model}' in 'EXTRA/MODEL/': {e}{RESET}", file=sys.stderr)
        logger.exception(f"[{SCRIPT_NAME}] Errore modello '{args.model}': {e}")
        sys.exit(1)

    ball_cls_id, ball_cls_name = detect_ball_cls(model, logger)

    ok_cnt = err_cnt = 0
    tot_frames = tot_matches = tot_boxes = 0
    tot_conf_sum = 0.0
    global_max_conf = 0.0

    with tqdm(
        clips,
        desc="05 | Conversione REEL",
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}{postfix}",
        colour="magenta",
        ncols=82,
    ) as bar:
        for clip in bar:
            label = short_clip_name(clip, tipo_slug)
            bar.postfix = f" - {label}"
            bar.refresh()
            out_reel = dirs["out"] / build_reel_clip_name(clip, tipo_slug)
            if out_reel.is_file() and out_reel.stat().st_size > 0 and out_reel.resolve() != clip.resolve():
                if args.auto:
                    logger.info(f"[{SCRIPT_NAME}][YOLO-STATS] clip='{clip.name}' | stato=SKIP_EXISTING ('{out_reel.name}')")
                    ok_cnt += 1
                    continue
            try:
                st = convert_single_clip(
                    input_path=clip,
                    output_path=out_reel,
                    tmp_dir=dirs["tmp"],
                    model=model,
                    model_path=model_path,
                    ball_cls_id=ball_cls_id,
                    ball_cls_name=ball_cls_name,
                    conf=args.conf,
                    imgsz=imgsz,
                    alpha=args.alpha,
                    gap_frames=args.gap_frames,
                    res=res,
                    logger=logger,
                )
                tot_frames += st["frames"]
                tot_matches += st["matches"]
                tot_boxes += st["boxes"]
                tot_conf_sum += st["conf_sum"]
                if st["conf_max"] > global_max_conf:
                    global_max_conf = st["conf_max"]
                ok_cnt += 1
            except Exception as e:
                err_cnt += 1
                logger.exception(f"[{SCRIPT_NAME}] Errore su '{clip.name}': {e}")
                print(f"\n{RED}[X] [{SCRIPT_NAME}] Errore su '{clip.name}': {e} (log: {log_file.name}){RESET}", file=sys.stderr)

    overall_ratio = (tot_matches / tot_frames * 100.0) if tot_frames > 0 else 0.0
    overall_avg_conf = (tot_conf_sum / tot_matches) if tot_matches > 0 else 0.0

    logger.info(
        f"[{SCRIPT_NAME}][YOLO-SUMMARY] model='{model_path.name}' | clips_ok={ok_cnt}/{len(clips)} | "
        f"clips_err={err_cnt} | total_frames={tot_frames} | total_matches={tot_matches} "
        f"({overall_ratio:.1f}%) | total_boxes={tot_boxes} | avg_conf={overall_avg_conf:.3f} | max_conf={global_max_conf:.3f}"
    )
    logger.info(f"[{SCRIPT_NAME}] Completato: ok={ok_cnt}, err={err_cnt}")

    if ok_cnt == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()