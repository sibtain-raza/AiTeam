"""Autonomous crash recovery: turn "the run died, a human must notice and
rescue it" into "the run waits out the interruption and resumes itself."

Built from a real incident, not speculation: a live run hit the
account-wide Claude Code session limit mid-fan-out and printed
"You've hit your session limit · resets 3:30am (Asia/Calcutta)" — then sat
dead until a human noticed, waited for the reset, and manually ran
`--resume`. Everything needed to do that automatically was already in the
error text; this module parses it.

Three pieces, all pure logic (unit-testable without the SDK):

- `parse_session_reset_wait()`: extracts the reset time + timezone from
  the session-limit message and returns how many seconds to sleep.
- `compute_retry_delay()`: the retry policy — session limits wait until
  the parsed reset (with a fallback when parsing fails), anything else
  gets a short bounded backoff (a transient network/API blip heals; a
  deterministic crash burns at most the small backoff schedule, and
  resume-from-checkpoint retries the failed turn only, never completed
  work).
- `acquire_run_lock()` / `release_run_lock()`: a PID lock file closing a
  documented operational hazard (README "Evidence this works"): two
  `--resume` invocations against the same checkpoint once ran
  concurrently against the same workspace for several minutes because
  nothing prevented it.

main.py owns the loop that uses these (auto-resume is its behavior);
this module owns the decisions, so the decisions are testable.
"""

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Session-limit waits are capped: if the reset is further out than this,
# we still wait this long and let the next attempt re-check (attempts are
# bounded by main.py's LOOPER_MAX_AUTO_RESUMES). Prevents a bad parse or a
# weird reset time from pinning a process for a day.
MAX_AUTO_WAIT_S = 6 * 3600

# Session-limit message seen but the reset time didn't parse: wait this
# long and try again rather than giving up — limits reset on wall-clock
# boundaries, so a blind 30min wait usually lands after one.
FALLBACK_SESSION_LIMIT_WAIT_S = 30 * 60

# Extra margin after the parsed reset time, so we don't resume seconds
# before the limit actually lifts and immediately re-fail.
RESET_BUFFER_S = 120

# Backoff schedule for failures that are NOT session limits. Short and
# bounded on purpose: transient blips heal within a minute or two, and a
# deterministic crash shouldn't be retried many times.
TRANSIENT_BACKOFF_S = (60.0, 300.0)

_SESSION_LIMIT_MARKER = "session limit"
# Matches the live-observed format: "resets 3:30am (Asia/Calcutta)".
# Minutes optional ("resets 4pm (...)" also parses).
_RESET_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", re.IGNORECASE
)


def parse_session_reset_wait(error_text: str, now: datetime | None = None) -> float | None:
    """Seconds to wait until the session-limit reset time named in
    `error_text`, plus RESET_BUFFER_S — or None if no parseable
    "resets <time> (<timezone>)" clause is present (unknown timezone
    included). `now` is injectable for tests; defaults to the real
    current time. If the named time has already passed today in that
    timezone, it means tomorrow's occurrence."""
    match = _RESET_RE.search(error_text)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3).lower()
    try:
        tz = ZoneInfo(match.group(4).strip())
    except Exception:
        return None
    if hour == 12:
        hour = 0
    if meridiem == "pm":
        hour += 12
    if hour > 23 or minute > 59:
        return None

    now = now.astimezone(tz) if now is not None else datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds() + RESET_BUFFER_S


def compute_retry_delay(
    error_text: str, attempt: int, now: datetime | None = None
) -> float | None:
    """The auto-resume retry policy: how long to wait before resuming
    after a failure, or None for "don't auto-retry this one" (caller
    falls back to printing manual `--resume` instructions, exactly the
    pre-autonomous behavior). `attempt` is 0-based: how many auto-resumes
    this process has already performed."""
    if _SESSION_LIMIT_MARKER in error_text.lower():
        wait = parse_session_reset_wait(error_text, now)
        if wait is None:
            wait = FALLBACK_SESSION_LIMIT_WAIT_S
        return min(wait, MAX_AUTO_WAIT_S)
    if attempt < len(TRANSIENT_BACKOFF_S):
        return TRANSIENT_BACKOFF_S[attempt]
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def acquire_run_lock(lock_path: Path) -> bool:
    """Take the per-run lock, or refuse if another LIVE process holds it.
    A lock file whose recorded PID is no longer running is stale (the
    holder crashed without cleanup — the exact scenario this guards
    follows) and is silently taken over. Not adversarial-proof (a
    check-then-write race exists); it guards the documented human
    mistake — starting a second `--resume` while the first is still
    alive — not malicious concurrency."""
    if lock_path.exists():
        try:
            holder = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            holder = None
        if holder is not None and holder != os.getpid() and _pid_alive(holder):
            return False
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()))
    return True


def release_run_lock(lock_path: Path) -> None:
    """Remove the lock if this process owns it (never someone else's)."""
    try:
        if lock_path.exists() and int(lock_path.read_text().strip()) == os.getpid():
            lock_path.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass
