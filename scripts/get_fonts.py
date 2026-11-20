"""Download the open-licensed (OFL) caption fonts into assets/fonts."""
import urllib.request
from pathlib import Path

FONTS = {
    "Anton-Regular.ttf": "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
    "Montserrat.ttf": "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
}

dest = Path(__file__).resolve().parent.parent / "assets" / "fonts"
dest.mkdir(parents=True, exist_ok=True)
for name, url in FONTS.items():
    p = dest / name
    if p.exists() and p.stat().st_size > 10_000:
        continue
    print("downloading", name)
    urllib.request.urlretrieve(url, p)
print("fonts ready in", dest)
