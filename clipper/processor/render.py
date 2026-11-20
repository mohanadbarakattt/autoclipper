"""1080x1920 vertical render: layout + word-highlight karaoke captions + 3.5 s hook banner, one ffmpeg pass."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ..config import FONTS_DIR, ROOT
from ..media import FFMPEG, probe, run

log = logging.getLogger("render")

OW, OH = 1080, 1920
TOP_H = 672  # facecam band (35%)
_whisper = {}


def transcribe(path: Path, model_size: str = "small", language: str | None = None) -> tuple[list[dict], str]:
    from faster_whisper import WhisperModel
    if model_size not in _whisper:
        dev = os.getenv("WHISPER_DEVICE", "cpu")
        _whisper[model_size] = WhisperModel(model_size, device=dev, compute_type="int8" if dev == "cpu" else "float16")
    segs, _ = _whisper[model_size].transcribe(str(path), word_timestamps=True, vad_filter=True, language=language)
    words, text = [], []
    for seg in segs:
        text.append(seg.text.strip())
        for w in seg.words or []:
            if w.word.strip():
                words.append({"start": float(w.start), "end": float(w.end), "word": w.word.strip()})
    return words, " ".join(text)


def _ts(sec: float) -> str:
    sec = max(0.0, sec)
    cs = int(round(sec * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(t: str) -> str:
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(words: list[dict], hook: str, cap: dict, hk: dict, duration: float) -> str:
    pri, hi, out = cap["primary_color"], cap["highlight_color"], cap["outline_color"]
    wpl = int(cap.get("words_per_line", 3))
    rtl = any("֐" <= ch <= "ࣿ" for w in words[:20] for ch in w["word"])  # Arabic/Hebrew/Persian
    spacing = 0 if rtl else 1  # letter spacing breaks Arabic letter joining in libass
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OW}", f"PlayResY: {OH}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{cap['font']},{cap['font_size']},{pri},{hi},{out},&H96000000,-1,0,0,0,100,100,{spacing},0,1,7,3,2,70,70,{cap['margin_v']},1",
        # BorderStyle 3 = opaque box: black text on a white pill
        f"Style: Hook,{hk['font']},{hk['font_size']},&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,22,0,8,90,90,{hk['margin_v']},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook:
        end = min(float(hk.get("seconds", 3.5)), duration)
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(end)},Hook,,0,0,0,,{{\\fad(120,200)}}{_esc(hook.upper())}")
    for i in range(0, len(words), wpl):
        chunk = words[i : i + wpl]
        for j, w in enumerate(chunk):
            start = w["start"]
            # hold the highlight until the next word starts so captions never flicker off
            end = chunk[j + 1]["start"] if j + 1 < len(chunk) else (words[i + wpl]["start"] if i + wpl < len(words) and words[i + wpl]["start"] - w["end"] < 0.6 else w["end"] + 0.25)
            toks = []
            for k, ww in enumerate(chunk):
                t = _esc(ww["word"].upper())
                toks.append(f"{{\\c{hi}\\fscx112\\fscy112}}{t}{{\\c{pri}\\fscx100\\fscy100}}" if k == j else t)
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(max(end, start + 0.05))},Caption,,0,0,0,,{' '.join(toks)}")
    return "\n".join(lines) + "\n"


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def _crop_box(W: int, H: int, cx: float, cy: float, aspect: float, max_w: float, max_h: float) -> tuple[int, int, int, int]:
    """Largest box of `aspect` (w/h) inside max_w x max_h, centred on (cx, cy), clamped to the frame."""
    w = min(max_w, max_h * aspect)
    h = w / aspect
    w, h = _even(min(w, W)), _even(min(h, H))
    x = _even(min(max(0, cx - w / 2), W - w))
    y = _even(min(max(0, cy - h / 2), H - h))
    return w, h, x, y


def layout_filter(W: int, H: int, mode: str, facecam: list[int] | None, center_x: int | None) -> str:
    cx = (center_x if center_x is not None else 500) / 1000 * W
    if mode == "stacked_facecam" and facecam and len(facecam) == 4:
        y0, x0, y1, x1 = [v / 1000 for v in facecam]
        fw, fh = (x1 - x0) * W, (y1 - y0) * H
        # grow the webcam box to the band's aspect, never beyond 2x its size
        fcw, fch, fcx, fcy = _crop_box(W, H, (x0 + x1) / 2 * W, (y0 + y1) / 2 * H, OW / TOP_H,
                                       max(fw, fh * OW / TOP_H) * 1.15, max(fh, fw * TOP_H / OW) * 1.15)
        bot_h = OH - TOP_H
        gw, gh, gx, gy = _crop_box(W, H, W / 2, H / 2, OW / bot_h, W, H)
        return (f"[0:v]split=2[a][b];[a]crop={fcw}:{fch}:{fcx}:{fcy},scale={OW}:{TOP_H}:flags=lanczos[top];"
                f"[b]crop={gw}:{gh}:{gx}:{gy},scale={OW}:{bot_h}:flags=lanczos[bot];[top][bot]vstack=inputs=2,setsar=1")
    if mode == "speaker_crop":
        w, h, x, y = _crop_box(W, H, cx, H / 2, OW / OH, W, H)
        return f"[0:v]crop={w}:{h}:{x}:{y},scale={OW}:{OH}:flags=lanczos,setsar=1"
    # blur_fit: whole frame centred over a blurred, zoomed copy of itself
    return (f"[0:v]split=2[a][b];[a]scale={OW}:{OH}:force_original_aspect_ratio=increase,crop={OW}:{OH},"
            f"boxblur=24:4,eq=brightness=-0.08[bg];[b]scale={OW}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=0:(H-h)/2,setsar=1")


def pick_layout(profile_mode: str, verdict: dict) -> str:
    if profile_mode != "auto":
        if profile_mode == "stacked_facecam" and not verdict.get("facecam_box"):
            return "blur_fit"
        return profile_mode
    hint = verdict.get("layout_hint", "")
    if hint == "stacked_facecam" and verdict.get("facecam_box"):
        return "stacked_facecam"
    if hint == "speaker_crop":
        return "speaker_crop"
    return "blur_fit"


def render_vertical(src: Path, out: Path, workdir: Path, words: list[dict], hook: str, verdict: dict, profile: dict) -> Path:
    info = probe(src)
    mode = pick_layout(profile.get("layout_mode", "auto"), verdict)
    ass = workdir / "subs.ass"
    ass.write_text(build_ass(words, hook, profile["caption_style"], profile["hook_style"], info["duration"]), encoding="utf-8")
    # libass paths: relative to ROOT with forward slashes (avoids Windows drive-letter escaping)
    rel_ass = os.path.relpath(ass, ROOT).replace("\\", "/")
    rel_fonts = os.path.relpath(FONTS_DIR, ROOT).replace("\\", "/")
    vf = layout_filter(info["width"], info["height"], mode, verdict.get("facecam_box"), verdict.get("subject_center_x"))
    vf += f",ass='{rel_ass}':fontsdir='{rel_fonts}'[v]"
    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(src), "-filter_complex", vf, "-map", "[v]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd += ["-c:v", "libx264", "-preset", os.getenv("X264_PRESET", "medium"), "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", "-movflags", "+faststart", str(out)]
    run(cmd, timeout=1800, cwd=str(ROOT))
    log.info("rendered %s (%s)", out.name, mode)
    return out
