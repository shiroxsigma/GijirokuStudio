"""Load faster-whisper with optional Windows CUDA runtime wheels."""
import os
from pathlib import Path
import sysconfig


def load_whisper_model(model_name, device, compute_type):
    if os.name == "nt" and device in ("cuda", "auto"):
        root = Path(sysconfig.get_path("platlib")) / "nvidia"
        paths = os.environ.get("PATH", "").split(os.pathsep)
        # CTranslate2 loads CUDA DLLs on demand, including during decoding.
        for component in ("cublas", "cudnn", "cuda_nvrtc"):
            directory = root / component / "bin"
            if directory.is_dir() and str(directory) not in paths:
                paths.insert(0, str(directory))
        os.environ["PATH"] = os.pathsep.join(paths)

    from faster_whisper import WhisperModel
    return WhisperModel(model_name, device=device, compute_type=compute_type)
