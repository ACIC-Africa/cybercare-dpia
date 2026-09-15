"""The Kenyan clocks, parsed from the brief rather than retyped.

The clock is the product. A deadline displayed confidently and wrongly is worse
than no deadline, because a DPO will trust it — so the timeline values are
asserted against the converted corpus, not against a number someone typed twice.
"""
import os
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from fides.api.privacycare.dsr.timelines import (
    KENYAN_RIGHTS,
    deadline_for,
    seed_timelines,
    timeline_days,
)

DB_URL = "postgresql://postgres:fides@127.0.0.1:5442/fides"
# Final review minor finding: this used to be a hardcoded absolute path into
# a SIBLING repo (LightHouse), not this one — every checkout of
# cybercare-dpia that doesn't happen to sit next to a LightHouse checkout at
# exactly this path failed every test in this file. Overridable by an
# environment variable, defaulting to the path that happened to be true on
# the machine this was written on, so CI or a different checkout layout can
# point it at wherever their copy of the converted brief actually lives.
BRIEF = pathlib.Path(
    os.environ.get(
        "PRIVACYCARE_DSR_BRIEF_PATH",
        "/home/shikoli/Cybota/LightHouse/docs/DataPrivacyManenos/converted/01_brief_for_dpia.md",
    )
)


@pytest.fixture
def db(monkeypatch):
    engine = sqlalchemy.create_engine(DB_URL)
    with Session(engine) as session:
        monkeypatch.setattr(session, "commit", session.flush)
        yield session
        session.rollback()


def _timelines_from_the_brief() -> dict[str, int]:
    """Parse §3's statutory-timeline table out of the converted brief.

    The corpus is the requirement. If someone edits the brief, this test starts
    failing and the configuration has to be revisited — which is the point.
    """
    rows = {}
    for line in BRIEF.read_text().splitlines():
        match = re.match(r"\|\s*([A-Za-z ()/]+?)\s*\|\s*(\d+)\s*days?\s*\|", line)
        if match:
            right = match.group(1).split("(")[0].strip().lower()
            rows[right] = int(match.group(2))
    return rows


def test_the_brief_still_says_what_we_think_it_says():
    # Guards the parser itself: if the table's shape changes, every other
    # assertion in this file would silently pass on an empty dict.
    parsed = _timelines_from_the_brief()
    assert parsed == {
        "access": 7,
        "rectification": 14,
        "erasure": 14,
        "restriction": 14,
        "portability": 30,
    }
    assert "objection" not in parsed, "the brief gives objection no timeline"


def test_all_six_rights_are_named(db):
    assert KENYAN_RIGHTS == (
        "access",
        "rectification",
        "erasure",
        "restriction",
        "portability",
        "objection",
    )


def test_every_seeded_clock_matches_the_brief(db):
    seed_timelines(db)
    for right, days in _timelines_from_the_brief().items():
        assert timeline_days(db, right) == days, f"{right} does not match the brief"


def test_objection_is_seeded_with_no_deadline(db):
    # OQ-PRIVACY-02: the brief gives objection no timeline. Engineering may not
    # invent one — it decides when a controller is in breach. Carol owns it.
    seed_timelines(db)
    assert timeline_days(db, "objection") is None


def test_seeding_twice_does_not_duplicate(db):
    seed_timelines(db)
    seed_timelines(db)
    count = db.execute(
        sqlalchemy.text('SELECT count(*) FROM privacycare_dsr_timeline')
    ).scalar()
    assert count == len(KENYAN_RIGHTS)


def test_a_deadline_is_the_clock_added_to_receipt():
    received = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
    assert deadline_for(received, 7) == received + timedelta(days=7)


def test_an_unclocked_right_has_no_deadline():
    # Not "today", not "never" — absent. The UI must be able to say "unclocked".
    assert deadline_for(datetime.now(timezone.utc), None) is None


def test_an_unknown_right_is_rejected(db):
    seed_timelines(db)
    with pytest.raises(ValueError, match="marriage"):
        timeline_days(db, "marriage")
