"""
CoralMotionDetector — fast object detection gate using Google Coral Edge TPU.

Runs a quantized MobileNet SSD v2 model on the Coral USB Accelerator to
pre-screen camera frames before expensive Ollama vision calls.

If the Coral is unavailable or the model is missing, the detector silently
disables itself and all calls return PASS (allowing the vision LLM to proceed).
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Any

import structlog

_LOGGER = structlog.get_logger()

# COCO classes that are considered "worth investigating" — everything else
# (e.g. background, sports ball, potted plant) skips the LLM call.
_CLASSES_OF_INTEREST = {
    "person", "bicycle", "car", "motorcycle", "bus", "truck",
    "dog", "cat", "horse", "sheep", "cow", "bird",
}

_EDGETPU_LIB = "/usr/lib/x86_64-linux-gnu/libedgetpu.so.1"
_DEFAULT_MODEL = Path(__file__).parent.parent.parent / "models" / "coral" / "ssd_mobilenet_v2_edgetpu.tflite"
_DEFAULT_LABELS = Path(__file__).parent.parent.parent / "models" / "coral" / "coco_labels.txt"

# Detection threshold — boxes with score below this are ignored
_SCORE_THRESHOLD = 0.40
# Input size expected by the model
_INPUT_SIZE = (300, 300)


def _load_labels(path: Path) -> dict[int, str]:
    labels: dict[int, str] = {}
    try:
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            labels[idx] = line.strip()
    except Exception:
        pass
    return labels


class CoralMotionDetector:
    """
    Wraps a Coral Edge TPU TFLite interpreter for real-time object detection.

    Usage:
        detector = CoralMotionDetector.build()
        result = await detector.check(jpeg_bytes)
        if result.skip:
            return  # Coral found nothing interesting — skip LLM
        # proceed with vision LLM using result.detections for context
    """

    class Result:
        __slots__ = ("skip", "detections", "inference_ms", "reason")

        def __init__(
            self,
            *,
            skip: bool,
            detections: list[str],
            inference_ms: float,
            reason: str,
        ) -> None:
            self.skip = skip
            self.detections = detections
            self.inference_ms = inference_ms
            self.reason = reason

        def __repr__(self) -> str:
            return (
                f"CoralResult(skip={self.skip}, detections={self.detections}, "
                f"{self.inference_ms:.1f}ms, reason={self.reason!r})"
            )

    # Sentinel returned when Coral is disabled — always PASS
    _PASS_THROUGH = None  # set after class definition

    def __init__(
        self,
        interpreter: Any,
        labels: dict[int, str],
        executor: Any = None,
    ) -> None:
        self._interp = interpreter
        self._labels = labels
        self._executor = executor
        inp = interpreter.get_input_details()[0]
        self._input_index = inp["index"]
        self._input_dtype = inp["dtype"]
        out = interpreter.get_output_details()
        # SSD outputs: [boxes, classes, scores, count]
        self._out_boxes   = out[0]["index"]
        self._out_classes = out[1]["index"]
        self._out_scores  = out[2]["index"]
        self._out_count   = out[3]["index"]
        _LOGGER.info(
            "coral.detector_ready",
            input_shape=list(inp["shape"]),
            label_count=len(labels),
        )

    @classmethod
    def build(
        cls,
        model_path: Path | str | None = None,
        label_path: Path | str | None = None,
    ) -> "CoralMotionDetector":
        """
        Attempt to load the Edge TPU delegate and model.
        Returns a no-op PassthroughDetector if unavailable.
        """
        model_path = Path(model_path) if model_path else _DEFAULT_MODEL
        label_path = Path(label_path) if label_path else _DEFAULT_LABELS

        if not model_path.exists():
            _LOGGER.warning(
                "coral.model_not_found",
                path=str(model_path),
                detail="Coral disabled — place ssd_mobilenet_v2_edgetpu.tflite in models/coral/",
            )
            return _PassthroughDetector()

        try:
            from ai_edge_litert.interpreter import Interpreter, load_delegate
        except ImportError:
            _LOGGER.warning(
                "coral.ai_edge_litert_missing",
                detail="Coral disabled — run: pip install ai-edge-litert",
            )
            return _PassthroughDetector()

        edgetpu_lib = Path(_EDGETPU_LIB)
        if not edgetpu_lib.exists():
            _LOGGER.warning(
                "coral.libedgetpu_missing",
                path=_EDGETPU_LIB,
                detail="Coral disabled — install libedgetpu1-std",
            )
            return _PassthroughDetector()

        try:
            delegate = load_delegate(str(edgetpu_lib))
        except Exception as exc:
            _LOGGER.warning(
                "coral.delegate_load_failed",
                exc=str(exc),
                detail="Coral disabled — USB device may not be connected or lacks permissions",
            )
            return _PassthroughDetector()

        try:
            interp = Interpreter(
                model_path=str(model_path),
                experimental_delegates=[delegate],
            )
            interp.allocate_tensors()
        except Exception as exc:
            _LOGGER.warning("coral.interpreter_init_failed", exc=str(exc))
            return _PassthroughDetector()

        labels = _load_labels(label_path)
        detector = cls(interp, labels)

        # Warm up with a dummy frame so the first real inference isn't slow
        try:
            import numpy as np
            dummy = np.zeros((1, _INPUT_SIZE[1], _INPUT_SIZE[0], 3), dtype=detector._input_dtype)
            interp.set_tensor(detector._input_index, dummy)
            interp.invoke()
            _LOGGER.info("coral.warmup_complete")
        except Exception as exc:
            _LOGGER.warning("coral.warmup_failed", exc=str(exc))

        return detector

    async def check(self, image_bytes: bytes, camera_id: str = "") -> "CoralMotionDetector.Result":
        """
        Run object detection on a JPEG/PNG frame.
        Returns a Result with skip=True if nothing of interest was detected.
        Runs the inference in a thread executor to avoid blocking the event loop.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            self._run_detection,
            image_bytes,
            camera_id,
        )

    def _run_detection(self, image_bytes: bytes, camera_id: str) -> "CoralMotionDetector.Result":
        import numpy as np
        from PIL import Image

        t0 = time.perf_counter()

        # Decode and resize to model input size
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            img = img.resize(_INPUT_SIZE, Image.BILINEAR)
            tensor = np.expand_dims(np.array(img, dtype=self._input_dtype), axis=0)
        except Exception as exc:
            _LOGGER.warning("coral.image_decode_failed", camera=camera_id, exc=str(exc))
            return CoralMotionDetector.Result(
                skip=False, detections=[], inference_ms=0.0, reason="decode_error"
            )

        self._interp.set_tensor(self._input_index, tensor)
        self._interp.invoke()

        classes = self._interp.get_tensor(self._out_classes)[0]
        scores  = self._interp.get_tensor(self._out_scores)[0]
        count   = int(self._interp.get_tensor(self._out_count)[0])
        elapsed_ms = (time.perf_counter() - t0) * 1000

        detections: list[str] = []
        for i in range(count):
            score = float(scores[i])
            if score < _SCORE_THRESHOLD:
                continue
            label = self._labels.get(int(classes[i]) + 1, "unknown").lower()
            if label in _CLASSES_OF_INTEREST:
                detections.append(f"{label}({score:.0%})")

        if detections:
            _LOGGER.info(
                "coral.detection",
                camera=camera_id,
                detections=detections,
                inference_ms=round(elapsed_ms, 1),
            )
            return CoralMotionDetector.Result(
                skip=False,
                detections=detections,
                inference_ms=elapsed_ms,
                reason="detected",
            )

        _LOGGER.info(
            "coral.no_detection",
            camera=camera_id,
            inference_ms=round(elapsed_ms, 1),
            detail="skipping Ollama vision call",
        )
        return CoralMotionDetector.Result(
            skip=True,
            detections=[],
            inference_ms=elapsed_ms,
            reason="nothing_of_interest",
        )

    @property
    def enabled(self) -> bool:
        return True


class _PassthroughDetector:
    """No-op detector used when Coral is unavailable. Always returns PASS."""

    async def check(self, image_bytes: bytes, camera_id: str = "") -> "CoralMotionDetector.Result":
        return CoralMotionDetector.Result(
            skip=False, detections=[], inference_ms=0.0, reason="coral_disabled"
        )

    @property
    def enabled(self) -> bool:
        return False
