"""Phrasing and judging one turn of the questionnaire chat.

generator.py answers what the customer's record supports and leaves the
rest — 17 of 24 questions on a real system. This module is how a data
protection officer answers those the record could not, in conversation. It
does two things, and only two:

  phrase_question  turn one assessment_question into a conversational
                    prompt for the officer, grounded in what the record
                    already knows.
  judge_reply       decide whether the officer's chat message actually
                    answered the question just asked, so the caller knows
                    whether to file it (chat.record_answer) or ask again.

Neither function may produce answer text, suggest an answer, or rephrase
what the officer said. The model ASKS and it JUDGES; it never AUTHORS. The
officer's own words, verbatim, are what chat.record_answer files — that is
the whole point of a DPIA a regulator can trust, and both system prompts
below say so explicitly, not just this docstring.

Same convention as generator.py: `complete` is imported BY NAME from
fides.api.privacycare.llm, never called through a provider SDK or
`httpx` directly. GatewayUnavailable is caught at exactly two call sites,
below, and never retried against anything else — that module exists so
every prompt is redacted before egress and audited after, and a fallback
path here would be the exact bypass it was written to prevent.
"""
from loguru import logger

from fides.api.privacycare.generator import _is_decline
from fides.api.privacycare.llm import DEFAULT_MODEL, GatewayUnavailable, complete

# Identifies this feature in the gateway's egress audit — it names the
# feature (the questionnaire chat), not this module, because that audit
# row is read by humans investigating what PrivacyCare sent to a model,
# not by anything that cares which .py file made the call.
CHAT_CALLER = "dpia-questionnaire"

# The model's one sanctioned way to say "that message did not answer the
# question". Without it the only options for a non-answer are a guess at
# what the officer meant or silent acceptance of a deflection — and
# "I'll check with legal" filed as the lawful-basis answer is exactly the
# failure a DPIA cannot afford. See _is_decline's reuse below for how this
# sentinel is recognised through whatever decoration a model wraps it in.
NOT_ANSWERED_SENTINEL = "NOT_ANSWERED"

_PHRASE_SYSTEM_PROMPT = """You are helping a data protection officer complete a Data \
Protection Impact Assessment through a chat conversation.

You are given ONE question from the assessment and, where the record \
already holds relevant facts, those facts. Your only task is to phrase \
that question naturally, in a conversational voice, for the officer to \
answer next.

Rules:
- You ASK the question. You never answer it, suggest an answer, hint at \
one, or restate what a good answer would contain.
- Do not invent facts about the organisation beyond what is supplied.
- Reply with the phrased question only — no preamble, no greeting, no \
explanation of why you are asking."""

_JUDGE_SYSTEM_PROMPT = f"""You are checking one turn of a Data Protection Impact \
Assessment chat: did the data protection officer's message answer the \
question that was just asked?

You are judging, not answering. Never state what the correct answer would \
be, never rephrase or improve the officer's message, and never fill in a \
gap in it.

Reply with exactly {NOT_ANSWERED_SENTINEL} if the message does not answer \
the question — a deferral ("I'll check with legal"), a question back, a \
greeting, an acknowledgement with no content, or anything else that leaves \
the question open.

Otherwise reply with exactly ANSWERED. However brief or informal, a \
message that substantively addresses the question counts.

Reply with the single word and nothing else — no explanation, no \
restatement, no punctuation beyond what the word itself needs."""


def _format_record(context: dict) -> str:
    """Render whatever facts `context` holds as plain "- Label: value" lines.

    `context` here is a plain, arbitrarily-nested dict of already-resolved
    facts (e.g. {"privacy_declaration": {"name": "Email campaigns"}}) — not
    generator.py's fides_sources/resolve_source machinery, which resolves
    dotted source keys against a database context. This module's caller
    (the chat route) hands over whatever it already has in hand for the
    current question; there is no second resolution step here, and no
    fact is invented for a key that is missing or None.
    """
    lines: list[str] = []
    for key, value in context.items():
        label = key.replace("_", " ").capitalize()
        if isinstance(value, dict):
            for subkey, subvalue in value.items():
                if subvalue is None:
                    continue
                sublabel = f"{label} {subkey.replace('_', ' ')}"
                lines.append(f"- {sublabel}: {subvalue}")
        elif value is not None:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def _phrase_prompt(question: dict, context: dict) -> str:
    facts = _format_record(context)
    facts_block = f"PROCESSING ACTIVITY RECORD:\n{facts}\n\n" if facts else ""
    guidance = question.get("guidance")
    guidance_block = f"\n\nGUIDANCE FOR THE ASSESSOR:\n{guidance}" if guidance else ""
    return (
        f"{facts_block}"
        f"QUESTION TO ASK THE OFFICER:\n{question['question_text']}{guidance_block}"
    )


def _judge_prompt(question: dict, reply: str) -> str:
    return (
        f"QUESTION ASKED:\n{question['question_text']}\n\n"
        f"OFFICER'S MESSAGE:\n{reply}"
    )


def phrase_question(question: dict, context: dict, *, model: str | None = None) -> str:
    """Turn `question` into a conversational chat message for the officer.

    On GatewayUnavailable this falls back to question["question_text"]
    itself — the officer still gets asked, just without conversational
    phrasing. Degrading to the raw question is strictly better than an
    empty chat: the alternative is not asking at all, and the raw
    question text is already something the officer has answered before
    (it is the same text the detail screen shows). This is the opposite
    default to judge_reply below, deliberately: a failure here costs
    nothing but politeness, so there is nothing to lose by degrading.
    """
    try:
        phrased = complete(
            CHAT_CALLER,
            [{"role": "user", "content": _phrase_prompt(question, context)}],
            model=model or DEFAULT_MODEL,
            max_tokens=256,
            system=_PHRASE_SYSTEM_PROMPT,
        )
    except GatewayUnavailable as exc:
        logger.warning(
            "PrivacyCare chat asked the raw question text ({}): {}",
            question.get("question_key", question["question_text"]),
            exc,
        )
        return question["question_text"]

    phrased = phrased.strip()
    return phrased or question["question_text"]


def judge_reply(question: dict, reply: str, *, model: str | None = None) -> bool:
    """Did `reply` answer `question`? True files it, False asks again.

    On GatewayUnavailable this returns True: the officer said something,
    and discarding their work because OUR model is unreachable would be
    the worse failure — they would have to retype an answer that was
    never in doubt on their end. This is the opposite default to
    phrase_question above, deliberately: there the failure is ours to
    absorb cheaply (ask less conversationally); here the failure would be
    passed on to the officer as lost work, which is not ours to spend.

    NOT_ANSWERED is recognised through generator._is_decline, reused
    rather than re-implemented: a model that wraps ITS one-word sentinel
    in "**...**", a trailing full stop, or a leading "- " will do the same
    to any other one-word sentinel it is asked to return, and that
    decoration-stripping already survived two rounds of review.
    """
    try:
        verdict = complete(
            CHAT_CALLER,
            [{"role": "user", "content": _judge_prompt(question, reply)}],
            model=model or DEFAULT_MODEL,
            max_tokens=16,
            system=_JUDGE_SYSTEM_PROMPT,
        )
    except GatewayUnavailable as exc:
        logger.warning(
            "PrivacyCare chat accepted a reply it could not judge ({}): {}",
            question.get("question_key", question["question_text"]),
            exc,
        )
        return True

    return not _is_decline(verdict or "", NOT_ANSWERED_SENTINEL)
