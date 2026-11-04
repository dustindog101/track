"""Pretty terminal rendering -- pure ANSI, zero dependencies.

Design goals: scannable in under a second, informative for agents
reading over a shoulder, and honest about data provenance (which
provider answered, whether it's from cache, timezone caveats).

Respects ``NO_COLOR`` and the ``color=False`` flag; falls back to plain
text automatically when piped (the CLI decides before calling us).
"""

from __future__ import annotations

from datetime import datetime

from .carriers import human_name
from .models import Shipment, Status

# --- palette -----------------------------------------------------------
_RESET, _BOLD, _DIM = "\033[0m", "\033[1m", "\033[2m"
_GREEN, _RED, _YELLOW = "\033[32m", "\033[31m", "\033[33m"
_BLUE, _CYAN, _MAGENTA = "\033[34m", "\033[36m", "\033[35m"

_STATUS_STYLE = {
    Status.DELIVERED: (_GREEN, "Delivered"),
    Status.OUT_FOR_DELIVERY: (_CYAN, "Out for delivery"),
    Status.IN_TRANSIT: (_BLUE, "In transit"),
    Status.PRE_TRANSIT: (_YELLOW, "Label created"),
    Status.EXCEPTION: (_RED, "Exception"),
    Status.EXPIRED: (_RED, "Expired"),
    Status.RETURNED: (_MAGENTA, "Returned"),
    Status.UNKNOWN: (_DIM, "Unknown"),
}

_WIDTH = 68


def _fmt_when(iso: str | None) -> str:
    """Humanize an ISO timestamp; tolerate naive/odd formats."""
    if not iso:
        return "n/a"
    raw = iso.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        base = dt.strftime("%b %d, %Y")
        if dt.hour or dt.minute:
            base += f" · {dt.strftime('%I:%M %p').lstrip('0').lower()}"
        if dt.tzinfo is not None:
            base += f" (UTC{dt.strftime('%z')[:3]})"
        return base
    except ValueError:
        return raw


class _Painter:
    def __init__(self, color: bool) -> None:
        self.color = color

    def __call__(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + _RESET


def render(shipment: Shipment, *, color: bool = True,
           cached: bool = False, cached_at: str | None = None,
           max_events: int = 8) -> str:
    """Render one shipment as a boxed summary card."""
    p = _Painter(color)
    line = "─" * _WIDTH
    out: list[str] = []

    carrier = human_name(shipment.carrier)
    out.append(p(f" {carrier} · {shipment.tracking_number}", _BOLD))
    out.append(line)

    tone, label = _STATUS_STYLE.get(shipment.status, _STATUS_STYLE[Status.UNKNOWN])
    human = shipment.status_human or label
    out.append(f" {p('Status', _BOLD)}  {p('●', tone)} {p(label, _BOLD)}"
               + (p(f"  —  {human}", _DIM) if shipment.status_human else ""))

    eta = _fmt_when(shipment.estimated_delivery)
    out.append(f" {p('ETA', _BOLD)}     {eta if shipment.estimated_delivery else p('not published by source', _DIM)}")

    provenance = f"source: {shipment.provider}"
    if cached:
        provenance += " (cached"
        if cached_at:
            provenance += f" {_fmt_when(cached_at)}"
        provenance += ")"
    out.append(p(f" {p('Source', _BOLD)}   {provenance}", _DIM))
    out.append(line)

    if shipment.events:
        newest_first = list(reversed(shipment.events))
        out.append(p(f" Timeline ({len(shipment.events)} events, newest first)",
                     _BOLD))
        for event in newest_first[:max_events]:
            when = _fmt_when(event.timestamp)
            out.append(f"  {p('│', _DIM)} {p(when, _DIM)}")
            out.append(f"  {p('●', tone)} {event.description or '(no description)'}")
            if event.location:
                out.append(f"  {p('│', _DIM)} {p(event.location, _DIM)}")
        if len(newest_first) > max_events:
            out.append(p(f"  … {len(newest_first) - max_events} older events "
                         f"(JSON output has them all)", _DIM))
    else:
        out.append(p(" No scan events yet — the carrier may not have picked "
                     "up the package.", _DIM))

    for note in shipment.notes:
        out.append(p(f" note: {note}", _DIM))
    return "\n".join(out)


def render_failure(number: str, kind: str, attempts, *, color: bool = True,
                   shipment: Shipment | None = None) -> str:
    """Render the not-found / all-failed dead ends.  (The attempt log
    itself is printed by the CLI so both outcomes share one format.)"""
    p = _Painter(color)
    out = [p(f" ✗ {number}", _BOLD)]
    if kind == "not_found":
        if shipment is not None:
            out.append("   A record exists for this number, but no carrier "
                       "has scanned it yet.")
            out.append(p("   Brand-new labels can take hours to activate "
                         "— retry later, or `track refresh <number>`.", _DIM))
        else:
            out.append("   No source recognizes this tracking number.")
            out.append(p("   Double-check it, or try again later if it is "
                         "brand new (labels can take a few hours to "
                         "activate).", _DIM))
    else:
        out.append("   Every data source failed (network, blocking, or "
                   "rate limits).")
    return "\n".join(out)
