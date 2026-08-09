from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lark_ledger.context import RequestContext
from lark_ledger.models import ChannelIdentity, Ledger, LedgerKind, User, UserStatus
from lark_ledger.services.ledger_management import normalize_ledger_name


class IdentityDisabledError(PermissionError):
    pass


class IdentityService:
    """Resolve channel subjects before requests cross into the ledger core."""

    def __init__(self, session: AsyncSession, *, currency: str, timezone: str) -> None:
        self._session = session
        self._currency = currency
        self._timezone = timezone

    async def resolve_or_bootstrap(
        self,
        *,
        channel: str,
        external_subject_id: str,
        display_name: str = "",
    ) -> RequestContext:
        normalized_channel = channel.strip().lower()
        normalized_subject = external_subject_id.strip()
        if not normalized_channel or not normalized_subject:
            raise ValueError("channel and external_subject_id are required")

        identity = await self._session.scalar(
            select(ChannelIdentity).where(
                ChannelIdentity.channel == normalized_channel,
                ChannelIdentity.external_subject_id == normalized_subject,
            )
        )
        if identity is None:
            user = User(display_name=display_name.strip(), status=UserStatus.ACTIVE.value)
            self._session.add(user)
            await self._session.flush()
            ledger = Ledger(
                owner_user_id=user.id,
                name="我的账本",
                normalized_name=normalize_ledger_name("我的账本")[1],
                kind=LedgerKind.PERSONAL.value,
                currency=self._currency,
                timezone=self._timezone,
                is_default=True,
            )
            identity = ChannelIdentity(
                user_id=user.id,
                channel=normalized_channel,
                external_subject_id=normalized_subject,
                current_ledger_id=ledger.id,
            )
            self._session.add_all([ledger, identity])
            await self._session.flush()
        else:
            loaded_user = await self._session.get(User, identity.user_id)
            if loaded_user is None:
                raise RuntimeError("channel identity references a missing user")
            user = loaded_user
            if user.status != UserStatus.ACTIVE.value:
                raise IdentityDisabledError("user is disabled")
            if display_name.strip() and not user.display_name:
                user.display_name = display_name.strip()
            default_ledger = await self._session.scalar(
                select(Ledger).where(
                    Ledger.owner_user_id == user.id,
                    Ledger.is_default.is_(True),
                )
            )
            if default_ledger is None:
                raise RuntimeError("user has no default ledger")
            ledger = None
            if identity.current_ledger_id is not None:
                ledger = await self._session.scalar(
                    select(Ledger).where(
                        Ledger.id == identity.current_ledger_id,
                        Ledger.owner_user_id == user.id,
                    )
                )
            if ledger is None:
                ledger = default_ledger
                identity.current_ledger_id = ledger.id

        assert ledger is not None

        return RequestContext(
            actor_user_id=user.id,
            ledger_id=ledger.id,
            source_channel=normalized_channel,
            channel_identity_id=identity.id,
            external_subject_id=normalized_subject,
        )
