---
name: outliers
description: Find outlier short-form videos (far above their creator's usual views) on TikTok and Instagram and save each one's opening stills, spoken hook, on-screen hook and transcript as Markdown notes (in the user's Obsidian vault or any folder set in the tool's .env). Use when the user types /outliers, or asks to "find outliers", "research viral videos in <niche>", "what's blowing up for @creator", "outlier research on <keyword>", and for follow-ups like "dry run fitness first", "only TikTok", "add @handle to a niche", "what niches do I have".
---

# Outlier Research

Finds videos that beat their creator's median views by 5× or more and saves the raw material for studying their hooks.
**Only English talking-head videos count**: a person talking to camera as the A-roll (overlay photos and B-roll on
top are fine). No faceless videos, animations, AI renders, character skits/POV acting, clips of someone else
speaking (fan/edit pages, podcast, news, debate or speech clips), or stitches/duets (even of the creator's own video). The script filters most of these; you catch the rest.
**Deliver raw material only.** Don't analyze why a video worked, rate hooks or critique scripts unless the user asks.

Everything mechanical is in `run.py`. Don't write your own fetching, scoring or note code. Your jobs are:
1. Run the right command.
2. Read each new note's stills: reject anything that breaks the content rules, and record the on-screen hook text.
3. Report the results.

```
PROJECT=__PROJECT_DIR__
PY=$PROJECT/.venv/bin/python
```

**First use:** if `$PY` or `$PROJECT/.env` doesn't exist, or `grep -q '^SCRAPECREATORS_API_KEY=.' $PROJECT/.env`
fails (never print `.env`; it holds the key), the tool isn't set up yet. Follow `$PROJECT/SETUP.md` with the user before running anything.

## Commands

| User says | Run |
|---|---|
| `/outliers <niche>` | `$PY $PROJECT/run.py niche "<niche>"` |
| `/outliers @handle`, `tiktok:handle`, `ig:handle`, or a profile URL | `$PY $PROJECT/run.py creator "<handle>"` (add `--platform tiktok\|ig` if the handle has no prefix and isn't in a niche; the script says so) |
| `/outliers "keyword"` | `$PY $PROJECT/run.py keyword "<term>"` (add `--platform tiktok` or `--platform ig` for only one) |
| "dry run", "just check", "how many would it find" | add `--dry-run` (fetches and scores, downloads nothing) |
| the user mentions a credit limit | add `--max-credits N` |
| "only 1–3 minute videos", "under 60 seconds", "no length limit" | add `--min-length 1:00` / `--max-length 3:00` (`off` removes a limit) for this run; to change the default, edit `min_duration_s` / `max_duration_s` in `config.yaml` |
| "at least 50k likes", "only well-liked videos", "no likes limit" | add `--min-likes-tiktok 50k` / `--min-likes-ig 10k` (`off` removes a limit) for this run; to change the default, edit `min_likes_tiktok` / `min_likes_ig` in `config.yaml` |
| "include AI videos", "don't filter AI" | add `--allow-ai` for this run; to change the default or the hashtag list, edit `exclude_ai` / `ai_hashtags` in `config.yaml` |
| "at least 1M views", "lower the view floor" | add `--min-views 1m` for this run; to change the default, edit `min_views` in `config.yaml` (currently 500k) |
| "include other languages" | add `--any-language` for this run (default: English only) |
| "include skits / faceless / clips", "any format" | add `--any-format` for this run (turns off the talking-head check and `skip_hashtags`) |
| "check fewer/more creators", tight on credits | add `--top-n N` (how many shortlisted videos get the paid creator check, ~3–4 credits each) |
| `/outliers add @a @b ...`, "add these creators", "here are some creators" | see **Adding creators** below |
| `/outliers niches`, "what niches do I have" | `$PY $PROJECT/run.py niches` and show the output |

If an argument could be a niche or a keyword, check the niche list first (`$PY $PROJECT/run.py niches`, or read `$PROJECT/config.yaml`).
Treat it as a niche only if one matches by name.

**Run it in the background** (Bash `run_in_background: true`). Each outlier takes about 2–3 minutes to download and transcribe, so a run can exceed the 10-minute foreground limit. You'll be notified when it finishes.
The last line printed is `summary: <path>/summary.json`.

## Testing search terms (seed videos)

When the user sends a seed video to work out search terms: read the video and the creator's other videos, draft
narrow niche phrases plus a few broad ones, then run
`$PY $PROJECT/run.py probe "<term>" ["<term>" ...] --seed <video URL> --repeat 2` (1 credit per search; the date window
follows `max_age_days`). It lists every result and grades it "fits format" (English, 1–3 min, no skipped hashtag, face on
the cover) and "fits filters" (also views/likes/age); `probe.json` in the run folder has every row. Judge a term by its
format-fit share and by whether it finds the seed creator's videos (TikTok shuffles results, so the exact seed often
won't show). Creators with an outlier (≥5× their median, ≥500k, right format) get added to a niche right away with
`add-fetch` + `add-write`, without asking; tell the user who was added. Add the terms that worked to the niche too.

## Hunts: keep going until N new outliers

A hunt finds outliers until a goal number of **new, kept** notes is reached, by crawling: bank creators →
turn their outliers into search terms → probe → harvest new creators → bank them → repeat. Niches can grow
freely: a creator outside every niche gets a new one.

**Starting one** (when the user asks for "N outliers"; best on an always-on machine, since a hunt runs for hours):
`bash $PROJECT/bin/outliers-hunt.sh 100` (options: `--budget` credits,
default 20 × goal; `--batch-minutes`, default 45; `--max-batches`, default 40; `--model` and `--effort` for the batch agents, e.g. `--model claude-sonnet-5-5 --effort medium`). It returns at once; a loop in tmux
runs one fresh agent per batch until the goal is reached, the budget is spent, 3 batches in a row keep nothing, or
`state/hunts/<id>.stop` exists. Progress: `run.py hunt status <id>`, and the report
`$PROJECT/reports/hunt-<id>.md`.

**Hunt mode: what one batch agent does.** `OUTLIER_HUNT` is set, so every `run.py` command tags its notes and
charges its credits to the hunt automatically. The tool keeps the facts: `state/explored.json` (every creator banked
and term probed, by any hunt) and `state/hunts/<id>.json`. You keep the judgement in the plan file.

1. Run `$PY $PROJECT/run.py hunt status $OUTLIER_HUNT` and read the plan file it names. On the first batch the plan
   is empty: seed its creator queue from `run.py harvest --niches` (niche creators not banked yet).
2. Work through the plan, aiming for as many new kept outliers as your time allows (each download takes ~3 min, so
   ~10–15 per batch; a creator check at `--baseline-pages 10` costs ~10 credits):
   - **Bank queued creators first**, best evidence first: `run.py creator tiktok:<handle> --baseline-pages 10`.
   - **When fewer than ~5 creators with evidence are queued, find more.** Draft 5–10 new search terms from this hunt's
     kept notes (captions, hashtags, transcript phrases, the topics of creators that worked). Prefer narrow niche
     phrases over broad words, which pull in other genres. Run `run.py probe "<t1>" "<t2>" ... --quiet` (terms probed
     in the last 30 days are skipped for free), then `run.py harvest <probe.json> [<summary.json> ...]` and queue
     the top creators. A term with a high format-fit share can also go straight to
     `run.py keyword "<term>" --platform tiktok`, which saves outliers itself and whose summary.json feeds harvest.
   - Check `hunt status` after each creator or keyword run; stop as soon as it says DONE.
3. After every command that saved notes, do **After a run** below for each new note (review, reject rule-breakers,
   fill in the on-screen hook). Add every creator with a kept outlier to a niche with `add-write` (no `add-fetch`
   needed; pick the niche from their notes' topics; create a new niche when none fits), and add search terms with a
   high format-fit share to that niche's `tiktok_keywords`.
4. When your time is about up (check `date`), or the hunt is DONE, before you exit:
   - **Plan file:** rewrite the creator queue (best first, with why), terms still to try, dead ends, and notes for
     the next batch.
   - **Report** `$PROJECT/reports/hunt-<id>.md` (create it if missing): a header with goal, kept so far and
     credits (from `hunt status`), then add a section for this batch: a table of its kept outliers (score, views,
     creator, note link, on-screen hook), creators and niches added, terms that worked, anything rejected or failed.
5. Stay inside the hunt: don't change code, don't run outlier commands without `OUTLIER_HUNT`, and follow the content
   rules above. Never ask the user anything; nobody is watching. Make the call and note it in the report.

## After a run (not a dry run): review each note, then read the on-screen hooks

Read `summary.json`. For every entry in `notes`:

1. Look at each image in `stills_abs` (stills at 0s, 1s, 2.5s and 4.5s), together with `creator`, `speech` and
   the note's caption. Reject the note if it breaks a content rule the script can't fully see:
   - **character skit or POV acting**: wigs, costumes, playing a role ("POV: your barista is Gen Z", "millennial vs Gen Z")
   - **someone else speaking**: a clip of another person (school-board or event speech, podcast, interview,
     news or debate footage, a fan or edit page reposting a known creator)
   - **not a person talking to camera**: animation, AI or 3D render, stock footage, text over music
   - **stitch or duet**: another video plays first or alongside (a stitched clip at the start, a split screen, an
     inset of someone else's video, the transcript opening in another voice), even when it's the creator's own older
     video. The early stills (0s, 1s) usually show it. TikTok marks no flag for these, so only review catches them
   - **not English**
   `$PY $PROJECT/run.py reject "<note_abs>" [more notes...] --reason "<short reason>"` moves the note and its stills
   to the Trash (`~/.Trash`, recoverable) and keeps the video blocked so no run saves it again. When unsure, keep it and
   say so in the report. Skip steps 2–4 for rejected notes.
2. Write down the text burned into the video **exactly as shown**: same words, line breaks, capitals and emoji. Merge the frames: list text that stays on screen across stills once, and add new text in the order it appears.
3. Ignore anything that isn't part of the creator's own text overlay: platform watermarks (e.g. "TikTok @handle"), auto-captions that just repeat the speech word for word, and app UI.
4. Save it:
   `$PY $PROJECT/run.py set-onscreen "<note_abs>" --text "<exact text>"`
   Use `--text none` if there's no overlay text.

## Report back

Keep it short:
- One line of counts: candidates checked, outliers found, notes written, credits used (and credits remaining).
- One line on the content rules: `rule_skipped` (free skips by reason, e.g. not English, #parody), `rejected`
  (deleted after download, with reasons like "no person on camera") and how many notes you rejected on review.
- A table of the top outliers (up to 10): score (e.g. `12.3×`), views, creator, platform, and the note as a markdown link (`note` is relative to the vault; use `note_abs` when the current folder isn't the vault). Add the spoken hook for each, quoted, if short.
- Any `failures`, in plain words. Failed downloads aren't marked as done, so the next run retries them.
- If `out_of_credits` is true, say the run stopped early and that results found before then were saved.
- For a dry run, show `top_checked` (the scores) and how many would be saved. Nothing was downloaded.
- If nothing passed, say so and give the best scores from `top_checked`, so the user can decide whether to lower `min_multiple` or `min_views` in `config.yaml`.

All notes land flat in the notes folder, `<OUTLIERS_VAULT>/<OUTLIERS_NOTES_DIR>` from `$PROJECT/.env` (default `$PROJECT/notes/Outliers/`; no subfolders). Each note's properties say where it came from: `niche` (empty for one-off keyword runs and creators outside a niche), `found_via` (the run, e.g. `niche: Education`) and `search` (the exact search that surfaced it, e.g. `keyword "education system"`, `#literacy`, `creator @handle`). No video clip is kept, only the four stills and the transcript. In Obsidian, the user can browse, filter and sort them with `Outliers.base` (an Obsidian Bases view the tool creates).

## Adding creators

The user gives handles; **you** work out each creator's niche, and the user confirms before anything is saved.

1. Every handle needs a platform: `tiktok:handle`, `ig:handle`, or a profile URL. If the user gave bare handles and
   didn't say which platform, ask once for all of them (or pass `--platform` if they said "these are all TikTok").
2. Fetch: `$PY $PROJECT/run.py add-fetch <handle> [<handle> ...]`. This costs about 3–4 credits per creator.
   It also caches each creator's baseline, so their first niche run costs less. It writes `add_candidates.json`,
   whose path is the last line printed.
3. Read `add_candidates.json`. For each creator, use their `bio`, `name` and recent `captions` to pick a niche:
   - **Prefer an existing niche** from `known_niches` (the config's niches plus any subfolders of
     the vault's `OUTLIERS_CREATORS_DIR` folder, if the user keeps one). Match
     the existing spelling and capitals exactly.
   - Suggest a **new** niche name only when nothing fits. Use a short Title Case name, like the existing folders.
   - Skip creators with `already_in_niche` (say where they already are) or an `error` (say what went wrong).
4. Show **one table** for the user to confirm: handle, platform, followers, proposed niche (mark **new**), and a
   one-line reason based on what they post. For new niches, also suggest 3–5 TikTok keywords and 3–5 Instagram
   hashtags for that niche. Then **wait** for the user to confirm or change rows.
5. Write the confirmed rows to a JSON file in your scratchpad and run
   `$PY $PROJECT/run.py add-write <file>`:
   ```json
   {"creators": [{"handle": "jonstertruck", "platform": "tiktok", "niche": "Philosophy"}],
    "niche_searches": {"Philosophy": {"tiktok_keywords": ["stoicism"], "ig_hashtags": ["philosophy"], "ig_keywords": []}}}
   ```
   Include `niche_searches` only for keywords and hashtags the user approved. Existing entries are never
   duplicated.
6. Report what was added. The user can then run `/outliers <niche>`.

## Good to know

- **Runs happen on this machine.** A sleeping laptop or a closed terminal kills a long run, so keep the machine awake
  for big ones. Never run two outlier commands at once: they share `$PROJECT/state/`.
  - **Unattended jobs** on an always-on Linux box (needs tmux, flock and the claude CLI there):
    `bash $PROJECT/bin/outliers-agent.sh "<task>"` starts a Claude agent in tmux that does the whole skill; its log is
    in `~/outlier-agent-logs/`.

- Scores compare each video against the median of the same creator's recent videos, leaving the video itself out. TikTok uses ~21 recent videos and Instagram ~36.
- Each creator's baseline is cached for 7 days, and videos already saved are skipped, so repeat runs are cheap.
- Instagram search results have no follower counts. The Stage 1 shortlist ranks them by raw views, but the final score is still based on the creator's median.
- **Content rules** (`config.yaml`, all on): `english_only` (TikTok's spoken-language tag filters for free;
  Instagram gets a ~25 s audio language check after download), `talking_head_only` (12 frames across the video
  get a face check (Apple Vision on macOS, OpenCV YuNet on Linux, calibrated to agree on ~98% of
  images), with a face in at least 60% of them; then the speech must reach `min_words_per_min`, 60, which
  catches text-over-music videos), and `skip_hashtags` (skit, POV, edit/fan-page, podcast/news/debate tags, with
  `*edit` matching any tag ending in "edit"). Videos that fail after download are deleted and recorded in
  `state/seen.json` with the reason, so they're never downloaded again. When you reject several notes for the
  same reason and their captions share a format hashtag, suggest adding it to `skip_hashtags`.
- Thresholds live in `config.yaml` under `thresholds` (min_multiple 5, min_views 500k, max_age_days 730 (2 years; TikTok search runs all-time and older videos are dropped), stage2_top_n 20, baseline_pages 3, plus min_duration_s / max_duration_s, currently 1:00–3:00, and min_likes_tiktok / min_likes_ig, currently 50k / 10k, plus exclude_ai with its ai_hashtags list, currently on). The AI filter skips videos the platform labels AI-generated or whose caption uses a listed hashtag; the summary's `ai_skipped` says how many. When you read stills and a saved video is still obviously AI-generated, tell the user and suggest the hashtag to add. Length and likes filters only apply to the videos being judged. A creator's baseline always uses all of their recent videos.
- TikTok keyword search returns a different mix each call, so a dry run is a sample, not an exact preview. Instagram keyword search is weak for broad terms; Instagram creator lists work better.
- The creator-video listing can miss some of a creator's videos (a provider limitation), so creator mode may not find every hit.
- If the API key is rejected or credits run out, tell the user. Never ask them to paste the key in chat; it goes in `$PROJECT/.env`.
