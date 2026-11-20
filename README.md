# Outlier Research

Finds outlier TikTok and Instagram videos (far above their creator's median views) and saves each one's
opening stills, transcript and on-screen hook as a Markdown note, for studying hooks. It comes with a
Claude Code skill, `/outliers`, that runs the tool, reviews each saved video and fills in the on-screen text.

**Setting this up with Claude Code?** Open Claude Code in this folder and say *"set this up"*. It follows
[SETUP.md](SETUP.md). Doing it by hand: `bash bin/setup.sh --models`, then fix whatever it lists as missing.

## What you need

- A [ScrapeCreators](https://scrapecreators.com) API key (paid, pay-per-credit; a niche run costs ~50-100 credits
  the first time, much less after). It goes in `.env`.
- Python 3.10+, `ffmpeg`, `whisper-cli` (whisper.cpp) and two whisper models in `~/.cache/whisper/`
  (`bin/setup.sh --models` downloads them, ~1.7 GB).
- macOS or Linux. The face check uses Apple Vision on macOS (needs `swiftc` from the Xcode command line tools) and
  OpenCV on Linux. Windows isn't supported (WSL should work as Linux, untested).

## Settings

- `.env` (from `.env.example`): the API key, and where notes go: `<OUTLIERS_VAULT>/<OUTLIERS_NOTES_DIR>/`.
  Point `OUTLIERS_VAULT` at an Obsidian vault to browse notes there (the tool writes an `Outliers.base` view);
  leave it empty to use `notes/` in this folder.
- `config.yaml` (from `config.example.yaml`): what counts as an outlier (5× the creator's median, 500k+ views,
  1-3 minutes, last 2 years), content rules (English talking-head videos only; no AI, skits, clip pages, stitches)
  and your niches (creators, keywords, hashtags). Every setting is explained in `config.example.yaml`.

## Updating

`git pull && bash bin/setup.sh`. Your `.env`, `config.yaml`, `state/`, `runs/` and notes are never touched.

## How it works

The talking-head face check uses Apple's Vision framework on macOS (`lib/faces.swift`) and OpenCV's YuNet model on
Linux (`lib/models/face_detection_yunet_2023mar.onnx`). YuNet was calibrated against Vision on 982 covers and stills
(height cutoff 0.047, confidence 0.78; the two agree on 965/982). `OUTLIER_FACE_BACKEND=vision|yunet` forces one.

TikTok and Instagram downloads break now and then; `.venv/bin/pip install -U yt-dlp` usually fixes them.

Run the tests: `.venv/bin/pytest` (fast, offline). `RUN_SLOW=1 .venv/bin/pytest` also runs real whisper.cpp on a spoken sample (~40s).

## Commands

```bash
.venv/bin/python run.py niche Philosophy            # every keyword, hashtag and creator in a niche
.venv/bin/python run.py creator tiktok:jonstertruck # one creator (or ig:handle, or a profile URL)
.venv/bin/python run.py keyword "study tips"        # one search term, TikTok + Instagram (--platform to pick one)
.venv/bin/python run.py set-onscreen <note.md> --text "..."   # used by the skill after reading stills
.venv/bin/python run.py add-fetch tiktok:handle ig:handle   # profiles + captions so Claude can propose niches (~3-4 credits each)
.venv/bin/python run.py add-write confirmed.json           # write confirmed creators/keywords to config.yaml
.venv/bin/python run.py niches                             # list niches
```

Flags: `--dry-run` (fetch and score, download nothing), `--max-credits N` (hard budget for this run),
`--min-length 1:00` / `--max-length 3:00` (length limits for this run; `off` removes one),
`--min-likes-tiktok 50k` / `--min-likes-ig 10k` (likes floors for this run; `off` removes one),
`--allow-ai` (keep AI-generated videos this run), `--top-n N` (how many shortlisted videos get the paid creator check),
`--vault PATH` (defaults to `OUTLIERS_VAULT` from `.env`), `--config PATH`.

Likes floors run in the free first stage (likes come with search results), so they cut credits rather than add them.
A video whose likes are hidden or missing is skipped while a floor is set. Like the length filter, they judge
only the candidate video, never the creator's baseline.

AI filter (`exclude_ai: true`, on by default): a video is skipped if the platform labels it AI-generated or its
caption uses a hashtag from `ai_hashtags` (exact tag matches, so `#ai` or `#brainrotting` don't trip `#brainrot`).
TikTok's label (`aigc_info` / `moderation_aigc_info`) comes with search results, so those are dropped for free.
Instagram's label (`gen_ai_detection_method` / `ai_label_info`) only appears in creator lists, so an Instagram search
hit is dropped after its creator check, before download. No AI-labeled Instagram reel has been seen live yet, so
that rule (any detection method except `NONE` / `CAMERA_CAPTURE`) is untested. Replay of the 2026-11-17 Education
run: 28 of 177 TikTok results carried the AI label, 3 more hit the hashtag list, and all 5 junk outliers from that
run were caught while the 3 real ones passed. Runs report `ai_skipped`.

Each run writes `runs/<timestamp>-<mode>-<value>/summary.json` and keeps raw API responses in `raw/` (both git-ignored).
The `/outliers` Claude skill (source in `skill/SKILL.md`, installed to `~/.claude/skills/outliers/`) wraps these
commands and fills in the on-screen hook text by reading each note's stills.

If credits run out (or `--max-credits` is reached), the run stops fetching but still saves the outliers found so far.

## API findings (live probe, 2026-11-14, 9 credits)

ScrapeCreators: `GET https://api.scrapecreators.com/...`, header `x-api-key`, **1 credit per call** (0 on a
`cache_max_age` cache hit). Every response includes `credits_charged` and `credits_remaining`. No rate limits.
Errors: 401 bad key, 402 out of credits, 403/404 private/age-gated/missing (uncharged), 5xx retried.

| Endpoint (helper) | Items/page | Views field | Followers | Posted | Duration | Video URL |
|---|---|---|---|---|---|---|
| `/v1/tiktok/search/keyword` (`tiktok_search`) | ~29 | `statistics.play_count` | `author.follower_count` ✅ | `create_time` (unix) | `video.duration` (**ms**) | `video.play_addr.url_list[0]` (also `download_no_watermark_addr`) |
| `/v3/tiktok/profile/videos` (`tiktok_profile_videos`) | **~7** | `statistics.play_count` | `author.follower_count` | `create_time` | `video.duration` (ms) | `video.play_addr.url_list[0]` |
| `/v1/tiktok/profile` (`tiktok_profile`) | – | – | `stats.followerCount` | – | – | – |
| `/v2/tiktok/video` (`tiktok_video`) | 1 | `statistics.play_count` | – | `create_time` | ms | `play_addr` (some videos are watermark-only) |
| `/v1/instagram/user/reels` (`ig_user_reels`) | ~12 | `media.play_count` | ❌ (use profile) | `media.taken_at` (unix) | `media.video_duration` (s) | `media.video_versions[0].url` |
| `/v2/instagram/reels/search` (`ig_reels_search`) | ~10 | `video_view_count` ✅ | ❌ | `taken_at` (ISO) | `video_duration` (s) | `video_url` |
| `/v1/instagram/search/hashtag` (`ig_hashtag_search`) | ~10 | `video_view_count` ✅ | ❌ | `taken_at` (ISO) | `video_duration` (s) | `video_url` |
| `/v1/instagram/profile` (`ig_profile`) | – | – | `data.user.edge_followed_by.count` | – | – | – |
| `/v1/instagram/post` (`ig_post`) | 1 | `video_play_count` | ❌ | `taken_at_timestamp` | `video_duration` (s) | `video_url` |

What this means for the design:

- **Instagram works.** Reels from creator lists, keyword search and hashtag search all return a view/play count and a
  direct video URL, so Instagram needs no special handling beyond missing follower counts.
- **Instagram search has no follower counts**, so Instagram's Stage 1 ranks by raw views (after the minimum-views and
  age filters) instead of views ÷ followers. Stage 2 (creator median) is unchanged.
- **TikTok creator pages return only ~7 videos per call.** A creator baseline uses up to 3 pages (~21 videos, 3 credits);
  Instagram also uses up to 3 pages (~36 reels, 3 credits) plus 1 profile call for the follower count. Baselines are cached for 7 days.
- **Search results include old videos**, so keyword calls pass `date_posted` (TikTok `last-3-months`, Instagram
  `last-month`/`last-year`) and the age filter still applies.
- **Downloads:** every video URL type downloaded directly (HTTP 206, H.264 + AAC) with no login, and `yt-dlp`
  works on both platforms as a fallback. Some TikTok videos only offer a watermarked file; that's fine for research.
- **Estimated cost of a niche run** (5 TikTok keywords, 5 TikTok creators, 5 Instagram hashtags, 5 Instagram creators,
  Stage 2 on the top 20): about 5 + 15 + 5 + 20 + ~60 ≈ **105 credits** on a first run, much less on repeats (cached baselines).

`fixtures/` holds trimmed copies of the probe responses (first 5 items, only the fields we use) for offline tests.

## Media processing notes

- Notes all sit flat in the notes folder; `niche`, `found_via` (the run) and `search` (the exact keyword, #hashtag or
  creator that surfaced the video) are properties, so `Outliers.base` can filter and sort by them.
- Each outlier gets `_media/<platform>-<id>/` with `still-0.0s.jpg`,
  `still-1.0s.jpg`, `still-2.5s.jpg`, `still-4.5s.jpg` (540px), `transcript.json` (raw whisper) and `transcript.txt`.
  The full download and the WAV are always deleted, even if a step fails.
- Downloads try the API's direct link first, then `yt-dlp` on the public post URL.
- Transcription uses whisper.cpp (large-v3-turbo) with VAD: about 40 s for a 10 s test clip, but **2-3 minutes for a real 1-3 minute video** on a laptop CPU (measured 2026-11-15).
- whisper.cpp quirk: with `--vad`, token times are on the silence-trimmed timeline while segment times are real.
  `parse_whisper` shifts each segment's words so the "spoken in the first 5 seconds" split is correct.
- Segments that are only `[Music]`, `(applause)` or `♪` count as no speech.

## Live test findings (2026-11-15, keyword `finance`, 1:00-3:00, 28 credits)

- Results: 3 TikTok outliers saved (201×, 5.9×, 868×), each with its stills, transcript and on-screen hook (5s clips were dropped on 2026-11-17).
  A rerun replayed through the scorer skipped both saved videos with no API calls.
- **TikTok keyword search returns a different mix of videos on every call.** A dry run and the real run that follows
  can surface different outliers, which is good for discovery but means a dry run is a sample, not a preview.
- **Instagram keyword search is weak for broad terms.** `finance` returned mostly corporate accounts (JPMorgan,
  Yahoo Finance, CFA Institute) with under 15K views, so nothing passed. Use Instagram creator lists for Instagram.
- **ScrapeCreators' creator-video list is incomplete for some creators.** @morgan.on.money's 2.5M-view video
  (found by keyword search) never appeared in her profile listing, sorted by latest or popular, and two recent videos
  were missing from one fetch but present in another. Creator mode can therefore miss a creator's real hits, and a
  baseline may be built from a partial list. Keyword and niche runs still find these videos through search.
- Pinned videos are excluded from creator lists (so they don't skew baselines). That's deliberate: the missing
  video above was not pinned.
