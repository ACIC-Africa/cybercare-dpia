# Queue names live in two places that cannot import each other.
#
# Python gets them from Ethyca: GENERATION_QUEUE is
# fides.api.tasks.PRIVACY_ASSESSMENTS_QUEUE_NAME, and
# DISCOVERY_MONITORS_DETECTION_QUEUE_NAME is Ethyca's own constant of the
# same name — both imported, not retyped. YAML cannot import anything, so
# docker-compose.privacycare.yml spells all eight out as literals — the
# seven Ethyca queues worker-other excludes, plus ours.
#
# That split is the hazard. Rename a queue upstream and the Python moves with
# it while the YAML does not: the API publishes to the new queue, the worker
# listens on the old one, and the message is never consumed. No error is
# raised anywhere — a DPIA generation, or a discovery scan, simply never
# happens, and the UI polls a task that stays `in_processing` forever.
#
# worker-privacycare (Task 4 fix) listens on BOTH queues: generation
# (api/tasks.py's route) and discovery detection
# (api/monitors.py's execute route) are two different task families served
# by the one process that imports both fides.api.privacycare.tasks and
# fides.api.privacycare.discovery.execute (see worker.py).
#
# So the YAML is checked against the Python. It is the only direction
# available, and it is enough to make the drift loud.
import pathlib
import re

import pytest
import yaml

from fides.api import tasks as ethyca_tasks
from fides.api.privacycare.tasks import GENERATION_QUEUE
from fides.api.tasks import DISCOVERY_MONITORS_DETECTION_QUEUE_NAME

COMPOSE = pathlib.Path(__file__).parents[2] / "docker-compose.privacycare.yml"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _known_queue_names() -> set[str]:
    """Every queue name Ethyca's task module exports."""
    names = {
        value
        for name, value in vars(ethyca_tasks).items()
        if name.endswith("QUEUE_NAME") and isinstance(value, str)
    }
    assert names, "fides.api.tasks exports no *_QUEUE_NAME constants any more"
    return names


def _queue_args(command: str, flag: str) -> set[str]:
    match = re.search(rf"--{flag}=(\S+)", command)
    if not match:
        return set()
    return {q for q in match.group(1).split(",") if q}


def _service_command(service: str) -> str:
    command = _compose()["services"][service].get("command", "")
    assert command, f"{service} declares no command in {COMPOSE}"
    return command if isinstance(command, str) else " ".join(command)


@pytest.mark.parametrize(
    "service,flag",
    [("worker-other", "exclude-queues"), ("worker-privacycare", "queues")],
)
def test_every_queue_name_in_the_compose_file_is_one_python_exports(service, flag):
    literals = _queue_args(_service_command(service), flag)
    assert literals, f"{service} names no queues with --{flag}"
    unknown = literals - _known_queue_names()
    assert not unknown, (
        f"{service}'s --{flag} names {sorted(unknown)}, which no *_QUEUE_NAME "
        f"constant in fides.api.tasks matches. Either the YAML has drifted "
        f"from a renamed constant or it has a typo; both mean a worker "
        f"listening on a queue nothing publishes to, silently."
    )


def test_the_privacycare_worker_listens_on_exactly_the_queues_python_publishes_to():
    # Two task families, two queues: DPIA generation (fides.api.privacycare.
    # tasks.GENERATION_QUEUE) and discovery-monitor execution
    # (api/monitors.py's execute route, which queues to Ethyca's
    # DISCOVERY_MONITORS_DETECTION_QUEUE_NAME). worker-privacycare is the
    # only process that imports both task modules (see worker.py), so it
    # must listen on exactly these two — no more, no fewer.
    listening = _queue_args(_service_command("worker-privacycare"), "queues")
    expected = {GENERATION_QUEUE, DISCOVERY_MONITORS_DETECTION_QUEUE_NAME}
    assert listening == expected, (
        f"worker-privacycare listens on {sorted(listening)} but the routes "
        f"publish to {sorted(expected)}. A mismatch means a generation or "
        f"discovery-scan message is queued and never consumed."
    )


def test_the_generic_worker_excludes_our_queue():
    # worker-other excludes queues rather than listing them, so without this
    # entry it happily consumes a generation message for a task it has never
    # registered — the message is acknowledged and dropped, the task never
    # runs, and nothing anywhere says so.
    excluded = _queue_args(_service_command("worker-other"), "exclude-queues")
    assert GENERATION_QUEUE in excluded, (
        f"worker-other does not exclude {GENERATION_QUEUE!r}; it will steal "
        f"generation messages it cannot execute."
    )
