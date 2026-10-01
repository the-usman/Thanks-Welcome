#!/usr/bin/env python3
"""
fal.ai Sandbox automation built on Scrapling's StealthyFetcher.

Drives the fal.ai Sandbox web UI with *your own* account (via cookies you
export from your browser) and saves the generated images / videos to disk. A local daily counter makes sure the
tool stops at your free daily allowance instead of running past it.

Usage:
    # put your exported fal.ai cookies in cookies.json (or cookies.txt) first
    python fal_sandbox.py image "a red fox in snow"   # generate an image
    python fal_sandbox.py video "waves at sunset"     # generate a video
    python fal_sandbox.py batch prompts.txt --mode image
    python fal_sandbox.py status                      # today's usage
"""
from __future__ import annotations

import argparse
import inspect
import json
import mimetypes
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable

try:
    from scrapling.fetchers import StealthyFetcher
except ImportError:  # pragma: no cover
    sys.exit('Scrapling is not installed. Run:  pip install "scrapling[fetchers]" && scrapling install')

try:
    from scrapling.fetchers import StealthySession
except ImportError:  # older Scrapling versions have no session class
    StealthySession = None

HOME = Path.home() / ".fal_sandbox"
CONFIG_PATH = Path(__file__).with_name("config.json")

DEFAULT_CONFIG = {
    "sandbox_url": "https://fal.ai/sandbox",
    "daily_limit": 50,
    "output_dir": "outputs",
    # Exported fal.ai cookies: Cookie-Editor / EditThisCookie JSON, Netscape
    # cookies.txt, or a raw "name=value; name2=value2" Cookie header string.
    "cookies_file": "cookies.json",
    "usage_file": str(HOME / "usage.json"),
    "headless": True,
    "humanize": True,
    "solve_cloudflare": True,
    "image_timeout_s": 180,
    "video_timeout_s": 900,
    "delay_between_jobs_s": 8,
    # Selectors are tried in order; the first visible match wins.
    # If fal.ai changes its UI, adjust these in config.json.
    "selectors": {
        "image_tab": ["button:has-text('Image')", "[role=tab]:has-text('Image')"],
        "video_tab": ["button:has-text('Video')", "[role=tab]:has-text('Video')"],
        "model_picker": ["button[aria-haspopup='listbox']", "button:has-text('Model')", "[data-testid='model-select']"],
        "model_search": ["input[placeholder*='Search' i]", "[role=combobox] input", "[cmdk-input]"],
        "prompt": [
            "textarea[placeholder*='prompt' i]",
            "textarea[name='prompt']",
            "textarea",
            "[contenteditable='true']",
        ],
        "generate": [
            "button:has-text('Generate')",
            "button:has-text('Run')",
            "button:has-text('Create')",
            "button[type='submit']",
        ],
    },
    # Responses whose URL matches this pattern and have an image/video
    # content type are treated as generation results.
    "media_url_pattern": r"(fal\.media|fal-cdn|storage\.googleapis\.com|\.fal\.run)",
    "quota_exhausted_text": [
        "out of free",
        "daily limit",
        "limit reached",
        "no free generations",
        "come back tomorrow",
    ],
}


# --------------------------------------------------------------------------- #
# Config & quota
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text())
        selectors = user.pop("selectors", {})
        cfg.update(user)
        cfg["selectors"].update(selectors)
    return cfg


class QuotaTracker:
    """Counts successful generations per local calendar day."""

    def __init__(self, path: str, limit: int):
        self.path = Path(path)
        self.limit = limit
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {}

    @property
    def used_today(self) -> int:
        return self._load().get(date.today().isoformat(), 0)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used_today)

    def record(self, n: int = 1) -> None:
        data = self._load()
        key = date.today().isoformat()
        data[key] = data.get(key, 0) + n
        self.path.write_text(json.dumps(data, indent=2))

    def mark_exhausted(self) -> None:
        """The site said we're out — trust it over our own counter."""
        data = self._load()
        data[date.today().isoformat()] = max(self.limit, data.get(date.today().isoformat(), 0))
        self.path.write_text(json.dumps(data, indent=2))


class QuotaExhausted(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Cookies
# --------------------------------------------------------------------------- #
_SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}


def _pw_cookie(c: dict) -> dict:
    """Convert a browser-extension cookie export entry to Playwright's format."""
    out = {
        "name": c["name"],
        "value": str(c["value"]),
        "domain": c.get("domain") or ".fal.ai",
        "path": c.get("path") or "/",
        "secure": bool(c.get("secure", True)),
        "httpOnly": bool(c.get("httpOnly", False)),
    }
    expires = c.get("expires", c.get("expirationDate"))
    if expires not in (None, -1) and not c.get("session"):
        out["expires"] = int(float(expires))
    same_site = _SAME_SITE.get(str(c.get("sameSite", "")).lower())
    if same_site:
        out["sameSite"] = same_site
        if same_site == "None":
            out["secure"] = True
    return out


def load_cookies(path: str) -> list[dict]:
    file = Path(path)
    if not file.exists():
        sys.exit(
            f"Cookie file '{path}' not found.\n"
            "Export your fal.ai cookies (e.g. with the Cookie-Editor extension -> Export -> JSON)\n"
            "and save them there, or pass --cookies PATH."
        )
    text = file.read_text().strip()

    if text.startswith(("[", "{")):  # JSON export
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        cookies = [_pw_cookie(c) for c in data]
    elif "\t" in text:  # Netscape cookies.txt
        cookies = []
        for line in text.splitlines():
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            if not line or line.startswith("#"):
                continue
            domain, _sub, cpath, secure, expires, name, value = line.split("\t")[:7]
            cookies.append(_pw_cookie({
                "domain": domain, "path": cpath, "secure": secure.upper() == "TRUE",
                "expires": int(expires) or None, "name": name, "value": value, "httpOnly": http_only,
            }))
    else:  # raw Cookie header "a=1; b=2" (Set-Cookie attributes like Max-Age are skipped)
        attrs = {"path", "domain", "expires", "max-age", "samesite", "secure", "httponly", "partitioned", "priority"}
        cookies = [
            _pw_cookie({"name": k.strip(), "value": v.strip()})
            for line in text.splitlines()
            for k, sep, v in (pair.partition("=") for pair in re.sub(r"^(Set-)?Cookie:", "", line.strip(), flags=re.I).split(";"))
            if sep and k.strip() and k.strip().lower() not in attrs
        ]

    cookies = [c for c in cookies if "fal" in c["domain"]]
    if not cookies:
        sys.exit(f"No fal.ai cookies found in '{path}'.")
    return cookies


# --------------------------------------------------------------------------- #
# Scrapling helpers
# --------------------------------------------------------------------------- #
def _supported_kwargs(fn: Callable, kwargs: dict) -> dict:
    """Drop kwargs this Scrapling version doesn't accept (API differs between releases)."""
    params = inspect.signature(fn).parameters
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def browser_kwargs(cfg: dict, headless: bool | None = None) -> dict:
    return {
        "headless": cfg["headless"] if headless is None else headless,
        "humanize": cfg["humanize"],
        "solve_cloudflare": cfg["solve_cloudflare"],
        "network_idle": True,
        "timeout": 90_000,
    }


def first_visible(page, selectors: list[str], timeout_ms: int = 4000):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in selectors:
            loc = page.locator(sel)
            try:
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if el.is_visible() and el.is_enabled():
                        return el
            except Exception:
                continue
        page.wait_for_timeout(250)
    return None


def page_has_quota_message(page, phrases: list[str]) -> bool:
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return False
    return any(p in text for p in phrases)


# --------------------------------------------------------------------------- #
# The generation page action
# --------------------------------------------------------------------------- #
def make_generate_action(
    cfg: dict, prompt: str, mode: str, model: str | None, out_dir: Path, result: dict, cookies: list[dict]
):
    sel = cfg["selectors"]
    media_re = re.compile(cfg["media_url_pattern"])
    want = "video/" if mode == "video" else "image/"
    timeout_s = cfg["video_timeout_s"] if mode == "video" else cfg["image_timeout_s"]

    def action(page):
        seen_before: set[str] = set()
        captured: list[str] = []
        armed = {"on": False}

        def on_response(resp):
            try:
                ctype = (resp.headers or {}).get("content-type", "")
                url = resp.url
                if not ctype.startswith(want) or not media_re.search(url):
                    return
                if not armed["on"]:
                    seen_before.add(url)
                elif url not in seen_before and url not in captured:
                    captured.append(url)
            except Exception:
                pass

        # Load your account cookies into the browser, then reload as you.
        page.context.add_cookies(cookies)
        page.on("response", on_response)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1500)

        if page_has_quota_message(page, cfg["quota_exhausted_text"]):
            result["quota_exhausted"] = True
            return page

        # Pick image / video mode (best effort — some layouts are model-driven).
        tab = first_visible(page, sel["video_tab" if mode == "video" else "image_tab"], 2500)
        if tab:
            tab.click()
            page.wait_for_timeout(800)

        # Optional model selection.
        if model:
            picker = first_visible(page, sel["model_picker"], 3000)
            if picker:
                picker.click()
                search = first_visible(page, sel["model_search"], 3000)
                if search:
                    search.fill(model)
                    page.wait_for_timeout(1000)
                option = first_visible(page, [f"[role=option]:has-text('{model}')", f"text={model}"], 4000)
                if option:
                    option.click()
                    page.wait_for_timeout(800)
                else:
                    print(f"  ! model '{model}' not found in picker, using the current one")
                    page.keyboard.press("Escape")

        box = first_visible(page, sel["prompt"], 10_000)
        if not box:
            result["error"] = "prompt box not found (update 'selectors.prompt' in config.json)"
            return page
        box.click()
        box.fill("")
        box.type(prompt, delay=25)  # human-ish typing

        armed["on"] = True
        btn = first_visible(page, sel["generate"], 5000)
        if btn:
            btn.click()
        else:
            page.keyboard.press("Control+Enter")

        # Wait for the result to arrive over the network.
        deadline = time.time() + timeout_s
        while time.time() < deadline and not captured:
            if page_has_quota_message(page, cfg["quota_exhausted_text"]):
                result["quota_exhausted"] = True
                return page
            page.wait_for_timeout(2000)

        if not captured:
            # Fallback: newest <video>/<img> in the DOM that wasn't there before.
            tag = "video" if mode == "video" else "img"
            srcs = page.eval_on_selector_all(
                f"{tag}, {tag} source",
                "els => els.map(e => e.currentSrc || e.src).filter(Boolean)",
            )
            captured.extend(s for s in srcs if media_re.search(s) and s not in seen_before)

        if not captured:
            result["error"] = f"no {mode} appeared within {timeout_s}s"
            return page

        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower())[:40].strip("-") or mode
        for i, url in enumerate(captured):
            resp = page.context.request.get(url)
            ext = mimetypes.guess_extension(resp.headers.get("content-type", "").split(";")[0]) or (
                ".mp4" if mode == "video" else ".png"
            )
            path = out_dir / f"{stamp}_{slug}{'_' + str(i) if i else ''}{ext}"
            path.write_bytes(resp.body())
            result.setdefault("files", []).append(str(path))
        return page

    return action


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_status(cfg: dict, _args) -> None:
    q = QuotaTracker(cfg["usage_file"], cfg["daily_limit"])
    print(f"Today: {q.used_today}/{q.limit} used, {q.remaining} remaining")


def run_jobs(cfg: dict, prompts: list[str], mode: str, model: str | None, headless: bool | None) -> int:
    cookies = load_cookies(cfg["cookies_file"])
    quota = QuotaTracker(cfg["usage_file"], cfg["daily_limit"])
    out_dir = Path(cfg["output_dir"]) / mode
    if quota.remaining == 0:
        print(f"Daily limit of {quota.limit} reached — try again tomorrow.")
        return 1

    if len(prompts) > quota.remaining:
        print(f"Only {quota.remaining} generations left today; running the first {quota.remaining} prompts.")
        prompts = prompts[: quota.remaining]

    kw = browser_kwargs(cfg, headless)
    failures = 0

    def one(fetch: Callable, prompt: str) -> None:
        nonlocal failures
        result: dict = {}
        action = make_generate_action(cfg, prompt, mode, model, out_dir, result, cookies)
        fetch(cfg["sandbox_url"], **_supported_kwargs(fetch, {**kw, "page_action": action}))
        if result.get("quota_exhausted"):
            quota.mark_exhausted()
            raise QuotaExhausted
        if result.get("files"):
            quota.record()
            for f in result["files"]:
                print(f"  ✓ saved {f}")
        else:
            failures += 1
            print(f"  ✗ {result.get('error', 'unknown error')}")

    try:
        if StealthySession is not None:
            session_kw = _supported_kwargs(StealthySession.__init__, kw)
            with StealthySession(**session_kw) as session:
                for n, p in enumerate(prompts, 1):
                    print(f"[{n}/{len(prompts)}] {mode}: {p}")
                    one(session.fetch, p)
                    if n < len(prompts):
                        time.sleep(cfg["delay_between_jobs_s"])
        else:
            for n, p in enumerate(prompts, 1):
                print(f"[{n}/{len(prompts)}] {mode}: {p}")
                one(StealthyFetcher.fetch, p)
                if n < len(prompts):
                    time.sleep(cfg["delay_between_jobs_s"])
    except QuotaExhausted:
        print("fal.ai reports your free generations are used up for today. Stopping.")
        return 1

    print(f"Done. Today: {quota.used_today}/{quota.limit} used.")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="fal.ai Sandbox image/video generator (Scrapling stealth mode)")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    ap.add_argument("--cookies", help="path to exported fal.ai cookies (default: cookies.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show today's usage")

    for mode in ("image", "video"):
        p = sub.add_parser(mode, help=f"generate a {mode}")
        p.add_argument("prompt")
        p.add_argument("--model", help="model name as shown in the sandbox model picker")

    b = sub.add_parser("batch", help="run one prompt per line from a text file")
    b.add_argument("file")
    b.add_argument("--mode", choices=("image", "video"), default="image")
    b.add_argument("--model")

    args = ap.parse_args()
    cfg = load_config()
    headless = False if args.show else None
    if args.cookies:
        cfg["cookies_file"] = args.cookies
    if args.cmd == "status":
        cmd_status(cfg, args)
        return 0
    if args.cmd in ("image", "video"):
        return run_jobs(cfg, [args.prompt], args.cmd, args.model, headless)
    prompts = [l.strip() for l in Path(args.file).read_text().splitlines() if l.strip() and not l.startswith("#")]
    return run_jobs(cfg, prompts, args.mode, args.model, headless)


if __name__ == "__main__":
    sys.exit(main())
