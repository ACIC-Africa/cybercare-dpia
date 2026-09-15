"""Celery worker entry point for PrivacyCare.

Fides' own worker (`fides worker` -> fides.api.worker.start_worker) imports
only Fides' task modules, and `autodiscover_task_locations` in
fides/api/tasks/__init__.py is an Ethyca-authored list we do not edit. A
task no process imports is a task no worker can run — the Generate button
would spin forever with nothing in the logs.

So PrivacyCare gets its own entry module, the same shape as asgi.py: import
our tasks first so the @celery_app.task decorator runs and registers them,
then re-export Fides' celery_app for the CLI to find. Start it with:

    celery -A fides.api.privacycare.worker worker \
           --queues=fidesplus.privacy_assessments,fidesplus.discovery_monitors_detection

Nothing Ethyca owns is edited; docker-compose.privacycare.yml repoints the
command.
"""
from fides.api.privacycare import tasks  # noqa: F401  (import registers the task)
from fides.api.privacycare.discovery import (
    execute,  # noqa: F401  (import registers the task)
)
from fides.api.tasks import celery_app

app = celery_app

__all__ = ["app", "celery_app", "tasks", "execute"]
