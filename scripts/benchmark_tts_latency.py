#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import time

from avatar_backend.config import get_settings
from avatar_backend.services.tts_service import AfroTTSService, IntronAfroTTSService


async def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark current AfroTTS against Intron Afro TTS sidecar.")
    parser.add_argument("--text", default="Hello from Nova. This is a benchmark of local accented speech synthesis.")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    settings = get_settings()
    providers = [
        ("afrotts", AfroTTSService(voice=settings.afrotts_voice, speed=settings.afrotts_speed)),
        (
            "intron_afro_tts",
            IntronAfroTTSService(
                base_url=settings.intron_afro_tts_url,
                timeout_s=settings.intron_afro_tts_timeout_s,
                reference_wav=settings.intron_afro_tts_reference_wav,
                language=settings.intron_afro_tts_language,
            ),
        ),
    ]

    for name, svc in providers:
        for run_idx in range(args.runs):
            t0 = time.perf_counter()
            wav = await svc.synthesise(args.text)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            print(f"{name}\trun={run_idx + 1}\telapsed_ms={elapsed_ms:.1f}\twav_bytes={len(wav)}")


if __name__ == "__main__":
    asyncio.run(main())
