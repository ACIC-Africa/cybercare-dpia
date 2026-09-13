"""phrase_question and judge_reply: the two gateway calls behind the chat.

No `db` fixture — see the task brief: this module has no database
dependency, and neither do these tests.
"""
import pytest

from fides.api.privacycare.chat_llm import CHAT_CALLER, judge_reply, phrase_question
from fides.api.privacycare.llm import GatewayUnavailable


def test_the_question_is_phrased_from_its_text_and_the_record(monkeypatch):
    captured = {}

    def _fake(caller, messages, *, model, max_tokens, system=None):
        captured["prompt"] = messages[0]["content"]
        captured["system"] = system
        return "What lawful basis do you rely on for the marketing emails?"

    monkeypatch.setattr("fides.api.privacycare.chat_llm.complete", _fake)

    text = phrase_question(
        {"question_text": "What is the lawful basis?", "guidance": None},
        {"privacy_declaration": {"name": "Email campaigns"}},
    )

    assert "lawful basis" in text
    assert "What is the lawful basis?" in captured["prompt"]
    assert "Email campaigns" in captured["prompt"]


def test_an_unreachable_gateway_still_asks_the_raw_question(monkeypatch):
    # Degrading to the question's own text is strictly better than an empty
    # chat: the officer is still asked, just not conversationally.
    monkeypatch.setattr(
        "fides.api.privacycare.chat_llm.complete",
        lambda *a, **k: (_ for _ in ()).throw(GatewayUnavailable("429")),
    )

    assert phrase_question(
        {"question_text": "What is the lawful basis?", "guidance": None}, {}
    ) == "What is the lawful basis?"


@pytest.mark.parametrize("reply", ["NOT_ANSWERED", "  NOT_ANSWERED\n",
                                   "**NOT_ANSWERED**", "NOT_ANSWERED."])
def test_a_non_responsive_reply_is_judged_unanswered(monkeypatch, reply):
    monkeypatch.setattr("fides.api.privacycare.chat_llm.complete",
                        lambda *a, **k: reply)
    assert judge_reply({"question_text": "What is the lawful basis?"},
                       "I'll check with legal.") is False


def test_a_real_answer_is_judged_answered(monkeypatch):
    monkeypatch.setattr("fides.api.privacycare.chat_llm.complete",
                        lambda *a, **k: "ANSWERED")
    assert judge_reply({"question_text": "What is the lawful basis?"},
                       "Consent, captured at signup.") is True


def test_an_unreachable_gateway_accepts_the_human_s_answer(monkeypatch):
    # The opposite default to phrase_question, deliberately: the person said
    # something, and discarding their work because OUR model is unreachable
    # would be the worse failure.
    monkeypatch.setattr(
        "fides.api.privacycare.chat_llm.complete",
        lambda *a, **k: (_ for _ in ()).throw(GatewayUnavailable("429")),
    )
    assert judge_reply({"question_text": "Q?"}, "An answer.") is True


def test_a_very_long_reply_is_truncated_before_it_reaches_the_gateway(monkeypatch):
    # The officer types into a chat box and nothing upstream bounds what
    # arrives. A pasted policy document would otherwise go to the gateway
    # whole — cost, latency, and a far larger redaction surface than one
    # conversational turn needs. What is FILED is still the reply in full;
    # only the judgement sees a bounded prefix.
    captured = {}

    def _fake(caller, messages, *, model, max_tokens, system=None):
        captured["prompt"] = messages[0]["content"]
        return "ANSWERED"

    monkeypatch.setattr("fides.api.privacycare.chat_llm.complete", _fake)

    judge_reply({"question_text": "Q?"}, "x" * 10_000)

    assert len(captured["prompt"]) < 5_000, (
        f"an unbounded reply reached the gateway: {len(captured['prompt'])} chars"
    )
    assert "truncated" in captured["prompt"]


def test_both_calls_send_their_system_prompt(monkeypatch):
    # Without this, a refactor that dropped `system=` would pass the whole
    # suite while removing the only instruction telling the model to ask
    # rather than answer on the officer's behalf.
    from fides.api.privacycare.chat_llm import (
        _JUDGE_SYSTEM_PROMPT,
        _PHRASE_SYSTEM_PROMPT,
    )

    seen = {}

    def _fake(caller, messages, *, model, max_tokens, system=None):
        seen[caller] = system
        return "ANSWERED"

    monkeypatch.setattr("fides.api.privacycare.chat_llm.complete", _fake)

    phrase_question({"question_text": "Q?", "guidance": None}, {})
    assert seen[CHAT_CALLER] == _PHRASE_SYSTEM_PROMPT

    judge_reply({"question_text": "Q?"}, "An answer.")
    assert seen[CHAT_CALLER] == _JUDGE_SYSTEM_PROMPT
