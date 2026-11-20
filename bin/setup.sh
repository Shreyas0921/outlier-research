#!/usr/bin/env bash
# One-command setup for outlier-research. Safe to run again any time (e.g. after `git pull`).
#
#   bash bin/setup.sh            set up the Python env, settings files and the /outliers skill, then check everything
#   bash bin/setup.sh --models   also download the two whisper models (~1.7 GB) if they're missing
#
# It never prints .env (that's where the API key lives). It ends with a checklist of what's still missing.
set -uo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT"
WANT_MODELS=""; [ "${1:-}" = "--models" ] && WANT_MODELS=1
missing=()
ok() { printf '  ok    %s\n' "$1"; }
bad() { printf '  MISSING  %s\n' "$1"; missing+=("$1"); }

echo "outlier-research setup in $PROJECT"

# 1. Python 3.10+ and the virtualenv
PYBIN=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PYBIN="$(command -v "$c")"; break
  fi
done
if [ -z "$PYBIN" ]; then
  bad "Python 3.10 or newer (macOS: brew install python@3.13; Linux: apt install python3 python3-venv)"
else
  if [ ! -x .venv/bin/python ]; then
    echo "creating .venv with $PYBIN"
    "$PYBIN" -m venv .venv || bad "python venv module (Linux: apt install python3-venv)"
  fi
  if [ -x .venv/bin/python ]; then
    echo "installing Python packages (quiet)..."
    .venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1
    if .venv/bin/python -m pip install -q -r requirements.txt; then ok "Python packages (.venv)"; else bad "pip install -r requirements.txt failed (see output above)"; fi
  fi
fi

# 2. Settings files (never overwritten)
[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "created .env from .env.example"; }
[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "created config.yaml from config.example.yaml"; }
if grep -q '^SCRAPECREATORS_API_KEY=.' .env; then ok "ScrapeCreators API key in .env"; else bad "ScrapeCreators API key: paste it after SCRAPECREATORS_API_KEY= in $PROJECT/.env"; fi

# 3. Command-line tools
for t in ffmpeg ffprobe; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t"; else bad "$t (macOS: brew install ffmpeg; Linux: apt install ffmpeg)"; fi
done
if command -v whisper-cli >/dev/null 2>&1; then ok "whisper-cli"; else bad "whisper-cli (macOS: brew install whisper-cpp; Linux: build whisper.cpp, see SETUP.md)"; fi
if [ "$(uname)" = "Darwin" ]; then
  if command -v swiftc >/dev/null 2>&1; then ok "swiftc (face check)"; else bad "swiftc for the face check (xcode-select --install)"; fi
fi

# 4. Whisper models (shared location, also used by other whisper.cpp tools)
MODELS="$HOME/.cache/whisper"
fetch_model() {  # name url
  local f="$MODELS/$1"
  if [ -s "$f" ]; then ok "model $1"; return; fi
  if [ -n "$WANT_MODELS" ]; then
    mkdir -p "$MODELS"; echo "downloading $1 ..."
    if curl -fL --progress-bar -o "$f.part" "$2" && mv "$f.part" "$f"; then ok "model $1"; else rm -f "$f.part"; bad "model $1 (download failed: $2)"; fi
  else
    bad "whisper model $1 (run: bash bin/setup.sh --models)"
  fi
}
fetch_model ggml-large-v3-turbo.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
fetch_model ggml-silero-v5.1.2.bin https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin

# 5. The /outliers skill for Claude Code
bash "$PROJECT/bin/install-skill.sh" >/dev/null && ok "/outliers skill installed in ~/.claude/skills/outliers"

# 6. Offline tests
if [ -x .venv/bin/python ]; then
  if .venv/bin/python -m pytest -q -p no:cacheprovider >/tmp/outliers-setup-tests.log 2>&1; then
    ok "tests ($(tail -1 /tmp/outliers-setup-tests.log))"
  else
    bad "tests failed (see /tmp/outliers-setup-tests.log)"
  fi
fi

# Where notes will go
VAULT=$(grep '^OUTLIERS_VAULT=' .env | cut -d= -f2-); NOTES=$(grep '^OUTLIERS_NOTES_DIR=' .env | cut -d= -f2-)
echo
echo "notes will be saved in: ${VAULT:-$PROJECT/notes}/${NOTES:-Outliers}/  (change OUTLIERS_VAULT / OUTLIERS_NOTES_DIR in .env)"
echo
if [ ${#missing[@]} -eq 0 ]; then
  echo "ALL SET. Start a new Claude Code session and type: /outliers niches"
else
  echo "STILL TO DO (${#missing[@]}):"
  printf '  - %s\n' "${missing[@]}"
  exit 1
fi
