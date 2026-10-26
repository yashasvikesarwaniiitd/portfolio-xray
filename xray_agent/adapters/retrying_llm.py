"""Retry wrapper for any LLMPort.

The loop already retries transient TOOL failures, but it had no equivalent for the model
call itself: an upstream 429 or 503 raised straight through and the investigation came back
FAILED. That was survivable against Anthropic on a paid key and is not survivable against
Groq's free tier, which rate limits at roughly 30 requests a minute while one investigation
makes 8-12 model calls.

Written as a decorator rather than as logic inside Investigator, for two reasons: the loop
should not know which provider it is talking to, and a wrapper is composable - evals can
retry hard and be slow, a live demo can retry once and stay responsive.

Provider errors are classified by status code, class name and message rather than by
importing anthropic or groq, so this adapter stays usable with either (or with neither,
in tests). `Retry-After` is honoured when the provider sends one, because guessing a
backoff when the server has told you the answer is just rudeness with extra steps.
"""
from __future__ import annotations

import re
import time

# A 400 from an OpenAI-compatible provider normally means WE sent something wrong, which no
# amount of retrying fixes. `tool_use_failed` is the exception: it means the MODEL emitted
# malformed JSON for its tool arguments and the provider rejected the generation before we
# ever saw a response. That is a sampling failure, not a bad request, and another draw has
# a good chance of being valid. Weaker models hit it often; found on Groq's gpt-oss-120b,
# which produced '{"name": "concentration", "arguments": {"}"}'.
GENERATION_FAILURES = ("tool_use_failed", "failed to parse tool call", "failed_generation")

# Phrases marking a quota that will NOT come back inside this run, however long we wait.
DAILY_QUOTA_MARKERS = ("per day", "tpd", "daily limit", "requests per day")

_WAIT_RE = re.compile(r"try again in\s*(?:(\d+)m)?\s*([\d.]+)s", re.IGNORECASE)

RETRYABLE_TYPE_NAMES = frozenset({
    "RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError",
    "ServiceUnavailableError", "OverloadedError", "APIStatusError", "Timeout",
    "ConnectionError", "TimeoutError",
})

RETRYABLE_MARKERS = (
    "rate limit", "rate_limit", "429", "too many requests", "timed out", "timeout",
    "connection", "overloaded", "temporarily", "502", "503", "504", "capacity",
)

MAX_BACKOFF = 30.0


def _status_of(exc: Exception):
    for attr in ("status_code", "http_status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def is_generation_failure(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in GENERATION_FAILURES)


def is_daily_quota(exc: Exception) -> bool:
    """A PER-DAY cap, as opposed to an ordinary per-minute rate limit.

    The distinction matters: a per-minute limit clears in seconds and is worth waiting
    for, while a per-day cap will not clear inside this investigation however long we sit
    on it. Keyed on the provider's wording, not on the 429 status — every rate limit is a
    429, and treating them all as fatal would throw away the retries that actually work.
    """
    return any(m in str(exc).lower() for m in DAILY_QUOTA_MARKERS)



def is_retryable(exc: Exception) -> bool:
    if is_generation_failure(exc):
        return True
    status = _status_of(exc)
    if status is not None:
        return status == 429 or 500 <= status < 600
    if type(exc).__name__ in RETRYABLE_TYPE_NAMES:
        return True
    msg = str(exc).lower()
    return any(m in msg for m in RETRYABLE_MARKERS)


def retry_after_seconds(exc: Exception):
    """Whatever the provider told us to wait, UNCLAMPED, from a header or the message body.

    Unclamped on purpose: the caller needs to know the difference between "wait 3 seconds"
    and "wait 8 minutes, your daily tokens are gone". Clamping here hid that and turned a
    quota exhaustion into six pointless retries.
    """
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-tokens",
                "x-ratelimit-reset-requests"):
        try:
            raw = headers.get(key)
        except Exception:
            continue
        if raw is None:
            continue
        try:
            return max(0.0, float(str(raw).rstrip("s")))
        except (TypeError, ValueError):
            continue
    # Groq puts it in the body: "Please try again in 8m25.872s"
    m = _WAIT_RE.search(str(exc))
    if m:
        minutes = float(m.group(1) or 0)
        return minutes * 60 + float(m.group(2))
    return None


class RetryingLLM:
    """Wraps an LLMPort and retries rate limits and transient upstream failures.

    A non-retryable error (bad request, auth, a model that does not exist) is re-raised
    immediately — retrying it would just burn the wall-clock budget to reach the same
    failure, and the Investigator already turns an exception into outcome FAILED.
    """

    def __init__(self, inner, max_retries: int = 4, base_delay: float = 2.0,
                 sleep=time.sleep, on_retry=None):
        self.inner = inner
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep = sleep
        self.on_retry = on_retry
        self.retries = 0

    @property
    def model(self):
        return getattr(self.inner, "model", "unknown")

    def generate(self, **kwargs):
        attempt = 0
        while True:
            try:
                return self.inner.generate(**kwargs)
            except Exception as e:
                if attempt >= self.max_retries or not is_retryable(e):
                    raise
                wait = retry_after_seconds(e)
                # A per-day cap, or any wait longer than we are willing to sit on, will not
                # clear inside this investigation. Fail now with the provider's own message
                # (it says when the quota resets) instead of spending the wall-clock budget
                # to reach the same failure.
                if is_daily_quota(e) or (wait is not None and wait > MAX_BACKOFF):
                    raise
                delay = wait if wait is not None else min(
                    self.base_delay * (2 ** attempt), MAX_BACKOFF)
                attempt += 1
                self.retries += 1
                if self.on_retry:
                    try:
                        self.on_retry(attempt, delay, e)
                    except Exception:
                        pass
                self.sleep(delay)
