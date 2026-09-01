"""
services/tts_service.py
Text-to-speech using Piper — a fully local neural TTS model.
No internet, no API key. All audio is generated on your machine.

── HOW TO SET UP PIPER ─────────────────────────────────────────────────────
1. Download the Piper release for Linux x86_64:
   https://github.com/rhasspy/piper/releases/latest
   File to download: piper_linux_x86_64.tar.gz

2. Extract it inside your backend folder:
   tar -xzf piper_linux_x86_64.tar.gz
   This creates a folder called  piper/  containing the binary and .so libs.

3. Download a voice model (medium quality, ~65 MB):
   cd piper/
   wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
   wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json

4. Your folder structure should look like:
   backend/
     piper/
       piper              ← the binary
       en_US-lessac-medium.onnx
       en_US-lessac-medium.onnx.json
       libonnxruntime.so.1.14.1   ← comes with the release
       ...other .so files

5. Test it works:
   echo "Hello world" | LD_LIBRARY_PATH=./piper ./piper/piper \
     --model ./piper/en_US-lessac-medium.onnx --output_file /tmp/test.wav
   aplay /tmp/test.wav
────────────────────────────────────────────────────────────────────────────
"""

import os
import subprocess

# Paths — override with env vars if you put piper somewhere else
PIPER_BINARY = os.getenv("PIPER_PATH",  os.path.abspath("piper/piper"))
PIPER_MODEL  = os.getenv("PIPER_MODEL", os.path.abspath("piper/en_US-lessac-medium.onnx"))
PIPER_LIBDIR = os.path.abspath("piper")   # folder containing libonnxruntime.so


def generate_speech(text: str, output_wav_path: str) -> None:
    """
    Convert text → WAV file using the local Piper model.

    Args:
        text            : The sentence(s) to speak.
        output_wav_path : Where to write the resulting .wav file.

    Raises:
        RuntimeError if Piper exits with a non-zero status.
    """
    if not os.path.exists(PIPER_BINARY):
        raise RuntimeError(
            f"Piper binary not found at '{PIPER_BINARY}'. "
            "See the setup instructions at the top of services/tts_service.py"
        )

    cmd = [
        PIPER_BINARY,
        "--model",       PIPER_MODEL,
        "--output_file", output_wav_path,
    ]

    # LD_LIBRARY_PATH tells the OS where to find libonnxruntime.so
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = PIPER_LIBDIR

    result = subprocess.run(
        cmd,
        input=text.encode("utf-8"),   # Piper reads text from stdin
        env=env,
        capture_output=True,
    )

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        raise RuntimeError(f"Piper TTS failed (exit {result.returncode}): {stderr}")
