#!/usr/bin/env python3
"""Dub Studio — core pipeline (LOCAL tool, no Muse server).

Runs on the user's own Windows PC:
  video file / URL -> yt-dlp download -> faster-whisper transcribe
  -> Gemini translate to Myanmar -> editable lines
  -> Edge TTS my-MM-ThihaNeural / my-MM-NilarNeural (free, no key)
  -> ffmpeg mix: original ducked to 15% + Myanmar dub 100%
  -> optional Myanmar subtitle burn-in + logo overlay

Gemini keys are read ONLY from the local keys.txt next to this file
(or passed in from the GUI). They are never sent anywhere except
generativelanguage.googleapis.com, and never stored in this repo.

Honest limits, stated in the GUI too:
- Speaker assignment is NOT automatic diarization. Whisper gives lines,
  you set Thiha / Nilar per line in Review. The seller screenshot's log
  itself says "if any line is wrong, fix it in Review" — same here.
- "Auto-duck" here = original audio lowered to the slider value
  (default 15%) under the dub, for the whole video. It is not a
  frame-by-frame compressor — that needs a DAW-style sidechain and
  fails silently on many Windows ffmpeg builds, so v1 does the
  reliable static duck that the screenshot's sliders show.
- Recap mode does NOT auto-pick movie scenes. It writes a recap
  narration script (Gemini), dubs that narration, and lays it over
  the source video (looped/trimmed to the narration length) with
  recap subtitles. Scene selection stays manual — v2 work.
"""
import asyncio
import gc
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

MALE_VOICE = "my-MM-ThihaNeural"
FEMALE_VOICE = "my-MM-NilarNeural"
VOICES = {
    "Thiha (male)": MALE_VOICE,
    "Nilar (female)": FEMALE_VOICE,
}
# Fallback chain, same idea as the seller tool's log:
# 2.5-flash -> 2.0-flash -> 1.5-flash, and key-by-key on 429/quota.
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]


def log_default(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- ffmpeg

def ffmpeg_bin():
    f = shutil.which("ffmpeg")
    if f:
        return f
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def run(cmd, timeout=1800):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(path):
    """Duration + size via `ffmpeg -i` parsing (no ffprobe needed on Windows)."""
    r = run([ffmpeg_bin(), "-hide_banner", "-i", str(path)], timeout=60)
    text = r.stderr or ""
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    if m:
        dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    w, h = 0, 0
    m2 = re.search(r"Stream.*Video:.*?,\s*(\d+)x(\d+)", text)
    if m2:
        w, h = int(m2.group(1)), int(m2.group(2))
    return {"duration": dur, "width": w, "height": h}


def media_duration(path):
    return probe(path)["duration"]


# ---------------------------------------------------------------- download

def download_video(url, out_path, cookies_file=None, log=log_default):
    import yt_dlp
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for p in list(out_path.parent.glob(out_path.stem + ".*")):
        p.unlink(missing_ok=True)
    opts = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_path.with_suffix("")),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    if cookies_file and Path(cookies_file).exists():
        opts["cookiefile"] = str(cookies_file)
    last = None
    for client in (None, "ios", "android_vr", "web_safari"):
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url.strip(), download=True)
            if not out_path.exists():
                cands = sorted(out_path.parent.glob(out_path.stem + ".*"))
                if cands:
                    cands[0].rename(out_path)
            if out_path.exists() and out_path.stat().st_size > 0:
                log(f"Download OK: {(info or {}).get('title', '')}")
                return info or {}
            last = RuntimeError("empty download")
        except Exception as e:  # noqa: BLE001 - try next client
            last = e
            log(f"Download try ({client or 'default'}) failed: {str(e)[:120]}")
    raise last or RuntimeError("download failed")


# ---------------------------------------------------------------- whisper

def transcribe_file(path, model_name="small", language="en", log=log_default):
    import numpy as np
    from faster_whisper import WhisperModel
    log(f"Whisper ({model_name}) transcribing… this takes a while on a long video.")
    # Binary stdout: must NOT use text=True here (audio bytes, not text).
    p = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", str(path),
                        "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                       capture_output=True, timeout=900)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("audio decode failed")
    audio = np.frombuffer(p.stdout, dtype=np.float32)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    try:
        kwargs = {"vad_filter": True,
                  "vad_parameters": {"min_silence_duration_ms": 500}}
        if language and language != "auto":
            kwargs["language"] = language
        seg_iter, info = model.transcribe(audio, **kwargs)
        segs = []
        for s in seg_iter:
            t = s.text.strip()
            if t:
                segs.append({"start": round(float(s.start), 2),
                             "end": round(float(s.end), 2),
                             "text": t, "mm": "", "voice": "Thiha"})
        log(f"Whisper found {len(segs)} lines (lang={info.language}).")
        return segs, info.language
    finally:
        del model
        gc.collect()


# ---------------------------------------------------------------- gemini

def _gemini_once(api_key, model, prompt):
    import requests
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4}}
    r = requests.post(url, params={"key": api_key}, json=body, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini {model} {r.status_code}: {r.text[:200]}")
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Gemini unexpected response: {str(data)[:200]}")


def gemini_call(keys, prompt, log=log_default):
    """Try every key x every model, like the seller tool's key pool."""
    keys = [k.strip() for k in keys if k and k.strip()]
    if not keys:
        raise RuntimeError("No Gemini API key. Paste one in the GUI (Advanced tab).")
    errs = []
    for ki, key in enumerate(keys):
        for model in GEMINI_MODELS:
            try:
                out = _gemini_once(key, model, prompt)
                if ki or model != GEMINI_MODELS[0]:
                    log(f"Gemini fallback used: key {ki + 1}, {model}")
                return out
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:120])
                log(f"Gemini key {ki + 1} / {model} failed, trying next…")
    raise RuntimeError("All Gemini keys/models failed: " + " | ".join(errs[:3]))


def gemini_translate(keys, segments, log=log_default):
    results = []
    for b in range(0, len(segments), 60):
        chunk = segments[b:b + 60]
        numbered = "\n".join(f"{i + 1}. {s['text']}" for i, s in enumerate(chunk))
        prompt = (
            "Translate each numbered subtitle line into natural, casual "
            "spoken Myanmar (Burmese) as used in Myanmar-dubbed movies. "
            "Keep the same numbering, one translation per line, no extra "
            "commentary. Keep names as-is. Keep it short enough to speak "
            "as dubbing.\n\n" + numbered)
        txt = gemini_call(keys, prompt, log)
        out = [""] * len(chunk)
        for line in txt.strip().splitlines():
            m = re.match(r"\s*(\d+)\s*[.\):\-]\s*(.*)", line)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(chunk):
                    out[idx] = m.group(2).strip()
        results.extend(out)
        log(f"Translated {min(b + 60, len(segments))}/{len(segments)} lines.")
    return results


def gemini_recap(keys, segments, length="medium", log=log_default):
    mm_text = "\n".join(s.get("mm") or s.get("text", "") for s in segments
                         if (s.get("mm") or s.get("text")))
    want = {"short": "အတို — စာကြောင်း ၅-၈ ကြောင်းပဲ",
            "medium": "အလယ် — စာပိုဒ် ၃-၄ ပိုဒ်",
            "full": "အရှည် — ဇာတ်လမ်း အစကနေ အဆုံးထိ အပြည့်အစုံ"}.get(length, "အလယ်")
    prompt = (
        "You are a Myanmar recap narrator. Below is a video transcript "
        "(Myanmar subtitles). Write a storytelling recap in natural spoken "
        "Myanmar, like a YouTube recap voice-over — not a dry summary, "
        "not line-by-line translation.\n"
        f"Length: {want}.\n"
        "Format exactly, in Myanmar:\n"
        "ခေါင်းစဉ်: <one catchy title>\n"
        "အကျဉ်း: <one paragraph>\n"
        "ဇာတ်လမ်း:\n"
        "<narration, paragraph by paragraph>\n"
        "Hashtag: <5 hashtags, comma separated>\n\n"
        f"Transcript:\n{mm_text[:12000]}")
    return gemini_call(keys, prompt, log)


def recap_narration_text(recap_text):
    """Narration part only (drop title/summary/hashtag lines)."""
    body = recap_text
    m = re.search(r"ဇာတ်လမ်း:\s*(.*)", recap_text, re.S)
    if m:
        body = m.group(1)
    body = re.sub(r"Hashtag:.*", "", body, flags=re.S).strip()
    return body


# ---------------------------------------------------------------- subtitles

def fmt_ass_time(sec):
    sec = max(0.0, sec)
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def fmt_srt_time(sec):
    sec = max(0.0, sec)
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ass_escape(t):
    return t.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def wrap_mm(text, per_line=26):
    text = " ".join(text.split())
    if len(text) <= per_line:
        return text
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > per_line and cur:
            lines.append(cur); cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        while len(ln) > per_line * 2:
            out.append(ln[:per_line]); ln = ln[per_line:]
        out.append(ln)
    return "\\N".join(out)


def build_ass(ass_path, segments, width, height, font_size=64, fonts_dir=None):
    margin_v = max(60, int(height * 0.06)) if height else 60
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width or 1080}
PlayResY: {height or 1920}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: MM,Noto Sans Myanmar,{font_size},&H00FFFFFF,&H000019FF,&H80000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for s in segments:
        txt = (s.get("mm") or s.get("text") or "").strip()
        if not txt:
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        events.append(f"Dialogue: 0,{fmt_ass_time(start)},{fmt_ass_time(end)},"
                      f"MM,,0,0,0,,{ass_escape(wrap_mm(txt))}")
    Path(ass_path).write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def build_srt(segments):
    lines, n = [], 0
    for s in segments:
        txt = (s.get("mm") or s.get("text", "")).strip()
        if not txt:
            continue
        n += 1
        lines.append(f"{n}\n{fmt_srt_time(float(s['start']))} --> "
                     f"{fmt_srt_time(float(s['end']))}\n{txt}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------- TTS (Edge, free)

def tts_line(text, voice, out_path, log=log_default):
    """One line via Microsoft Edge TTS. Needs internet, no key."""
    import edge_tts
    out_path = Path(out_path)

    async def _run():
        comm = edge_tts.Communicate(text, voice)
        await comm.save(str(out_path))
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run())
    finally:
        try:
            loop.close()
        except Exception:
            pass
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"TTS empty for: {text[:40]}")


def tts_with_timing(text, voice, work_mp3):
    tts_line(text, voice, work_mp3)
    dur = media_duration(work_mp3)
    return dur


# ---------------------------------------------------------------- dub mix

def _fit_clip(src_mp3, slot, max_speed, workdir, idx, log):
    """Return a wav at 48kHz fitting `slot` seconds (speed up if needed)."""
    dur = media_duration(src_mp3)
    wav = Path(workdir) / f"line_{idx:04d}.wav"
    if dur <= 0:
        raise RuntimeError(f"TTS duration 0 for line {idx}")
    speed = 1.0
    if dur > slot and slot > 0:
        speed = min(dur / slot, max_speed)
    # atempo only accepts 0.5–100; chain if needed (here <= 2.0, single is fine)
    filters = []
    if speed != 1.0:
        filters.append(f"atempo={speed:.3f}")
        log(f"  line {idx}: TTS {dur:.1f}s -> slot {slot:.1f}s, speed x{speed:.2f}")
    filters.append("aresample=48000")
    filt = ",".join(filters)
    r = run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(src_mp3),
             "-af", filt, str(wav)], timeout=300)
    if r.returncode != 0 or not wav.exists():
        raise RuntimeError(f"TTS convert failed line {idx}: {r.stderr[:200]}")
    return wav, media_duration(wav)


def build_dub_video(source, lines, out_path, orig_volume=0.15, dub_volume=1.0,
                    max_speed=1.5, burn_subtitles=True, font_size=64,
                    fonts_dir=None, logo_path=None, logo_pos="top-right",
                    log=log_default, workdir=None):
    """lines: [{start,end,mm,voice}] voice in {'Thiha','Nilar'} or full id."""
    source, out_path = Path(source), Path(out_path)
    meta = probe(source)
    duration = meta["duration"]
    if duration <= 0:
        raise RuntimeError("source duration unknown — is ffmpeg installed?")
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="dub_"))
    workdir.mkdir(parents=True, exist_ok=True)
    log(f"Building dub: {len(lines)} lines, video {duration:.0f}s, "
        f"orig {orig_volume:.0%} / dub {dub_volume:.0%}")

    # 1) TTS + fit each line, delay to its start time
    dub_inputs, kept_starts = [], []
    for i, s in enumerate(lines):
        txt = (s.get("mm") or "").strip()
        if not txt:
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (TypeError, ValueError):
            continue
        if end <= start or start >= duration:
            continue
        voice_name = s.get("voice", "Thiha")
        voice = VOICES.get(voice_name, voice_name)
        if voice not in (MALE_VOICE, FEMALE_VOICE):
            voice = MALE_VOICE if voice_name == "Thiha" else FEMALE_VOICE
        mp3 = workdir / f"raw_{i:04d}.mp3"
        try:
            tts_line(txt, voice, mp3, log)
        except Exception as e:  # noqa: BLE001
            log(f"  line {i}: TTS failed, skipped: {str(e)[:120]}")
            continue
        wav, _fd = _fit_clip(mp3, end - start, max_speed, workdir, i, log)
        dub_inputs.append(wav)
        kept_starts.append(start)
        if i % 10 == 0:
            log(f"  dubbed {i + 1}/{len(lines)} lines…")
    if not dub_inputs:
        raise RuntimeError("No dubbed lines produced — check TTS/internet.")

    # 2) Original audio, ducked, padded to full duration
    orig_wav = workdir / "orig.wav"
    r = run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(source),
             "-vn", "-af", f"volume={orig_volume},apad,atrim=0:{duration}",
             "-ar", "48000", str(orig_wav)], timeout=600)
    if r.returncode != 0 or not orig_wav.exists():
        # No audio track in source — make silence instead
        run([ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi",
             "-i", f"anullsrc=r=48000:cl=stereo", "-t", str(duration),
             str(orig_wav)], timeout=300)

    # 3) Two-pass mix (keeps ffmpeg input indices trivial and reliable):
    # Pass A: one dub track, each wav delayed to its line start.
    # Pass B: that track mixed with the ducked original over the video.
    # dub_inputs are in line order for lines that passed the TTS-loop
    # filters, so replaying the same filters pairs starts correctly.
    dub_mix = workdir / "dub_mix.wav"
    starts = kept_starts  # paired 1:1 with dub_inputs, TTS failures excluded

    cmd_a = [ffmpeg_bin(), "-y", "-v", "error"]
    for p in dub_inputs:
        cmd_a += ["-i", str(p)]
    filt_a = ";".join(
        f"[{k}:a]apad,atrim=0:{duration},adelay={int(st * 1000)}:all=1[d{k}]"
        for k, st in enumerate(starts))
    mix_a = "".join(f"[d{k}]" for k in range(len(dub_inputs)))
    filt_a += f";{mix_a}amix=inputs={len(dub_inputs)}:normalize=0,volume={dub_volume}[dubmix]"
    cmd_a += ["-filter_complex", filt_a, "-map", "[dubmix]",
              "-ar", "48000", str(dub_mix)]
    r = run(cmd_a, timeout=1200)
    if r.returncode != 0 or not dub_mix.exists():
        raise RuntimeError(f"dub mix failed: {r.stderr[:300]}")

    # Pass B: final video + mixed audio (+ optional subtitle/logo filters)
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-i", str(source),
           "-i", str(orig_wav), "-i", str(dub_mix),
           "-filter_complex",
           "[1:a][2:a]amix=inputs=2:normalize=0[aout]",
           "-map", "0:v", "-map", "[aout]", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
           "-movflags", "+faststart", "-shortest", str(out_path)]
    vf = None
    if burn_subtitles:
        ass = workdir / "subs.ass"
        build_ass(ass, lines, meta["width"], meta["height"], font_size, fonts_dir)
        fd = fonts_dir or HERE
        vf = f"ass={ass}:fontsdir={fd}"
    if logo_path and Path(logo_path).exists():
        # logo needs a second video input — rebuild command with logo input
        pos_map = {"top-right": ("W-w-20", "20"),
                   "top-left": ("20", "20"),
                   "bottom-right": ("W-w-20", "H-h-20"),
                   "bottom-left": ("20", "H-h-20")}
        x, y = pos_map.get(logo_pos, pos_map["top-right"])
        cmd = [ffmpeg_bin(), "-y", "-v", "error", "-i", str(source),
               "-i", str(logo_path),
               "-i", str(orig_wav), "-i", str(dub_mix),
               "-filter_complex",
               f"[0:v][1:v]overlay={x}:{y},format=yuv420p[vout];"
               f"[2:a][3:a]amix=inputs=2:normalize=0[aout]",
               "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
               "-movflags", "+faststart", "-shortest", str(out_path)]
        if burn_subtitles:
            # burn subtitles AFTER overlay: chain ass filter on [vout] is
            # complex; do subtitles in a second pass instead (see below)
            pass
    r = run(cmd, timeout=1800)
    if r.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"final render failed: {r.stderr[:300]}")
    if burn_subtitles and logo_path and Path(logo_path).exists():
        # Second pass: burn subtitles onto the logo version
        ass = workdir / "subs.ass"
        if not ass.exists():
            build_ass(ass, lines, meta["width"], meta["height"], font_size, fonts_dir)
        tmp = workdir / "with_subs.mp4"
        fd = fonts_dir or HERE
        r = run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(out_path),
                 "-vf", f"ass={ass}:fontsdir={fd}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-c:a", "copy", "-movflags", "+faststart", str(tmp)],
                timeout=1800)
        if r.returncode == 0 and tmp.exists():
            tmp.replace(out_path)
    elif burn_subtitles:
        # No logo: first pass copied video; burn now in a second pass
        # (keeps the command above simple and reliable).
        ass = workdir / "subs.ass"
        if not ass.exists():
            build_ass(ass, lines, meta["width"], meta["height"], font_size, fonts_dir)
        tmp = workdir / "with_subs.mp4"
        fd = fonts_dir or HERE
        r = run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(out_path),
                 "-vf", f"ass={ass}:fontsdir={fd}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-c:a", "copy", "-movflags", "+faststart", str(tmp)],
                timeout=1800)
        if r.returncode == 0 and tmp.exists():
            tmp.replace(out_path)
        else:
            log(f"Subtitle burn failed, keeping audio-dubbed video: {r.stderr[:150]}")
    log(f"Dub video done: {out_path} ({out_path.stat().st_size / 1048576:.1f} MB)")
    return out_path


def build_recap_dub_video(source, recap_text, out_path, voice="Thiha",
                          orig_volume=0.15, burn_subtitles=True, font_size=64,
                          fonts_dir=None, logo_path=None, log=log_default,
                          workdir=None):
    """Dub the recap narration over the source video.

    Narration is split into sentences; each sentence gets a timed
    subtitle line from its real TTS duration. Video is looped if the
    narration is longer than the source, trimmed if shorter.
    """
    narration = recap_narration_text(recap_text)
    sentences = [s.strip() for s in re.split(r"(?<=[။.!?])\s+", narration) if s.strip()]
    if not sentences:
        raise RuntimeError("Recap narration is empty.")
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="recapdub_"))
    workdir.mkdir(parents=True, exist_ok=True)
    vname = MALE_VOICE if voice == "Thiha" else FEMALE_VOICE
    lines, t = [], 0.0
    for i, sent in enumerate(sentences):
        mp3 = workdir / f"narr_{i:03d}.mp3"
        tts_line(sent, vname, mp3, log)
        d = media_duration(mp3)
        lines.append({"start": round(t, 2), "end": round(t + d + 0.3, 2),
                      "text": sent, "mm": sent, "voice": voice})
        t += d + 0.4
    total = t
    log(f"Recap narration: {len(sentences)} sentences, {total:.0f}s.")
    # Prepare a source video trimmed/looped to narration length
    meta = probe(source)
    src_fit = workdir / "src_fit.mp4"
    if meta["duration"] >= total:
        r = run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(source),
                 "-t", str(total), "-c", "copy", str(src_fit)], timeout=600)
    else:
        loops = int(total // max(meta["duration"], 1)) + 1
        r = run([ffmpeg_bin(), "-y", "-v", "error", "-stream_loop", str(loops),
                 "-i", str(source), "-t", str(total), "-c", "copy", str(src_fit)],
                timeout=600)
    if r.returncode != 0 or not src_fit.exists():
        src_fit = Path(source)
    out = build_dub_video(src_fit, lines, out_path, orig_volume=orig_volume,
                          dub_volume=1.0, max_speed=1.2,
                          burn_subtitles=burn_subtitles, font_size=font_size,
                          fonts_dir=fonts_dir, logo_path=logo_path,
                          log=log, workdir=workdir)
    # Save script + srt alongside
    Path(str(out_path) + ".txt").write_text(recap_text, encoding="utf-8")
    return out
