"""Event-log vocabulary shared by event writers and the inbox read model.

Every row in the ``events`` table records *who* originated the event via a
coarse two-value ``actor`` enum. The inbox surfaces only agent-driven activity
("New since last visit"), so the developer's own actions in the webapp — most
notably submitting comments — must be recorded as ``user`` and never re-surface
to them.

This is deliberately a coarse origin enum, distinct from the free-text ``actor``
recorded on ``document_versions`` (which carries finer provenance like the
importer name or the submitting agent's identity).
"""

from __future__ import annotations

from typing import Literal

EventActor = Literal["agent", "user"]

#: Activity by the feature-workflow agents or the file importer — surfaces in the inbox.
ACTOR_AGENT: EventActor = "agent"
#: Activity by the developer in the webapp (e.g. submitting comments) — does not surface.
ACTOR_USER: EventActor = "user"

# NOTE: these values are the schema contract. The inbox surfacing predicates filter on
# the bare literal 'agent' in SQL (storage/inbox.py, storage/read_state.py) and the
# migration backfills 'user'/'agent' directly — if these strings ever change, update
# those SQL sites and add a migration too.
