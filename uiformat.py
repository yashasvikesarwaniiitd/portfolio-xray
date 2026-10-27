"""Pure presentation helpers for the frontend — no Streamlit, no network, so they're
unit-testable. app.py imports these; nothing here computes finance, it only formats.
"""
import html as _html
import re

_NUM_IN_TEXT = re.compile(r"(\d)(?=(\d\d)+$)")


def inr(n, decimals: int = 0) -> str:
    """Indian digit grouping: ₹18,42,367 (last three, then pairs) — as in the design."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if n < 0 else ""
    whole = f"{abs(n):.{decimals}f}"
    frac = ""
    if "." in whole:
        whole, frac = whole.split(".")
        frac = "." + frac
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        whole = _NUM_IN_TEXT.sub(r"\1,", head) + "," + tail
    return f"{sign}₹{whole}{frac}"


def pct(n, decimals: int = 1, signed: bool = False) -> str:
    if n is None:
        return "—"
    return f"{n:+.{decimals}f}%" if signed else f"{n:.{decimals}f}%"


_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
_CODE = re.compile(r"`(.+?)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _inline(s: str) -> str:
    """Escape first (the text is model output), then apply inline markdown."""
    s = _html.escape(s)
    s = _LINK.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _ITALIC.sub(r"<em>\1</em>", s)
    s = _CODE.sub(r"<code>\1</code>", s)
    return s


def md_to_html(text: str) -> str:
    """Minimal markdown → HTML so assistant prose renders inside the design's serif block.

    Handles paragraphs, headings, **bold**/*italic*/`code`/links, and BOTH bulleted and
    numbered lists. Numbered lists matter: the model uses them to enumerate fund variants,
    and treating those lines as prose ran the items together on one line.
    """
    out: list[str] = []
    buf: list[str] = []
    open_list: str | None = None

    def flush_para():
        if buf:
            out.append("<p>" + " ".join(buf) + "</p>")
            buf.clear()

    def close_list():
        nonlocal open_list
        if open_list:
            out.append(f"</{open_list}>")
            open_list = None

    def ensure_list(kind: str):
        nonlocal open_list
        if open_list != kind:
            close_list()
            out.append(f"<{kind}>")
            open_list = kind

    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        bullet = _BULLET.match(line)
        numbered = None if bullet else _NUMBERED.match(line)
        if bullet or numbered:
            flush_para()
            ensure_list("ul" if bullet else "ol")
            out.append("<li>" + _inline((bullet or numbered).group(1)) + "</li>")
            continue
        close_list()
        if not line.strip():
            flush_para()
        elif _HEADING.match(line):
            flush_para()
            out.append("<p><strong>" + _inline(_HEADING.match(line).group(1))
                       + "</strong></p>")
        else:
            buf.append(_inline(line))
    close_list()
    flush_para()
    return "".join(out)


SLOW_SERVER_NOTE = (
    "The server took too long to answer. After it has been idle, the first analysis can take "
    "a couple of minutes while it warms up - and it keeps working in the background, so "
    "waiting a minute and trying again is usually much faster.")

_PROXY_STATUSES = {502, 503, 504, 520, 521, 522, 523, 524}


def describe_http_failure(code: int, body: bytes | str) -> str | None:
    """A readable message for a reply that is not the JSON the app expects, else None.

    The host's proxy answers with ITS OWN HTML error page when the backend takes too long or
    is restarting. That was shown to the user verbatim - a screenful of `<!DOCTYPE html>` and
    IE6 conditional comments in a red box - because anything that failed to parse as JSON was
    displayed as the error text. A 5xx proxy status, or a body that is plainly a web page,
    is the server being slow or unavailable, which the person can act on; the markup is not.
    """
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    head = text.lstrip()[:200].lower()
    looks_like_page = head.startswith(("<!doctype", "<html", "<head", "<body")) or "<html" in head
    if looks_like_page or code in _PROXY_STATUSES:
        return SLOW_SERVER_NOTE
    return None

