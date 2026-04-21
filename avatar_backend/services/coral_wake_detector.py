"""
CoralWakeDetector — 4-stage wake word detection pipeline.

Stage 1 — Coral Edge TPU TFLite (if nova_wakeword_edgetpu.tflite is present)
           ~1-3ms on device. Fires when a trained "Nova" keyword model is available.
           Train via the admin panel → Settings → Train Wake Word Model.

Stage 1b — CPU TFLite (if nova_wakeword.tflite is present but no Edge TPU model)
            ~3-8ms on CPU. Same model architecture, just not compiled for Edge TPU.

Stage 2 — Verifier model (if nova_verifier.pkl is present)
           ~2ms cosine-similarity check against a trained centroid.
           Fast pre-filter before Whisper.

Stage 3 — openWakeWord Silero VAD (CPU, ~14ms per second of audio)
           Detects whether the audio chunk contains speech at all.
           Eliminates ~90% of Whisper calls (silent/ambient audio is discarded).

Stage 4 — Whisper transcribe_wake (GPU/CPU, ~150-400ms)
           Only called when VAD confirms actual speech.
           Checks transcript against _WAKE_VARIANTS ("nova", "noah", etc.).
           This is the existing Whisper path, now used as a targeted fallback.

Result includes 'method' field: "coral" | "tflite_cpu" | "verifier" |
"whisper_after_vad" | "whisper_direct" | "vad_silence"
so you can see in logs which stage fired the wake event.
"""
from __future__ import annotations

import asyncio
import io
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

import numpy as np
import structlog

_LOGGER = structlog.get_logger()

_EDGETPU_LIB   = "/usr/lib/x86_64-linux-gnu/libedgetpu.so.1"
_MODEL_DIR     = Path(__file__).parent.parent.parent / "models" / "coral"
_CORAL_MODEL   = _MODEL_DIR / "nova_wakeword_edgetpu.tflite"
_CPU_MODEL     = _MODEL_DIR / "nova_wakeword.tflite"
_NUMPY_MODEL   = _MODEL_DIR / "nova_wakeword_weights.npz"
_VERIFIER_PATH = _MODEL_DIR / "nova_verifier.pkl"

# VAD threshold — audio is passed to Whisper only if any 30ms frame exceeds this
_VAD_THRESHOLD = 0.4
# Coral/TFLite confidence — scores above this are treated as a confirmed wake
_CORAL_THRESHOLD = 0.85
# Verifier cosine similarity threshold (overridden by trained model's own threshold)
_VERIFIER_THRESHOLD = 0.65
# Frame size expected by Silero VAD (30ms at 16 kHz)
_VAD_FRAME_SIZE = 480
_SAMPLE_RATE    = 16000


@dataclass
class WakeResult:
    wake:        bool
    transcript:  str   = ""
    method:      str   = ""   # coral | tflite_cpu | verifier | whisper_after_vad | whisper_direct | vad_silence
    score:       float = 0.0
    elapsed_ms:  float = 0.0


class CoralWakeDetector:
    """
    Drop-in replacement for the raw Whisper /stt/wake path.

    Usage:
        detector = CoralWakeDetector.build(stt_service)
        result = await detector.detect(raw_audio_bytes)
    """

    def __init__(
        self,
        stt,                            # STTService with .transcribe_wake()
        is_wake_word_fn: Callable[[str], bool],
        coral_interp=None,              # ai_edge_litert Interpreter on Edge TPU (optional)
        cpu_interp=None,                # ai_edge_litert Interpreter on CPU (optional)
        numpy_model=None,               # dict with W1,b1,W2,b2 numpy weights (optional)
        verifier=None,                  # dict with centroid + threshold (optional)
        vad=None,                       # openwakeword VAD (optional)
        executor=None,
    ) -> None:
        self._stt            = stt
        self._is_wake_word   = is_wake_word_fn
        self._coral          = coral_interp
        self._cpu_tflite     = cpu_interp
        self._numpy_model    = numpy_model
        self._verifier       = verifier
        self._vad            = vad
        self._executor       = executor

        if coral_interp is not None:
            _LOGGER.info("coral_wake.coral_active",
                         detail="Coral Edge TPU wake word model loaded")
        if cpu_interp is not None:
            _LOGGER.info("coral_wake.cpu_tflite_active",
                         detail="CPU TFLite wake word model loaded (~3-8ms)")
        if numpy_model is not None:
            _LOGGER.info("coral_wake.numpy_model_active",
                         detail="Numpy classifier model loaded (~3-5ms)")
        if verifier is not None:
            _LOGGER.info("coral_wake.verifier_active",
                         detail="Cosine-similarity verifier loaded (~2ms)")
        if vad is not None:
            _LOGGER.info("coral_wake.vad_active",
                         detail="Silero VAD gate active — Whisper skipped on silent chunks")

    @classmethod
    def build(cls, stt, is_wake_word_fn: Callable[[str], bool]) -> "CoralWakeDetector":
        coral_interp = cls._try_load_coral()
        cpu_interp = cls._try_load_cpu_tflite() if coral_interp is None else None
        numpy_model = cls._try_load_numpy_model() if (coral_interp is None and cpu_interp is None) else None
        verifier = cls._try_load_verifier()
        vad = cls._try_load_vad()
        return cls(stt, is_wake_word_fn,
                   coral_interp=coral_interp,
                   cpu_interp=cpu_interp,
                   numpy_model=numpy_model,
                   verifier=verifier,
                   vad=vad)


    # ── Stage 1: Coral Edge TPU ───────────────────────────────────────────────

    @staticmethod
    def _try_load_coral():
        if not _CORAL_MODEL.exists():
            _LOGGER.info(
                "coral_wake.no_edgetpu_model",
                path=str(_CORAL_MODEL),
                detail="Coral Edge TPU wake word skipped — train a model via admin Settings",
            )
            return None
        try:
            from ai_edge_litert.interpreter import Interpreter, load_delegate
            delegate = load_delegate(_EDGETPU_LIB)
            interp = Interpreter(model_path=str(_CORAL_MODEL),
                                 experimental_delegates=[delegate])
            interp.allocate_tensors()
            # Test invoke to catch runtime failures early
            import numpy as np
            inp = interp.get_input_details()
            interp.set_tensor(inp[0]["index"], np.zeros(inp[0]["shape"], dtype=inp[0]["dtype"]))
            interp.invoke()
            _LOGGER.info("coral_wake.coral_ready", model=str(_CORAL_MODEL))
            return interp
        except Exception as exc:
            _LOGGER.warning("coral_wake.coral_load_failed", exc=str(exc))
            return None

    # ── Stage 1b: CPU TFLite ──────────────────────────────────────────────────

    @staticmethod
    def _try_load_cpu_tflite():
        if not _CPU_MODEL.exists():
            _LOGGER.info(
                "coral_wake.no_cpu_model",
                path=str(_CPU_MODEL),
                detail="CPU TFLite wake word skipped — train a model via admin Settings",
            )
            return None
        try:
            from ai_edge_litert.interpreter import Interpreter
            interp = Interpreter(model_path=str(_CPU_MODEL))
            interp.allocate_tensors()
            # Test invoke to catch runtime failures early
            import numpy as np
            inp = interp.get_input_details()
            interp.set_tensor(inp[0]["index"], np.zeros(inp[0]["shape"], dtype=inp[0]["dtype"]))
            interp.invoke()
            _LOGGER.info("coral_wake.cpu_tflite_ready", model=str(_CPU_MODEL))
            return interp
        except Exception as exc:
            _LOGGER.warning("coral_wake.cpu_tflite_load_failed", exc=str(exc))
            return None

    # ── Stage 1c: Numpy model ─────────────────────────────────────────────────

    @staticmethod
    def _try_load_numpy_model():
        if not _NUMPY_MODEL.exists():
            return None
        try:
            data = np.load(str(_NUMPY_MODEL))
            model = {
                "W1": data["W1"],
                "b1": data["b1"],
                "W2": data["W2"],
                "b2": data["b2"],
            }
            _LOGGER.info("coral_wake.numpy_model_ready", path=str(_NUMPY_MODEL))
            return model
        except Exception as exc:
            _LOGGER.warning("coral_wake.numpy_model_load_failed", exc=str(exc))
            return None

    # ── Stage 2: Verifier ─────────────────────────────────────────────────────

    @staticmethod
    def _try_load_verifier():
        if not _VERIFIER_PATH.exists():
            return None
        try:
            import pickle
            with open(_VERIFIER_PATH, "rb") as f:
                data = pickle.load(f)
            if isinstance(data, dict) and "centroid" in data:
                _LOGGER.info("coral_wake.verifier_ready",
                             threshold=data.get("threshold", _VERIFIER_THRESHOLD),
                             wake_word=data.get("wake_word", "unknown"))
                return data
            return None
        except Exception as exc:
            _LOGGER.warning("coral_wake.verifier_load_failed", exc=str(exc))
            return None

    # ── Stage 3: Silero VAD ───────────────────────────────────────────────────

    @staticmethod
    def _try_load_vad():
        try:
            from openwakeword.vad import VAD
            vad = VAD()
            # Warm up
            silence = np.zeros(_VAD_FRAME_SIZE, dtype=np.float32)
            vad.predict(silence)
            _LOGGER.info("coral_wake.vad_ready", frame_ms=30, threshold=_VAD_THRESHOLD)
            return vad
        except Exception as exc:
            _LOGGER.warning("coral_wake.vad_load_failed", exc=str(exc),
                            detail="Whisper will run on all audio chunks")
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    async def detect(self, audio_bytes: bytes) -> WakeResult:
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()

        # Stage 1: Coral Edge TPU TFLite keyword model
        if self._coral is not None:
            result = await loop.run_in_executor(
                self._executor, self._run_tflite, audio_bytes, self._coral, "coral"
            )
            if result is not None and result.wake:
                result.elapsed_ms = (time.perf_counter() - t0) * 1000
                _LOGGER.info(
                    "coral_wake.result",
                    wake=result.wake, method=result.method,
                    score=round(result.score, 3),
                    elapsed_ms=round(result.elapsed_ms, 1),
                )
                return result

        # Stage 1b: CPU TFLite keyword model (fallback when no Edge TPU model)
        if self._cpu_tflite is not None:
            result = await loop.run_in_executor(
                self._executor, self._run_tflite, audio_bytes, self._cpu_tflite, "tflite_cpu"
            )
            if result is not None and result.wake:
                result.elapsed_ms = (time.perf_counter() - t0) * 1000
                _LOGGER.info(
                    "coral_wake.result",
                    wake=result.wake, method=result.method,
                    score=round(result.score, 3),
                    elapsed_ms=round(result.elapsed_ms, 1),
                )
                return result

        # Stage 1c: Numpy classifier model (fallback when no TFLite at all)
        if self._numpy_model is not None:
            result = await loop.run_in_executor(
                self._executor, self._run_numpy_model, audio_bytes
            )
            if result is not None and result.wake:
                result.elapsed_ms = (time.perf_counter() - t0) * 1000
                _LOGGER.info(
                    "coral_wake.result",
                    wake=result.wake, method=result.method,
                    score=round(result.score, 3),
                    elapsed_ms=round(result.elapsed_ms, 1),
                )
                return result

        # Stage 2: Verifier (cosine similarity against trained centroid)
        if self._verifier is not None:
            result = await loop.run_in_executor(
                self._executor, self._run_verifier, audio_bytes
            )
            if result is not None:
                result.elapsed_ms = (time.perf_counter() - t0) * 1000
                _LOGGER.info(
                    "coral_wake.verifier_check",
                    wake=result.wake, score=round(result.score, 3),
                    elapsed_ms=round(result.elapsed_ms, 1),
                )
                # Verifier is a pre-filter: if it says YES, trust it.
                # If it says NO, still fall through to VAD+Whisper for safety.
                if result.wake:
                    return result

        # Stage 3: VAD gate — SKIPPED for wake word checks.
        # The client already performs energy detection before sending audio to
        # /stt/wake, so the audio reaching this point has confirmed speech energy.
        # The Silero VAD was rejecting valid speech due to WebM encoding artifacts
        # and rolling-buffer timing, causing intermittent wake word failures.
        # Whisper is the reliable fallback — let it always run.

        # Stage 4: Whisper (fallback — always runs if pre-filters didn't confirm wake)
        method = "whisper_fallback"
        try:
            transcript = await self._stt.transcribe_wake(audio_bytes)
        except Exception as exc:
            _LOGGER.warning("coral_wake.whisper_failed", exc=str(exc))
            return WakeResult(wake=False, method=method,
                              elapsed_ms=(time.perf_counter() - t0) * 1000)

        wake = self._is_wake_word(transcript)
        elapsed = (time.perf_counter() - t0) * 1000
        _LOGGER.info(
            "coral_wake.result",
            wake=wake, method=method,
            transcript=transcript[:60],
            elapsed_ms=round(elapsed, 1),
        )
        return WakeResult(wake=wake, transcript=transcript,
                          method=method, elapsed_ms=elapsed)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_tflite(self, audio_bytes: bytes, interp, method_name: str):
        """Run a TFLite wake word model synchronously (Edge TPU or CPU).

        The model was trained on 128-bin normalized FFT magnitude features,
        so we must extract the same features from the raw audio before inference.
        """
        try:
            pcm = _bytes_to_pcm_f32(audio_bytes)

            # Extract the same 128-bin FFT features used during training
            target_len = 16000  # 1 second at 16kHz
            if len(pcm) < target_len:
                pcm = np.pad(pcm, (0, target_len - len(pcm)))
            else:
                pcm = pcm[:target_len]

            fft = np.abs(np.fft.rfft(pcm))
            bin_size = max(1, len(fft) // 128)
            features = np.array(
                [fft[i * bin_size:(i + 1) * bin_size].mean() for i in range(128)],
                dtype=np.float32,
            )
            norm = np.linalg.norm(features)
            if norm > 0:
                features = features / norm

            inp = interp.get_input_details()[0]
            tensor = features.reshape(inp["shape"])

            # Quantize float32 → int8 if the model expects quantized input
            if inp["dtype"] == np.int8:
                qp = inp.get("quantization_parameters", {})
                scale = qp.get("scales", [1.0])[0]
                zp = qp.get("zero_points", [0])[0]
                tensor = np.clip(np.round(tensor / scale + zp), -128, 127).astype(np.int8)
            else:
                tensor = tensor.astype(inp["dtype"])

            interp.set_tensor(inp["index"], tensor)
            interp.invoke()

            out = interp.get_tensor(interp.get_output_details()[0]["index"]).copy()
            out_details = interp.get_output_details()[0]

            # Dequantize if the model uses int8 quantization
            if out_details["dtype"] == np.int8:
                scale = out_details["quantization_parameters"]["scales"]
                zero_point = out_details["quantization_parameters"]["zero_points"]
                out = (out.astype(np.float32) - zero_point) * scale

            # Handle both single-output (sigmoid) and multi-output (softmax) models
            if out.size == 1:
                score = float(out.flat[0])
            else:
                # Class 1 = wake word
                score = float(out.flat[1]) if out.size >= 2 else float(out.flat[0])

            wake = score >= _CORAL_THRESHOLD
            _LOGGER.info(f"coral_wake.{method_name}_score", score=round(score, 3), wake=wake)
            return WakeResult(wake=wake, method=method_name, score=score)
        except Exception as exc:
            _LOGGER.warning(f"coral_wake.{method_name}_infer_failed", exc=str(exc))
            return None  # fall through to next stage

    def _run_verifier(self, audio_bytes: bytes):
        """Run the cosine-similarity verifier synchronously."""
        try:
            pcm = _bytes_to_pcm_f32(audio_bytes)
            centroid = np.array(self._verifier["centroid"], dtype=np.float32)
            threshold = float(self._verifier.get("threshold", _VERIFIER_THRESHOLD))

            # Extract same features as training: 128-bin FFT magnitude
            # Normalize to 1 second (16000 samples)
            target_len = 16000
            if len(pcm) < target_len:
                pcm = np.pad(pcm, (0, target_len - len(pcm)))
            else:
                pcm = pcm[:target_len]

            fft = np.abs(np.fft.rfft(pcm))
            bin_size = len(fft) // 128
            if bin_size < 1:
                return None
            features = np.array([fft[i * bin_size:(i + 1) * bin_size].mean() for i in range(128)])
            norm = np.linalg.norm(features)
            if norm > 0:
                features = features / norm

            score = float(np.dot(features, centroid))
            wake = score >= threshold
            return WakeResult(wake=wake, method="verifier", score=score)
        except Exception as exc:
            _LOGGER.warning("coral_wake.verifier_infer_failed", exc=str(exc))
            return None

    def _run_vad(self, audio_bytes: bytes) -> bool:
        """Return True if the audio contains speech above VAD_THRESHOLD."""
        try:
            pcm = _bytes_to_pcm_f32(audio_bytes)
            for i in range(0, len(pcm), _VAD_FRAME_SIZE):
                frame = pcm[i : i + _VAD_FRAME_SIZE]
                if len(frame) < _VAD_FRAME_SIZE:
                    break
                score = float(self._vad.predict(frame))
                if score >= _VAD_THRESHOLD:
                    _LOGGER.debug("coral_wake.vad_speech_detected",
                                  score=round(score, 3))
                    return True
            return False
        except Exception as exc:
            _LOGGER.warning("coral_wake.vad_error", exc=str(exc))
            return True  # fail-safe: pass through to Whisper

    def _run_numpy_model(self, audio_bytes: bytes):
        """Run the numpy classifier model synchronously (~3-5ms)."""
        try:
            pcm = _bytes_to_pcm_f32(audio_bytes)
            # Extract same 128-bin FFT features as training
            target_len = 16000
            if len(pcm) < target_len:
                pcm = np.pad(pcm, (0, target_len - len(pcm)))
            else:
                pcm = pcm[:target_len]

            fft = np.abs(np.fft.rfft(pcm))
            bin_size = max(1, len(fft) // 128)
            features = np.array([fft[i * bin_size:(i + 1) * bin_size].mean() for i in range(128)], dtype=np.float32)
            norm = np.linalg.norm(features)
            if norm > 0:
                features = features / norm

            # Forward pass: input(128) → Dense(64, relu) → Dense(2, softmax)
            W1 = self._numpy_model["W1"]
            b1 = self._numpy_model["b1"]
            W2 = self._numpy_model["W2"]
            b2 = self._numpy_model["b2"]

            h = np.maximum(0, features @ W1 + b1)  # relu
            logits = h @ W2 + b2
            # softmax
            e = np.exp(logits - logits.max())
            probs = e / e.sum()

            score = float(probs[1])  # class 1 = wake word
            wake = score >= _CORAL_THRESHOLD
            _LOGGER.info("coral_wake.numpy_score", score=round(score, 3), wake=wake)
            return WakeResult(wake=wake, method="numpy_classifier", score=score)
        except Exception as exc:
            _LOGGER.warning("coral_wake.numpy_infer_failed", exc=str(exc))
            return None

    @property
    def coral_available(self) -> bool:
        return self._coral is not None

    @property
    def cpu_tflite_available(self) -> bool:
        return self._cpu_tflite is not None

    @property
    def numpy_model_available(self) -> bool:
        return self._numpy_model is not None

    @property
    def vad_available(self) -> bool:
        return self._vad is not None

    @property
    def verifier_available(self) -> bool:
        return self._verifier is not None

    def describe_pipeline(self) -> list[str]:
        """Return a list of active pipeline stages."""
        stages = []
        if self._coral is not None:
            stages.append("coral_tflite")
        if self._cpu_tflite is not None:
            stages.append("cpu_tflite")
        if self._numpy_model is not None:
            stages.append("numpy_classifier")
        if self._verifier is not None:
            stages.append("verifier_model")
        stages.append("whisper_fallback")
        return stages

    def reload_verifier(self) -> None:
        """Reload the verifier model from disk (called after training)."""
        self._verifier = self._try_load_verifier()

    def reload_tflite(self) -> None:
        """Reload TFLite and numpy models from disk (called after training)."""
        new_coral = self._try_load_coral()
        if new_coral is not None:
            self._coral = new_coral
            self._cpu_tflite = None
            self._numpy_model = None
            _LOGGER.info("coral_wake.coral_reloaded")
            return
        new_cpu = self._try_load_cpu_tflite()
        if new_cpu is not None:
            self._cpu_tflite = new_cpu
            self._numpy_model = None
            _LOGGER.info("coral_wake.cpu_tflite_reloaded")
            return
        new_numpy = self._try_load_numpy_model()
        if new_numpy is not None:
            self._numpy_model = new_numpy
            _LOGGER.info("coral_wake.numpy_model_reloaded")


def _bytes_to_pcm_f32(audio_bytes: bytes) -> np.ndarray:
    """Convert raw bytes (PCM16, WAV, or WebM/Opus) to float32 PCM at 16kHz."""
    # WAV
    if audio_bytes[:4] == b"RIFF":
        import wave
        with wave.open(io.BytesIO(audio_bytes)) as wf:
            raw = wf.readframes(wf.getnframes())
            pcm16 = np.frombuffer(raw, dtype=np.int16)
            pcm = pcm16.astype(np.float32) / 32768.0
            if wf.getframerate() != _SAMPLE_RATE:
                pcm = _resample_simple(pcm, wf.getframerate(), _SAMPLE_RATE)
            return pcm

    # WebM / OGG / any container — decode via PyAV
    try:
        import av
        container = av.open(io.BytesIO(audio_bytes))
        audio_stream = next((s for s in container.streams if s.type == "audio"), None)
        if audio_stream is not None:
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=_SAMPLE_RATE)
            frames = []
            for frame in container.decode(audio_stream):
                for rf in resampler.resample(frame):
                    frames.append(rf.to_ndarray().flatten())
            container.close()
            if frames:
                return np.concatenate(frames).astype(np.float32)
        container.close()
    except Exception:
        pass

    # Fallback: raw PCM16
    raw = audio_bytes
    if len(raw) % 2 != 0:
        raw = raw[:-1]
    pcm16 = np.frombuffer(raw, dtype=np.int16)
    return pcm16.astype(np.float32) / 32768.0


def _resample_simple(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Simple linear resampling."""
    if src_rate == dst_rate:
        return pcm
    ratio = dst_rate / src_rate
    n = int(len(pcm) * ratio)
    indices = np.linspace(0, len(pcm) - 1, n)
    return np.interp(indices, np.arange(len(pcm)), pcm).astype(np.float32)
