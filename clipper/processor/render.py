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


def _smooth_track(track: list[dict] | None, duration: float) -> list[tuple[float, float]]:
    """Clean Gemini's action track: sort, drop tiny moves (anti-jitter), 3-point average."""
    pts = sorted((float(p["t"]), min(1000.0, max(0.0, float(p["x"])))) for p in (track or [])
                 if isinstance(p, dict) and "t" in p and "x" in p and -1 <= float(p["t"]) <= duration + 1)
    out: list[tuple[float, float]] = []
    for t, x in pts:
        if out and t - out[-1][0] < 1.5:
            continue
        if out and abs(x - out[-1][1]) < 60:
            x = out[-1][1]
        out.append((max(0.0, t), x))
    if len(out) >= 3:
        xs = [p[1] for p in out]
        out = [(t, (xs[max(0, i - 1)] + xs[i] + xs[min(len(xs) - 1, i + 1)]) / 3) for i, (t, _) in enumerate(out)]
    return out


def _pan_expr(track: list[tuple[float, float]], W: int, w: int, fallback_cx: float) -> str:
    """Per-frame crop x for ffmpeg: piecewise-linear through the track, clamped to the frame."""
    if w >= W:
        return "0"
    if len(track) < 2:
        return str(_even(min(max(0, fallback_cx - w / 2), W - w)))
    px = [(t, x / 1000 * W - w / 2) for t, x in track]
    expr = f"{px[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(px, px[1:]))):
        seg = f"{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.2f})/{max(t1 - t0, 0.01):.2f}"
        expr = f"if(lt(t\\,{t1:.2f})\\,{seg}\\,{expr})"
    expr = f"if(lt(t\\,{px[0][0]:.2f})\\,{px[0][1]:.1f}\\,{expr})"
    return f"clip({expr}\\,0\\,{W - w})"


def _zoom(w: int, h: int, payoff: float | None) -> str:
    """Quick 12% punch-in around the payoff second, then back out."""
    if payoff is None or payoff < 0:
        return ""
    p0, p1 = max(0.0, payoff - 0.25), payoff + 1.6
    z = f"1+0.12*clip((it-{p0:.2f})/0.25\\,0\\,1)*clip(({p1:.2f}-it)/0.3\\,0\\,1)"
    return (f",zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps=30")


def layout_filter(W: int, H: int, mode: str, facecam: list[int] | None, center_x: int | None,
                  track: list[tuple[float, float]] | None = None, payoff: float | None = None) -> str:
    cx = (center_x if center_x is not None else 500) / 1000 * W
    track = track or []
    if mode == "stacked_facecam" and facecam and len(facecam) == 4:
        y0, x0, y1, x1 = [v / 1000 for v in facecam]
        fw, fh = (x1 - x0) * W, (y1 - y0) * H
        # grow the webcam box to the band's aspect, never beyond 2x its size
        fcw, fch, fcx, fcy = _crop_box(W, H, (x0 + x1) / 2 * W, (y0 + y1) / 2 * H, OW / TOP_H,
                                       max(fw, fh * OW / TOP_H) * 1.15, max(fh, fw * TOP_H / OW) * 1.15)
        bot_h = OH - TOP_H
        gw, gh, gx, gy = _crop_box(W, H, W / 2, H / 2, OW / bot_h, W, H)
        gx_expr = _pan_expr(track, W, gw, W / 2)
        return (f"[0:v]fps=30,split=2[a][b];[a]crop={fcw}:{fch}:{fcx}:{fcy},scale={OW}:{TOP_H}:flags=lanczos[top];"
                f"[b]crop={gw}:{gh}:'{gx_expr}':{gy},scale={OW}:{bot_h}:flags=lanczos{_zoom(OW, bot_h, payoff)}[bot];"
                f"[top][bot]vstack=inputs=2,setsar=1")
    if mode == "speaker_crop":
        w, h, x, y = _crop_box(W, H, cx, H / 2, OW / OH, W, H)
        x_expr = _pan_expr(track, W, w, cx)
        return f"[0:v]fps=30,crop={w}:{h}:'{x_expr}':{y},scale={OW}:{OH}:flags=lanczos{_zoom(OW, OH, payoff)},setsar=1"
    # blur_fit: whole frame centred over a blurred, zoomed copy of itself
    return (f"[0:v]fps=30,split=2[a][b];[a]scale={OW}:{OH}:force_original_aspect_ratio=increase,crop={OW}:{OH},"
            f"boxblur=24:4,eq=brightness=-0.08[bg];[b]scale={OW}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=0:(H-h)/2{_zoom(OW, OH, payoff)},setsar=1")


def make_thumb(video: Path, at: float = 1.5) -> Path | None:
    """Poster image for the dashboard (same name, .jpg)."""
    thumb = video.with_suffix(".jpg")
    try:
        run([FFMPEG, "-y", "-v", "error", "-ss", f"{max(0.0, at):.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", "scale=360:-2", "-q:v", "4", str(thumb)], timeout=60)
        return thumb
    except Exception:
        return None


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
    hk = dict(profile["hook_style"])
    if mode == "stacked_facecam":
        hk["margin_v"] = TOP_H - hk.get("font_size", 64) // 2 - 22  # sit on the seam, never cover the face
    ass.write_text(build_ass(words, hook, profile["caption_style"], hk, info["duration"]), encoding="utf-8")
    # libass paths: relative to ROOT with forward slashes (avoids Windows drive-letter escaping)
    rel_ass = os.path.relpath(ass, ROOT).replace("\\", "/")
    rel_fonts = os.path.relpath(FONTS_DIR, ROOT).replace("\\", "/")
    # verdict times here are relative to this (already trimmed) clip - pipeline shifts them
    track = _smooth_track(verdict.get("action_track"), info["duration"]) if profile.get("follow_action", True) else []
    payoff = verdict.get("payoff_sec") if profile.get("payoff_zoom", True) else None
    if payoff is not None and not (0.5 <= float(payoff) <= info["duration"] - 1):
        payoff = None
    vf = layout_filter(info["width"], info["height"], mode, verdict.get("facecam_box"), verdict.get("subject_center_x"),
                       track, float(payoff) if payoff is not None else None)
    vf += f",ass='{rel_ass}':fontsdir='{rel_fonts}'[v]"
    cmd = [FFMPEG, "-y", "-v", "error", "-i", str(src), "-filter_complex", vf, "-map", "[v]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd += ["-c:v", "libx264", "-preset", os.getenv("X264_PRESET", "medium"), "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "30", "-movflags", "+faststart", str(out)]
    run(cmd, timeout=1800, cwd=str(ROOT))
    make_thumb(out, payoff if payoff is not None else min(2.0, info["duration"] / 2))
    log.info("rendered %s (%s)", out.name, mode)
    return out
