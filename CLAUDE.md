# outlier-research

A tool plus a Claude Code skill (`/outliers`) that finds outlier TikTok/Instagram videos and saves their hooks as notes.

- **Not set up yet** (no `.venv/`, no `.env`, or `bash bin/setup.sh` doesn't end with ALL SET), or the user asks to
  "set this up" / "install this": follow [SETUP.md](SETUP.md) step by step.
- **Using it**: the installed skill (`~/.claude/skills/outliers/SKILL.md`, source in `skill/SKILL.md`) has the commands.
- **Updating**: `git pull && bash bin/setup.sh` (re-installs the skill too).
- Never read or print `.env`; it holds the user's ScrapeCreators API key.
- Tests: `.venv/bin/pytest` (offline, a few seconds). Your `.env`, `config.yaml`, `state/`, `runs/` and `notes/` are
  git-ignored and personal.
