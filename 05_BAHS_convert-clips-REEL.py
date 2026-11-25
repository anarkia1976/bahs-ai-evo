import os
import re
import sys
import math
import ctypes
import shutil
import logging
import argparse
import warnings
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import cv2
import torch
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


def _parse_device_token(tok: str, cuda_count: int) -> str | None:
    t = tok.strip().lower()
    if not t:
        return None
    if t == "cpu":
        return "cpu"
    if t.isdigit():
        idx = int(t)
        return f"cuda:{idx}" if 0 <= idx < cuda_count else None
    if t == "cuda":
        return "cuda:0" if cuda_count > 0 else None
    if t.startswith("cuda:"):
        tail = t.split(":", 1)[1].strip()
        if tail.isdigit():
            idx = int(tail)
            return f"cuda:{idx}" if 0 <= idx < cuda_count else None
        return t if cuda_count > 0 else None
    return None


def rileva_gpu(requested_device: str, logger: logging.Logger) -> dict:
    req = (requested_device or "auto").strip().lower()
    cuda_available = False
    cuda_count = 0
    devices_detail: list[dict] = []

    try:
        cuda_available = bool(torch.cuda.is_available())
        cuda_count = int(torch.cuda.device_count()) if cuda_available else 0
    except Exception as e:
        logger.warning(f"[{SCRIPT_NAME}][GPU] Errore verifica CUDA: {e}")
        cuda_available = False
        cuda_count = 0

    for idx in range(cuda_count):
        try:
            name = torch.cuda.get_device_name(idx)
        except Exception:
            name = f"CUDA:{idx}"
        devices_detail.append({
            "index": idx,
            "device": f"cuda:{idx}",
            "name": name,
        })

    if devices_detail:
        desc_gpus = ", ".join(f"cuda:{d['index']} ({d['name']})" for d in devices_detail)
        logger.info(f"[{SCRIPT_NAME}][GPU] Rilevate {cuda_count} GPU: {desc_gpus}")
    else:
        logger.info(f"[{SCRIPT_NAME}][GPU] Nessuna GPU CUDA rilevata.")

    if req == "cpu":
        selected_devices = ["cpu"]
    elif req == "auto":
        selected_devices = [f"cuda:{idx}" for idx in range(cuda_count)] if cuda_count > 0 else ["cpu"]
    else:
        tokens = [tok for tok in req.split(",") if tok.strip()]
        parsed: list[str] = []
        for tok in tokens:
            mapped = _parse_device_token(tok, cuda_count)
            if mapped and mapped not in parsed:
                parsed.append(mapped)
        if not parsed:
            if req != "cpu":
                logger.warning(
                    f"[{SCRIPT_NAME}][GPU] Dispositivo '{requested_device}' non valido o non disponibile -> fallback su 'cpu'."
                )
            selected_devices = ["cpu"]
        else:
            selected_devices = parsed

    ff_encoder = "libx264"
    ff_hw_mode = "cpu"
    use_gpu = any(d.startswith("cuda") for d in selected_devices)

    if use_gpu or req == "auto":
        try:
            enc_out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout or ""

            candidates: list[tuple[str, str]] = []
            if "h264_nvenc" in enc_out:
                candidates.append(("h264_nvenc", "nvenc"))
            if "h264_amf" in enc_out:
                candidates.append(("h264_amf", "amf"))

            for codec, mode in candidates:
                test_cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-f", "lavfi",
                    "-i", "color=c=black:s=128x128:r=1:d=0.1",
                    "-c:v", codec,
                    "-f", "null",
                    "-",
                ]
                proc = subprocess.run(test_cmd, capture_output=True, text=True, check=False)
                if proc.returncode == 0:
                    ff_encoder = codec
                    ff_hw_mode = mode
                    break
        except Exception as e:
            logger.warning(f"[{SCRIPT_NAME}][GPU] Verifica encoder hardware FFmpeg fallita, uso libx264: {e}")
            ff_encoder = "libx264"
            ff_hw_mode = "cpu"

    info = {
        "cuda_available": cuda_available,
        "cuda_count": cuda_count,
        "devices_detail": devices_detail,
        "yolo_devices": selected_devices,
        "primary_device": selected_devices[0],
        "ff_encoder": ff_encoder,
        "ff_hw_mode": ff_hw_mode,
    }
    logger.info(
        f"[{SCRIPT_NAME}][GPU] req='{requested_device}' | count={cuda_count} | "
        f"devices={selected_devices} | ff_encoder='{ff_encoder}'"
    )
    return info


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


def prepare_model_path(model_arg: str, model_dir: Path, base_dir: Path, logger: logging.Logger) -> Path:
    model_dir.mkdir(parents=True, exist_ok=True)
    p = Path(model_arg)
    norm_name = model_arg if (p.is_file() or p.suffix.lower() in MODEL_EXTS) else f"{model_arg.lower()}.pt"
    model_filename = Path(norm_name).name
    target = model_dir / model_filename

    if target.is_file() and target.stat().st_size > 0:
        logger.info(f"[{SCRIPT_NAME}][YOLO-MODEL] Modello locale trovato in '{target}'.")
        return target

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
            return target

    logger.info(f"[{SCRIPT_NAME}][YOLO-MODEL] Download modello '{model_filename}' in '{model_dir}'...")
    YOLO(str(target))

    for possible_dl in (model_dir / model_filename, base_dir / model_filename, Path.cwd() / model_filename):
        if possible_dl.is_file() and possible_dl.stat().st_size > 0 and possible_dl.resolve() != target.resolve():
            shutil.move(str(possible_dl), str(target))
            break

    if not target.is_file() or target.stat().st_size == 0:
        raise FileNotFoundError(f"Modello '{model_filename}' non disponibile in '{model_dir}' dopo il download.")

    logger.info(f"[{SCRIPT_NAME}][YOLO-MODEL] Modello scaricato e pronto in '{target}'.")
    return target


def load_yolo_for_device(model_path: Path, device: str, logger: logging.Logger) -> tuple[YOLO, str]:
    try:
        logger.info(f"[{SCRIPT_NAME}][YOLO-MODEL] Caricamento '{model_path.name}' su '{device}'...")
        m = YOLO(str(model_path))
        if device.startswith("cuda") and torch.cuda.is_available():
            try:
                m.to(device)
            except Exception as move_err:
                logger.warning(f"[{SCRIPT_NAME}][GPU] m.to('{device}') avviso: {move_err}")
        return m, device
    except Exception as e:
        if device != "cpu":
            logger.warning(f"[{SCRIPT_NAME}][GPU-FALLBACK] Caricamento su '{device}' fallito ({e}), ripiego su 'cpu'.")
            m = YOLO(str(model_path))
            return m, "cpu"
        raise


def detect_ball_cls(model: YOLO, logger: logging.Logger) -> tuple[int, str]:
    names = model.names if isinstance(model.names, dict) else {}
    for cid, lbl in names.items():
        if str(lbl).lower().strip() in BALL_KEYWORDS:
            logger.info(f"[{SCRIPT_NAME}][YOLO-CLASS] Classe target rilevata: ID={int(cid)} ('{lbl}')")
            return int(cid), str(lbl)
    fallback_lbl = str(names.get(COCO_SPORTS_BALL_CLASS, "sports ball (default)"))
    logger.info(f"[{SCRIPT_NAME}][YOLO-CLASS] Uso classe default: ID={COCO_SPORTS_BALL_CLASS} ('{fallback_lbl}')")
    return COCO_SPORTS_BALL_CLASS, fallback_lbl


def fill_gaps(centers_x: list[float | None], fallback_x: float, max_gap: int = 35) -> list[float]:
    n = len(centers_x)
    if n == 0:
        return []
    known = [i for i, v in enumerate(centers_x) if v is not None]
    if not known:
        return [fallback_x] * n

    safe_gap = max(1, int(max_gap))
    out = [0.0] * n
    for k in known:
        out[k] = float(centers_x[k])  # type: ignore[arg-type]

    for i in range(n):
        if centers_x[i] is not None:
            continue
        prev_k = max([k for k in known if k < i], default=None)
        next_k = min([k for k in known if k > i], default=None)

        if prev_k is not None and next_k is not None and (next_k - prev_k - 1) <= safe_gap:
            r = (i - prev_k) / (next_k - prev_k)
            out[i] = (1.0 - r) * float(centers_x[prev_k]) + r * float(centers_x[next_k])  # type: ignore[index]
        elif prev_k is not None:
            dt = i - prev_k
            vx = 0.0
            prev2_k = max([k for k in known if k < prev_k], default=None)
            if prev2_k is not None and (prev_k - prev2_k) > 0:
                vx = (float(centers_x[prev_k]) - float(centers_x[prev2_k])) / float(prev_k - prev2_k)  # type: ignore[index]
            damping = 0.85 ** min(dt, 15)
            extrap = float(centers_x[prev_k]) + vx * min(dt, 12) * damping  # type: ignore[index]
            blend = min(1.0, dt / safe_gap)
            out[i] = (1.0 - blend) * extrap + blend * float(centers_x[prev_k])  # type: ignore[index]
        elif next_k is not None:
            out[i] = float(centers_x[next_k])  # type: ignore[index]
        else:
            out[i] = fallback_x
    return out


def _gaussian_smooth_1d(vals: list[float], sigma_frames: float) -> list[float]:
    n = len(vals)
    if n <= 2 or sigma_frames <= 0.0:
        return list(vals)

    radius = max(1, int(math.ceil(sigma_frames * 2.5)))
    weights = [
        math.exp(-0.5 * (k / sigma_frames) ** 2)
        for k in range(-radius, radius + 1)
    ]

    out = [0.0] * n
    for i in range(n):
        acc = 0.0
        w_sum = 0.0
        lo = max(0, i - radius)
        hi = min(n - 1, i + radius)
        for j in range(lo, hi + 1):
            w = weights[j - i + radius]
            acc += vals[j] * w
            w_sum += w
        out[i] = acc / w_sum if w_sum > 0 else vals[i]
    return out


def smooth_camera_track(
    vals: list[float],
    fps: float,
    crop_w: int,
    smooth_seconds: float = 0.45,
    deadzone_ratio: float = 0.07,
    max_pan_widths_per_sec: float = 0.85,
) -> list[float]:
    if not vals:
        return []
    if len(vals) == 1:
        return [vals[0]]

    safe_fps = fps if fps and fps > 1.0 else 30.0
    sigma_frames = max(1.0, smooth_seconds * safe_fps * 0.5)
    target_track = _gaussian_smooth_1d(vals, sigma_frames=sigma_frames)

    deadzone_px = max(4.0, crop_w * deadzone_ratio)
    max_step_px = max(1.0, (crop_w * max_pan_widths_per_sec) / safe_fps)

    cam_x = [target_track[0]]
    cur_x = target_track[0]
    cur_vel = 0.0

    for target in target_track[1:]:
        diff = target - cur_x
        abs_diff = abs(diff)

        if abs_diff <= deadzone_px:
            desired_x = cur_x + diff * 0.15
        else:
            sign = 1.0 if diff > 0 else -1.0
            effective_diff = diff - sign * deadzone_px
            desired_x = cur_x + sign * deadzone_px + effective_diff * 0.65

        desired_step = desired_x - cur_x
        clamped_step = max(-max_step_px, min(max_step_px, desired_step))
        cur_vel = 0.75 * cur_vel + 0.25 * clamped_step
        cur_x += cur_vel
        cam_x.append(cur_x)

    return cam_x


def _release_cuda_cache(device_str: str):
    if device_str.startswith("cuda") and torch.cuda.is_available():
        try:
            if ":" in device_str:
                idx = int(device_str.split(":", 1)[1])
                with torch.cuda.device(idx):
                    torch.cuda.empty_cache()
            else:
                torch.cuda.empty_cache()
        except Exception:
            pass


def predict_frame_with_fallback(
    model: YOLO,
    frame,
    conf: float,
    imgsz: int | None,
    ball_cls_id: int,
    active_device: str,
    logger: logging.Logger,
) -> tuple[list, str]:
    kwargs = {
        "conf": conf,
        "classes": [ball_cls_id],
        "verbose": False,
        "device": active_device,
    }
    if imgsz:
        kwargs["imgsz"] = imgsz

    try:
        return model.predict(frame, **kwargs), active_device
    except Exception as e:
        if active_device.startswith("cuda"):
            _release_cuda_cache(active_device)
            logger.warning(
                f"[{SCRIPT_NAME}][GPU-FALLBACK] Erro YOLO su '{active_device}' ({e}), "
                f"passo definitivamente a 'cpu'."
            )
            kwargs["device"] = "cpu"
            return model.predict(frame, **kwargs), "cpu"
        raise


def build_ffmpeg_pipe_cmd(
    input_path: Path,
    tmp_out: Path,
    fps: float,
    out_w: int,
    out_h: int,
    res: dict,
    encoder: str,
    threads_override: int | None = None,
) -> list[str]:
    ff_threads = str(threads_override or res["threads"])
    if encoder == "h264_nvenc":
        v_args = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-pix_fmt", "yuv420p"]
    elif encoder == "h264_amf":
        v_args = ["-c:v", "h264_amf", "-quality", "balanced", "-pix_fmt", "yuv420p"]
    else:
        v_args = [
            "-c:v", "libx264",
            "-preset", res["preset"],
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-threads", ff_threads,
        ]

    return [
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{out_w}x{out_h}",
        "-r", f"{fps:.6f}",
        "-i", "-",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "1:a?",
        *v_args,
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(tmp_out),
    ]


def encode_cropped_video_direct(
    input_path: Path,
    tmp_out: Path,
    smooth_x: list[float],
    initial_x: float,
    fps: float,
    src_w: int,
    crop_w: int,
    crop_h: int,
    out_w: int,
    out_h: int,
    res: dict,
    encoder: str,
    threads_override: int | None = None,
) -> tuple[bool, str]:
    half_w = crop_w / 2.0
    cmd = build_ffmpeg_pipe_cmd(
        input_path=input_path,
        tmp_out=tmp_out,
        fps=fps,
        out_w=out_w,
        out_h=out_h,
        res=res,
        encoder=encoder,
        threads_override=threads_override,
    )

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        return False, f"Impossibile riaprire {input_path.name} per il ritaglio REEL"

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    stderr_bytes = b""
    idx = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            cx = smooth_x[idx] if idx < len(smooth_x) else initial_x
            x1 = max(0, min(int(round(cx - half_w)), src_w - crop_w))
            cropped = frame[0:crop_h, x1:x1 + crop_w]
            resized = cv2.resize(cropped, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)
            if proc.stdin:
                proc.stdin.write(resized.tobytes())
            idx += 1
    except BrokenPipeError:
        pass
    finally:
        cap.release()
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        if proc.stderr:
            stderr_bytes = proc.stderr.read()
            try:
                proc.stderr.close()
            except Exception:
                pass
        rc = proc.wait()

    if rc == 0 and tmp_out.is_file() and tmp_out.stat().st_size > 0:
        return True, ""
    err_msg = stderr_bytes.decode("utf-8", errors="replace").strip() or f"returncode={rc}"
    return False, err_msg


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
    assigned_device: str,
    ff_encoder: str,
    logger: logging.Logger,
    threads_override: int | None = None,
    out_w: int = 1080,
    out_h: int = 1920,
) -> dict:
    active_device = assigned_device
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"Impossibile aprire {input_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    logger.info(
        f"[{SCRIPT_NAME}][YOLO-TRACK-START] clip='{input_path.name}' | model='{model_path.name}' | "
        f"device='{active_device}' | cls={ball_cls_id}('{ball_cls_name}') | conf={conf} | "
        f"imgsz={imgsz or 'native'} | res={src_w}x{src_h}@{fps:.2f}fps | frames_meta={total_frames}"
    )

    raw_x: list[float | None] = []
    match_confs: list[float] = []
    total_boxes_ball = 0
    last_known_x: float | None = None

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            results, active_device = predict_frame_with_fallback(
                model=model,
                frame=frame,
                conf=conf,
                imgsz=imgsz,
                ball_cls_id=ball_cls_id,
                active_device=active_device,
                logger=logger,
            )
            best_x = None
            best_score = -1.0
            best_c = 0.0
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    cid = int(box.cls[0].item())
                    if cid != ball_cls_id:
                        continue
                    total_boxes_ball += 1
                    c = float(box.conf[0].item())
                    x1, _, x2, _ = box.xyxy[0].tolist()
                    cx_cand = (x1 + x2) / 2.0
                    if last_known_x is not None:
                        dist_norm = abs(cx_cand - last_known_x) / max(1.0, float(src_w))
                        score = c - (0.18 * dist_norm)
                    else:
                        score = c
                    if score > best_score:
                        best_score = score
                        best_x = cx_cand
                        best_c = c
            if best_x is not None:
                match_confs.append(best_c)
                last_known_x = best_x
            raw_x.append(best_x)
    finally:
        cap.release()
        _release_cuda_cache(active_device)

    analyzed_frames = len(raw_x)
    matched_frames = len(match_confs)
    unmatched_frames = analyzed_frames - matched_frames
    match_ratio = (matched_frames / analyzed_frames * 100.0) if analyzed_frames > 0 else 0.0
    avg_conf = (sum(match_confs) / matched_frames) if matched_frames > 0 else 0.0
    max_conf = max(match_confs) if match_confs else 0.0
    min_conf = min(match_confs) if match_confs else 0.0

    crop_w = min(src_w, int(round(src_h * (out_w / out_h))))
    crop_h = src_h

    initial_x = raw_x[0] if (raw_x and raw_x[0] is not None) else (
        next((v for v in raw_x if v is not None), src_w / 2.0)
    )
    filled_x = fill_gaps(raw_x, fallback_x=initial_x, max_gap=gap_frames)
    smooth_seconds = max(0.20, min(1.20, alpha * 1.8))
    smooth_x = smooth_camera_track(
        filled_x,
        fps=fps,
        crop_w=crop_w,
        smooth_seconds=smooth_seconds,
        deadzone_ratio=0.07,
        max_pan_widths_per_sec=0.85,
    )

    logger.info(
        f"[{SCRIPT_NAME}][YOLO-STATS] clip='{input_path.name}' | model='{model_path.name}' | "
        f"device='{active_device}' | frames={analyzed_frames}/{total_frames} | "
        f"matches={matched_frames} ({match_ratio:.1f}%) | no_match={unmatched_frames} | "
        f"ball_boxes={total_boxes_ball} | conf_avg={avg_conf:.3f} "
        f"(min={min_conf:.3f}, max={max_conf:.3f}) | alpha={alpha} | smooth_s={smooth_seconds:.2f} | gap_max={gap_frames}"
    )

    tmp_mux = tmp_dir / f"{output_path.stem}_{os.getpid()}_{threading.get_ident()}.part.mp4"
    used_encoder = ff_encoder

    try:
        ok_enc, err_enc = encode_cropped_video_direct(
            input_path=input_path,
            tmp_out=tmp_mux,
            smooth_x=smooth_x,
            initial_x=initial_x,
            fps=fps,
            src_w=src_w,
            crop_w=crop_w,
            crop_h=crop_h,
            out_w=out_w,
            out_h=out_h,
            res=res,
            encoder=used_encoder,
            threads_override=threads_override,
        )

        if not ok_enc and used_encoder != "libx264":
            logger.warning(
                f"[{SCRIPT_NAME}][GPU-FALLBACK] Encoder '{used_encoder}' fallito su {input_path.name}: "
                f"{err_enc} -> fallback su 'libx264'."
            )
            used_encoder = "libx264"
            tmp_mux.unlink(missing_ok=True)
            ok_enc, err_enc = encode_cropped_video_direct(
                input_path=input_path,
                tmp_out=tmp_mux,
                smooth_x=smooth_x,
                initial_x=initial_x,
                fps=fps,
                src_w=src_w,
                crop_w=crop_w,
                crop_h=crop_h,
                out_w=out_w,
                out_h=out_h,
                res=res,
                encoder=used_encoder,
                threads_override=threads_override,
            )

        if not ok_enc:
            raise RuntimeError(f"Errore FFmpeg REEL su {input_path.name}: {err_enc}")

        os.replace(tmp_mux, output_path)
    finally:
        if tmp_mux.exists():
            try:
                tmp_mux.unlink()
            except Exception:
                pass

    logger.info(f"[{SCRIPT_NAME}] Convertita clip REEL ({active_device}, {used_encoder}): '{output_path.name}'")

    return {
        "clip": input_path.name,
        "frames": analyzed_frames,
        "matches": matched_frames,
        "boxes": total_boxes_ball,
        "conf_sum": sum(match_confs),
        "conf_max": max_conf,
    }


def main():
    abilita_ansi()
    warnings.filterwarnings("ignore", message=".*'half' is deprecated.*", category=UserWarning)
    logging.getLogger("ultralytics").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description=f"{SCRIPT_NAME} - Conversione clip in formato REEL 9:16")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--tipo", default="HIGHLIGHTS")
    parser.add_argument("--sottotitolo", default="CAMPIONATO - TEAM vs TEAM")
    parser.add_argument("--formato", default="REEL")
    parser.add_argument("--model", default="yolo26l.pt")
    parser.add_argument("--device", default="auto", help="Dispositivo YOLO: auto, cpu, cuda:0, 0,1, cuda:0,cuda:1...")
    parser.add_argument("--conf", type=float, default=0.50)
    parser.add_argument("--alpha", type=float, default=0.25)
    parser.add_argument("--gap-frames", type=int, default=35)
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()

    base_dir = Path(args.base_dir).resolve()
    dirs, logger, log_file = setup_dirs_and_logger(base_dir)
    gpu_info = rileva_gpu(args.device, logger)
    res = calcola_risorse()
    imgsz = res["imgsz_default"]
    tipo_slug = sanitize_slug(args.tipo)

    logger.info(
        f"[{SCRIPT_NAME}] Avvio | base_dir='{base_dir}' | tipo='{args.tipo}' | model='{args.model}' | "
        f"devices={gpu_info['yolo_devices']} | ff_encoder='{gpu_info['ff_encoder']}' | "
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

    existing_reels = [
        dirs["out"] / build_reel_clip_name(c, tipo_slug)
        for c in clips
        if (dirs["out"] / build_reel_clip_name(c, tipo_slug)).is_file()
        and (dirs["out"] / build_reel_clip_name(c, tipo_slug)).stat().st_size > 0
        and (dirs["out"] / build_reel_clip_name(c, tipo_slug)).resolve() != c.resolve()
    ]

    overwrite_existing = False
    if existing_reels and not args.auto:
        print(f"\n{YELLOW}[!] ATTENZIONE: Trovate {len(existing_reels)} clip REEL già presenti in 'OUT/'.{RESET}")
        print("   1. Sovrascrivi e riconverti tutte le clip")
        print("   2. Salta (mantieni clip REEL esistenti)")
        print("   3. Interrompi script")
        while True:
            sc = input("👉 Scelta [INVIO = 2 Salta]: ").strip()
            if sc in ("", "2"):
                logger.info(f"[{SCRIPT_NAME}] Scelta utente: mantieni {len(existing_reels)} clip REEL esistenti.")
                break
            if sc == "1":
                overwrite_existing = True
                logger.info(f"[{SCRIPT_NAME}] Scelta utente: sovrascrittura clip REEL.")
                break
            if sc == "3":
                print(f"{YELLOW}[!] [{SCRIPT_NAME}] Elaborazione interrotta dall'utente.{RESET}")
                logger.info(f"[{SCRIPT_NAME}] Interrotto dall'utente.")
                return
            print(f"{RED}[!] Scelta non valida.{RESET}")

    tasks: list[tuple[Path, Path]] = []
    ok_cnt = err_cnt = 0
    for clip in clips:
        out_reel = dirs["out"] / build_reel_clip_name(clip, tipo_slug)
        if out_reel.is_file() and out_reel.stat().st_size > 0 and out_reel.resolve() != clip.resolve():
            if args.auto or not overwrite_existing:
                logger.info(f"[{SCRIPT_NAME}][YOLO-STATS] clip='{clip.name}' | stato=SKIP_EXISTING ('{out_reel.name}')")
                ok_cnt += 1
                continue
        tasks.append((clip, out_reel))

    if not tasks:
        logger.info(f"[{SCRIPT_NAME}] Completato (SKIP): ok={ok_cnt}, err=0")
        return

    try:
        model_path = prepare_model_path(args.model, dirs["model"], base_dir, logger)
    except Exception as e:
        print(f"{RED}[X] [{SCRIPT_NAME}] Impossibile preparare il modello '{args.model}' in 'EXTRA/MODEL/': {e}{RESET}", file=sys.stderr)
        logger.exception(f"[{SCRIPT_NAME}] Errore modello '{args.model}': {e}")
        sys.exit(1)

    worker_devices = gpu_info["yolo_devices"]
    models_by_dev: dict[str, tuple[YOLO, str]] = {}
    ball_cls_id = COCO_SPORTS_BALL_CLASS
    ball_cls_name = "sports ball"

    try:
        for dev in worker_devices:
            m_inst, actual_dev = load_yolo_for_device(model_path, dev, logger)
            models_by_dev[dev] = (m_inst, actual_dev)
        first_model = next(iter(models_by_dev.values()))[0]
        log_model_info(first_model, model_path, logger)
        ball_cls_id, ball_cls_name = detect_ball_cls(first_model, logger)
    except Exception as e:
        print(f"{RED}[X] [{SCRIPT_NAME}] Impossibile caricare il modello '{model_path.name}': {e}{RESET}", file=sys.stderr)
        logger.exception(f"[{SCRIPT_NAME}] Errore caricamento modello '{model_path.name}': {e}")
        sys.exit(1)

    tot_frames = tot_matches = tot_boxes = 0
    tot_conf_sum = 0.0
    global_max_conf = 0.0

    num_workers = min(len(tasks), len(worker_devices)) if len(worker_devices) > 1 else 1
    threads_per_worker = max(1, res["threads"] // max(1, num_workers))

    def _run_task(task_idx: int, clip_in: Path, clip_out: Path) -> dict:
        dev_key = worker_devices[task_idx % len(worker_devices)]
        m_inst, actual_dev = models_by_dev[dev_key]
        return convert_single_clip(
            input_path=clip_in,
            output_path=clip_out,
            tmp_dir=dirs["tmp"],
            model=m_inst,
            model_path=model_path,
            ball_cls_id=ball_cls_id,
            ball_cls_name=ball_cls_name,
            conf=args.conf,
            imgsz=imgsz,
            alpha=args.alpha,
            gap_frames=args.gap_frames,
            res=res,
            assigned_device=actual_dev,
            ff_encoder=gpu_info["ff_encoder"],
            logger=logger,
            threads_override=threads_per_worker,
        )

    with tqdm(
        total=len(clips),
        initial=ok_cnt,
        desc="05 | Conversione REEL",
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt}{postfix}",
        colour="magenta",
        ncols=82,
    ) as bar:
        if num_workers > 1:
            logger.info(f"[{SCRIPT_NAME}][MULTI-GPU] Avvio elaborazione parallela su {num_workers} dispositivi: {worker_devices}")
            with ThreadPoolExecutor(max_workers=num_workers) as pool:
                futures = {
                    pool.submit(_run_task, i, c_in, c_out): c_in
                    for i, (c_in, c_out) in enumerate(tasks)
                }
                for fut in as_completed(futures):
                    clip = futures[fut]
                    bar.postfix = f" - {short_clip_name(clip, tipo_slug)}"
                    try:
                        st = fut.result()
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
                    bar.update(1)
        else:
            for i, (clip, out_reel) in enumerate(tasks):
                bar.postfix = f" - {short_clip_name(clip, tipo_slug)}"
                bar.refresh()
                try:
                    st = _run_task(i, clip, out_reel)
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
                bar.update(1)

    overall_ratio = (tot_matches / tot_frames * 100.0) if tot_frames > 0 else 0.0
    overall_avg_conf = (tot_conf_sum / tot_matches) if tot_matches > 0 else 0.0

    logger.info(
        f"[{SCRIPT_NAME}][YOLO-SUMMARY] model='{model_path.name}' | devices={worker_devices} | "
        f"ff_encoder='{gpu_info['ff_encoder']}' | clips_ok={ok_cnt}/{len(clips)} | "
        f"clips_err={err_cnt} | total_frames={tot_frames} | total_matches={tot_matches} | "
        f"({overall_ratio:.1f}%) | total_boxes={tot_boxes} | avg_conf={overall_avg_conf:.3f} | max_conf={global_max_conf:.3f}"
    )
    logger.info(f"[{SCRIPT_NAME}] Completato: ok={ok_cnt}, err={err_cnt}")

    if ok_cnt == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()