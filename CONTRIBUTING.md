# Contributing to AutoClipper

Thanks for helping. Bug reports, presets and small fixes are all welcome.

## Quick start

```bash
git clone https://github.com/mohanadbarakattt/autoclipper && cd autoclipper
# Windows: start.bat      macOS / Linux: ./start.sh
```

The first run creates `.venv`, installs everything and asks for a free Gemini key. Work on a branch off `dev`
and open your pull request against `dev` (`main` is what the one-line installers download).

## Good first contributions

- **A new preset** in `config/profiles/` for a game or niche (copy `gaming.json`, change the filter, emotes and niche text).
- **Better captions or hooks** in `clipper/processor/render.py` (fonts, colors, positions).
- **Docs**: anything that confused you during setup is worth a line in the README FAQ.
- Issues labelled [`good first issue`](https://github.com/mohanadbarakattt/autoclipper/labels/good%20first%20issue).

## Rules

- Never commit `.env`, `config/streamers.json`, anything in `data/`, keys, tokens or real stream footage.
  Demo media must be synthetic or your own.
- Keep it $0 to run: no feature may require a paid API.
- Keep PRs small and say how you tested them.
