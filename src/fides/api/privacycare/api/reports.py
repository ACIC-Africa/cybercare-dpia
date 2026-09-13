# GET .../{assessment_id}/pdf — the DPIA report a DPO files with Kenya's
# ODPC, rendered from the Report document model (report.py) into an actual
# PDF (pdf.py). This module's whole job is HTTP shape: auth, 404 for an
# unknown assessment, 503 for a renderer failure, and a safe filename —
# exactly the read-route pattern get_assessment already uses in
# assessments.py (LookupError -> 404), plus one wrinkle neither of those
# routes has: a binary body instead of a validated Pydantic response.
#
# Registered on the SAME privacycare_router as tasks/config/assessments (see
# api/router.py's own comment on why THEIR three imports are order-sensitive
# with each other). This route's path is `/{assessment_id}/pdf` — two path
# segments — which cannot ever be shadowed by, or shadow, any single-segment
# route like `/{assessment_id}` or `/tasks`; Starlette matches by the whole
# path template, not a prefix. So this import carries none of that ordering
# hazard and can be registered anywhere in that block. It is placed after
# assessments/processes so a reader sees the whole `/{assessment_id}*`
# family together.
import re
from typing import Literal

from fastapi import Depends, HTTPException, Response, Security, status
from sqlalchemy.orm import Session

from fides.api.deps import get_db
from fides.api.oauth.utils import verify_oauth_client
from fides.api.privacycare.api.router import privacycare_router
from fides.api.privacycare.pdf import PDFRenderError, render_pdf
from fides.api.privacycare.report import build_report
from fides.common.scope_registry import SYSTEM_READ

# The admin UI's downloadAssessmentReport (privacy-assessments.slice.ts)
# extracts the saved filename from Content-Disposition with EXACTLY this
# regex: /filename="?([^";\n]+)"?/ — the captured group excludes only `"`,
# `;` and `\n`. report.title is `f"Data Protection Impact Assessment:
# {detail.name}"`, and detail.name is officer-supplied free text on the
# assessment itself: nothing stops a DPO naming an assessment
# `Fuel "Card"; CRM\nreview`, and that name reaching this header unsanitised
# would either truncate the filename the UI extracts (at the first `"`
# or `;`) or, worse, break the regex's assumption that the value has no
# embedded newline. Stripping everything but a conservative safe set before
# it ever reaches the header means no character sequence in an assessment's
# name can produce a header the UI's regex parses wrong — tested directly
# against that regex in tests/privacycare/test_api_reports.py, not against a
# guess at what it accepts.
_FILENAME_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 ._-]+")


def _sanitize_filename(text: str) -> str:
    """A safe, non-empty base filename (without extension) derived from
    officer-supplied free text.

    Keeps only letters, digits, spaces, dots, hyphens and underscores —
    deliberately conservative (not just the 3 characters the UI's regex
    excludes): this also strips path separators and shell/filesystem
    metacharacters a name could contain, so the result is safe to hand to
    `saveAs()` on any OS, not merely "passes one regex".
    """
    cleaned = _FILENAME_UNSAFE_CHARS.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "assessment-report"


@privacycare_router.get(
    "/{assessment_id}/pdf",
    dependencies=[Security(verify_oauth_client, scopes=[SYSTEM_READ])],
    # bytes, not a Pydantic model: this route returns a raw PDF binary body
    # (fastapi.Response, media_type="application/pdf"), which has no
    # validated shape to declare. Set explicitly (rather than left to
    # infer from the `-> Response` return annotation, which FastAPI would
    # instead resolve to response_model=None) purely so
    # test_every_route_has_a_response_model_declared_or_inferred
    # (test_api_registration.py) — which requires every plus/
    # privacy-assessments route to have SOME non-None response_model —
    # still passes. Returning an actual Response instance bypasses
    # response_model serialization entirely, so this declared type is never
    # applied to the real body. See test_response_model_ts_parity.py's
    # ALLOWLIST["bytes"] for why that model has, and needs, no TS
    # counterpart.
    response_model=bytes,
)
def get_assessment_pdf(
    assessment_id: str,
    export_mode: Literal["internal", "external"] = "external",
    *,
    db: Session = Depends(get_db),
) -> Response:
    """Render and return the DPIA report for `assessment_id` as a PDF.

    `export_mode` is accepted and threaded through to the Report (and
    printed in the PDF's footer) but does not change what is rendered
    today — both values produce the same document. What a controller may
    lawfully withhold from the regulator in an "internal" vs "external"
    export is a disclosure judgement, not something this route invents;
    tracked as OQ-PDF-01 for the customer's privacy SME.

    404 for an unknown assessment_id (LookupError from build_report, same
    mapping get_assessment already uses). 503 — with a JSON `detail` naming
    the cause — for a PDFRenderError from render_pdf: a broken/missing
    ReportLab install or a missing Unicode font must not look like "this
    assessment doesn't exist" to the officer, and the admin UI's
    downloadAssessmentReport reads exactly `errorJson.detail` to show them
    why.
    """
    try:
        report = build_report(db, assessment_id, export_mode=export_mode)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No assessment with id {assessment_id}",
        )

    try:
        pdf_bytes = render_pdf(report)
    except PDFRenderError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    filename = f"{_sanitize_filename(report.title)}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
