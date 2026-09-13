"""phrase_question and judge_reply: the two gateway calls behind the chat.

No `db` fixture — see the task brief: this module has no database
dependency, and neither do these tests.
"""
import pytest

from fides.api.privacycare.chat_llm import judge_reply, phrase_question
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
