from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.services.dashboard_auth import DashboardAuthError, DashboardAuthService

pytestmark = pytest.mark.postgres


async def test_dashboard_sessions_are_isolated_and_revocable_in_postgres(
    postgres_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(
        _env_file=None,
        dashboard_enabled=True,
        dashboard_base_url="http://ledger.test",
        dashboard_session_secret="integration-only-secret-long-enough-123456",
        dashboard_cookie_secure=False,
        dashboard_admin_open_ids="ou_admin",
        lark_app_id="cli_test",
        lark_app_secret="test-secret",
    )
    service = DashboardAuthService(settings, postgres_session_factory)
    user = await service.create_session(_identity("ou_user", "用户"))
    admin = await service.create_session(_identity("ou_admin", "管理员"))

    assert (await service.authenticate(user.session_token)).role == "USER"
    assert (await service.authenticate(admin.session_token)).role == "ADMIN"
    await service.revoke(user.session_token)
    with pytest.raises(DashboardAuthError, match="失效"):
        await service.authenticate(user.session_token)
    assert (await service.authenticate(admin.session_token)).user_open_id == "ou_admin"


def _identity(open_id: str, name: str) -> dict[str, Any]:
    return {"open_id": open_id, "name": name, "avatar_url": ""}
