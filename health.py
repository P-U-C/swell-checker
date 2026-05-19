#!/usr/bin/env python3
"""health.py - verify claude CLI auth + env. Same structure as peptide-corpus."""
import os
import shutil
import subprocess
import sys


def main():
    claude = shutil.which("claude")
    if not claude:
        print("FAIL: 'claude' not found on PATH", file=sys.stderr)
        print(f"  PATH={os.environ.get('PATH','<unset>')}", file=sys.stderr)
        sys.exit(1)
    print(f"ok   claude CLI at: {claude}")

    home = os.environ.get("HOME")
    if not home:
        print("FAIL: $HOME is not set. In cron, add 'HOME=/home/swell' to the crontab.", file=sys.stderr)
        sys.exit(3)
    print(f"ok   HOME={home}")
    if os.path.isdir(os.path.join(home, ".claude")):
        print(f"ok   ~/.claude/ exists")
    else:
        print(f"WARN ~/.claude/ does not exist — login may not have completed", file=sys.stderr)

    try:
        r = subprocess.run(
            ["claude", "-p", "Reply with exactly: auth ok", "--output-format", "text"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        print("FAIL: claude CLI timed out (60s)", file=sys.stderr)
        sys.exit(3)

    if r.returncode != 0:
        err = (r.stderr or "").strip()
        lc = err.lower()
        if any(m in lc for m in ("login", "unauthorized", "authenticate", "not logged in", "credentials", "oauth", "token")):
            print(f"FAIL: auth required — run 'claude' as this user to log in.", file=sys.stderr)
            print(f"  stderr: {err[:300]}", file=sys.stderr)
            sys.exit(2)
        print(f"FAIL: claude exited {r.returncode}: {err[:300]}", file=sys.stderr)
        sys.exit(3)

    print(f"ok   round-trip prompt succeeded")
    print("\nhealth: all checks passed")


if __name__ == "__main__":
    main()
