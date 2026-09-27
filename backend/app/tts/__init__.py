"""Read-aloud (text-to-speech) behind one interface. See docs/TTS.md for the comparison
of providers and how to set up the natural Kokoro voice."""
from .base import SPEEDS, Audio, TTSError, Voice
from .service import AudioCache, TTSService, get_tts, set_tts

__all__ = ["SPEEDS", "Audio", "TTSError", "Voice", "AudioCache", "TTSService", "get_tts", "set_tts"]
