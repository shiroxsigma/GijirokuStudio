"""Recorded audio transcription."""
import os
import subprocess
import numpy as np
from ..paths import FFMPEG_PATH, MODELS_DIR, ROLE_SELF, ROLE_OTHER
from ..asr.realtime import remove_cross_role_line_duplicates
from ..settings import load_app_settings, load_glossary

def build_whisper_prompt(terms):
    """Build a Whisper initial_prompt (vocabulary bias) from canonical forms.
    Caps at ~80 terms / 400 chars to stay within the prompt token budget.
    Returns None when there is nothing to bias with.
    """
    forms = [t["form"] for t in terms if t.get("form")]
    if not forms:
        return None
    return "、".join(forms[:80])[:400]


def detect_ja_en(model, audio_f32):
    """Detect whether a 16kHz mono float32 clip is Japanese or English.

    Uses faster-whisper's model.detect_language(), restricted to just the
    ja/en pair. Falls back to a transcribe()-based detection (reading
    info.language, coercing anything that isn't 'en' to 'ja') for older
    faster-whisper versions that lack detect_language(). Defaults to 'ja'
    if detection fails entirely.
    """
    try:
        _, _, all_language_probs = model.detect_language(audio_f32)
        probs = dict(all_language_probs)
        ja_p = probs.get("ja", 0.0)
        en_p = probs.get("en", 0.0)
        return "en" if en_p > ja_p else "ja"
    except Exception:
        pass
    try:
        _, info = model.transcribe(audio_f32, language=None)
        return "en" if getattr(info, "language", "ja") == "en" else "ja"
    except Exception:
        return "ja"


def iter_speech_windows(audio_f32, sr, window_s=25.0):
    """Yield (start_sample, end_sample) windows covering speech in audio_f32.

    Uses faster-whisper's bundled Silero VAD (get_speech_timestamps) to find
    speech spans, then greedily groups consecutive spans into windows up to
    ~window_s seconds each. On any failure, or if no speech is detected,
    falls back to fixed contiguous windows of window_s seconds spanning the
    whole array. Windows shorter than ~0.5s are never yielded.
    """
    min_samples = int(0.5 * sr)
    window_samples = int(window_s * sr)
    total = len(audio_f32)

    def _fixed_windows():
        start = 0
        while start < total:
            end = min(start + window_samples, total)
            if end - start >= min_samples:
                yield (start, end)
            start = end

    try:
        from faster_whisper.vad import get_speech_timestamps
        spans = get_speech_timestamps(audio_f32)
        if not spans:
            yield from _fixed_windows()
            return

        cur_start = spans[0]["start"]
        cur_end = spans[0]["end"]
        for sp in spans[1:]:
            if sp["end"] - cur_start <= window_samples:
                cur_end = sp["end"]
            else:
                if cur_end - cur_start >= min_samples:
                    yield (cur_start, cur_end)
                cur_start = sp["start"]
                cur_end = sp["end"]
        if cur_end - cur_start >= min_samples:
            yield (cur_start, cur_end)
    except Exception:
        yield from _fixed_windows()


def apply_glossary(lines, terms):
    """Exact longest-match replacement of registered aliases -> canonical form.
    Correctly-recognised canonical forms are left untouched. Mutates `lines`
    in place; returns the number of lines that were changed.
    """
    repl = {}
    for t in terms:
        form = t.get("form")
        if not form:
            continue
        for pat in t.get("aliases", []):
            if pat and pat != form:
                repl[pat] = form
    if not repl:
        return 0
    patterns = sorted(repl.keys(), key=len, reverse=True)  # longest first
    changed = 0
    for ln in lines:
        text = ln.get("text", "")
        for pat in patterns:
            if pat in text:
                text = text.replace(pat, repl[pat])
        if text != ln.get("text", ""):
            ln["text"] = text
            changed += 1
    return changed


def decode_audio_16k_mono(path):
    """Decode any audio file to a float32 mono 16kHz array (whisper's input)."""
    import wave
    tmp = f"{path}._tmp16k.wav"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run(
        [FFMPEG_PATH, "-y", "-i", path, "-ar", "16000", "-ac", "1", "-f", "wav", tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        creationflags=flags)
    try:
        with wave.open(tmp, "rb") as wf:
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, sr


# ---------------------------------------------------------------- Transcribing

class _WhisperEngine:
    """One interface over faster-whisper and the moonshine-voice fallback."""

    def __init__(self, prompt, report):
        self.prompt = prompt
        self.model = None
        self.name = "moonshine-voice"
        try:
            from faster_whisper import WhisperModel
            cfg = load_app_settings()
            model_name = cfg.get("whisper_model", "large-v3-turbo")
            device = cfg.get("whisper_device", "cpu")
            compute = cfg.get("whisper_compute", "int8")
            report(None, f"モデル読み込み中: {model_name} ({device})")
            self.model = WhisperModel(model_name, device=device, compute_type=compute)
            self.name = f"faster-whisper ({model_name})"
        except ImportError:
            report(None, "faster-whisper 未導入 → moonshine-voice にフォールバック")

    def detect_language(self, clip):
        return detect_ja_en(self.model, clip) if self.model is not None else "ja"

    def transcribe(self, clip, lang, sr):
        """Yield (start_seconds_within_clip, text, language) for one audio window."""
        if self.model is not None:
            segments, _info = self.model.transcribe(
                clip, language=lang, initial_prompt=self.prompt,
                vad_filter=True, beam_size=5)
            for seg in segments:
                text = seg.text.strip()
                if text:
                    yield (seg.start or 0.0), text, lang
            return
        from moonshine_voice import Transcriber, get_model_for_language
        model_path, arch = get_model_for_language(
            "ja" if lang in (None, "auto") else lang)
        transcriber = Transcriber(model_path=model_path, model_arch=arch)
        try:
            transcript = transcriber.transcribe_without_streaming(clip.tolist(), sr)
        finally:
            transcriber.close()
        for line in transcript.lines:
            if line.text.strip():
                yield (line.start_time or 0.0), line.text.strip(), lang


class _FastPostEngine:
    """Run the fast Japanese/English recognizers on recorded audio."""

    def __init__(self, terms, report):
        from ..asr.fast import FastJapaneseEnglishASR
        cfg = load_app_settings()
        replacements = {alias: term["form"] for term in terms
                        for alias in term.get("aliases", [])
                        if alias and alias != term["form"]}
        report(None, "高速ASRモデルを読み込み中...")
        self.model = FastJapaneseEnglishASR(
            MODELS_DIR,
            threads=cfg.get("fast_asr_threads", 4), replacements=replacements)
        self.name = "高速ASR (ReazonSpeech / Parakeet)"
        self.report = report

    def transcribe(self, clip, lang, sr):
        if lang not in ("ja", "en", None):
            raise ValueError("高速ASRは日本語・英語のみに対応しています。後処理ASRをWhisperに変更してください。")
        session = self.model.clone_session()
        session.partials_enabled = False
        results = []

        def collect(events):
            for event in events:
                if event.kind == "final":
                    results.append(event)
                elif event.kind == "refine":
                    results[:] = [old for old in results
                                  if not (event.start_sample <= old.start_sample
                                          < event.end_sample)]
                    results.append(event)

        step = sr
        for start in range(0, len(clip), step):
            collect(session.accept(clip[start:start + step], lang_hint=lang))
            self.report.check()
        collect(session.flush(lang_hint=lang))
        for event in sorted(results, key=lambda e: e.start_sample):
            yield event.start_sample / sr, event.text, event.language


def _lang_at(lang_segments, elapsed, default_lang):
    """Language in force at a given elapsed second, per the switch log."""
    lang = default_lang
    for seg in sorted(lang_segments, key=lambda s: s.get("elapsed", 0.0)):
        if seg.get("elapsed", 0.0) <= elapsed:
            lang = seg.get("lang", lang)
        else:
            break
    return lang


def transcribe_meeting(data, report, span=(0.10, 0.80), backend="whisper"):
    """Transcribe every audio track of a meeting into data["lines"].

    When per-role tracks exist the mixed audio is skipped: transcribing it as
    well would cover the same speech a third time for no benefit. Each track is
    split into VAD speech windows. The fast recognizer also performs its own
    VAD within each window to preserve accurate utterance times.
    """
    terms = load_glossary()
    prompt = build_whisper_prompt(terms)
    print(f"Glossary terms: {len(terms)}")

    if data["role_tracks"]:
        tracks = [(path, role) for role, path in sorted(data["role_tracks"].items())]
        report(None, f"話者別トラック: {' / '.join(r for _, r in tracks)}")
    else:
        tracks = [(data["audio_file"], None)]

    if backend == "fast_ja_en":
        if not data["auto_mode"] and data["language"] not in ("ja", "en"):
            raise ValueError(
                "高速ASRは日本語・英語のみに対応しています。後処理ASRをWhisperに変更してください。")
        engine = _FastPostEngine(terms, report)
    elif backend == "whisper":
        engine = _WhisperEngine(prompt, report)
    else:
        raise ValueError(f"不明な後処理ASR: {backend}")
    data["engine"] = engine.name

    lines = []
    detected = set()
    auto_mode = data["auto_mode"]
    default_lang = "ja" if auto_mode else data["language"]
    lo, hi = span
    per_track = (hi - lo) / len(tracks)

    for t_idx, (path, speaker) in enumerate(tracks):
        base = lo + per_track * t_idx
        tag = f"[{speaker}] " if speaker else ""
        report(base, f"{tag}音声をデコード中...")
        audio, sr = decode_audio_16k_mono(path)
        duration = len(audio) / sr
        data["duration"] = max(data["duration"], duration)

        windows = [w for w in iter_speech_windows(audio, sr)
                   if (w[1] - w[0]) >= sr * 0.5]
        spoken = sum((e - s) / sr for s, e in windows) or 1.0
        report(base, f"{tag}発話区間 {len(windows)}件 / {spoken:.0f}秒")

        done = 0.0
        for start_sample, end_sample in windows:
            clip = audio[start_sample:end_sample]
            start_s = start_sample / sr
            lang = ("auto" if auto_mode and backend == "fast_ja_en" else
                    engine.detect_language(clip) if auto_mode
                    else _lang_at(data["lang_segments"], start_s, default_lang))
            if backend == "fast_ja_en" and lang not in ("ja", "en", "auto"):
                raise ValueError(
                    "高速ASRは日本語・英語のみに対応しています。後処理ASRをWhisperに変更してください。")
            report(base + per_track * (done / spoken),
                   f"{tag}文字起こし {done:.0f}/{spoken:.0f}秒 [{start_s:.0f}s〜] {lang}")
            hint = None if backend == "fast_ja_en" and lang == "auto" else lang
            for offset, text, actual_lang in engine.transcribe(clip, hint, sr):
                detected.add(actual_lang)
                lines.append({"start": start_s + offset, "text": text,
                              "speaker": speaker})
                # faster-whisper decodes lazily as segments are consumed, so a
                # cancel lands mid-window instead of waiting 25 seconds for one.
                report.check()
            done += (end_sample - start_sample) / sr
        del audio

    corrected = apply_glossary(lines, terms)
    if corrected:
        print(f"用語辞書による補正: {corrected}行")

    # Stable order: same instant sorts 相手 before 自分 so overlaps read the same
    # way every run.
    rank = {ROLE_OTHER: 0, ROLE_SELF: 1}
    lines.sort(key=lambda l: (l["start"], rank.get(l.get("speaker"), 9)))
    lines = remove_cross_role_line_duplicates(lines, ROLE_SELF, ROLE_OTHER)
    data["lines"] = lines
    data["detected_langs"] = sorted(detected)
    report(hi, f"文字起こし完了: {len(lines)}行")
    return lines
