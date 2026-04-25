"""Coral wake word endpoints: status, training, Edge TPU compiler install."""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from avatar_backend.bootstrap.container import AppContainer, get_container
from avatar_backend.runtime_paths import install_dir

from .common import _get_session, _require_session

router = APIRouter()

@router.get("/coral/wake-status")
async def coral_wake_status(request: Request, container: AppContainer = Depends(get_container)):
    """Return current Coral wake detector status."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    detector = getattr(container, "coral_wake_detector", None)
    model_dir = Path(install_dir()) / "models" / "coral"
    edgetpu_path = model_dir / "nova_wakeword_edgetpu.tflite"
    cpu_path = model_dir / "nova_wakeword.tflite"
    verifier_path = model_dir / "nova_verifier.pkl"
    return {
        "coral_available": detector.coral_available if detector else False,
        "cpu_tflite_available": detector.cpu_tflite_available if detector else False,
        "numpy_model_available": detector.numpy_model_available if detector else False,
        "vad_available": detector.vad_available if detector else False,
        "verifier_available": detector.verifier_available if detector else False,
        "coral_model_exists": edgetpu_path.exists(),
        "cpu_model_exists": cpu_path.exists(),
        "verifier_model_exists": verifier_path.exists(),
        "pipeline_stages": detector.describe_pipeline() if detector else [],
        "edgetpu_compiler_available": _check_edgetpu_compiler(),
    }


def _check_edgetpu_compiler() -> bool:
    """Check if edgetpu_compiler is installed."""
    import shutil
    return shutil.which("edgetpu_compiler") is not None


def _build_quantized_tflite(W1, b1, W2, b2, calibration_data) -> bytes | None:
    """Build a TFLite model from numpy weights.

    Tries TensorFlow first for proper TFLite conversion.
    Falls back to saving a numpy model archive that the detector can load directly.
    """
    try:
        import tensorflow as tf

        model = tf.keras.Sequential([
            tf.keras.layers.InputLayer(input_shape=(128,)),
            tf.keras.layers.Dense(64, activation="relu"),
            tf.keras.layers.Dense(2, activation="softmax"),
        ])
        model.layers[0].set_weights([W1, b1])
        model.layers[1].set_weights([W2, b2])

        converter = tf.lite.TFLiteConverter.from_keras_model(model)
        converter.optimizations = [tf.lite.Optimize.DEFAULT]

        def _representative_dataset():
            for sample in calibration_data:
                yield [sample.reshape(1, 128).astype(np.float32)]

        converter.representative_dataset = _representative_dataset
        try:
            converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
            converter.inference_input_type = tf.int8
            converter.inference_output_type = tf.int8
            return converter.convert()
        except Exception:
            converter2 = tf.lite.TFLiteConverter.from_keras_model(model)
            converter2.optimizations = [tf.lite.Optimize.DEFAULT]
            return converter2.convert()
    except ImportError:
        pass
    except Exception:
        pass

    return None


def _save_numpy_model(model_dir, W1, b1, W2, b2) -> bool:
    """Save trained weights as a numpy archive for the detector to load."""
    try:
        path = model_dir / "nova_wakeword_weights.npz"
        np.savez(path, W1=W1, b1=b1, W2=W2, b2=b2)
        return True
    except Exception:
        return False


@router.post("/coral/train-wakeword")
async def train_wakeword(request: Request, container: AppContainer = Depends(get_container)):
    """Stream-train wake word models: verifier + TFLite (+ Edge TPU if compiler available)."""
    _require_session(request, min_role="admin")
    import json as _json_ww

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    wake_word = str(body.get("wake_word", "Nova")).strip() or "Nova"

    async def _generate():
        def _emit(stage: str, progress: int, message: str):
            payload = {"stage": stage, "progress": progress, "message": message}
            return f"data: {_json_ww.dumps(payload)}\n\n"

        yield _emit("init", 0, f"Training wake word models for '{wake_word}'...")

        model_dir = Path(install_dir()) / "models" / "coral"
        model_dir.mkdir(parents=True, exist_ok=True)
        verifier_path = model_dir / "nova_verifier.pkl"
        tflite_path = model_dir / "nova_wakeword.tflite"
        edgetpu_path = model_dir / "nova_wakeword_edgetpu.tflite"

        # ── Stage 1: Generate positive audio samples ──────────────────────────
        yield _emit("generate", 5, f"Generating synthetic '{wake_word}' audio samples via Piper TTS...")

        tts = getattr(container, "tts_service", None)
        if tts is None:
            yield _emit("error", 0, "TTS service not available — check Piper configuration")
            return

        import wave as _wave
        import io as _io

        positive_phrases = [
            wake_word,
            wake_word.lower(),
            f"Hey {wake_word}",
            f"hey {wake_word}",
            f"{wake_word}!",
            f"{wake_word}?",
            f"{wake_word}.",
            f"Hey {wake_word}!",
            f"OK {wake_word}",
            f"ok {wake_word}",
            f"Excuse me {wake_word}",
            f"{wake_word}, hello",
        ]
        negative_phrases = [
            "Hello there",
            "What time is it",
            "Turn on the lights",
            "Good morning",
            "Play some music",
            "Set a timer",
            "How is the weather",
            "Open the door",
            "Thank you very much",
            "I need help",
            "What is happening",
            "Stop playing",
        ]

        def _wav_to_pcm16k(wav_data: bytes) -> np.ndarray:
            with _io.BytesIO(wav_data) as buf:
                with _wave.open(buf, "rb") as wf:
                    sr = wf.getframerate()
                    frames = wf.readframes(wf.getnframes())
                    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            if sr != 16000 and sr > 0:
                ratio = 16000 / sr
                new_len = int(len(audio) * ratio)
                indices = np.linspace(0, len(audio) - 1, new_len).astype(int)
                audio = audio[indices]
            target_len = 16000  # 1 second at 16kHz
            if len(audio) < target_len:
                audio = np.pad(audio, (0, target_len - len(audio)))
            else:
                audio = audio[:target_len]
            return audio

        def _extract_features(audio: np.ndarray) -> np.ndarray:
            fft = np.abs(np.fft.rfft(audio))
            bin_size = max(1, len(fft) // 128)
            features = np.array([fft[i * bin_size:(i + 1) * bin_size].mean() for i in range(128)])
            norm = np.linalg.norm(features)
            if norm > 0:
                features = features / norm
            return features

        positive_audio = []
        positive_features = []
        total_phrases = len(positive_phrases) + len(negative_phrases)

        for i, phrase in enumerate(positive_phrases):
            try:
                wav_bytes, _ = await tts.synthesise_with_timing(phrase)
                audio = _wav_to_pcm16k(wav_bytes)
                positive_audio.append(audio)
                positive_features.append(_extract_features(audio))
                pct = 5 + int((i / total_phrases) * 30)
                yield _emit("generate", pct, f"Positive {i+1}/{len(positive_phrases)}: '{phrase}'")
            except Exception as exc:
                yield _emit("warn", 5, f"Skipped '{phrase}': {exc}")

        if len(positive_audio) < 3:
            yield _emit("error", 0, f"Only {len(positive_audio)} positive samples — need at least 3")
            return

        # ── Stage 2: Generate negative audio samples ──────────────────────────
        yield _emit("generate", 35, "Generating negative (non-wake) samples...")

        negative_audio = []
        negative_features = []
        for i, phrase in enumerate(negative_phrases):
            try:
                wav_bytes, _ = await tts.synthesise_with_timing(phrase)
                audio = _wav_to_pcm16k(wav_bytes)
                negative_audio.append(audio)
                negative_features.append(_extract_features(audio))
                pct = 35 + int((i / len(negative_phrases)) * 10)
                yield _emit("generate", pct, f"Negative {i+1}/{len(negative_phrases)}: '{phrase}'")
            except Exception as exc:
                yield _emit("warn", 35, f"Skipped negative '{phrase}': {exc}")

        # Add silence as negative
        silence = np.zeros(16000, dtype=np.float32)
        negative_audio.append(silence)
        negative_features.append(_extract_features(silence))
        # Add noise as negative
        noise = np.random.randn(16000).astype(np.float32) * 0.01
        negative_audio.append(noise)
        negative_features.append(_extract_features(noise))

        yield _emit("generate", 45, f"Generated {len(positive_audio)} positive + {len(negative_audio)} negative samples")

        positive_features_arr = np.array(positive_features, dtype=np.float32)
        negative_features_arr = np.array(negative_features, dtype=np.float32)

        # ── Stage 3: Train verifier (centroid + threshold) ────────────────────
        yield _emit("train_verifier", 48, "Building cosine-similarity verifier...")

        try:
            import pickle

            centroid = positive_features_arr.mean(axis=0)
            centroid_norm = np.linalg.norm(centroid)
            if centroid_norm > 0:
                centroid = centroid / centroid_norm

            pos_sims = np.array([np.dot(f, centroid) for f in positive_features_arr])
            neg_sims = np.array([np.dot(f, centroid) for f in negative_features_arr])
            mean_pos = float(pos_sims.mean())
            min_pos = float(pos_sims.min())
            max_neg = float(neg_sims.max())

            threshold = max(0.3, (min_pos + max_neg) / 2.0)

            verifier_data = {
                "wake_word": wake_word,
                "centroid": centroid.tolist(),
                "threshold": threshold,
                "mean_similarity": mean_pos,
                "min_similarity": min_pos,
                "max_negative_similarity": max_neg,
                "num_positive": len(positive_features_arr),
                "num_negative": len(negative_features_arr),
                "feature_dim": 128,
                "trained_at": __import__("datetime").datetime.now().isoformat(),
            }
            with open(verifier_path, "wb") as f:
                pickle.dump(verifier_data, f)

            yield _emit("train_verifier", 55, f"Verifier saved — threshold={threshold:.3f}, pos_mean={mean_pos:.3f}, neg_max={max_neg:.3f}")
        except Exception as exc:
            yield _emit("error", 0, f"Verifier training failed: {exc}")
            return

        # ── Stage 4: Build TFLite classification model ────────────────────────
        yield _emit("train_tflite", 58, "Building TFLite keyword classification model...")

        tflite_built = False
        final_acc = 0.0
        try:
            X = np.vstack([positive_features_arr, negative_features_arr])
            y = np.array([1] * len(positive_features_arr) + [0] * len(negative_features_arr), dtype=np.float32)

            np.random.seed(42)
            W1 = np.random.randn(128, 64).astype(np.float32) * 0.1
            b1 = np.zeros(64, dtype=np.float32)
            W2 = np.random.randn(64, 2).astype(np.float32) * 0.1
            b2 = np.zeros(2, dtype=np.float32)

            def _relu(x):
                return np.maximum(0, x)

            def _softmax(x):
                e = np.exp(x - x.max(axis=-1, keepdims=True))
                return e / e.sum(axis=-1, keepdims=True)

            lr = 0.05
            n_epochs = 200
            n_samples = len(X)

            yield _emit("train_tflite", 60, f"Training classifier: {n_samples} samples, {n_epochs} epochs...")

            for epoch in range(n_epochs):
                h = _relu(X @ W1 + b1)
                logits = h @ W2 + b2
                probs = _softmax(logits)

                y_onehot = np.zeros((n_samples, 2), dtype=np.float32)
                y_onehot[np.arange(n_samples), y.astype(int)] = 1.0

                d_logits = (probs - y_onehot) / n_samples
                dW2 = h.T @ d_logits
                db2 = d_logits.sum(axis=0)
                d_h = d_logits @ W2.T
                d_h[X @ W1 + b1 <= 0] = 0
                dW1 = X.T @ d_h
                db1 = d_h.sum(axis=0)

                W1 -= lr * dW1
                b1 -= lr * db1
                W2 -= lr * dW2
                b2 -= lr * db2

                if epoch % 50 == 0:
                    preds = probs.argmax(axis=1)
                    acc = (preds == y.astype(int)).mean()
                    pct = 60 + int((epoch / n_epochs) * 15)
                    yield _emit("train_tflite", pct, f"Epoch {epoch}/{n_epochs} — accuracy: {acc:.1%}")

            h_final = _relu(X @ W1 + b1)
            probs_final = _softmax(h_final @ W2 + b2)
            preds_final = probs_final.argmax(axis=1)
            final_acc = (preds_final == y.astype(int)).mean()
            yield _emit("train_tflite", 76, f"Training complete — accuracy: {final_acc:.1%}")

            # ── Convert to TFLite with int8 quantization ──────────────────────
            yield _emit("quantize", 78, "Quantizing model to int8 TFLite...")

            tflite_bytes = _build_quantized_tflite(W1, b1, W2, b2, X)
            if tflite_bytes is not None:
                tflite_path.write_bytes(tflite_bytes)
                tflite_built = True
                yield _emit("quantize", 82, f"TFLite model saved ({len(tflite_bytes)} bytes)")
            else:
                if _save_numpy_model(model_dir, W1, b1, W2, b2):
                    yield _emit("quantize", 82, "Saved numpy model (TensorFlow not available for TFLite conversion)")
                else:
                    yield _emit("warn", 78, "Could not save model — verifier will be used instead")

        except Exception as exc:
            yield _emit("warn", 58, f"TFLite training failed (verifier still works): {exc}")

        # ── Stage 5: Edge TPU compilation (if compiler available) ─────────────
        edgetpu_compiled = False
        if tflite_built and _check_edgetpu_compiler():
            yield _emit("edgetpu", 84, "Compiling for Edge TPU...")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "edgetpu_compiler",
                    "-s",
                    "-o", str(model_dir),
                    str(tflite_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                output = (stdout or b"").decode("utf-8", "ignore")
                compiled_path = model_dir / "nova_wakeword_edgetpu.tflite"
                if compiled_path.exists():
                    edgetpu_compiled = True
                    yield _emit("edgetpu", 90, "Edge TPU model compiled successfully!")
                else:
                    yield _emit("warn", 84, f"Edge TPU compilation produced no output: {output[:200]}")
            except Exception as exc:
                yield _emit("warn", 84, f"Edge TPU compilation failed: {exc}")
        elif tflite_built:
            yield _emit("info", 84, "edgetpu_compiler not installed — CPU TFLite model will be used (~3-8ms)")

        # Always save numpy model as guaranteed fallback
        _save_numpy_model(model_dir, W1, b1, W2, b2)

        # ── Stage 6: Reload detector ──────────────────────────────────────────
        yield _emit("reload", 92, "Reloading wake word detector...")
        detector = getattr(container, "coral_wake_detector", None)
        if detector:
            detector.reload_verifier()
            detector.reload_tflite()
            yield _emit("reload", 98, "Detector reloaded with new models")

        models_built = ["verifier", "numpy_classifier"]
        if tflite_built:
            models_built.append("cpu_tflite")
        if edgetpu_compiled:
            models_built.append("edgetpu_tflite")
        summary = (
            f"Wake word '{wake_word}' trained! "
            f"Models: {', '.join(models_built)}. "
            f"{len(positive_audio)} positive + {len(negative_audio)} negative samples. "
            f"Verifier threshold={threshold:.3f}, classifier accuracy={final_acc:.1%}"
        )
        yield _emit("done", 100, summary)

    return StreamingResponse(_generate(), media_type="text/event-stream")


@router.post("/coral/install-edgetpu-compiler")
async def install_edgetpu_compiler(request: Request):
    """Install the Edge TPU compiler on the system."""
    _require_session(request, min_role="admin")

    if _check_edgetpu_compiler():
        return {"ok": True, "message": "edgetpu_compiler is already installed"}

    try:
        commands = [
            'curl -s https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key add -',
            'echo "deb https://packages.cloud.google.com/apt coral-edgetpu-stable main" | sudo tee /etc/apt/sources.list.d/coral-edgetpu.list',
            'sudo apt-get update -qq',
            'sudo apt-get install -y -qq edgetpu-compiler',
        ]
        for cmd in commands:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)

        installed = _check_edgetpu_compiler()
        return {
            "ok": installed,
            "message": "edgetpu_compiler installed successfully" if installed else "Installation completed but compiler not found",
        }
    except Exception as exc:
        return {"ok": False, "message": f"Installation failed: {exc}"}
