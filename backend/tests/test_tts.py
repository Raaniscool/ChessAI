"""Read-aloud: providers, voice catalog, caching, fallbacks, API."""
import io
import json
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.tts import SPEEDS, Audio, AudioCache, TTSError, TTSService, set_tts
from app.tts.base import Voice
from app.tts.providers import KokoroProvider, OpenAICompatibleProvider, to_wav
from app.tts import voices as V


class FakeProvider:
    def __init__(self, pid="kokoro", ok=True, fail=False):
        self.id, self.name, self.ok, self.fail = pid, f"Fake {pid}", ok, fail
        self.calls = []

    def available(self):
        return (self.ok, "" if self.ok else "not installed")

    def voices(self):
        return [Voice("v1", "One", "female", "US"), Voice("v2", "Two", "male", "UK")]

    def cache_key(self):
        return f"fake:{self.id}"

    def synthesize(self, text, voice, speed):
        self.calls.append((text, voice, speed))
        if self.fail:
            raise TTSError("boom")
        return Audio(f"{voice}|{speed}|{text}".encode(), "audio/wav")


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "tts_provider", "auto")
    fake = FakeProvider()
    svc = TTSService([fake, FakeProvider("openai", ok=False)], AudioCache(tmp_path / "cache", max_mb=5))
    return svc, fake


# ------------------------------------------------------------------ catalog
@pytest.mark.parametrize("catalog", [V.KOKORO, V.OPENAI])
def test_each_persona_has_two_or_three_real_voices(catalog):
    for persona in ("female", "male"):
        n = sum(1 for v in catalog if v.persona == persona)
        assert 2 <= n <= 3
    assert len({v.id for v in catalog}) == len(catalog)  # no duplicates dressed up as new voices


def test_openai_compatible_voices_follow_the_model():
    assert V.for_model("kokoro") is V.KOKORO and V.for_model("gpt-4o-mini-tts") is V.OPENAI


# ------------------------------------------------------------------ service
def test_speak_uses_the_voice_and_speed_and_caches(service):
    svc, fake = service
    audio, cached = svc.speak("Knight takes E5, check.", voice="v2", speed="fast")
    assert not cached and audio.data.startswith(b"v2|1.2|") and audio.mime == "audio/wav"
    again, cached = svc.speak("Knight  takes E5, check. ", voice="v2", speed="fast")  # same after cleaning
    assert cached and again.data == audio.data and len(fake.calls) == 1
    svc.speak("Knight takes E5, check.", voice="v2", speed="slow")
    svc.speak("Knight takes E5, check.", voice="v1", speed="fast")
    assert len(fake.calls) == 3 and svc.stats == {"hits": 1, "misses": 3, "errors": 0}


def test_unknown_voice_falls_back_to_the_providers_first_voice(service):
    svc, fake = service
    svc.speak("Hello", voice="af_nonexistent")
    assert fake.calls[-1][1] == "v1"


def test_speeds_are_slow_normal_fast():
    assert list(SPEEDS) == ["slow", "normal", "fast"] and SPEEDS["slow"] < 1 < SPEEDS["fast"]


def test_empty_or_too_long_text_is_refused(service):
    svc, _ = service
    with pytest.raises(TTSError):
        svc.speak("   ")
    with pytest.raises(TTSError):
        svc.speak("word " * 200)


def test_provider_failure_raises_and_is_counted(tmp_path):
    svc = TTSService([FakeProvider(fail=True)], AudioCache(tmp_path))
    with pytest.raises(TTSError):
        svc.speak("Hello")
    assert svc.stats["errors"] == 1
    assert not tmp_path.exists() or not any(tmp_path.iterdir())  # nothing cached


def test_no_usable_provider_means_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "tts_provider", "auto")
    svc = TTSService([FakeProvider(ok=False), FakeProvider("openai", ok=False)], AudioCache(tmp_path))
    status = svc.status()
    assert status["default"] == "browser"
    assert all(not p["available"] and p["reason"] and p["voices"] == [] for p in status["providers"])
    with pytest.raises(TTSError):
        svc.speak("Hello")


def test_provider_choice_explicit_configured_and_browser(tmp_path, monkeypatch):
    a, b = FakeProvider("kokoro"), FakeProvider("openai")
    svc = TTSService([a, b], AudioCache(tmp_path))
    monkeypatch.setattr(get_settings(), "tts_provider", "openai")
    assert svc.provider() is b and svc.provider("kokoro") is a
    monkeypatch.setattr(get_settings(), "tts_provider", "browser")
    assert svc.provider() is None and svc.status()["default"] == "browser"
    b.ok = False
    monkeypatch.setattr(get_settings(), "tts_provider", "openai")
    assert svc.provider() is a  # configured one unavailable: the next usable one


def test_cache_is_trimmed_oldest_first(tmp_path):
    import os
    import time
    cache = AudioCache(tmp_path, max_mb=0)
    cache.max_bytes = 2500
    for i in range(4):
        cache.put(f"k{i}", Audio(b"x" * 1000, "audio/wav"))
        os.utime(tmp_path / f"k{i}.wav", (time.time() - 100 + i, time.time() - 100 + i))
    cache._trim()
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["k2.wav", "k3.wav"]
    assert cache.get("k3").data == b"x" * 1000 and cache.get("k0") is None


# ------------------------------------------------------------------ providers
def test_kokoro_is_unavailable_without_model_files(tmp_path):
    p = KokoroProvider(tmp_path / "missing.onnx", tmp_path / "missing.bin")
    ok, why = p.available()
    assert not ok and ("setup_tts" in why or "kokoro-onnx" in why)
    with pytest.raises(TTSError):
        p.synthesize("Hello", "af_heart", 1.0)
    assert [v.id for v in p.voices()][:2] == ["af_heart", "af_bella"]


def test_kokoro_synthesizes_wav_through_its_engine(tmp_path, monkeypatch):
    pytest.importorskip("kokoro_onnx")
    model, voices = tmp_path / "m.onnx", tmp_path / "v.bin"
    model.write_bytes(b"x")
    voices.write_bytes(b"x")
    p = KokoroProvider(model, voices)

    class Engine:
        def create(self, text, voice, speed, lang):
            self.args = (text, voice, speed, lang)
            return [0.0, 0.5, -0.5, 1.0], 24000
    engine = Engine()
    monkeypatch.setattr(p, "_load", lambda: engine)
    audio = p.synthesize("Hello there", "bf_emma", 0.85)
    assert engine.args == ("Hello there", "bf_emma", 0.85, "en-gb")
    with wave.open(io.BytesIO(audio.data)) as w:
        assert w.getframerate() == 24000 and w.getnframes() == 4 and w.getsampwidth() == 2


def test_to_wav_writes_a_valid_file():
    data = to_wav([0.0, 0.25, -1.5], 16000)
    with wave.open(io.BytesIO(data)) as w:
        assert w.getnchannels() == 1 and w.getframerate() == 16000 and w.getnframes() == 3


def test_openai_compatible_sends_the_speech_request():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"ID3mp3", headers={"content-type": "audio/mpeg"})

    p = OpenAICompatibleProvider("http://localhost:8880/v1/", "", "kokoro", transport=httpx.MockTransport(handler))
    assert p.available() == (True, "") and "on this computer" in p.name
    audio = p.synthesize("Rook to E1.", "am_michael", 1.2)
    assert seen["url"] == "http://localhost:8880/v1/audio/speech" and seen["auth"] is None
    assert seen["body"] == {"model": "kokoro", "input": "Rook to E1.", "voice": "am_michael",
                            "response_format": "mp3", "speed": 1.2}
    assert audio.data == b"ID3mp3" and audio.mime == "audio/mpeg"


def test_openai_compatible_errors_and_configuration():
    bad = OpenAICompatibleProvider("http://localhost:1/v1", "", "kokoro",
                                   transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(TTSError):
        bad.synthesize("Hi", "af_heart", 1.0)

    def down(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(TTSError):
        OpenAICompatibleProvider("http://localhost:1/v1", "", "kokoro",
                                 transport=httpx.MockTransport(down)).synthesize("Hi", "af_heart", 1.0)
    assert not OpenAICompatibleProvider("", "", "kokoro").available()[0]
    paid = OpenAICompatibleProvider("https://api.openai.com/v1", "", "gpt-4o-mini-tts")
    assert not paid.available()[0] and "API_KEY" in paid.available()[1]
    assert OpenAICompatibleProvider("https://api.openai.com/v1", "sk-x", "gpt-4o-mini-tts").voices() == V.OPENAI


# ------------------------------------------------------------------ API
@pytest.fixture()
def api(service):
    from app.main import app
    svc, fake = service
    set_tts(svc)
    with TestClient(app) as c:
        yield c, fake
    set_tts(None)


def test_status_endpoint_lists_available_voices(api):
    c, _ = api
    body = c.get("/api/tts").json()
    assert body["default"] == "kokoro" and body["speeds"] == ["slow", "normal", "fast"]
    kokoro = next(p for p in body["providers"] if p["id"] == "kokoro")
    assert kokoro["available"] and [v["id"] for v in kokoro["voices"]] == ["v1", "v2"]
    other = next(p for p in body["providers"] if p["id"] == "openai")
    assert not other["available"] and other["voices"] == []


def test_speak_endpoint_returns_audio_and_reports_cache_hits(api):
    c, fake = api
    r = c.post("/api/tts/speak", json={"text": "Correct!", "voice": "v2", "speed": "slow"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.headers["x-tts-cache"] == "miss" and r.content.startswith(b"v2|0.85|")
    r2 = c.post("/api/tts/speak", json={"text": "Correct!", "voice": "v2", "speed": "slow"})
    assert r2.headers["x-tts-cache"] == "hit" and len(fake.calls) == 1


def test_speak_endpoint_tells_the_browser_to_fall_back(api):
    c, fake = api
    fake.fail = True
    r = c.post("/api/tts/speak", json={"text": "Hello"})
    assert r.status_code == 503 and r.json()["fallback"] == "browser"
