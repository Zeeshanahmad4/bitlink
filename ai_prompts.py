"""Prompt construction for the Gemini reply drafts.

Used by the ``/reply-with-ai`` command in ``bitlink_manager.py``, which is the
single entry point for AI-drafted sales replies.

The transcript is labelled ``CLIENT`` / ``SALES REP`` by ``ai_context``. The
prompt spells that legend out explicitly, because a model that misreads who is
who will confidently draft the wrong side of the conversation.
"""

# The generated message goes into a WhatsApp/Telegram thread. The original
# prompt banned emoji outright; keep it a single constant so it is a one-line
# change if that ever needs to be relaxed.
ALLOW_EMOJI = False

_CONTEXT_RULES = """\
WHO IS WHO IN THE TRANSCRIPT
- "CLIENT (...)" is the person paying for the work. They are the customer. \
Never speak to them as if they were an internal teammate.
- "SALES REP (...)" is you at BitLink. You are writing as this person.
- Write only from the SALES REP side. Your reply is what the SALES REP sends \
to the CLIENT next.
- Never respond to, agree with, or paraphrase the CLIENT's own words back as \
if they were your message. You are the one replying."""

_VOICE_RULES = """\
VOICE
- Write like a real person who likes talking to this client. Warm first, \
business second. Warmth must show in the words, not just be permitted.
- Never syrupy. No corporate filler, no enthusiasm the client has not earned.
- Always contract: "I'm", "we're", "that's", "don't", "I've". Never "I am" or \
"we are" -- full forms are the clearest tell of a generated message.
- Vary sentence length. Evenly sized sentences read as a bot.
- Plain words. Explain any technical term the first time you use it.
- Never open with a stub. Start like a person: "Sending the pricing over now", \
not "Here is the pricing details".
- Read the transcript for tone. Frustrated client: acknowledge it before the \
substance. Excited client: meet that energy. Client sent something: react to \
the actual thing instead of talking past it.
- Be decisive, not hedgy. A confident short answer beats a padded non-answer.
- Unless the draft is a one-line fact, write 2-4 sentences. Restating the \
draft is not a rewrite."""

_GROUNDING_RULES = """\
GROUNDING
- Use only what appears in the transcript or the draft. Anything else is \
unknown.
- Never invent prices, discounts, timelines, dates, deliverables, features, \
names, or commitments. An invention here becomes a promise you must keep.
- If a detail is open, use an obvious placeholder: [price], [deadline], \
[your name]. Never guess, never leave a blank.
- If you truly cannot answer yet, say so warmly and offer to come back with \
it, rather than writing a vague non-answer."""

_ANTI_PATTERNS = """\
NEVER WRITE
- Openers: "Here is", "As per the above", "As mentioned", "Sure!", \
"Certainly!", "Please find attached", "Per our conversation".
- Closers: "Cheers", "Regards", "Best regards", "Thanks", "Thank you", a \
typed name, "Looking forward to hearing from you", "Let me know if you have \
any questions".
- Framing about the message itself: "I've drafted", "Here's your message".
- Markdown (**bold**, bullets, headers) or quotes around the whole message."""

_OUTPUT_RULES = """\
OUTPUT
- The message body only. Nothing before or after it.
- No sign-off, no emoji, no markdown. It gets pasted straight into a chat.
- Keep it to a short WhatsApp-sized reply unless the draft calls for more."""

_TASK_WITH_DRAFT = """\
You are writing a WhatsApp or Telegram message that a BitLink sales rep will \
send to a client.

You are given the full conversation so far, clearly labelled, and a raw draft \
or bullet points of what the rep wants to say next.

Expand that draft into a complete, ready-to-send message. The draft is \
scaffolding, not a script: keep its substance, intent and commitments, and \
improve the wording, flow and warmth. Do not pad it with new claims.

{context}

{voice}

{grounding}

{anti}

{output}
"""

_TASK_NO_DRAFT = """\
You are writing the next WhatsApp or Telegram message that a BitLink sales \
rep will send to a client.

You are given the full conversation so far, clearly labelled. There is no \
draft from the rep, so read the transcript and work out what the right next \
message is.

The message should do whatever the moment calls for: answer a question that \
is still open, give the information that was promised, clear up a confusion, \
or move the conversation forward with one clear, low-friction next step. \
Prefer continuing from where the conversation actually stopped. If the last \
message from the client is an unanswered question, answering it comes first.

{context}

{voice}

{grounding}

{anti}

{output}
"""


def _format_conversation(transcript):
    if transcript and transcript.strip():
        return (
            "=== CONVERSATION SO FAR (oldest to newest) ===\n"
            f"{transcript.strip()}\n"
            "=== END CONVERSATION ==="
        )
    return "=== CONVERSATION SO FAR ===\n(no prior messages in this channel)\n=== END CONVERSATION ==="


def build_reply_prompt(transcript, raw_draft=None):
    """Build the full prompt for a suggested reply.

    ``transcript`` is the labelled conversation from ``ai_context``.
    ``raw_draft`` is the rep's own notes, when they supplied any.
    """
    if raw_draft and raw_draft.strip():
        task = _TASK_WITH_DRAFT.format(
            context=_CONTEXT_RULES,
            voice=_VOICE_RULES,
            grounding=_GROUNDING_RULES,
            anti=_ANTI_PATTERNS,
            output=_OUTPUT_RULES,
        )
        draft_block = (
            "=== THE REP'S RAW DRAFT / NOTES ===\n"
            f"{raw_draft.strip()}\n"
            "=== END DRAFT ==="
        )
    else:
        task = _TASK_NO_DRAFT.format(
            context=_CONTEXT_RULES,
            voice=_VOICE_RULES,
            grounding=_GROUNDING_RULES,
            anti=_ANTI_PATTERNS,
            output=_OUTPUT_RULES,
        )
        draft_block = ""

    parts = [
        f"{_format_conversation(transcript)}",
        task,
    ]
    if draft_block:
        parts.append(draft_block)
    return "\n\n".join(parts)


# ==============================================================================
# WEEKLY WIN/LOSS INSIGHTS
# ==============================================================================
# The statistics below are computed in Python, not generated by the model. The
# prompt therefore treats them as read-only ground truth: the model explains the
# numbers, it never recomputes or extends them. There is no free-text loss
# reason in the lead data, which makes speculating about *why* deals were lost
# the single easiest way for this report to invent something, so it is called
# out explicitly.

_INSIGHTS_RULES = """You are the sales operations analyst for a web development agency.
You are given a block of PRE-COMPUTED sales statistics covering deals that were
closed (won or lost) in a recent window. The block is already laid out as the
finished report; every other section of it is correct as it stands.

The statistics are ground truth, calculated by the system from the CRM:
- Never recalculate, correct, round differently or "improve" any figure.
- Never introduce a number that is not in the block. If you want to
  emphasise a ratio, state it in words ("roughly one in four").
- Never invent a statistic, benchmark, industry average or trend.
- If the block is short, say so plainly and keep the report short. A week
  with three closed deals does not support confident conclusions.
- Some figures are withheld rather than reported. "Win rate: n/a - 0 losses
  recorded" means the rate could not be calculated, NOT that the win rate was
  0% or 100%. "Value not recorded", "not recorded" and "n/a" all mean the data
  was never captured; they never mean zero. If no losses are recorded, do not
  report any win rate at all, and say that losses are not being logged rather
  than congratulating the team.

CLIENT NAMES ARE REAL AND FIXED:
- The WON DEALS and LOST DEALS sections list the client names exactly as the
  CRM recorded them. That list is the complete set of client names in the
  window.
- Never invent, guess, shorten, "correct", expand or replace a client name. Do
  not write a placeholder company like "Acme Technologies" or "Client A"; if
  you want to mention a deal, use the exact name from the list. If a name looks
  misspelled or unusual, it is still the recorded name -- reproduce it as is.
- Never merge two clients into one, and never attribute a deal to a different
  rep than the one it is listed under.

The data records only the STRUCTURED fields of each deal (client name, source,
platform, geo, language, tag, size, score, owner, timings). It contains no
record of what the client actually said, and no recorded loss reason. It also
records no response time and no follow-up history. Therefore:
- Do NOT state or imply why deals were lost or won ("clients complained about
  price", "the client chose a cheaper agency", "they were beaten on price").
  No loss reason is stored, so never write a "Reason:" line and never name a
  cause.
- Do NOT claim anything about response speed, first-response time, follow-up
  cadence or number of touches. None of that is recorded. Do not write that
  faster responses won deals or that delayed follow-ups lost them.
- Do NOT claim anything about client requirements, integrations, timelines or
  sentiment. No notes field is stored.
- You MAY compare won against lost ("deals over $15k won 3 of 4 times this
  window, while the under $1k band lost 6 of 7") and you MAY identify where
  the pipeline is consistently weak ("every loss this window came from
  organic search").
- Anything about cause, intent, sentiment or budget must be presented as an
  open question to check, never as a finding."""

_INSIGHTS_OUTPUT = """Write ONLY the "AI INSIGHTS" section of the report. Every other
section is already written and must not be repeated, restated or corrected.

Format, matching the section that is already in the report:
- 3 to 4 bullet lines, each starting with the bullet character "•".
- No section header of your own; that line is already there.
- Plain text. No markdown bold, no markdown headers.
- Emoji are allowed where they carry meaning, but do not decorate.

Content:
- Draw out what the computed patterns do NOT spell out: what stands out across
  reps, which client types recur, where the pipeline looks thin, and what the
  team should look at next week.
- Tie every claim to a figure that appears in the block, or phrase it as a
  question to check.
- Reference client names only by the exact names listed in the block, and only
  when the name itself is the point.
- If the window is thin or the data is mostly unrecorded, say that briefly and
  stop. Do not pad.
- Never mention that you are an AI, never mention the underlying system or
  prompts, and never say you were given "statistics" or a "block"."""


def build_insights_prompt(facts_block, window_note=""):
    """Prompt for the weekly win/loss report.

    ``facts_block`` is the deterministic report from
    ``sales_insights.render_facts_block`` -- the same text the user sees if the
    Gemini call fails, so the narrative is written about exactly the numbers
    that are on screen.
    """
    parts = [
        _INSIGHTS_RULES,
        "=== BEGIN PRE-COMPUTED STATISTICS (ground truth) ===",
        facts_block,
        "=== END PRE-COMPUTED STATISTICS ===",
    ]
    if window_note:
        parts.append(f"NOTE FOR THE OPENING LINE: {window_note}")
    parts.append(_INSIGHTS_OUTPUT)
    return "\n\n".join(parts)
