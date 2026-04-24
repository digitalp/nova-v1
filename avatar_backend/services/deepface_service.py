import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict

import structlog

_LOGGER = structlog.get_logger(__name__)

class DeepFaceService:
    def __init__(self, deepface_home: str = "/mnt/data/deepface_models"):
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._deepface_home = deepface_home
        os.environ["DEEPFACE_HOME"] = deepface_home
        self._ready = False
        self._model_name = "ArcFace"
        self._detector_backend = "mtcnn"
        self._actions = ["emotion", "age", "gender"]
        self._align = True
        self._anti_spoofing = False
        self._expand_percentage = 0
        self._enforce_detection = False
        self._use_gpu = False
        self._preprocess_training = True
        # Force CPU by default; set before TF is imported
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    async def analyze(self, img_path: str) -> Dict[str, Any]:
        """Runs emotion/age/gender analysis in background thread."""
        loop = asyncio.get_event_loop()
        try:
            start_t = time.perf_counter()
            result = await loop.run_in_executor(self._executor, self._sync_analyze, img_path)
            elapsed = (time.perf_counter() - start_t) * 1000
            _LOGGER.info("deepface.analyzed", elapsed_ms=int(elapsed), path=img_path)
            return result
        except Exception as exc:
            _LOGGER.warning("deepface.analyze_failed", exc=str(exc))
            return {}

    def _sync_analyze(self, img_path: str) -> Dict[str, Any]:
        # Deferred import to prevent startup blocking
        from deepface import DeepFace
        
        objs = DeepFace.analyze(
            img_path=img_path,
            actions=tuple(self._actions) if self._actions else ("emotion", "age", "gender"),
            enforce_detection=self._enforce_detection,
            detector_backend=self._detector_backend,
            align=self._align,
            expand_percentage=self._expand_percentage,
            anti_spoofing=self._anti_spoofing,
            silent=True,
        )
        if not objs:
            return {}
        
        # Take largest face
        res = objs[0]
        return {
            "emotion": res.get("dominant_emotion"),
            "age": int(res.get("age", 0)),
            "gender": res.get("dominant_gender"),
            "region": res.get("region"),
        }

    def preprocess_for_training(self, image_bytes: bytes) -> bytes | None:
        """
        Detect, align and crop the dominant face from image_bytes.
        Returns JPEG bytes of the aligned face (≥160px) ready for CPAI,
        or None if no face was detected.
        """
        import io
        import numpy as np
        try:
            from deepface import DeepFace
            from PIL import Image
            results = DeepFace.extract_faces(
                img_path=io.BytesIO(image_bytes),
                detector_backend=self._detector_backend,
                enforce_detection=True,
                align=self._align,
                expand_percentage=max(self._expand_percentage, 10),
                anti_spoofing=self._anti_spoofing,
                normalize_face=False,
            )
            if not results:
                return None
            # Pick largest face by area
            best = max(results, key=lambda r: r['facial_area']['w'] * r['facial_area']['h'])
            face_arr = best['face']  # uint8 RGB numpy array
            if face_arr.dtype != np.uint8:
                face_arr = (face_arr * 255).clip(0, 255).astype(np.uint8)
            img = Image.fromarray(face_arr, 'RGB')
            # Ensure CPAI gets at least 160x160
            if img.width < 160 or img.height < 160:
                scale = max(160 / img.width, 160 / img.height)
                img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=92)
            _LOGGER.info('deepface.preprocess_ok',
                         size=f'{img.width}x{img.height}',
                         confidence=round(best.get('confidence', 0), 2))
            return buf.getvalue()
        except Exception as exc:
            _LOGGER.warning('deepface.preprocess_failed', exc=str(exc)[:120])
            return None

    def _apply_device(self):
        """Set CUDA_VISIBLE_DEVICES before TF imports."""
        if self._use_gpu:
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            os.environ["CUDA_VISIBLE_DEVICES"] = ""

    def warmup(self):
        """Pre-load models."""
        self._apply_device()
        self._executor.submit(self._sync_warmup)

    def _sync_warmup(self):
        try:
            from deepface import DeepFace
            _LOGGER.info("deepface.warming_up", detector=self._detector_backend, model=self._model_name)
            # Just trigger imports and basic load
            DeepFace.build_model(self._model_name)
            self._ready = True
            _LOGGER.info("deepface.ready")
        except Exception as exc:
            _LOGGER.warning("deepface.warmup_failed", exc=str(exc))

