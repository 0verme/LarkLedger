from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Deterministic identity and ledger scope supplied by a channel adapter.

    ``actor_kind`` is the credential family behind the request — ``user`` for
    a human (Feishu identity or a browser ``UserSession``) or ``client`` for a
    machine ``ClientCredential``. ``source_channel`` remains the transport
    (``feishu`` / ``web`` / ``client_api``); the business layer must never
    branch on it.
    """

    actor_user_id: UUID
    ledger_id: UUID
    source_channel: str
    channel_identity_id: UUID | None = None
    external_subject_id: str | None = None
    actor_kind: str = "user"
