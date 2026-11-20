#!/usr/bin/env bash
# Install (or update) the /outliers Claude Code skill from skill/SKILL.md, pointed at this project folder.
# Run again after `git pull` so the skill picks up changes. Override the target with CLAUDE_SKILLS_DIR.
set -euo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/outliers"
mkdir -p "$DEST"
# | as the sed delimiter: project paths contain / but never |
sed "s|__PROJECT_DIR__|$PROJECT|g" "$PROJECT/skill/SKILL.md" > "$DEST/SKILL.md"
echo "installed $DEST/SKILL.md (project: $PROJECT)"
