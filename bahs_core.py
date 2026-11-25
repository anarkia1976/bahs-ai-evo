import os
import re
import sys
import ctypes
import shutil
import logging
import subprocess
from datetime import datetime
from pathlib import Path

REQUIRED_DIRS = ("IN", "OUT", "FINAL", "TMP", "LOG", "MUSIC", "EXTRA")
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
FONT_EXTS = (".ttf",)
LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
MODEL_EXTS = (".pt", ".onnx", ".engine", ".tflite", ".pb")
NO_MUSIC_TYPES = {"GAMEDAY", "FRIENDLY MATCH"}

DEFAULT_TOP_TEXT = "HIGHLIGHTS"
DEFAULT_SUB_TEXT = "CAMPIONATO - TEAM vs TEAM"
DEFAULT_FORMAT = "1080P"
ALLOWED_FORMATS = ("1080P", "REEL")

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"


def enable_ansi_console() -> None:
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            os.system("")


def disable_windows_quick_edit() -> None:
    """Evita che un click accidentale sulla console Windows metta in pausa l'output."""
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            h_in = kernel32.GetStdHandle(-10)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h_in, ctypes.byref(mode)):
                enable_quick_edit_mode = 0x0040
                enable_extended_flags = 0x0080
                new_mode = (mode.value & ~enable_quick_edit_mode) | enable_extended_flags
                kernel32.SetConsoleMode(h_in, new_mode)
        except Exception:
            pass


def clear_console() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def detect_ram_gb() -> tuple[float, float]:
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


def compute_system_resources(profile: str = "default") -> dict:
    threads = os.cpu_count() or 4
    cores = max(2, threads // 2)
    ram_tot, ram_avail = detect_ram_gb()

    if threads <= 4 or ram_avail < 6.0:
        max_workers = 2
        threads_per_worker = max(1, threads // 2)
        preset = "veryfast"
        imgsz_default: int | None = 640
    elif threads <= 8 or ram_avail < 12.0:
        max_workers = min(4, cores)
        threads_per_worker = 2
        preset = "fast"
        imgsz_default = 960
    else:
        max_workers = min(cores, 8)
        threads_per_worker = min(4, max(2, threads // max_workers))
        preset = "medium" if profile == "merge" else "fast"
        imgsz_default = None

    return {
        "threads": threads,
        "cores": cores,
        "ram_tot": ram_tot,
        "ram_avail": ram_avail,
        "max_workers": max_workers,
        "norm_workers": min(6, max_workers),
        "threads_per_worker": threads_per_worker,
        "preset": preset,
        "imgsz_default": imgsz_default,
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


def detect_gpu_and_encoder(requested_device: str = "auto", logger: logging.Logger | None = None) -> dict:
    req = (requested_device or "auto").strip().lower()
    cuda_available = False
    cuda_count = 0
    devices_detail: list[dict] = []

    try:
        import torch  # type: ignore

        cuda_available = bool(torch.cuda.is_available())
        cuda_count = int(torch.cuda.device_count()) if cuda_available else 0
        for idx in range(cuda_count):
            try:
                name = torch.cuda.get_device_name(idx)
            except Exception:
                name = f"CUDA:{idx}"
            devices_detail.append({"index": idx, "device": f"cuda:{idx}", "name": name})
    except Exception as exc:
        if logger:
            logger.warning(f"[BAHS-CORE][GPU] Verifica CUDA non disponibile o in errore: {exc}")

    if req == "cpu":
        selected_devices = ["cpu"]
    elif req == "auto":
        selected_devices = [f"cuda:{idx}" for idx in range(cuda_count)] if cuda_count > 0 else ["cpu"]
    else:
        parsed: list[str] = []
        for tok in req.split(","):
            mapped = _parse_device_token(tok, cuda_count)
            if mapped and mapped not in parsed:
                parsed.append(mapped)
        selected_devices = parsed or ["cpu"]

    ff_encoder = "libx264"
    ff_hw_mode = "cpu"
    use_gpu = any(d.startswith("cuda") for d in selected_devices)

    if (use_gpu or req == "auto") and shutil.which("ffmpeg") is not None:
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
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=128x128:r=1:d=0.1",
                    "-c:v",
                    codec,
                    "-f",
                    "null",
                    "-",
                ]
                proc = subprocess.run(test_cmd, capture_output=True, text=True, check=False)
                if proc.returncode == 0:
                    ff_encoder = codec
                    ff_hw_mode = mode
                    break
        except Exception as exc:
            if logger:
                logger.warning(f"[BAHS-CORE][GPU] Test encoder hardware fallito, uso libx264: {exc}")

    info = {
        "cuda_available": cuda_available,
        "cuda_count": cuda_count,
        "devices_detail": devices_detail,
        "yolo_devices": selected_devices,
        "primary_device": selected_devices[0],
        "ff_encoder": ff_encoder,
        "ff_hw_mode": ff_hw_mode,
    }
    if logger:
        logger.info(
            f"[BAHS-CORE][GPU] req='{requested_device}' | cuda_count={cuda_count} | "
            f"devices={selected_devices} | ff_encoder='{ff_encoder}'"
        )
    return info


def check_ffmpeg_tools(require_ffprobe: bool = True) -> tuple[bool, list[str]]:
    missing: list[str] = []
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")
    if require_ffprobe and shutil.which("ffprobe") is None:
        missing.append("ffprobe")
    return len(missing) == 0, missing


def sanitize_slug(text: str, fallback: str = "INCONTRO") -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (text or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip("_") or fallback


def normalize_format(fmt: str | None) -> str:
    val = (fmt or DEFAULT_FORMAT).strip().upper()
    return val if val in ALLOWED_FORMATS else DEFAULT_FORMAT


def build_stage_name(
    tipo_slug: str,
    fmt_slug: str,
    fade: bool = True,
    music: bool = False,
    text: bool = False,
    logo: bool = False,
) -> str:
    parts = [tipo_slug, normalize_format(fmt_slug), "with", "fade" if fade else "nofade"]
    if music:
        parts.append("music")
    if text:
        parts.append("text")
    if logo:
        parts.append("logo")
    return "_".join(parts) + ".mp4"


def build_reel_clip_name(clip_path: Path, tipo_slug: str) -> str:
    match = re.match(r"^.+_(?:1080P|REEL)_(clip_\d{2}_.+)$", clip_path.stem, re.IGNORECASE)
    if match:
        return f"{tipo_slug}_REEL_{match.group(1)}.mp4"
    return f"{tipo_slug}_REEL_{clip_path.stem}.mp4"


def ensure_project_dirs(base_dir: Path, include_model_dir: bool = False) -> dict[str, Path]:
    root = base_dir.resolve()
    extra_dir = root / "EXTRA"
    dirs = {
        "root": root,
        "in": root / "IN",
        "out": root / "OUT",
        "final": root / "FINAL",
        "tmp": root / "TMP",
        "log": root / "LOG",
        "music": root / "MUSIC",
        "extra": extra_dir,
    }
    if include_model_dir:
        dirs["model"] = extra_dir / "MODEL"

    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def setup_step_logger(log_dir: Path, script_name: str) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{script_name}_{ts}.log"

    logger = logging.getLogger(script_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger, log_file


def parse_time_to_seconds(time_str: str) -> float | None:
    raw_parts = [p.strip().replace(",", ".") for p in (time_str or "").strip().split(":")]
    if not raw_parts or any(not p for p in raw_parts):
        return None
    try:
        parts = [float(x) for x in raw_parts]
    except ValueError:
        return None
    if any(x < 0 for x in parts):
        return None
    if len(parts) == 3:
        return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60.0 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return None


def seconds_to_time_str(seconds: float) -> str:
    safe_sec = max(0.0, float(seconds))
    mins = int(safe_sec // 60)
    secs = int(safe_sec % 60)
    if mins >= 60:
        hours = mins // 60
        mins = mins % 60
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def read_timestamps_file(txt_path: Path, validate_seconds: bool = False) -> list[str]:
    if not txt_path.is_file() or txt_path.stat().st_size == 0:
        return []
    values: list[str] = []
    for line in txt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        for token in s.split(","):
            t = token.strip()
            if not t:
                continue
            if not validate_seconds or parse_time_to_seconds(t) is not None:
                values.append(t)
    return values


def has_valid_timestamps(txt_path: Path) -> bool:
    return len(read_timestamps_file(txt_path, validate_seconds=True)) > 0


def resolve_timestamp_files(out_dir: Path, tipo_slug: str, fmt_slug: str) -> tuple[Path, Path]:
    specific = out_dir / f"{tipo_slug}_{normalize_format(fmt_slug)}_timestamps.txt"
    generic = out_dir / "timestamps.txt"
    return specific, generic


def sync_generic_timestamps(
    src_txt: Path,
    out_dir: Path,
    logger: logging.Logger | None = None,
    overwrite_generic: bool = False,
) -> Path:
    dst_txt = out_dir / "timestamps.txt"
    if src_txt.resolve() == dst_txt.resolve():
        return dst_txt
    if not overwrite_generic and has_valid_timestamps(dst_txt):
        if logger:
            logger.info(f"[BAHS-CORE] Mantenuto file timestamp generico esistente '{dst_txt}'.")
        return dst_txt
    if src_txt.is_file() and src_txt.stat().st_size > 0:
        shutil.copy2(src_txt, dst_txt)
        if logger:
            logger.info(f"[BAHS-CORE] Sincronizzato '{src_txt}' -> '{dst_txt}'.")
    return dst_txt


def resolve_merged_orig_path(
    out_dir: Path,
    tipo_slug: str,
    fmt_slug: str,
    requested_name: str | None = None,
) -> Path | None:
    if requested_name:
        cand = out_dir / requested_name
        return cand if cand.is_file() else None
    candidates = [
        out_dir / f"{tipo_slug}_{normalize_format(fmt_slug)}_merged_orig.mp4",
        out_dir / f"merged_orig_{tipo_slug}_{normalize_format(fmt_slug)}.mp4",
    ]
    return next((p for p in candidates if p.is_file()), None)


def cleanup_temp_paths(paths: list[Path], logger: logging.Logger | None = None) -> None:
    for p in paths:
        try:
            if p.is_symlink() or p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except Exception as exc:
            if logger:
                logger.warning(f"[BAHS-CORE] Pulizia temporanea fallita per '{p}': {exc}")