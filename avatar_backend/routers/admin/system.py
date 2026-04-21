"""System sub-router: restart, tunnel, coral/*, heating-shadow/*, camera-discovery/*, music/*, music-ui/*, selfheal/*."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
import structlog
import numpy as np

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response as RawResponse

from avatar_backend.bootstrap.container import AppContainer, get_container

from avatar_backend.runtime_paths import install_dir

from .common import (
    _ENV_FILE,
    _INSTALL_DIR,
    _get_session,
    _require_session,
    _update_env_value,
    MusicControlBody,
)

_LOGGER = structlog.get_logger()
router = APIRouter()

_RESTART_KIOSK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_SELFHEAL_BASE = "http://localhost:7779"

# ── Intron Afro TTS sidecar toggle ────────────────────────────────────────────

@router.get("/intron-tts/status")
async def intron_tts_status(request: Request):
    _require_session(request, min_role="viewer")
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "avatar_intron_afro_tts"],
            capture_output=True, text=True, timeout=5,
        )
        running = result.stdout.strip() == "true"
        return {"running": running}
    except Exception:
        return {"running": False}


@router.post("/intron-tts/toggle")
async def intron_tts_toggle(request: Request):
    _require_session(request, min_role="admin")
    body = await request.json()
    enable = body.get("enable", False)
    try:
        cmd = ["docker", "start" if enable else "stop", "avatar_intron_afro_tts"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        ok = result.returncode == 0
        return {"ok": ok, "running": enable if ok else not enable}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:100]}, status_code=500)
_MUSIC_ASSISTANT_BASE = "http://localhost:8095"


# ── Server controls ───────────────────────────────────────────────────────────

@router.post("/restart")
async def restart_server(request: Request):
    _require_session(request, min_role="admin")
    _LOGGER.info("admin.restart_requested")

    async def _do_restart():
        await asyncio.sleep(0.5)
        subprocess.Popen(["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "avatar-backend"])

    asyncio.create_task(_do_restart())
    return {"restarting": True}


# ── Cloudflare Tunnel ─────────────────────────────────────────────────────────

@router.post("/tunnel/refresh")
async def refresh_tunnel(request: Request):
    """Restart the Cloudflare quick tunnel and update PUBLIC_URL with the new URL."""
    _require_session(request, min_role="admin")
    _LOGGER.info("admin.tunnel_refresh_requested")

    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/sudo", "/usr/bin/systemctl", "restart", "cloudflared-nova",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)

    await asyncio.sleep(6)
    new_url = await _read_tunnel_url()

    if not new_url:
        return {"ok": False, "error": "Tunnel restarted but URL not yet available. Check logs."}

    _update_env_value("PUBLIC_URL", new_url)

    from avatar_backend.config import get_settings
    get_settings.cache_clear()

    _LOGGER.info("admin.tunnel_refreshed", url=new_url)
    return {"ok": True, "url": new_url}


@router.get("/tunnel/status")
async def tunnel_status(request: Request):
    """Check the current Cloudflare tunnel URL."""
    _require_session(request, min_role="viewer")
    url = await _read_tunnel_url()
    current_public = ""
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            if line.strip().startswith("PUBLIC_URL="):
                current_public = line.strip().split("=", 1)[1].strip()
                break
    return {
        "tunnel_url": url or "",
        "public_url": current_public,
        "match": bool(url and current_public == url),
        "tunnel_active": bool(url),
    }


async def _read_tunnel_url() -> str | None:
    """Read the current tunnel URL from journalctl."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/journalctl", "-u", "cloudflared-nova", "--no-pager", "-n", "30",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        output = stdout.decode("utf-8", "ignore")
        import re
        matches = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', output)
        return matches[-1] if matches else None
    except Exception:
        return None


# ── Coral Wake Word ───────────────────────────────────────────────────────────

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


# ── Heating Shadow ────────────────────────────────────────────────────────────

@router.get("/heating-shadow/history")
async def get_heating_shadow_history(request: Request, limit: int = 40, container: AppContainer = Depends(get_container)):
    """Return recent heating shadow decision log entries for the admin panel."""
    _require_session(request)
    log = getattr(container, "decision_log", None)
    if log is None:
        return {"entries": []}
    kinds = {
        "heating_shadow_eval_start",
        "heating_shadow_tool_call",
        "heating_shadow_round_silent",
        "heating_shadow_max_rounds",
        "heating_shadow_eval_error",
        "heating_shadow_comparison",
    }
    all_entries = log.recent(500)
    filtered = [e for e in all_entries if e.get("kind") in kinds][-limit:]
    return {"entries": filtered}


@router.post("/heating-shadow/force")
async def force_heating_shadow(request: Request, scenario: str = "winter", container: AppContainer = Depends(get_container)):
    """Trigger a shadow-only heating evaluation with an injected scenario."""
    _require_session(request, min_role="admin")
    proactive = getattr(container, "proactive_service", None)
    if proactive is None:
        return {"ok": False, "message": "Proactive service not available"}
    if not hasattr(proactive, "run_heating_shadow_force"):
        return {"ok": False, "message": "Shadow force not supported by this proactive version"}
    try:
        records = await proactive.run_heating_shadow_force(scenario=scenario)
        writes = [r for r in records if r["is_write"]]
        reads = [r for r in records if not r["is_write"]]
        return {
            "ok": True,
            "scenario": scenario,
            "total_tool_calls": len(records),
            "write_calls_intercepted": len(writes),
            "read_calls_executed": len(reads),
            "writes": writes,
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


# ── Camera Discovery ──────────────────────────────────────────────────────────

@router.get("/camera-discovery")
async def get_camera_discovery(request: Request, container: AppContainer = Depends(get_container)):
    """Return the auto-discovered camera/motion sensor mappings from HA areas."""
    _require_session(request, min_role="admin")
    discovery = getattr(container, "camera_discovery", None)
    if discovery is None:
        return {"discovered": False, "message": "Camera discovery not available or not yet run"}
    proactive = getattr(container, "proactive_service", None)
    return {
        "discovered": discovery.discovered,
        "outdoor_cameras": discovery.outdoor_cameras,
        "camera_areas": discovery.camera_areas,
        "motion_camera_map_discovered": discovery.motion_camera_map,
        "bypass_cameras_discovered": list(discovery.bypass_global_motion_cameras),
        "vision_prompts_discovered": list(discovery.camera_vision_prompts.keys()),
        "active_motion_camera_map": dict(getattr(proactive, "_motion_camera_map", {})) if proactive else {},
        "active_bypass_cameras": list(getattr(proactive, "_bypass_global_motion_cameras", set())) if proactive else [],
    }


@router.post("/camera-discovery/refresh")
async def refresh_camera_discovery(request: Request, container: AppContainer = Depends(get_container)):
    """Re-run camera discovery from HA area registry."""
    _require_session(request, min_role="admin")
    from avatar_backend.services.camera_discovery import CameraDiscoveryService
    from avatar_backend.config import get_settings
    settings = get_settings()
    discovery = CameraDiscoveryService(settings.ha_url, settings.ha_token)
    result = await discovery.discover(timeout_s=15.0)
    if result.discovered:
        container.camera_discovery = result
        proactive = getattr(container, "proactive_service", None)
        if proactive and hasattr(proactive, "apply_discovery"):
            proactive.apply_discovery(result)
    return {
        "discovered": result.discovered,
        "outdoor_cameras": result.outdoor_cameras,
        "motion_camera_map": result.motion_camera_map,
        "bypass_cameras": list(result.bypass_global_motion_cameras),
    }


# ── Music ─────────────────────────────────────────────────────────────────────

@router.get("/music/players")
async def music_players(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "music_service", None)
    if not svc:
        return {"players": []}
    return {"players": await svc.get_players()}


@router.get("/music/now-playing")
async def music_now_playing(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "music_service", None)
    if not svc:
        return {"players": []}
    return {"players": await svc.get_now_playing()}


@router.post("/music/control")
async def music_control(body: MusicControlBody, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = getattr(container, "music_service", None)
    if not svc:
        raise HTTPException(status_code=503, detail="Music service not available")
    action_map = {
        "play": lambda: svc.play_media(body.entity_id, str(body.value), "music") if body.value else svc.play(body.entity_id),
        "pause": lambda: svc.pause(body.entity_id),
        "stop": lambda: svc.stop(body.entity_id),
        "next": lambda: svc.next_track(body.entity_id),
        "previous": lambda: svc.previous_track(body.entity_id),
        "volume": lambda: svc.set_volume(body.entity_id, float(body.value or 0.5)),
        "mute": lambda: svc.mute(body.entity_id, True),
        "unmute": lambda: svc.mute(body.entity_id, False),
    }
    fn = action_map.get(body.action)
    if not fn:
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")
    return await fn()


@router.get("/music/search")
async def music_search(request: Request, q: str = "", media_type: str = "track", limit: int = 10, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "music_service", None)
    if not svc:
        return {"results": []}
    return {"results": await svc.search(q, media_type, limit)}


@router.get("/music/status")
async def music_assistant_status(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "music_service", None)
    if not svc:
        return {"available": False, "configured": False}
    return {
        "configured": svc.music_assistant_available,
        "available": await svc.check_music_assistant() if svc.music_assistant_available else False,
    }


# ── Music Assistant UI proxy ─────────────────────────────────────────────────

@router.api_route("/music-ui/{path:path}", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
async def music_assistant_proxy(request: Request, path: str = ""):
    """Reverse proxy to Music Assistant, stripping X-Frame-Options for iframe embedding."""
    sess = _get_session(request)
    if not sess:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    url = f"{_MUSIC_ASSISTANT_BASE}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                content=await request.body(),
            )
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("x-frame-options", "content-security-policy", "transfer-encoding")}
        return RawResponse(content=resp.content, status_code=resp.status_code, headers=headers)
    except httpx.ConnectError:
        return JSONResponse({"error": "Music Assistant is not running"}, status_code=503)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=502)


# ── nova-selfheal proxy ────────────────────────────────────────────────────────

@router.api_route("/selfheal/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def selfheal_proxy(path: str, request: Request):
    _require_session(request, min_role="viewer")
    url = f"{_SELFHEAL_BASE}/{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                content=await request.body(),
                headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
            )
        try:
            content = resp.json()
        except Exception:
            content = {"error": resp.text[:300]}
        return JSONResponse(content=content, status_code=resp.status_code)
    except httpx.ConnectError:
        return JSONResponse({"error": "nova-selfheal is not running"}, status_code=503)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.post("/selfheal-restart")
async def selfheal_restart(request: Request):
    _require_session(request, min_role="admin")
    import subprocess as _sp
    result = _sp.run(
        ["sudo", "systemctl", "restart", "nova-selfheal"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return {"ok": True}
    return JSONResponse({"error": result.stderr[:200]}, status_code=500)


@router.post("/selfheal-test")
async def selfheal_test(request: Request):
    """Inject a fake ERROR log entry to test nova-selfheal pipeline."""
    _require_session(request, min_role="admin")
    import structlog as _sl
    _log = _sl.get_logger("avatar_backend.services.ha_proxy")
    _log.error(
        "test.error",
        exc_type="TestError",
        exc="selfheal test injection",
        logger="avatar_backend.services.ha_proxy",
    )
    return {"ok": True, "message": "Test error logged — check Telegram and Self-Heal tab."}


# ── Gemini Key Pool ───────────────────────────────────────────────────────────

@router.get("/gemini-pool")
async def get_gemini_pool(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    pool = container.gemini_key_pool
    if not pool:
        return {"keys": [], "stats": {"pool_size": 0}}
    return {"keys": pool.get_status(), "stats": pool.get_stats()}


@router.post("/gemini-pool/add")
async def add_gemini_key(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    key = (body.get("key") or "").strip()
    label = (body.get("label") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key is required")
    pool = container.gemini_key_pool
    if not pool:
        raise HTTPException(status_code=503, detail="Key pool not initialized")
    pool.add_key(key, label)
    return {"ok": True, "pool_size": pool.size}


@router.delete("/gemini-pool/{index}")
async def remove_gemini_key(index: int, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    pool = container.gemini_key_pool
    if not pool:
        raise HTTPException(status_code=503, detail="Key pool not initialized")
    ok = pool.remove_key(index)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"ok": True, "pool_size": pool.size}


@router.post("/gemini-pool/pin")
async def pin_camera_to_key(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    key_index = body.get("key_index")
    camera_id = (body.get("camera_id") or "").strip()
    if key_index is None or not camera_id:
        raise HTTPException(status_code=400, detail="key_index and camera_id required")
    pool = container.gemini_key_pool
    if not pool:
        raise HTTPException(status_code=503, detail="Key pool not initialized")
    pool.pin_camera(int(key_index), camera_id)
    return {"ok": True}


@router.post("/gemini-pool/unpin")
async def unpin_camera(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    camera_id = (body.get("camera_id") or "").strip()
    if not camera_id:
        raise HTTPException(status_code=400, detail="camera_id required")
    pool = container.gemini_key_pool
    if not pool:
        raise HTTPException(status_code=503, detail="Key pool not initialized")
    pool.unpin_camera(camera_id)
    return {"ok": True}


# ── Vision Camera Selection ───────────────────────────────────────────────────

@router.get("/vision-cameras")
async def get_vision_cameras(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    from avatar_backend.services.home_runtime import load_home_runtime_config
    runtime = load_home_runtime_config()
    all_cameras = dict(runtime.camera_labels)
    # Also include cameras from motion map
    for cam in set(runtime.motion_camera_map.values()):
        if cam not in all_cameras:
            all_cameras[cam] = cam.replace("camera.", "").replace("_", " ").title()
    enabled = set(runtime.vision_enabled_cameras)
    cameras = [{"entity_id": k, "label": v, "vision_enabled": k in enabled or not enabled} for k, v in sorted(all_cameras.items())]
    return {"cameras": cameras}


@router.get("/rooms")
async def get_rooms(request: Request, container: AppContainer = Depends(get_container)):
    """Return configured avatar rooms and which are currently connected."""
    _require_session(request, min_role="viewer")
    from avatar_backend.services.home_runtime import load_home_runtime_config, _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    rooms = raw.get("avatar_rooms", [])
    # Attach live connection status from ws_manager
    ws_mgr = getattr(container, "ws_manager", None)
    connected_rooms: set[str] = set()
    if ws_mgr is not None:
        for sess in ws_mgr.list_voice_sessions():
            if sess.get("room_id"):
                connected_rooms.add(sess["room_id"])
    from avatar_backend.config import get_settings as _gs
    _s = _gs()
    public_url = (_s.public_url or "").rstrip("/")
    ak = _s.api_key
    return {
        "rooms": [
            {**r, "connected": r.get("id", "") in connected_rooms,
             "avatar_url": f"{public_url}/avatar?room={r['id']}&api_key={ak}" if public_url else f"/avatar?room={r['id']}&api_key={ak}",
             "local_url": f"http://192.168.0.249:8001/avatar?room={r['id']}&api_key={ak}",
             "glb_url": (f"{public_url}/static/avatars/{r['glb']}" if r.get("glb") else None)}
            for r in rooms
        ]
    }


@router.post("/rooms")
async def add_room(request: Request, container: AppContainer = Depends(get_container)):
    """Add a new room to the avatar_rooms list."""
    _require_session(request, min_role="admin")
    body = await request.json()
    label = str(body.get("label") or "").strip()
    room_id = str(body.get("id") or "").strip().lower().replace(" ", "_")
    import re as _re
    room_id = _re.sub(r"[^a-z0-9_]", "", room_id)
    if not label or not room_id:
        from fastapi import HTTPException
        raise HTTPException(400, "label and id are required")
    from avatar_backend.services.home_runtime import _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    rooms = raw.get("avatar_rooms", [])
    if any(r.get("id") == room_id for r in rooms):
        from fastapi import HTTPException
        raise HTTPException(409, f"Room '{room_id}' already exists")
    glb = str(body.get("glb") or "").strip() or None
    entry: dict = {"id": room_id, "label": label}
    if glb:
        entry["glb"] = glb
    rooms.append(entry)
    raw["avatar_rooms"] = rooms
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "room": entry}


@router.patch("/rooms/{room_id}")
async def update_room(room_id: str, request: Request, container: AppContainer = Depends(get_container)):
    """Update a room's label or glb assignment."""
    _require_session(request, min_role="admin")
    body = await request.json()
    from avatar_backend.services.home_runtime import _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    rooms = raw.get("avatar_rooms", [])
    updated = False
    for r in rooms:
        if r.get("id") == room_id:
            if "label" in body:
                r["label"] = str(body["label"]).strip()
            if "glb" in body:
                r["glb"] = str(body["glb"]).strip() or None
                if not r["glb"]:
                    r.pop("glb", None)
            updated = True
            break
    if not updated:
        from fastapi import HTTPException
        raise HTTPException(404, f"Room '{room_id}' not found")
    raw["avatar_rooms"] = rooms
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True}


@router.delete("/rooms/{room_id}")
async def delete_room(room_id: str, request: Request, container: AppContainer = Depends(get_container)):
    """Remove a room from the avatar_rooms list."""
    _require_session(request, min_role="admin")
    from avatar_backend.services.home_runtime import _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    rooms = raw.get("avatar_rooms", [])
    rooms = [r for r in rooms if r.get("id") != room_id]
    raw["avatar_rooms"] = rooms
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True}


@router.post("/vision-cameras")
async def save_vision_cameras(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    enabled = body.get("enabled", [])
    from avatar_backend.services.home_runtime import load_home_runtime_config, _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    raw["vision_enabled_cameras"] = enabled
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "enabled": len(enabled)}
