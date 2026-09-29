#!/usr/bin/env python3
"""Nagios-style health check for a running broker: exit 0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN.

Reads `/api/health`, `/api/session/status` and `/api/session/exports`, prints
one status line and exits with the worst state it found. Standard library
only, so it runs from cron, a container healthcheck or a monitoring agent with
nothing installed.

    BROKER_SECRET=... scripts/check_broker.py --url http://127.0.0.1:8000/streaming

Where to run it, and what every line it prints means, is in
docs/content/docs/deployment/monitoring.mdx.
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any, NamedTuple, Optional

OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3
_LABELS = {OK: "OK", WARNING: "WARNING", CRITICAL: "CRITICAL", UNKNOWN: "UNKNOWN"}


class Limits(NamedTuple):
    """The thresholds a reading is judged against.

    Attributes:
        max_session_hours: A session running longer than this is probably one nobody exited.
        max_exit_seconds: An exit slower than this is a teardown worth looking at.
        max_kept_archives: Save archives on disk above this many never reached RomM.
        max_last_exit_hours: How long an alert about the last exit stays raised
            when nothing on disk tracks it.
    """

    max_session_hours: float
    max_exit_seconds: float
    max_kept_archives: int
    max_last_exit_hours: float


def evaluate(
    status: dict[str, Any], exports: list[dict[str, Any]], now: float, limits: Limits
) -> list[tuple[int, str]]:
    """Judge one status and exports reading against the limits.

    Args:
        status: The `/api/session/status` body.
        exports: The `exports` list from `/api/session/exports`.
        now: The current Unix time, which session age is measured against.
        limits: The thresholds to apply.

    Returns:
        Every problem found as `(state, message)`, worst first; empty when healthy.
    """
    problems: list[tuple[int, str]] = []
    sid = status.get("session_id")
    if status.get("active"):
        if not status.get("emulator_alive"):
            problems.append((CRITICAL, f"session {sid}: emulator {status.get('emulator')} is not running"))
        if status.get("boot_failed"):
            problems.append((WARNING, f"session {sid}: emulator reports a failed boot"))
        started = status.get("started_at")
        if started is not None and now - started > limits.max_session_hours * 3600:
            hours = (now - started) / 3600
            problems.append((WARNING, f"session {sid} has run {hours:.1f}h"))

    last = status.get("last_exit")
    if last:
        what = f"last exit {last.get('session_id')}"
        # A kept archive is tracked by the exports listing, so its alert clears
        # once the operator deletes it. The rest leave nothing on disk to watch
        # and would otherwise stay raised until the next exit, fixed or not.
        recent = now - last.get("ended_at", now) <= limits.max_last_exit_hours * 3600
        kept = last.get("archive_path")
        kept_names = {e.get("name") for e in exports}
        if last.get("dump_error"):
            if recent:
                problems.append((CRITICAL, f"{what}: save dump failed: {last['dump_error']}"))
        elif last.get("upload") == "failed" and not kept:
            if recent:
                problems.append((CRITICAL, f"{what}: upload failed and the archive could not be kept"))
        elif last.get("upload") == "failed" and os.path.basename(kept) in kept_names:
            problems.append((WARNING, f"{what}: upload failed, archive kept at {kept}"))
        if recent and last.get("duration_s", 0) > limits.max_exit_seconds:
            problems.append((WARNING, f"{what} took {last['duration_s']}s"))

    if len(exports) > limits.max_kept_archives:
        problems.append((WARNING, f"{len(exports)} save archive(s) kept on disk, not in RomM"))
    problems.sort(key=lambda p: p[0], reverse=True)
    return problems


def _default_url() -> str:
    """The broker's own address inside the container, under its `SUBFOLDER` prefix.

    Run through `docker exec`, the script inherits the container's `BROKER_HOST`,
    `BROKER_PORT` and `SUBFOLDER`, so it finds the broker the way the broker's
    settings place it.

    Returns:
        `$BROKER_URL` when set, else the broker's bind address plus the prefix,
        with a wildcard bind address read as loopback.
    """
    if os.environ.get("BROKER_URL"):
        return os.environ["BROKER_URL"]
    host = os.environ.get("BROKER_HOST", "").strip()
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"
    port = os.environ.get("BROKER_PORT", "8000").strip()
    prefix = "/" + os.environ.get("SUBFOLDER", "/streaming/").strip().strip("/")
    return f"http://{host}:{port}" + prefix.rstrip("/")


def _get(url: str, secret: Optional[str], timeout: float, context: Optional[ssl.SSLContext]) -> Any:
    """GET a broker route and decode its JSON body.

    Args:
        url: The full route URL.
        secret: Sent as `X-Broker-Secret` when set.
        timeout: Seconds before the request is abandoned.
        context: The TLS context for an https URL, or None for the default.

    Returns:
        The decoded JSON body.
    """
    req = urllib.request.Request(url, headers={"X-Broker-Secret": secret} if secret else {})
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return json.load(resp)


def main(argv: Optional[list[str]] = None) -> int:
    """Run the check once and print its one-line verdict.

    Args:
        argv: Command-line arguments; `sys.argv[1:]` when None.

    Returns:
        The Nagios exit code of the worst state found.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=_default_url())
    parser.add_argument("--timeout", type=float, default=5.0)
    # The container's own port 3001 serves a self-signed certificate.
    parser.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    parser.add_argument("--max-session-hours", type=float, default=12.0)
    parser.add_argument("--max-exit-seconds", type=float, default=120.0)
    parser.add_argument("--max-kept-archives", type=int, default=0)
    parser.add_argument("--max-last-exit-hours", type=float, default=24.0)
    args = parser.parse_args(argv)
    base = args.url.rstrip("/") + "/api"
    secret = os.environ.get("BROKER_SECRET") or None
    limits = Limits(
        args.max_session_hours, args.max_exit_seconds, args.max_kept_archives, args.max_last_exit_hours
    )
    context = None
    if args.insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    try:
        _get(f"{base}/health", None, args.timeout, context)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"CRITICAL: broker health at {base}/health unreachable: {exc}")
        return CRITICAL
    try:
        status = _get(f"{base}/session/status", secret, args.timeout, context)
        exports = _get(f"{base}/session/exports", secret, args.timeout, context)["exports"]
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print(f"UNKNOWN: broker refused the secret (HTTP {exc.code}) on {exc.url}; is BROKER_SECRET set?")
        else:
            print(f"UNKNOWN: broker answered HTTP {exc.code} on {exc.url}")
        return UNKNOWN
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"UNKNOWN: could not read broker status: {exc}")
        return UNKNOWN

    problems = evaluate(status, exports, time.time(), limits)
    if not problems:
        state = "session " + str(status.get("session_id")) if status.get("active") else "idle"
        print(f"OK: broker up, {state}, {len(exports)} kept archive(s)")
        return OK
    worst = problems[0][0]
    print(f"{_LABELS[worst]}: " + "; ".join(msg for _, msg in problems))
    return worst


if __name__ == "__main__":
    sys.exit(main())
