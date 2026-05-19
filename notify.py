#!/usr/bin/env python3
"""
notify.py - Telegram notifications for swell-checker.

Three modes:
  alert      - critical failure (from cron-wrap.sh)
  watchlist  - weekly digest (reads stdin OR regenerates from db)
  stdin      - pipe any text through; used by cron for arbitrary output

Shares bot + chat with peptide-corpus. Uses 🌊 prefix to distinguish.
"""
import os
import sys
import urllib.request
import urllib.parse
import urllib.error

TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
CHAT = os.environ.get("TG_CHAT_ID", "").strip()
MAX_MSG = 3800


def send(text):
    if not TOKEN or not CHAT:
        print("notify: TG_BOT_TOKEN or TG_CHAT_ID not set; skipping", file=sys.stderr)
        return False
    if len(text) > MAX_MSG:
        text = text[:MAX_MSG] + "\n…[truncated]"
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT, "text": text, "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"notify: telegram send failed: {e}", file=sys.stderr)
        return False


def mode_alert(args):
    host = os.uname().nodename
    msg = f"🌊 swell-checker ALERT ({host})\n" + " ".join(args)
    send(msg)


def mode_watchlist(args):
    # If stdin has content, send it. Otherwise, run watchlist.py.
    if not sys.stdin.isatty():
        text = sys.stdin.read()
        if text.strip():
            send(text)
            return
    # Fallback: invoke watchlist.py
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    result = subprocess.run(
        ["python3", os.path.join(here, "watchlist.py")],
        capture_output=True, text=True, timeout=60,
    )
    if result.stdout.strip():
        send(result.stdout)


def mode_stdin(args):
    """Generic: pipe arbitrary text through. Prepends 🌊 header if not present."""
    text = sys.stdin.read()
    if not text.strip():
        return
    if not text.startswith("🌊"):
        text = "🌊 swell-checker\n" + text
    send(text)


MODES = {"alert": mode_alert, "watchlist": mode_watchlist, "stdin": mode_stdin}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in MODES:
        print(f"usage: {sys.argv[0]} [{'|'.join(MODES)}] [args...]", file=sys.stderr)
        sys.exit(2)
    MODES[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
