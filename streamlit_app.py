#!/usr/bin/env python3
"""Dub Studio — PHONE version (Streamlit web app).

Same dub_core.py as the Windows desktop app, so the result is the same:
  video / link -> Whisper -> Gemini Myanmar -> Review -> Edge TTS
  Thiha / Nilar dub -> original ducked to 15% -> subtitle burn + logo.

Why two versions?
  - Windows desktop app = your PC does the work, no time/size limit
    except your PC, keys stay on your PC.
  - This phone version = you control it from iPhone / iPad Safari.
    It runs on Streamlit Community Cloud (FREE, your own GitHub repo —
    NOT Muse's server). The phone itself does NOT render video;
    Safari is only the controller, like the Recap Studio v1 iPad guide.
    Limits on the free cloud: ~10 min video, 200 MB upload, sleeps
    after ~12h idle (~45s to wake). For a 2-hour movie, use the PC app.

Gemini key is typed in this page only (session), never saved to the repo.
"""
import gc
import shutil
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

import dub_core as core

MAX_VIDEO_SECONDS = 600
HERE = Path(__file__).resolve().parent

st.set_page_config(page_title="Dub Studio — Phone", layout="centered")
st.title("🎙 Dub Studio — Phone")
st.caption("ဗီဒီယို / link → စာသားထုတ် → မြန်မာလို ပြန်ဆို → Review → "
           "Thiha / Nilar အသံသွင်း → မြန်မာစာတန်းထိုး ဗီဒီယို")
st.caption("⚠️ ကိုယ်ပိုင်ဗီဒီယို / ခွင့်ပြုထားတဲ့ ဗီဒီယိုပဲ သုံးပါ။ "
           "သူများဗီဒီယို ခိုးပြီး dub/recap တင်ရင် strike ထိနိုင်တယ်။")
st.caption("📱 iPhone/iPad မှာ Python run မရဘူး — ဒီ web version ကို "
           "Safari နဲ့ သုံးရတယ်။ ရုပ်ရှင်ရှည်ရှည်ဆို Windows app ကို သုံးပါ။")


def ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


FONT_URL = ("https://raw.githubusercontent.com/google/fonts/main/"
            "ofl/notosansmyanmar/NotoSansMyanmar%5Bwdth%2Cwght%5D.ttf")


def ensure_font_dir():
    """Bundled TTF first; if iPhone GitHub upload dropped the binary,
    download it on first build (same trick as Subtitle Studio)."""
    if (HERE / "NotoSansMyanmar.ttf").exists():
        return HERE
    fonts_dir = WORK / "assets"
    font = fonts_dir / "NotoSansMyanmar.ttf"
    if font.exists():
        return fonts_dir
    import requests
    fonts_dir.mkdir(parents=True, exist_ok=True)
    r = requests.get(FONT_URL, timeout=120)
    r.raise_for_status()
    font.write_bytes(r.content)
    return fonts_dir


workdir = ss("workdir", None)
if workdir is None:
    workdir = tempfile.mkdtemp(prefix="dubphone_")
    st.session_state.workdir = workdir
WORK = Path(workdir)
ss("video_path", None)
ss("video_meta", None)
ss("segments", [])
ss("output_path", None)
ss("recap_text", "")
ss("api_key", "")

if st.button("＋ အသစ်စ"):
    shutil.rmtree(WORK, ignore_errors=True)
    st.session_state.workdir = tempfile.mkdtemp(prefix="dubphone_")
    for k in ("video_path", "video_meta", "output_path"):
        st.session_state[k] = None
    st.session_state.segments = []
    st.session_state.recap_text = ""
    st.rerun()

# ---------------------------------------------------------------- 1 video
st.header("၁။ ဗီဒီယို")
st.caption(f"ကန့်သတ်ချက် (free cloud): {MAX_VIDEO_SECONDS // 60} မိနစ်အထိ, 200MB အထိ")
url = st.text_input("YouTube link", placeholder="https://youtube.com/shorts/…")
if st.button("ဗီဒီယို ရယူမယ်", disabled=not url.strip()):
    with st.spinner("ဗီဒီယို ရယူနေပါတယ်…"):
        try:
            target = WORK / "source.mp4"
            if target.exists():
                target.unlink()
            info = core.download_video(url, target, log=lambda m: None)
            pr = core.probe(target)
            if pr["duration"] > MAX_VIDEO_SECONDS:
                st.error("ဗီဒီယိုရှည်လွန်းတယ် — ၁၀ မိနစ်အထိပဲ ရတယ်။ ရှည်ရင် PC app သုံးပါ။")
            else:
                st.session_state.video_path = str(target)
                st.session_state.video_meta = {"title": (info or {}).get("title", "video"),
                                               "duration": pr["duration"],
                                               "width": pr["width"], "height": pr["height"]}
                st.session_state.segments = []
                st.success("ရပါပြီ ✅")
                st.rerun()
        except Exception as e:
            msg = str(e)
            if "403" in msg or "bot" in msg.lower() or "Sign in" in msg:
                st.error("YouTube က ဒီ server ကနေ တိုက်ရိုက်ဆွဲတာ ပိတ်ထားလို့ မရပါ။ "
                         "ssyoutube.com နဲ့ download လုပ်ပြီး အောက်က Upload နဲ့ တင်ပါ။")
            else:
                st.error(f"ဒေါင်းလုဒ် မရပါ: {msg[:200]}")

up = st.file_uploader("သို့မဟုတ် ဖိုင်တင်မယ် (Photos / Files ကနေ ရွေးပါ)",
                      type=["mp4", "mov", "mkv", "webm"])
if up is not None and st.button("ဖိုင်ကို သုံးမယ်"):
    target = WORK / "source.mp4"
    with open(target, "wb") as f:
        f.write(up.getbuffer())
    pr = core.probe(target)
    if pr["duration"] <= 0:
        st.error("ဗီဒီယိုဖိုင် ဖတ်မရပါ")
    elif pr["duration"] > MAX_VIDEO_SECONDS:
        st.error("ဗီဒီယိုရှည်လွန်းတယ် — ၁၀ မိနစ်အထိပဲ ရတယ်။")
    else:
        st.session_state.video_path = str(target)
        st.session_state.video_meta = {"title": up.name, "duration": pr["duration"],
                                       "width": pr["width"], "height": pr["height"]}
        st.session_state.segments = []
        st.success("ရပါပြီ ✅")
        st.rerun()

# ---------------------------------------------------------------- 2 transcribe
if st.session_state.video_path:
    meta = st.session_state.video_meta or {}
    st.header("၂။ စာသားထုတ် + မြန်မာလို ပြန်ဆို")
    st.video(st.session_state.video_path)
    st.caption(f"{meta.get('duration', 0):.0f} စက္ကန့် · {meta.get('width', 0)}×{meta.get('height', 0)}")
    key = st.text_input("Gemini API key (aistudio.google.com/apikey — အလကား)",
                        type="password", value=st.session_state.api_key)
    st.session_state.api_key = key
    mode = st.radio("Mode", ["dub", "recap"],
                    format_func=lambda m: "Dub only (လိုင်းအလိုက် dub)" if m == "dub"
                    else "Recap + Dub (script ရေးပြီး အသံသွင်း)")
    recap_len = st.selectbox("Recap အရှည်", ["short", "medium", "full"],
                             format_func=lambda x: {"short": "အတို", "medium": "အလယ်",
                                                    "full": "အရှည်"}[x])
    if st.button("▶ START ① စာသားထုတ် + ပြန်ဆို"):
        with st.spinner("စာသားထုတ်နေပါတယ်… (ဗီဒီယိုအရှည်ပေါ် မူတည်ပြီး ကြာနိုင်တယ်)"):
            try:
                segs, lang = core.transcribe_file(Path(st.session_state.video_path),
                                                  model_name="base", language="en",
                                                  log=lambda m: None)
                if not key.strip():
                    st.error("Gemini key ထည့်မှ ပြန်ဆိုလို့ရမယ်။")
                    st.session_state.segments = segs
                else:
                    mm = core.gemini_translate([key.strip()], segs, log=lambda m: None)
                    for s, t in zip(segs, mm):
                        if t:
                            s["mm"] = t
                    st.session_state.segments = segs
                    if mode == "recap":
                        txt = core.gemini_recap([key.strip()], segs, recap_len,
                                                log=lambda m: None)
                        st.session_state.recap_text = txt
                        (WORK / "recap_script.txt").write_text(txt, encoding="utf-8")
                gc.collect()
                st.success(f"စာကြောင်း {len(st.session_state.segments)} ကြောင်း ရပါပြီ ✅")
                st.rerun()
            except Exception as e:
                st.error(f"မရပါ: {e}")

# ---------------------------------------------------------------- 3 review
if st.session_state.segments:
    st.header("၃။ Review — စာသား / အသံ ပြင်မယ်")
    st.caption("⚠️ အသံကို အလိုလို မခွဲဘူး — voice ကော်လံမှာ Thiha / Nilar လို့ "
               "ရိုက်ပြင်ပေးပါ။ မှားနေတဲ့ လိုင်းကို ဒီမှာပဲ ပြင်ပါ။")
    df = pd.DataFrame(st.session_state.segments,
                      columns=["start", "end", "text", "mm", "voice"])
    edited = st.data_editor(
        df, num_rows="dynamic", use_container_width=True,
        column_config={
            "start": st.column_config.NumberColumn("စ (s)", format="%.2f"),
            "end": st.column_config.NumberColumn("ဆုံး (s)", format="%.2f"),
            "text": st.column_config.TextColumn("မူရင်းစာသား", width="medium"),
            "mm": st.column_config.TextColumn("မြန်မာစာသား", width="large"),
            "voice": st.column_config.TextColumn("အသံ (Thiha/Nilar)", width="small"),
        }, key="seg_editor")
    clean = []
    for _, r in edited.iterrows():
        try:
            s_, e_ = float(r["start"]), float(r["end"])
        except (TypeError, ValueError):
            continue
        if e_ <= s_ or s_ < 0:
            continue
        v = str(r["voice"] or "Thiha")
        v = "Nilar" if "nilar" in v.lower() else "Thiha"
        clean.append({"start": round(s_, 2), "end": round(e_, 2),
                      "text": str(r["text"] or "")[:500],
                      "mm": str(r["mm"] or "")[:500], "voice": v})
    st.session_state.segments = clean

    recap_text = st.text_area("Recap script (Recap mode ဆို ဒီမှာ ပြင်လို့ရတယ်)",
                              value=st.session_state.recap_text, height=220)
    st.session_state.recap_text = recap_text
    if recap_text:
        st.download_button("📄 Recap script txt ယူမယ်", recap_text.encode("utf-8"),
                           file_name="recap_script.txt", mime="text/plain")

    st.header("၄။ အသံညှိခြင်း + Build")
    c1, c2 = st.columns(2)
    with c1:
        orig_vol = st.slider("မူရင်းသံ %", 0, 100, 15)
        max_speed = st.slider("အမြန်ဆုံး အရှိန်", 1.0, 2.0, 1.5)
    with c2:
        dub_vol = st.slider("မြန်မာသံ %", 0, 100, 100)
        burn_sub = st.checkbox("မြန်မာစာတန်းထိုး ဗီဒီယိုထဲ ထည့်မယ်", value=True)
    logo = st.file_uploader("Logo PNG (မထည့်လည်းရတယ်)", type=["png"])
    logo_path = None
    if logo is not None:
        lp = WORK / "logo.png"
        lp.write_bytes(logo.getvalue())
        logo_path = str(lp)
    logo_pos = st.selectbox("Logo နေရာ", ["top-right", "top-left", "bottom-right", "bottom-left"])

    if st.button("③ Build dub video", type="primary"):
        with st.spinner("Dub ဗီဒီယို ထုတ်နေပါတယ်… TTS လိုင်းများရင် ကြာနိုင်တယ်"):
            try:
                src = Path(st.session_state.video_path)
                work = WORK / "dubwork"
                work.mkdir(exist_ok=True)
                if st.session_state.recap_text and mode == "recap":
                    out = WORK / "recap_dubbed.mp4"
                    core.build_recap_dub_video(src, st.session_state.recap_text, out,
                                               voice="Thiha", orig_volume=orig_vol / 100,
                                               burn_subtitles=burn_sub, fonts_dir=ensure_font_dir(),
                                               logo_path=logo_path, log=lambda m: None,
                                               workdir=work)
                else:
                    out = WORK / "myanmar_dubbed.mp4"
                    core.build_dub_video(src, clean, out, orig_volume=orig_vol / 100,
                                         dub_volume=dub_vol / 100, max_speed=max_speed,
                                         burn_subtitles=burn_sub, fonts_dir=ensure_font_dir(),
                                         logo_path=logo_path, logo_pos=logo_pos,
                                         log=lambda m: None, workdir=work)
                    srt = core.build_srt(clean)
                    (WORK / "subtitles_mm.srt").write_text(srt, encoding="utf-8")
                gc.collect()
                st.session_state.output_path = str(out)
                st.success(f"ပြီးပါပြီ ✅ ({Path(out).stat().st_size / 1048576:.1f} MB)")
                st.rerun()
            except Exception as e:
                st.error(f"Build မရပါ: {e}")

    if st.session_state.output_path and Path(st.session_state.output_path).exists():
        st.video(st.session_state.output_path)
        with open(st.session_state.output_path, "rb") as f:
            st.download_button("⬇ Dub ဗီဒီယို ဒေါင်းလုဒ်", f,
                               file_name="myanmar_dubbed.mp4", mime="video/mp4")
        srt_path = WORK / "subtitles_mm.srt"
        if srt_path.exists():
            st.download_button("📄 SRT ယူမယ်", srt_path.read_bytes(),
                               file_name="subtitles_mm.srt", mime="text/plain")

st.divider()
st.caption("Dub Studio Phone — Windows app နဲ့ အတူ dub_core.py တစ်ခု တည်းကို သုံးတယ်။ "
           "ရှည်တဲ့ ဗီဒီယို / ၂ နာရီ ရုပ်ရှင်ဆို PC app ကို သုံးပါ။")
