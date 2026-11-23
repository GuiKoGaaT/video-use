"""Local transcription with WhisperX, written in the same JSON shape as ElevenLabs Scribe.

The rest of video-use (pack_transcripts.py, render.py, timeline_view.py) reads
Scribe's `words` list: entries of type 'word', 'spacing' or 'audio_event', each
with `text`, `start`, `end` and `speaker_id`. This module produces exactly that,
so nothing downstream has to change.

Pipeline:
  1. Silero VAD over the 16 kHz mono wav -> speech regions. The gaps between them
     are the silences, written as 'spacing' entries and as a `silences` list.
  2. faster-whisper (through WhisperX) transcribes, with an initial_prompt full of
     Portuguese fillers so the model keeps "hum", "é", "tipo", "né" instead of
     cleaning them up.
  3. WhisperX forced alignment (wav2vec2) gives a start/end for every word.

Model: large-v3-turbo on an NVIDIA GPU (CUDA), medium on CPU (int8).
Override with the VIDEO_USE_WHISPER_MODEL environment variable.
"""

from __future__ import annotations

import os
import threading
import warnings
from pathlib import Path

# pyannote (pulled in by WhisperX, unused here) warns that torchcodec can't find
# FFmpeg DLLs. Audio is loaded through the ffmpeg CLI, so the warning is noise.
warnings.filterwarnings("ignore", message=r".*torchcodec.*")
warnings.filterwarnings("ignore", module=r"pyannote.*")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Fillers in the prompt bias Whisper toward verbatim output. Written as a natural
# sentence, because Whisper copies the style of the prompt, not just its words.
FILLER_PROMPT = (
    "Hum, é, tipo, né, então... Ah, eh, hã, tá, é... "
    "Então, tipo assim, né, hum, a gente vai ver, é, isso aí."
)

SAMPLE_RATE = 16000

_lock = threading.Lock()
_cache: dict[str, object] = {}


def _device() -> tuple[str, str, str]:
    """(device, compute_type, default model)."""
    import torch

    if torch.cuda.is_available():
        return "cuda", "float16", "large-v3-turbo"
    return "cpu", "int8", "medium"


def _asr_model(language: str, vocab: str | None = None):
    device, compute_type, default_model = _device()
    name = os.environ.get("VIDEO_USE_WHISPER_MODEL", default_model)
    prompt = FILLER_PROMPT
    if vocab:
        # Proper nouns and jargon spelled the way they should come out.
        prompt = f"{FILLER_PROMPT} Termos: {vocab}."
    key = f"asr:{name}:{language}:{prompt}"
    if key not in _cache:
        import whisperx

        _cache[key] = whisperx.load_model(
            name,
            device,
            compute_type=compute_type,
            language=language,
            vad_method="silero",
            asr_options={
                "initial_prompt": prompt,
                # Keep disfluencies: don't let earlier text steer later windows
                # toward cleaned-up prose.
                "condition_on_previous_text": False,
                "suppress_blank": False,
            },
        )
        _cache[f"name:{key}"] = name
    return _cache[key], _cache[f"name:{key}"], device


def _align_model(language: str, device: str):
    key = f"align:{language}"
    if key not in _cache:
        import whisperx

        _cache[key] = whisperx.load_align_model(language_code=language, device=device)
    return _cache[key]


def _silero_speech(audio) -> list[tuple[float, float]]:
    """Speech regions in seconds, from Silero VAD."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    if "silero" not in _cache:
        _cache["silero"] = load_silero_vad()
    stamps = get_speech_timestamps(
        torch.from_numpy(audio),
        _cache["silero"],
        sampling_rate=SAMPLE_RATE,
        min_silence_duration_ms=250,
        speech_pad_ms=30,
        return_seconds=True,
    )
    return [(float(s["start"]), float(s["end"])) for s in stamps]


def _silences(speech: list[tuple[float, float]], duration: float, min_len: float = 0.25):
    out = []
    cursor = 0.0
    for s, e in speech:
        if s - cursor >= min_len:
            out.append({"start": round(cursor, 3), "end": round(s, 3)})
        cursor = max(cursor, e)
    if duration - cursor >= min_len:
        out.append({"start": round(cursor, 3), "end": round(duration, 3)})
    return out


def _fill_missing_times(words: list[dict]) -> None:
    """Alignment leaves some tokens (numbers, symbols) without times. Interpolate
    them between their neighbours so every word has a start and end."""
    n = len(words)
    i = 0
    while i < n:
        if "start" in words[i] and "end" in words[i]:
            i += 1
            continue
        j = i
        while j < n and not ("start" in words[j] and "end" in words[j]):
            j += 1
        left = words[i - 1]["end"] if i > 0 else (words[j]["start"] if j < n else 0.0)
        right = words[j]["start"] if j < n else left + 0.3 * (j - i)
        step = max(right - left, 0.0) / (j - i)
        for k in range(i, j):
            words[k]["start"] = left + step * (k - i)
            words[k]["end"] = left + step * (k - i + 1)
        i = j


def _respect_silences(words: list[dict], silences: list[dict], min_word: float = 0.05) -> None:
    """Forced alignment often stretches the last word of a phrase across the pause
    that follows it (or starts the next word early). Silero is the authority on
    where speech is: trim any word that runs into a silence."""
    for w in words:
        for s in silences:
            s0, s1 = s["start"], s["end"]
            if s1 <= w["start"] or s0 >= w["end"]:
                continue
            if w["start"] < s0:            # runs into the silence: end where it starts
                w["end"] = max(w["start"] + min_word, s0)
            elif w["end"] > s1:            # starts inside the silence: start where it ends
                w["start"] = min(s1, w["end"] - min_word)
            # a word wholly inside a silence (VAD miss) is left alone


def transcribe_local(audio_path: Path, language: str = "pt", vocab: str | None = None) -> dict:
    """Transcribe a 16 kHz mono wav. Returns a Scribe-shaped dict.

    vocab: comma-separated terms (names, brands, jargon) added to the prompt so
    Whisper spells them right, e.g. "ASA, PETG, FDM Racing Parts".
    """
    import whisperx

    with _lock:  # one model, many batch workers: run one file at a time
        audio = whisperx.load_audio(str(audio_path))
        duration = len(audio) / SAMPLE_RATE

        speech = _silero_speech(audio)
        silences = _silences(speech, duration)

        model, model_name, device = _asr_model(language, vocab)
        result = model.transcribe(audio, batch_size=8, language=language)

        align_model, metadata = _align_model(language, device)
        aligned = whisperx.align(
            result["segments"], align_model, metadata, audio, device,
            return_char_alignments=False,
        )

    raw = []
    for seg in aligned.get("segments", []):
        for w in seg.get("words", []):
            text = (w.get("word") or "").strip()
            if not text:
                continue
            item = {"text": text}
            if w.get("start") is not None and w.get("end") is not None:
                item["start"] = float(w["start"])
                item["end"] = float(w["end"])
            if w.get("score") is not None:
                item["logprob"] = float(w["score"])
            raw.append(item)
    _fill_missing_times(raw)
    _respect_silences(raw, silences)

    words: list[dict] = []
    prev_end = 0.0
    for w in raw:
        start, end = round(w["start"], 3), round(max(w["end"], w["start"]), 3)
        if start > prev_end:
            words.append({
                "text": " ", "start": round(prev_end, 3), "end": start,
                "type": "spacing", "speaker_id": "speaker_0",
            })
        entry = {
            "text": w["text"], "start": start, "end": end,
            "type": "word", "speaker_id": "speaker_0",
        }
        if "logprob" in w:
            entry["logprob"] = w["logprob"]
        words.append(entry)
        prev_end = max(prev_end, end)

    return {
        "language_code": language,
        "language_probability": 1.0,
        "text": " ".join(w["text"] for w in raw),
        "words": words,
        "silences": silences,
        "backend": "whisperx-local",
        "model": model_name,
        "device": device,
    }
