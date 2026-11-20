"""One-time setup. Usage:

    python scripts/setup.py --public-url https://post.yourdomain.com

Creates .env, trypost.env (with generated secrets), config/streamers.json and downloads fonts.
Safe to re-run: never overwrites values you already filled in.
"""
import argparse
import base64
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def rsa_pair() -> tuple[str, str]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        k = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption()).decode()
        pub = k.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        return priv, pub
    except ImportError:
        openssl = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:4096",
                            "-out", f"{d}/priv.pem"], check=True, capture_output=True)
            subprocess.run([openssl, "pkey", "-in", f"{d}/priv.pem", "-pubout", "-out", f"{d}/pub.pem"],
                           check=True, capture_output=True)
            return Path(f"{d}/priv.pem").read_text(), Path(f"{d}/pub.pem").read_text()


def set_values(path: Path, values: dict, only_if_empty: bool = True) -> None:
    text = path.read_text(encoding="utf-8")
    for k, v in values.items():
        m = re.search(rf"^{k}=(.*)$", text, re.M)
        if m and only_if_empty and m.group(1).strip() not in ("", '""'):
            continue
        line = f"{k}={v}"
        text = re.sub(rf"^{k}=.*$", lambda _: line, text, flags=re.M) if m else text + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public-url", default="", help="public HTTPS URL that points at TryPost (port 8000)")
    a = ap.parse_args()

    env, tp = ROOT / ".env", ROOT / "trypost.env"
    if not env.exists():
        shutil.copy(ROOT / ".env.example", env)
        print("created .env")
    if not tp.exists():
        shutil.copy(ROOT / "trypost.env.example", tp)
        print("created trypost.env")

    db_pw = re.search(r"^DB_PASSWORD=(.*)$", tp.read_text(), re.M).group(1).strip() or secrets.token_urlsafe(24)
    priv, pub = ("", "")
    if not re.search(r"^PASSPORT_PRIVATE_KEY=.+$", tp.read_text(), re.M):
        print("generating TryPost passport keys...")
        priv, pub = rsa_pair()
    vals = {
        "APP_KEY": "base64:" + base64.b64encode(os.urandom(32)).decode(),
        "DB_PASSWORD": db_pw,
        "REVERB_APP_SECRET": secrets.token_urlsafe(32),
    }
    if priv:
        vals["PASSPORT_PRIVATE_KEY"] = '"' + priv.strip().replace("\n", "\\n") + '"'
        vals["PASSPORT_PUBLIC_KEY"] = '"' + pub.strip().replace("\n", "\\n") + '"'
    set_values(tp, vals)

    if a.public_url:
        url = a.public_url.rstrip("/")
        set_values(tp, {"APP_URL": url}, only_if_empty=False)
        for p in ("youtube", "instagram", "tiktok"):
            set_values(tp, {f"{p.upper() if p != 'youtube' else 'GOOGLE'}_CLIENT_REDIRECT": f'"{url}/accounts/{p}/callback"'},
                       only_if_empty=False)

    # compose needs the DB password for the postgres container too
    set_values(env, {"TRYPOST_DB_PASSWORD": db_pw})

    streamers = ROOT / "config" / "streamers.json"
    if not streamers.exists():
        shutil.copy(ROOT / "config" / "streamers.example.json", streamers)
        print("created config/streamers.json (edit it or use the dashboard)")

    subprocess.run([os.sys.executable, str(ROOT / "scripts" / "get_fonts.py")], check=False)
    for d in ("state", "ring_buffers", "candidates", "ready_clips", "work"):
        (ROOT / "data" / d).mkdir(parents=True, exist_ok=True)

    print("""
Done. Next:
  1. Put your free GEMINI_API_KEY in .env  (https://aistudio.google.com/apikey)
  2. Run `claude setup-token` and paste the token as CLAUDE_CODE_OAUTH_TOKEN in .env
  3. docker compose up -d --build
  4. First-time TryPost DB seed:
       docker compose exec trypost php artisan db:seed --force
       docker compose exec trypost php artisan db:seed --class=UserSeeder --force
     Log in at http://localhost:8000 (admin@trypost.it / password -> CHANGE IT), connect your
     YouTube / Instagram / TikTok accounts, then Settings -> API -> Generate API Key ->
     paste into .env as TRYPOST_API_KEY and run: docker compose up -d clipper
  5. Dashboard: http://localhost:8090
""")


if __name__ == "__main__":
    main()
