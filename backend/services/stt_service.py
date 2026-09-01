"""
services/stt_service.py
Speech-to-text using faster-whisper (runs fully locally, no API key needed).
"""

from faster_whisper import WhisperModel

# "small" is better quality than "base"
# Use "medium" for even better accuracy on accents
_model = WhisperModel("small", device="cpu", compute_type="int8")


def transcribe_audio(audio_path: str) -> str:
    """
    Transcribe a WAV/MP3/etc file and return the full transcript as a string.

    Args:
        audio_path: path to the audio file on disk

    Returns:
        Transcribed text (empty string if nothing was said or only silence)
    """
    # Specify English language explicitly to avoid language detection issues
    segments, _ = _model.transcribe(
        audio_path,
        language="en",
        condition_on_previous_text=False,
    )
    
    text = " ".join(seg.text.strip() for seg in segments).strip()
    
    # If no segments or text is very short, return empty
    if not text or len(text) < 5:
        return ""
    
    return text
