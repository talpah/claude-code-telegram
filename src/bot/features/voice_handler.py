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
import time
from pathlib import Path

import structlog

from ...config.settings import Settings

logger = structlog.get_logger()

# Maps display language names (lowercase) → ISO 639-1 codes accepted by whisper.cpp
_WHISPER_LANG_MAP: dict[str, str] = {
    "english": "en",
    "romanian": "ro",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "dutch": "nl",
    "polish": "pl",
    "russian": "ru",
}


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

            t_start = time.monotonic()

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

            duration_secs = wav_path.stat().st_size / (16000 * 2) if wav_path.exists() else 0.0

            # Resolve whisper language: map display name → ISO code, "auto" → None
            lang_setting = getattr(self.config, "preferred_language", "auto") or "auto"
            whisper_lang: str | None = None
            if lang_setting.lower() != "auto":
                key = lang_setting.lower()
                whisper_lang = _WHISPER_LANG_MAP.get(key, key)  # use as-is if already a code

            # Build whisper.cpp command
            cmd = [self.config.whisper_binary]
            if self.config.whisper_model_path:
                cmd += ["-m", self.config.whisper_model_path]
            cmd += ["-f", str(wav_path), "--task", "transcribe", "-otxt"]
            if whisper_lang:
                cmd += ["-l", whisper_lang]

            whisper_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_bytes = await whisper_proc.communicate()
            if whisper_proc.returncode != 0:
                stderr_text = (stderr_bytes or b"").decode(errors="replace").strip()
                raise RuntimeError(
                    f"whisper.cpp exited with code {whisper_proc.returncode}"
                    + (f": {stderr_text[:200]}" if stderr_text else "")
                )

            # Whisper outputs <wavfile>.txt
            txt_path = wav_path.with_suffix(".wav.txt")
            if not txt_path.exists():
                txt_path = wav_path.with_suffix(".txt")
            if not txt_path.exists():
                raise RuntimeError("Whisper output file not found after transcription")

            text = txt_path.read_text(encoding="utf-8").strip()
            elapsed = time.monotonic() - t_start
            logger.info(
                "Whisper transcription complete",
                duration_secs=round(duration_secs, 2),
                elapsed_secs=round(elapsed, 2),
                binary=self.config.whisper_binary,
                text_preview=text[:80],
            )
            return text

        finally:
            for path in [ogg_path, wav_path, txt_path]:
                if path and path.exists():
                    try:
                        path.unlink()
                    except Exception:
                        pass
