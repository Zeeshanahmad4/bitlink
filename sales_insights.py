"""Weekly Win/Loss insights: aggregate closed deals, then let Gemini narrate them.

Two rules shaped this module:

1. The numbers are computed here in Python, not read out of the model's text.
   The AI only ever sees a finished block of statistics and is asked to explain
   them, so a hallucinated figure cannot reach the report.

2. The statistics stand on their own. ``sales_leads`` rows carry no free-text
   loss reason, so the report can only compare won against lost across the
   structured columns. When the Gemini call fails -- the free tier is capped at
   a small number of requests per day and runs out constantly -- the caller can
   still return the computed report instead of an error.

Everything here is pure so it can be tested without importing the bridge, which
opens live clients on import.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta

CLOSED_STAGES = ("won", "lost")

# A weekly report built from one or two deals is noise, so widen the window when
# the week is too quiet to say anything and label whatever window was used.
MIN_DEALS_FOR_REPORT = 3
FALLBACK_WINDOW_DAYS = 30

_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%m/%d/%Y",
)

# Deal size bands, ordered low to high. The upper bound is exclusive.
VALUE_BANDS = (
    ("Under $1k", None, 1000.0),
    ("$1k-$5k", 1000.0, 5000.0),
    ("$5k-$15k", 5000.0, 15000.0),
    ("$15k-$50k", 15000.0, 50000.0),
    ("$50k+", 50000.0, None),
)

_UNCATEGORISED = "Uncategorised"

# Not a size band. Deals land here when deal_value was never filled in, so they
# are never described as being worth some amount of money.
VALUE_NOT_RECORDED = "Value not recorded"


def parse_deal_value(raw) -> float:
    """Parse a deal value that may arrive as a number, Decimal or formatted string.

    ``db_store._num_out`` renders these as strings like ``'5000'`` or
    ``'1500.5'``. The parsing stays defensive because rows written before that
    normalisation can still carry formatted values like ``'$5,000'``.
    """
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).replace("$", "").replace(",", "").replace("USD", "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        logging.debug(f"Unparseable deal value: {raw!r}")
        return 0.0


def recorded_value(raw):
    """The deal value, or None when nothing was recorded.

    ``db_store._num_out`` renders a missing value as the string ``"0"``, so an
    absent value and a genuine zero are indistinguishable here. Either way a
    closed deal that reports no value is a data gap rather than a known deal
    size, and treating it as $0 quietly dragged every unrecorded deal into the
    "Under $1k" band and dragged the averages down.
    """
    return parse_deal_value(raw) or None


def parse_timestamp(raw):
    """Best-effort timestamp parse; returns None rather than raising."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        logging.debug(f"Unparseable timestamp: {raw!r}")
        return None


def _clean(value) -> str:
    return str(value or "").strip()


def _is_closed(lead) -> bool:
    return _clean(lead.get("stage")).lower() in CLOSED_STAGES


def collect_closed_deals(leads, now, window_days):
    """Closed deals closed on or after ``now - window_days``.

    A won/lost row with no usable ``closed_at`` cannot be placed in a time
    window, so it is set aside and reported separately rather than silently
    dropped -- the totals still have to add up for whoever reads the report.
    """
    cutoff = now - timedelta(days=window_days)
    dated, undated = [], []
    for lead in leads or []:
        if not _is_closed(lead):
            continue
        closed_at = parse_timestamp(lead.get("closed_at"))
        if closed_at is None:
            undated.append(lead)
        elif closed_at >= cutoff:
            dated.append(lead)
    return dated, undated


def _win_rate(won, lost):
    """Win percentage, or None when there is no loss to divide by.

    A "100%" win rate over zero losses is not a result, it is missing data, so
    the rate is withheld rather than reported as perfect.
    """
    if not won and not lost:
        return None
    if not lost:
        return None
    return round(100.0 * won / (won + lost), 1)


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _mean(values):
    return round(sum(values) / len(values), 1) if values else None


def _group_by(closed, key_fn, cap=8):
    """Won/lost split per key, biggest buckets first.

    A dimension is only worth reporting when both sides are actually present --
    a "100% win rate" on a single deal is noise, and a bucket with only losses
    cannot be compared to anything.
    """
    buckets = defaultdict(lambda: {"won": 0, "lost": 0, "value": 0.0})
    for lead in closed:
        key = _clean(key_fn(lead)) or _UNCATEGORISED
        stage = _clean(lead.get("stage")).lower()
        buckets[key][stage if stage in CLOSED_STAGES else "lost"] += 1
        if stage == "won":
            value = recorded_value(lead.get("deal_value"))
            if value is not None:
                buckets[key]["value"] += value

    rows = []
    for key, data in buckets.items():
        total = data["won"] + data["lost"]
        rows.append({
            "key": key,
            "won": data["won"],
            "lost": data["lost"],
            "total": total,
            "win_rate": _win_rate(data["won"], data["lost"]),
            "won_value": round(data["value"], 2),
        })
    # A dimension with a single bucket tells the reader nothing, so drop it.
    if len(rows) < 2:
        return []
    rows.sort(key=lambda r: (-r["total"], r["key"]))
    return rows[:cap]


def _value_bands(closed):
    """Closed deals split by size, with unrecorded values kept out of the money bands."""
    # One pass to count the data gaps, so a deal with no value is counted once
    # rather than once per band it would otherwise have been dragged into.
    pairs = [(lead, recorded_value(lead.get("deal_value"))) for lead in closed]
    missing = sum(1 for _, value in pairs if value is None)

    rows = []
    for label, low, high in VALUE_BANDS:
        won = lost = 0
        for lead, value in pairs:
            if value is None:
                continue
            if (low is None or value >= low) and (high is None or value < high):
                if _clean(lead.get("stage")).lower() == "won":
                    won += 1
                else:
                    lost += 1
        if won + lost:
            rows.append({
                "key": label, "won": won, "lost": lost,
                "total": won + lost, "win_rate": _win_rate(won, lost),
            })
    if missing:
        # Kept as its own row so the sizes still add up to the closed total.
        rows.append({
            "key": VALUE_NOT_RECORDED, "won": missing, "lost": 0,
            "total": missing, "win_rate": None,
        })
    return rows


def _cycle_times(closed, label):
    """Days from created to closed, for won and lost separately."""
    spans = []
    for lead in closed:
        if _clean(lead.get("stage")).lower() != label:
            continue
        created = parse_timestamp(lead.get("created_at"))
        closed_at = parse_timestamp(lead.get("closed_at"))
        if created and closed_at:
            spans.append((closed_at - created).total_seconds() / 86400.0)
    return spans


# Values that were clearly never filled in. Anything else is passed through
# untouched, because a strange client name is still the recorded truth and
# quietly renaming it would be worse than showing it.
_BLANK_CLIENT_NAMES = {
    "", ".", "..", "...", "-", "--", "?", "??", "n/a", "na", "none", "null",
    "undefined", "untitled", "no name", "noname",
}


def client_label(raw):
    """The client name exactly as the CRM recorded it."""
    name = _clean(raw)
    if not name or name.lower() in _BLANK_CLIENT_NAMES:
        return "Unnamed lead"
    return name


def _deal_rows(leads, rep_label):
    """Closed deals grouped under their owner, biggest value first."""
    rows = {}
    for lead in leads:
        rows.setdefault(rep_label(lead), []).append({
            "client": client_label(lead.get("client_name")),
            "value": recorded_value(lead.get("deal_value")),
        })
    for deals in rows.values():
        # Deals with a recorded value first, then alphabetical, so a rep with no
        # value recorded does not have their list look randomly ordered.
        deals.sort(key=lambda d: (d["value"] is None, -(d["value"] or 0), d["client"]))
    return rows


def _value_mix(raws):
    """Count how often each recorded value appears, skipping blanks."""
    counts = {}
    for raw in raws:
        value = _clean(raw)
        if value and value.lower() not in _BLANK_CLIENT_NAMES:
            counts[value] = counts.get(value, 0) + 1
    return counts


def build_insight_facts(closed, undated, rep_names, now, window_days):
    """Everything the report is allowed to say, as plain data."""
    won_leads = [l for l in closed if _clean(l.get("stage")).lower() == "won"]
    lost_leads = [l for l in closed if _clean(l.get("stage")).lower() == "lost"]

    # Averages and revenue cover only deals whose value was actually recorded.
    # Including the unrecorded ones as $0 understated every average.
    won_values = [v for v in (recorded_value(l.get("deal_value")) for l in won_leads) if v is not None]
    lost_values = [v for v in (recorded_value(l.get("deal_value")) for l in lost_leads) if v is not None]
    won_cycle = _cycle_times(closed, "won")
    lost_cycle = _cycle_times(closed, "lost")

    def _owner_label(lead):
        owner = _clean(lead.get("owner_slack_id"))
        if not owner:
            return "Unassigned"
        return rep_names.get(owner) or owner

    def _score(raw):
        try:
            return float(str(raw).replace("%", "").strip())
        except (TypeError, ValueError):
            return None

    won_scores = [s for s in (_score(l.get("lead_score")) for l in won_leads) if s is not None]
    lost_scores = [s for s in (_score(l.get("lead_score")) for l in lost_leads) if s is not None]

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "window_days": window_days,
        "window_label": (
            f"{(now - timedelta(days=window_days)).strftime('%d %b')} - "
            f"{now.strftime('%d %b %Y')}"
        ),
        # Long-form labels so the header reads as a date range, not a code.
        "window_range_long": (
            f"{(now - timedelta(days=window_days)).strftime('%B %d')} \u2013 "
            f"{now.strftime('%B %d, %Y')}"
        ),
        "generated_date_long": now.strftime("%B %d, %Y"),
        "total_closed": len(closed),
        "won": len(won_leads),
        "lost": len(lost_leads),
        "win_rate": _win_rate(len(won_leads), len(lost_leads)),
        "undated_closed": len(undated),
        "won_revenue": round(sum(won_values), 2) if won_values else None,
        "won_revenue_deals": len(won_values),
        "won_value_missing": len(won_leads) - len(won_values),
        "lost_value_missing": len(lost_leads) - len(lost_values),
        "avg_won_value": _mean(won_values),
        "avg_lost_value": _mean(lost_values),
        "median_won_cycle_days": _median(won_cycle),
        "median_lost_cycle_days": _median(lost_cycle),
        "avg_won_lead_score": _mean(won_scores),
        "avg_lost_lead_score": _mean(lost_scores),
        "by_source": _group_by(closed, lambda l: l.get("source")),
        "by_platform": _group_by(closed, lambda l: l.get("platform")),
        "by_geo": _group_by(closed, lambda l: l.get("geo")),
        "by_language": _group_by(closed, lambda l: l.get("language")),
        "by_tag": _group_by(closed, lambda l: l.get("tag")),
        "by_value_band": _value_bands(closed),
        "by_rep": _group_by(closed, _owner_label),
        # Deal-by-deal detail, grouped under the owner who closed it. The client
        # name is whatever the CRM recorded; it is never inferred or substituted.
        "won_deals": _deal_rows(won_leads, _owner_label),
        "lost_deals": _deal_rows(lost_leads, _owner_label),
        "won_cycle_range": (min(won_cycle), max(won_cycle)) if won_cycle else None,
        "lost_cycle_range": (min(lost_cycle), max(lost_cycle)) if lost_cycle else None,
        "won_score_mix": _value_mix(l.get("lead_score") for l in won_leads),
        "lost_score_mix": _value_mix(l.get("lead_score") for l in lost_leads),
    }


def _fmt_rate(value, won=None, lost=None):
    """A rate, or a plain statement of why there isn't one."""
    if lost == 0 and won:
        return "n/a — 0 losses recorded"
    if won == 0 and lost:
        return "n/a — 0 wins recorded"
    return "n/a" if value is None else f"{value}%"


def _fmt_money(value):
    return "not recorded" if value is None else f"${value:,.0f}"


def _fmt_days(value):
    return "n/a" if value is None else f"{value:.0f} days"


_RULE = "━" * 36

# The sample layout uses emoji headers, so the report keeps them. These are the
# only ones used, so a model rewrite cannot drift into decoration.
ICON_WON = "✅"
ICON_LOST = "❌"
ICON_RATE = "📊"
ICON_MONEY = "💰"
ICON_PERSON = "👤"
ICON_WIN_PATTERNS = "🟢"
ICON_LOSS_PATTERNS = "🔴"
ICON_AI = "🤖"
ICON_CHART = "📈"
ICON_TROPHY = "🏆"
ICON_TEAM = "👥"
ICON_BOARD = "📊"


def _section(title):
    return ["", _RULE, title, _RULE, ""]


def _deal_section(heading, deal_icon, deals, stage, count):
    """One block per rep, each listing the clients that rep actually closed."""
    lines = _section(heading)
    if not deals:
        lines.append(f"No {stage} deals recorded in this window.")
        return lines
    for rep in sorted(deals, key=lambda r: (-len(deals[r]), r)):
        lines.append(f"{ICON_PERSON} {rep}")
        for deal in deals[rep]:
            lines.append(f"   {deal_icon} {deal['client']} — {_fmt_money(deal['value'])}")
        lines.append("")
    lines.append(f"Total {stage.capitalize()}: {count} deal(s)")
    return lines


def _top_dimension(facts, key, dimension, of):
    """The single most common value in a grouped breakdown, if it stands out."""
    rows = facts[key]
    if not rows or not of:
        return None
    best = max(rows, key=lambda r: (r["won"] + r["lost"], r["key"]))
    share = (best["won"] + best["lost"]) / of
    # Only worth a sentence when it genuinely dominates; an even spread says
    # nothing and inventing a "top" source from a tie would mislead.
    if len(rows) < 2 or share < 0.6:
        return None
    return f"{best['won'] + best['lost']} of {of} {dimension} came from {best['key']}."


def _cycle_sentence(spans, stage, of):
    if not spans or of < 2:
        return None
    low, high = min(spans), max(spans)
    if round(high) == round(low):
        return f"Every {stage} deal took {low:.0f} days to close."
    return f"{stage.capitalize()} deals took {low:.0f} to {high:.0f} days to close."


def _score_sentence(mix, stage, of):
    """Only call out the score when it is uniform; a mix is not a finding."""
    if len(mix) != 1 or of < 2:
        return None
    (value, count), = mix.items()
    if count != of:
        return None
    return f"All {of} {stage} deals carried the same lead score ({value})."


def _win_patterns(facts):
    out = []
    for sentence in (
        _cycle_sentence(facts["won_cycle_range"] and
                        [facts["won_cycle_range"][0], facts["won_cycle_range"][1]],
                        "won", facts["won"]),
        _score_sentence(facts["won_score_mix"], "won", facts["won"]),
        _top_dimension(facts, "by_source", "won deals", facts["won"]),
        _top_dimension(facts, "by_platform", "won deals", facts["won"]),
        _top_dimension(facts, "by_geo", "won deals", facts["won"]),
    ):
        if sentence and sentence not in out:
            out.append(sentence)
    return out[:4]


def _loss_patterns(facts):
    if not facts["lost"]:
        return [
            f"No lost deals were recorded in this {facts['window_days']}-day window, "
            "so there is no loss data to read patterns from."
        ]
    out = []
    for sentence in (
        _cycle_sentence(facts["lost_cycle_range"] and
                        [facts["lost_cycle_range"][0], facts["lost_cycle_range"][1]],
                        "lost", facts["lost"]),
        _score_sentence(facts["lost_score_mix"], "lost", facts["lost"]),
        _top_dimension(facts, "by_source", "lost deals", facts["lost"]),
        _top_dimension(facts, "by_value_band", "lost deals", facts["lost"]),
    ):
        if sentence and sentence not in out:
            out.append(sentence)
    return out[:4] or [f"{facts['lost']} lost deal(s) recorded; no single dimension dominates."]


def _overview_lines(facts):
    lines = [
        f"{ICON_WON} Won: {facts['won']}",
        f"{ICON_LOST} Lost: {facts['lost']}",
        f"{ICON_RATE} Win Rate: "
        f"{_fmt_rate(facts['win_rate'], facts['won'], facts['lost'])}",
    ]
    if facts["won_revenue"] is None:
        lines.append(
            f"{ICON_MONEY} Total Won Value: not recorded "
            "(no won deal in this window has a value)"
        )
    else:
        money = f"{ICON_MONEY} Total Won Value: {_fmt_money(facts['won_revenue'])}"
        if facts["won_value_missing"]:
            money += (
                f" (from {facts['won_revenue_deals']} won deal(s) with a value; "
                f"{facts['won_value_missing']} without one)"
            )
        lines.append(money)
    return lines


def _team_lines(facts):
    lines = []
    for row in facts["by_rep"]:
        total = row["won"] + row["lost"]
        lines.append(f"{ICON_PERSON} {row['key']}")
        lines.append(f"   {ICON_WON} Won: {row['won']}   {ICON_LOST} Lost: {row['lost']}")
        rate = _fmt_rate(row["win_rate"], row["won"], row["lost"])
        lines.append(f"   {ICON_RATE} Win Rate: {rate}")
        won_value = row.get("won_value")
        if won_value:
            lines.append(f"   {ICON_MONEY} Won Value: {_fmt_money(won_value)}")
        lines.append(f"   Closed: {total} deal(s)")
        lines.append("")
    return lines


def _empty_body(facts):
    """The same shape with zeros, so a quiet week still renders as a report."""
    return "\n".join([
        *_section(f"{ICON_CHART} WEEKLY OVERVIEW"),
        f"{ICON_WON} Won: 0",
        f"{ICON_LOST} Lost: 0",
        f"{ICON_RATE} Win Rate: n/a",
        f"{ICON_MONEY} Total Won Value: $0",
        *_section(f"{ICON_TROPHY} WON DEALS"),
        "No won deals recorded in this window.",
        *_section(f"{ICON_LOST} LOST DEALS"),
        "No lost deals recorded in this window.",
        *_section(f"{ICON_TEAM} SALES TEAM PERFORMANCE"),
        "No closed deals to report per rep.",
        *_section(f"{ICON_WIN_PATTERNS} WIN PATTERNS"),
        f"• Nothing closed in the last {facts['window_days']} days, so there is no "
        "pattern to read.",
        *_section(f"{ICON_LOSS_PATTERNS} LOSS PATTERNS"),
        f"• Nothing closed in the last {facts['window_days']} days, so there is no "
        "loss data.",
    ])


def render_facts_block(facts) -> str:
    """The deterministic, data-only report.

    This is ground truth: it is what the modal shows when Gemini is
    unavailable, and it is the text handed to the model so the narrative cannot
    contradict the numbers. It contains only what the CRM actually recorded --
    no loss reasons, response times or follow-up history, because none of those
    are fields we store.
    """
    header = [
        f"{ICON_BOARD} WEEKLY WIN / LOSS INSIGHTS",
        facts["window_range_long"],
    ]
    if not facts["total_closed"]:
        return "\n".join(header) + "\n" + _empty_body(facts)

    lines = list(header)
    lines += _section(f"{ICON_CHART} WEEKLY OVERVIEW")
    lines += _overview_lines(facts)
    lines += _deal_section(f"{ICON_TROPHY} WON DEALS", ICON_WON,
                           facts["won_deals"], "won", facts["won"])
    lines += _deal_section(f"{ICON_LOST} LOST DEALS", ICON_LOST,
                           facts["lost_deals"], "lost", facts["lost"])
    lines += _section(f"{ICON_TEAM} SALES TEAM PERFORMANCE")
    lines += _team_lines(facts)
    lines += _section(f"{ICON_WIN_PATTERNS} WIN PATTERNS")
    lines += [f"• {p}" for p in _win_patterns(facts)] or [
        "• No pattern is strong enough in this window to report."
    ]
    lines += _section(f"{ICON_LOSS_PATTERNS} LOSS PATTERNS")
    lines += [f"• {p}" for p in _loss_patterns(facts)]
    return "\n".join(lines)


def render_report(facts, ai_narrative=None) -> str:
    """The full report as shown and shared, with the model-written section.

    Split from :func:`render_facts_block` so the block handed to Gemini never
    contains a placeholder that it might mistake for a finding.
    """
    lines = [render_facts_block(facts), ""]
    lines += _section(f"{ICON_AI} AI INSIGHTS")
    if ai_narrative:
        lines += [ln.rstrip() for ln in str(ai_narrative).splitlines() if ln.strip()]
    else:
        lines.append(
            "• No AI commentary this week. Everything above is computed "
            "directly from the CRM."
        )
    lines += [f"📅 Report generated: {facts['generated_date_long']}"]
    return "\n".join(lines)


def select_window(leads, now, window_days=7):
    """Pick a window that holds enough closed deals to be worth reporting on."""
    dated, undated = collect_closed_deals(leads, now, window_days)
    used = window_days
    if len(dated) < MIN_DEALS_FOR_REPORT and window_days < FALLBACK_WINDOW_DAYS:
        wider, undated = collect_closed_deals(leads, now, FALLBACK_WINDOW_DAYS)
        if len(wider) > len(dated):
            dated, used = wider, FALLBACK_WINDOW_DAYS
    return dated, undated, used
