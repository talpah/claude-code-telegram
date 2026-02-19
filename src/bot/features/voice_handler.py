"""Voice message transcription handler.

Supports two providers:
  - groq: POST to Groq Whisper API (fast, cloud-based)
  - local: OGG -> WAV via ffmpeg, then whisper.cpp binary

Usage:
    handler = VoiceHandler(settings)
    text = await handler.transcribe(ogg_bytes)
"""

import asyncio
import tempfile
from pathlib import Path

import structlog

from ...config.settings import Settings

logger = structlog.get_logger()


class VoiceHandler:
    """Transcribe Telegram voice/audio messages to text."""

    def __init__(self, config: Settings) -> None:
        self.config = config

    async def transcribe(self, ogg_bytes: bytes) -> str:
        """Transcribe OGG audio bytes to text using the configured provider."""
        provider = self.config.voice_provider
        if provider == "groq":
            return await self._transcribe_groq(ogg_bytes)
        elif provider == "local":
            return await self._transcribe_local(ogg_bytes)
        else:
            raise ValueError(f"Unknown voice provider: {provider!r}. Use 'groq' or 'local'.")

    async def _transcribe_groq(self, ogg_bytes: bytes) -> str:
        """Transcribe using Groq Whisper API (whisper-large-v3-turbo)."""
        import httpx

        if not self.config.groq_api_key:
            raise ValueError("GROQ_API_KEY not set. Required for voice_provider=groq.")
        api_key = self.config.groq_api_key.get_secret_value()

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("audio.ogg", ogg_bytes, "audio/ogg")},
                data={"model": "whisper-large-v3-turbo"},
            )
            response.raise_for_status()
            return str(response.json().get("text", "")).strip()

    async def _transcribe_local(self, ogg_bytes: bytes) -> str:
        """Transcribe using local whisper.cpp (requires ffmpeg + whisper binary)."""
        ogg_path: Path | None = None
        wav_path: Path | None = None
        txt_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                f.write(ogg_bytes)
                ogg_path = Path(f.name)

            wav_path = ogg_path.with_suffix(".wav")

            # Convert OGG -> 16kHz mono WAV
            ffmpeg_proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(ogg_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await ffmpeg_proc.communicate()
            if ffmpeg_proc.returncode != 0:
                raise RuntimeError("ffmpeg conversion failed")

            # Run whisper.cpp
            cmd = [self.config.whisper_binary, "-f", str(wav_path), "-otxt"]
            if self.config.whisper_model_path:
                cmd = [self.config.whisper_binary, "-m", self.config.whisper_model_path, "-f", str(wav_path), "-otxt"]

            whisper_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await whisper_proc.communicate()

            # Whisper outputs <wavfile>.txt
            txt_path = wav_path.with_suffix(".wav.txt")
            if not txt_path.exists():
                txt_path = wav_path.with_suffix(".txt")
            if not txt_path.exists():
                raise RuntimeError("Whisper output file not found after transcription")

            return txt_path.read_text(encoding="utf-8").strip()

        finally:
            for path in [ogg_path, wav_path, txt_path]:
                if path and path.exists():
                    try:
                        path.unlink()
                    except Exception:
                        pass
