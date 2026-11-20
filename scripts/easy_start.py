"""Easy mode: no Docker, no TryPost. Run with start.bat (Windows) or `python scripts/easy_start.py`.

First run: creates .venv, installs everything, asks for the free Gemini key, checks Claude, offers to
start with Windows. Every run after that: just starts the clipper and opens the dashboard.
"""
import os
import re
import shutil
import subprocess
import sys
import time
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
ENV = ROOT / ".env"
PORT = 8090


def say(msg: str) -> None:
    print(f"\n== {msg}")


def env_get(key: str) -> str:
    if not ENV.exists():
        return ""
    m = re.search(rf"^{key}=(.*)$", ENV.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else ""


def env_set(key: str, value: str) -> None:
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    line = f"{key}={value}"
    text = re.sub(rf"^{key}=.*$", lambda _: line, text, flags=re.M) if re.search(rf"^{key}=", text, re.M) else text + f"\n{line}\n"
    ENV.write_text(text, encoding="utf-8")


def ensure_venv() -> None:
    stamp = VENV / ".installed"
    req = ROOT / "requirements.txt"
    if stamp.exists() and stamp.stat().st_mtime >= req.stat().st_mtime:
        return
    say("First run: installing (5-10 minutes, only once)...")
    if not PY.exists():
        venv.EnvBuilder(with_pip=True).create(VENV)
    subprocess.run([str(PY), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=False)
    subprocess.run([str(PY), "-m", "pip", "install", "-q", "-r", str(req)], check=True)
    subprocess.run([str(PY), str(ROOT / "scripts" / "get_fonts.py")], check=True)
    stamp.touch()


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
    say("Installing FFmpeg...")
    if os.name == "nt" and shutil.which("winget"):
        subprocess.run(["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--silent",
                        "--accept-source-agreements", "--accept-package-agreements"], check=False)
        print("FFmpeg installed. Close this window and double-click start.bat again so Windows picks it up.")
    else:
        print("Please install FFmpeg (https://ffmpeg.org/download.html) and run again.")
    input("Press Enter to exit...")
    sys.exit(1)


def ensure_gemini() -> None:
    if env_get("GEMINI_API_KEY"):
        return
    say("One-time step: free Gemini key")
    print("A browser tab is opening. Sign in with Google, click 'Create API key', copy it.")
    print("(Do NOT add billing - the free tier is enough.)")
    webbrowser.open("https://aistudio.google.com/apikey")
    while True:
        key = input("\nPaste your Gemini key here and press Enter: ").strip()
        if key.startswith("AIza") and len(key) > 30:
            break
        print("That doesn't look like a Gemini key (it starts with AIza). Try again.")
    env_set("GEMINI_API_KEY", key)
    print("Saved.")


def check_claude() -> None:
    exe = shutil.which("claude")
    if not exe:
        say("Claude Code not found -> Gemini will write the hooks and scores (still free).")
        print("Optional: install it later with  npm install -g @anthropic-ai/claude-code  then run  claude  once to log in.")
        return
    say("Checking Claude (uses your Claude subscription, no API key)...")
    env = dict(os.environ)
    if env_get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = env_get("CLAUDE_CODE_OAUTH_TOKEN")
    try:
        p = subprocess.run([exe, "-p", "Reply with only the word OK"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90, env=env, stdin=subprocess.DEVNULL)
        if "OK" in (p.stdout or ""):
            print("Claude is ready.")
            return
        print((p.stdout or p.stderr or "").strip()[:300])
    except subprocess.TimeoutExpired:
        print("Claude didn't answer in time.")
    if env_get("CLAUDE_LOGIN_SKIPPED"):
        print("Claude isn't logged in -> Gemini does the scoring. (Run  claude  in a terminal to log in any time.)")
        return
    ans = input("Claude isn't logged in. Log in now with your Claude account? [Y/n]: ").strip().lower()
    if ans not in ("", "y", "yes"):
        env_set("CLAUDE_LOGIN_SKIPPED", "1")
        return
    print("\nClaude Code will open. Pick 'Claude account with subscription', finish the login in your browser,")
    print("then type  /exit  and press Enter to come back here.\n")
    input("Press Enter to open Claude...")
    subprocess.run([exe], cwd=str(ROOT))
    p = subprocess.run([exe, "-p", "Reply with only the word OK"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90, stdin=subprocess.DEVNULL)
    print("Claude is ready." if "OK" in (p.stdout or "") else "Still not logged in -> Gemini will do the scoring for now.")


def offer_autostart() -> None:
    if os.name != "nt" or env_get("AUTOSTART_ASKED"):
        return
    env_set("AUTOSTART_ASKED", "1")
    ans = input("\nStart AutoClipper automatically when Windows starts? [Y/n]: ").strip().lower()
    if ans in ("", "y", "yes"):
        startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
        (startup / "AutoClipper.bat").write_text(
            f'@echo off\ncd /d "{ROOT}"\nset AUTOCLIPPER_NO_BROWSER=1\nstart "AutoClipper" /min cmd /c start.bat\n',
            encoding="utf-8")
        print("Done. It will start minimized every time you log in.")


def main() -> None:
    os.chdir(ROOT)
    if not ENV.exists():
        shutil.copy(ROOT / ".env.example", ENV)
    streamers = ROOT / "config" / "streamers.json"
    if not streamers.exists():
        streamers.write_text('{\n  "streamers": []\n}\n', encoding="utf-8")
    ensure_ffmpeg()
    ensure_venv()
    ensure_gemini()
    check_claude()
    offer_autostart()

    say(f"AutoClipper is running -> http://localhost:{PORT}   (close this window to stop it)")
    env = {**os.environ, "CLIPPER_ROOT": str(ROOT), "PYTHONIOENCODING": "utf-8", "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"}
    proc = subprocess.Popen([str(PY), "-m", "clipper"], cwd=ROOT, env=env)
    time.sleep(6)
    if not os.environ.get("AUTOCLIPPER_NO_BROWSER"):
        webbrowser.open(f"http://localhost:{PORT}")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
