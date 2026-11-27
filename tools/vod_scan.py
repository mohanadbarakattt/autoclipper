"""Client VOD clipper with no Claude usage: Kick Arabic auto-subtitles -> Gemini (free) picks moments ->
download only those stretches -> local faster-whisper word timings -> jump-cut dead air -> render -> QA.

    python tools/vod_scan.py pick   <slug> [--vods 4] [--per-vod 3]   # writes data/vod/<slug>/picks.json
    python tools/vod_scan.py make    <slug> <profile> <id> [<id> ...]  # download, cut, proofread, render, QA
    python tools/vod_scan.py deliver <slug> <profile> <folder> <id> [<id> ...]
Only public VODs are used (subscriber-only streams have no source URL and are skipped).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clipper.config import DATA_DIR, load_profile  # noqa: E402
from clipper.media import FFMPEG, run  # noqa: E402
from clipper.processor import ai, render  # noqa: E402

H = {"User-Agent": "Mozilla/5.0"}
TIME = re.compile(r"(\d+):(\d\d):(\d\d)\.(\d+) --> ")


SESSION = requests.Session()  # keep-alive + retries: the stream CDN resets bursts of fresh connections
SESSION.headers.update(H)
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(total=6, backoff_factor=1.5, status_forcelist=[429, 502, 503])))


def get(url: str) -> str:
    r = SESSION.get(url, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def public_vods(slug: str) -> list[dict]:
    v = requests.get(f"https://kick.com/api/v2/channels/{slug}/videos", headers=H, timeout=30).json()
    return [x for x in v if x.get("source")]


def transcript(vod: dict, cache: Path) -> list[tuple[int, str]]:
    """Rolling auto-captions -> [(second, new words)] merged into ~10 s lines."""
    f = cache / f"{vod['video']['uuid']}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    base = vod["source"].rsplit("/", 1)[0] + "/subtitles/ar/"
    chunks = [l for l in get(base + "playlist.m3u8").splitlines() if l and not l.startswith("#")]
    prev: list[str] = []
    out: list[list] = []
    for c in chunks:
        for block in get(base + c).split("\n\n"):
            lines = [l.strip() for l in block.strip().splitlines()]
            for i, l in enumerate(lines):
                m = TIME.match(l)
                if not m:
                    continue
                t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
                words = " ".join(lines[i + 1:]).split()
                k = 0  # longest suffix of the previous cue that this cue starts with = already seen
                for n in range(min(len(prev), len(words)), 0, -1):
                    if prev[-n:] == words[:n]:
                        k = n
                        break
                new = words[k:]
                if words:
                    prev = words
                if new:
                    if out and t - out[-1][0] < 10:
                        out[-1][1] += " " + " ".join(new)
                    else:
                        out.append([t, " ".join(new)])
                break
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def ask_gemini(lines: list, profile: dict, n: int) -> list[dict]:
    from google.genai import types
    text = "\n".join(f"[{t}] {w}" for t, w in lines)
    prompt = f"""You are a senior short-form clip editor. Streamer: {profile['niche_description']}
Below is the auto-generated Arabic transcript of one whole livestream. [N] = second of the stream.
The transcript has recognition errors. Pick the {n} best self-contained moments for TikTok/Reels/Shorts:
a strong opinion or rant, a funny exchange, shouting/laughing, shock at a glitch or crash. The streamer must
be talking through most of it. A stranger must understand it without context. No sexual content, insults to
religion/countries/people, politics, or reading donations only. Length 25-50 seconds.
Return JSON: a list of {{"start": int, "end": int, "score": 0-10, "hook": "short Gulf Arabic on-screen hook, max 6 words",
"title": "Arabic title", "caption": "Arabic caption, 1 line", "why": "English, max 12 words"}}.
Describe only what the transcript really supports.

{text}"""
    resp = ai._gen(prompt, types.GenerateContentConfig(response_mime_type="application/json", temperature=0.3))
    d = json.loads(resp.text)
    return d if isinstance(d, list) else d.get("moments", [])


def cmd_pick(a) -> None:
    root = DATA_DIR / "vod" / a.slug
    profile = load_profile(a.profile or f"client_{a.slug}")
    store = root / "picks.json"
    picks = json.loads(store.read_text(encoding="utf-8")) if store.exists() else []
    done = {p["vod"] for p in picks}
    todo = [v for v in public_vods(a.slug) if v["video"]["uuid"] not in done][: a.vods]
    for v in todo:
        uid = v["video"]["uuid"]
        try:
            lines = transcript(v, root / "subs")
            found = ask_gemini(lines, profile, a.per_vod)
        except Exception as e:
            print(v["start_time"][:10], "failed:", str(e)[:200])
            continue
        for m in found:
            if not (0 <= m["start"] < m["end"] <= v["duration"] / 1000 and 10 <= m["end"] - m["start"] <= 90):
                continue
            m.update(id=len(picks) + 1, vod=uid, date=v["start_time"][:10], source=v["source"])
            picks.append(m)
            print(f"{m['id']:3} {m['date']} {m['start']:6}-{m['end']:<6} {m['score']:>4} {m['why']} | {m['hook']}")
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(picks, ensure_ascii=False, indent=1), encoding="utf-8")
    if not todo:
        print("no new public VODs")


def download(master: str, start: float, length: float, dst: Path) -> None:
    """Fetch only the HLS segments covering the window (ffmpeg's own seek stalls on this CDN), then trim."""
    variant = next(l for l in get(master).splitlines() if l and not l.startswith("#"))  # first = best quality
    url = requests.compat.urljoin(master, variant)
    t, dur, rng, pos, first = 0.0, 0.0, None, {}, None
    part = dst.with_suffix(".ts")
    with part.open("wb") as fh:
        for l in get(url).splitlines():
            if l.startswith("#EXTINF:"):
                dur = float(l[8:].split(",")[0])
            elif l.startswith("#EXT-X-BYTERANGE:"):
                rng = l.split(":")[1]
            elif l and not l.startswith("#"):
                seg, headers = requests.compat.urljoin(url, l), {}
                if rng:
                    n, _, o = rng.partition("@")
                    off = int(o) if o else pos.get(seg, 0)
                    pos[seg] = off + int(n)
                    headers = {"Range": f"bytes={off}-{off + int(n) - 1}"}
                if t + dur > start and t < start + length:
                    first = t if first is None else first
                    r = SESSION.get(seg, headers=headers, timeout=60)
                    r.raise_for_status()
                    fh.write(r.content)
                t, rng = t + dur, None
                if t >= start + length:
                    break
    run([FFMPEG, "-y", "-v", "error", "-ss", f"{start - first:.3f}", "-i", part, "-t", f"{length:.3f}",
         "-c", "copy", dst], timeout=300)
    part.unlink()


def proofread(raw: Path, words: list[dict], work: Path) -> dict:
    """Free Gemini pass over the audio: fix Whisper's spelling word by word (timings stay), judge the clip."""
    from google.genai import types
    mp3 = work / "audio.mp3"
    run([FFMPEG, "-y", "-v", "error", "-i", raw, "-vn", "-ac", "1", "-b:a", "64k", mp3], timeout=300)
    listing = chr(10).join(f"{i} [{w['start']:.1f}] {w['word']}" for i, w in enumerate(words))
    prompt = f"""This is audio from an Arabic GTA 6 livestream. Below is a speech recognizer's word list (index, second, word).
Listen and correct it. Return JSON: {{"words": {{"<index>": "<corrected word>", ...}} for ONLY the wrong words
(use "" to delete a word that was not spoken; keep the streamer's own dialect, never translate),
"hook": "Gulf Arabic on-screen hook, max 5 words, no punctuation, no English, only what is really said",
"title": "Arabic title", "caption": "one-line Arabic caption",
"score": 0-10 how good this is as a standalone short, "why": "English, max 15 words",
"unsafe": true if it has sexual content, slurs or insults to religion, nationality or people}}

{listing}"""
    audio = types.Part.from_bytes(data=mp3.read_bytes(), mime_type="audio/mp3")
    resp = ai._gen([audio, prompt], types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1))
    return json.loads(resp.text)


def speech_segments(words: list[dict], total: float, max_gap: float = 2.2) -> list[list[float]]:
    segs: list[list[float]] = []
    for w in words:
        a, b = max(0.0, w["start"] - 0.2), min(total, w["end"] + 0.3)
        if segs and a - segs[-1][1] <= max_gap:
            segs[-1][1] = max(segs[-1][1], b)
        else:
            segs.append([a, b])
    return segs


def jumpcut(src: Path, dst: Path, segs: list[list[float]]) -> None:
    parts = []
    for i, (a, b) in enumerate(segs):
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];[0:a]atrim={a:.3f}:{b:.3f},"
                     f"asetpts=PTS-STARTPTS,afade=t=in:d=0.03,afade=t=out:st={b - a - 0.04:.3f}:d=0.04[a{i}];")
    fc = "".join(parts) + "".join(f"[v{i}][a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=1[v][a]"
    run([FFMPEG, "-y", "-v", "error", "-i", src, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-c:a", "aac", "-b:a", "192k", dst], timeout=900)


def cmd_make(a) -> None:
    root = DATA_DIR / "vod" / a.slug
    profile = load_profile(a.profile)
    picks = {p["id"]: p for p in json.loads((root / "picks.json").read_text(encoding="utf-8"))}
    for i in a.ids:
        p = picks[i]
        work = root / "work" / str(i)
        work.mkdir(parents=True, exist_ok=True)
        raw = work / "raw.mp4"
        if not raw.exists():
            download(p["source"], max(0, p["start"] - 2), p["end"] - p["start"] + 5, raw)
        lines = json.loads((root / "subs" / f"{p['vod']}.json").read_text(encoding="utf-8"))
        hint = " ".join(w for t, w in lines if p["start"] - 10 <= t <= p["end"])[:600]
        cache = work / "words.json"
        if cache.exists():
            words = json.loads(cache.read_text(encoding="utf-8"))
        else:
            model = profile.get("whisper_model", "small")
            from faster_whisper import WhisperModel
            if model not in render._whisper:
                render._whisper[model] = WhisperModel(model, device="cpu", compute_type="int8")
            segs, _ = render._whisper[model].transcribe(str(raw), language="ar", word_timestamps=True,
                                                        vad_filter=False, initial_prompt=hint,
                                                        condition_on_previous_text=False)
            words = [{"start": float(w.start), "end": float(w.end), "word": w.word.strip()}
                     for s in segs if s.no_speech_prob < 0.8 for w in s.words or []
                     if w.word.strip() and w.probability > 0.15]
            cache.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        review_file = work / "review.json"
        if review_file.exists():
            review = json.loads(review_file.read_text(encoding="utf-8"))
        else:
            review = proofread(raw, words, work)
            review_file.write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
        if isinstance(review, list):  # Gemini sometimes wraps the object in a list
            review = review[0]
        fixes = review.get("words") or {}
        words = [{**w, "word": str(fixes.get(str(n), w["word"])).strip()} for n, w in enumerate(words)]
        words = [w for w in words if w["word"].strip(" .،؟!")]
        # optional hand trim in picks.json ("trim_from"/"trim_to", seconds into raw.mp4) to drop a bad line
        words = [w for w in words if p.get("trim_from", 0) <= w["start"] < p.get("trim_to", 1e9)]
        dur = render.probe(raw)["duration"]
        keep = speech_segments(words, dur)
        out_words, t = [], 0.0
        for s0, s1 in keep:
            out_words += [{"word": w["word"], "start": round(t + w["start"] - s0, 3),
                           "end": round(t + min(w["end"], s1) - s0, 3)} for w in words if s0 <= w["start"] < s1]
            t += s1 - s0
        clip = work / "clip.mp4"
        jumpcut(raw, clip, keep)
        verdict = {"facecam_box": p.get("facecam") or profile.get("facecam_box"), "layout_hint": "stacked_facecam",
                   "subject_center_x": 470, "payoff_sec": None, "action_track": []}
        out = DATA_DIR / "ready_clips" / f"{a.slug}_{i}.mp4"
        render.render_vertical(clip, out, work, out_words, review.get("hook") or p["hook"], verdict, profile)
        if out.stat().st_size > 28_000_000:  # stay under the 30 MB send limit
            tmp = work / "small.mp4"
            run([FFMPEG, "-y", "-v", "error", "-i", out, "-c:v", "libx264", "-crf", "23", "-maxrate", "5M",
                 "-bufsize", "10M", "-c:a", "copy", "-movflags", "+faststart", tmp], timeout=900)
            tmp.replace(out)
        q = subprocess.run([FFMPEG, "-v", "info", "-i", str(out), "-vf", "freezedetect=n=0.001:d=0.6",
                            "-an", "-f", "null", "-"], capture_output=True, text=True, errors="ignore")
        (work / "result.json").write_text(json.dumps({**review, "words": None, "seconds": round(t, 1)},
                                                     ensure_ascii=False), encoding="utf-8")
        print(f"score {review.get('score')} unsafe={review.get('unsafe')} {review.get('why')} | {review.get('hook')}")
        print(f"clip {i}: {t:.1f}s, {len(out_words)} words, freezes={q.stderr.count('freeze_start')}, -> {out.name}")
        print("   " + " ".join(w["word"] for w in out_words))


def cmd_deliver(a) -> None:
    """Copy finished clips + captions.txt into <folder>/<today>/ (a Google Drive desktop folder)."""
    root = DATA_DIR / "vod" / a.slug
    dst = Path(a.folder) / date.today().isoformat()
    dst.mkdir(parents=True, exist_ok=True)
    default_tags = " ".join(load_profile(a.profile).get("required_hashtags") or [])
    notes = []
    for n, i in enumerate(a.ids, 1):
        r = json.loads((root / "work" / str(i) / "result.json").read_text(encoding="utf-8"))
        title = re.sub(r"[^\w ]", "", r["hook"]).strip()
        name = f"{n} - {title}.mp4"
        shutil.copy2(DATA_DIR / "ready_clips" / f"{a.slug}_{i}.mp4", dst / name)
        tags = r.get("tags") or default_tags
        notes.append(f"{name}\n{r['title']}\n{r['caption']}\n{tags}\n")
    (dst / "captions.txt").write_text("\n".join(notes), encoding="utf-8-sig")
    print(dst, len(a.ids), "clips")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pick")
    s.add_argument("slug")
    s.add_argument("--profile")
    s.add_argument("--vods", type=int, default=4)
    s.add_argument("--per-vod", type=int, default=3)
    s.set_defaults(fn=cmd_pick)
    s = sub.add_parser("make")
    s.add_argument("slug")
    s.add_argument("profile")
    s.add_argument("ids", type=int, nargs="+")
    s.set_defaults(fn=cmd_make)
    s = sub.add_parser("deliver")
    s.add_argument("slug")
    s.add_argument("profile")
    s.add_argument("folder")
    s.add_argument("ids", type=int, nargs="+")
    s.set_defaults(fn=cmd_deliver)
    args = ap.parse_args()
    args.fn(args)
