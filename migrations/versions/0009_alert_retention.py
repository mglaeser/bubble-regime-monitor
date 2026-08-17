"""Message-body retention: allow ONE redaction of a final render.

Addendum B H-07 asks for two retention horizons — long for event metadata,
short for rendered message bodies and raw model output — and, crucially, for
the short one to expire the bodies *without deleting* the metadata around them:
episode transitions, delivery status and timing, member mapping, ruleset and
phrase provenance, actionability labels, budget decisions, replay eligibility.

Deleting the `alert_render` row would satisfy the first half and violate the
second: the row also carries `planning_phrase_set_sha256`, the render source,
the septet count and the validation results, which are provenance rather than
message content. So the body is redacted in place and the row survives.

That collides with `alert_render_no_update`, which aborts any change to
`final_message` — correctly, because a render a retry reuses must not be
rewritable. The trigger is therefore replaced by one that permits EXACTLY one
transition and nothing else:

    final_message -> '' , at the same time as body_redacted_at goes NULL -> set

Anything else still aborts, including a second redaction (the guard requires
`OLD.body_redacted_at IS NULL`), any non-empty rewrite, and any attempt to
clear the stamp afterwards. `gsm7_septets` is deliberately left alone: the
LENGTH of what was sent stays auditable after the text is gone.

`alert_llm_attempt` needs no change — it never stored raw model output, only
status, hashes and a redacted error string.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-16
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_REDACTABLE_RENDER_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS alert_render_no_update
BEFORE UPDATE ON alert_render
WHEN OLD.final_message <> NEW.final_message
     AND NOT (NEW.final_message = ''
              AND NEW.body_redacted_at IS NOT NULL
              AND OLD.body_redacted_at IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'a final render is immutable; create a new render instead');
END
"""

_ORIGINAL_RENDER_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS alert_render_no_update
BEFORE UPDATE ON alert_render
WHEN OLD.final_message <> NEW.final_message
BEGIN
    SELECT RAISE(ABORT, 'a final render is immutable; create a new render instead');
END
"""


def upgrade() -> None:
    op.execute("ALTER TABLE alert_render ADD COLUMN body_redacted_at DATETIME")
    op.execute("DROP TRIGGER IF EXISTS alert_render_no_update")
    op.execute(_REDACTABLE_RENDER_TRIGGER)


def downgrade() -> None:
    # The column cannot come back off without a table rebuild, and a rebuild
    # would drop the very rows retention is about. Restoring the strict trigger
    # is the meaningful half: after this, no render body can be redacted.
    op.execute("DROP TRIGGER IF EXISTS alert_render_no_update")
    op.execute(_ORIGINAL_RENDER_TRIGGER)
