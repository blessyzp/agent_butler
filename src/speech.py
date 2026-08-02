"""语音转文字 —— 本地 faster-whisper，惰性加载模型（首次用到才占内存/显存）。

默认跑在 CPU 上（int8），避免和聊天/视觉模型抢 GPU 显存；语音识别本身
计算量远小于 LLM，CPU 上也足够快。想用 GPU 可在 config.yaml 里改
speech.device: "cuda"。
"""
from __future__ import annotations

import io

from .config import Config, get_config


class Transcriber:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or get_config()
        self._model = None

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel
        size = self.cfg.get("speech.model_size", "small")
        device = self.cfg.get("speech.device", "cpu")
        compute_type = self.cfg.get("speech.compute_type", "int8")
        self._model = WhisperModel(size, device=device, compute_type=compute_type)

    def transcribe_bytes(self, audio_bytes: bytes) -> str:
        if not self.available():
            raise RuntimeError("faster-whisper 未安装（pip install faster-whisper）")
        self._ensure_loaded()
        language = self.cfg.get("speech.language") or None
        segments, _info = self._model.transcribe(io.BytesIO(audio_bytes), language=language)
        return "".join(seg.text for seg in segments).strip()
