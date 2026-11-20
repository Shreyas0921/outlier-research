# Setup guide (for Claude Code)

You are setting up **outlier-research** and its `/outliers` skill for a new user. Work through the steps in order.
Do the work yourself; ask the user only where a step says to. Keep your messages short and plain.

`PROJECT` below means this folder (the one this file is in). Use its absolute path in every command.

## Rules while setting up

- **Never read, print or `cat` `.env`.** It holds the user's API key. To check the key is there, use
  `grep -q '^SCRAPECREATORS_API_KEY=.' "$PROJECT/.env"` (exit code only).
- **Never ask the user to paste the API key into the chat.** They paste it into `.env` themselves (step 3).
- Ask before anything that needs `sudo` or installs system packages, and say what it installs.
- Don't edit the code. If something fails, show the user the error and the fix you'd suggest.

## 1. Check the machine

Run `uname -s`, and on macOS check `command -v brew`.
- **macOS**: Homebrew is needed. If it's missing, ask the user to install it from https://brew.sh (it asks for their
  password), then continue.
- **Linux**: you'll use `apt` (or the distro's package manager) with `sudo`.
- **Windows**: not supported. Tell the user it needs macOS, Linux, or WSL (Ubuntu), and stop unless they're in WSL.

## 2. Run the setup script

```bash
bash "$PROJECT/bin/setup.sh"
```

It creates `.venv`, installs the Python packages, creates `.env` and `config.yaml` from the examples (never overwrites
them), installs the skill into `~/.claude/skills/outliers/`, runs the offline tests, and ends with a **STILL TO DO**
list. Fix each item, then run it again until it prints **ALL SET**. Usual fixes:

| Missing | macOS | Linux (Debian/Ubuntu) |
|---|---|---|
| Python 3.10+ | `brew install python@3.13` | `sudo apt install python3 python3-venv` |
| ffmpeg / ffprobe | `brew install ffmpeg` | `sudo apt install ffmpeg` |
| whisper-cli | `brew install whisper-cpp` | build it: see below |
| swiftc (macOS only) | `xcode-select --install` (the user clicks through a dialog) | – |
| whisper models | `bash "$PROJECT/bin/setup.sh" --models` (~1.7 GB download; tell the user first) | same |
| API key | step 3 | step 3 |

whisper.cpp on Linux (needs `git`, `cmake`, `build-essential`):
```bash
git clone --depth 1 https://github.com/ggml-org/whisper.cpp ~/whisper.cpp
cmake -S ~/whisper.cpp -B ~/whisper.cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build ~/whisper.cpp/build -j --config Release
sudo ln -sf ~/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper-cli
```

## 3. API key (the user does this)

Tell the user:
1. Make an account at https://scrapecreators.com and buy some credits (every search and creator check costs credits;
   a first niche run is roughly 50-100).
2. Open `PROJECT/.env` (give the full path) in a text editor and paste the key right after `SCRAPECREATORS_API_KEY=`,
   with no spaces or quotes, then save.

Wait for them to say it's done, then check with the `grep -q` command above.

## 4. Where notes go (ask the user)

Ask: **"Do you use Obsidian? If so, which vault should the outlier notes go in?"**
- **Yes**: set `OUTLIERS_VAULT=<absolute vault path>` in `.env`, and `OUTLIERS_NOTES_DIR=<folder in the vault>` if they
  want something other than `Outliers`. Edit only those lines, e.g.
  `sed -i.bak 's|^OUTLIERS_VAULT=.*|OUTLIERS_VAULT=/path/to/vault|' "$PROJECT/.env" && rm "$PROJECT/.env.bak"`.
  The tool creates an `Outliers.base` view there for browsing and sorting the notes.
- **No**: leave both empty. Notes go to `PROJECT/notes/Outliers/` as plain Markdown files with images.

## 5. Check it works

1. Run `bash "$PROJECT/bin/setup.sh"` once more. It must end with **ALL SET**.
2. Check the key and see what a search finds without downloading anything (costs about 10 credits; tell the user):
   ```bash
   "$PROJECT/.venv/bin/python" "$PROJECT/run.py" keyword "study tips" --platform tiktok --dry-run --max-credits 10
   ```
   A `401` means the key is wrong; `402` means no credits left.

## 6. Hand over

Tell the user, briefly:
- Setup is done. **Restart Claude Code** (or open a new session) so `/outliers` shows up.
- Start by adding creators they like: `/outliers add tiktok:handle ig:handle ...`. Claude proposes a niche for each
  and saves them once they confirm. Then `/outliers <niche>` finds outliers in that niche.
- One-off searches: `/outliers "keyword"` or `/outliers @handle`. Add "dry run" to preview without downloading.
- What counts as an outlier, and the content rules (English talking-head videos only, 1-3 minutes, 500k+ views,
  5× the creator's median), are in `PROJECT/config.yaml`; they can ask Claude to change any of them.
- To update later: `cd PROJECT && git pull && bash bin/setup.sh`.
