from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Deterministic identity and ledger scope supplied by a channel adapter."""

    actor_user_id: UUID
    ledger_id: UUID
    source_channel: str
    channel_identity_id: UUID | None = None
    external_subject_id: str | None = None
