# fal.ai Sandbox Generator (Scrapling stealth mode)

Generates images and videos through the [fal.ai Sandbox](https://fal.ai/sandbox) web UI using
**your own fal.ai account's free daily generations**. It uses Scrapling's `StealthyFetcher`
(stealth browser, human-like input, Cloudflare handling) and a saved browser profile, so you only
log in once.

A local counter stops the tool at your daily limit (`daily_limit`, default 50). If fal.ai shows an
"out of free generations" message first, the tool stops and marks the day as used up.

## Setup

```bash
cd fal-sandbox-scraper
pip install -r requirements.txt
scrapling install                 # downloads the stealth browser
python fal_sandbox.py login       # a window opens, sign in to fal.ai, then press Enter
```

## Usage

```bash
python fal_sandbox.py image "a red fox in fresh snow, golden hour"
python fal_sandbox.py video "ocean waves crashing at sunset, slow motion"
python fal_sandbox.py image "logo of a coffee shop" --model "FLUX"   # pick a model from the picker
python fal_sandbox.py batch prompts.example.txt --mode image
python fal_sandbox.py status                                        # e.g. "Today: 7/50 used"
python fal_sandbox.py --show image "..."                            # watch the browser work
```

Files are saved to `outputs/image/` and `outputs/video/`.

## How it works

1. Opens the sandbox in a Scrapling stealth browser that reuses your saved login (`~/.fal_sandbox/profile`).
2. Selects Image/Video mode (and a model if `--model` is given), types the prompt and clicks Generate.
3. Watches network responses for new image/video files from fal's CDN (falls back to the newest
   `<img>`/`<video>` on the page) and downloads them.
4. Records the generation in `~/.fal_sandbox/usage.json`.

## If fal.ai changes its UI

Copy `config.example.json` to `config.json` and edit the CSS/Playwright selectors. Run with `--show`
to see which step fails. Each selector list is tried in order.

## Notes

- Use one account — your own. The daily cap is there so you stay within what fal.ai gives you
  for free; don't use this to get around their limits.
- Video generations can take several minutes; raise `video_timeout_s` if needed.
