from src.application.common.policy import ROLE_PERMISSIONS, Permission
from src.application.use_cases.ad_link.commands.manage import (
    CreateAdLink,
    DeleteAdLink,
    UpdateAdLink,
)
from src.application.use_cases.ad_link.queries.list import GetAdLinks
from src.application.use_cases.ad_link.queries.stats import GetAdLinkStats
from src.core.enums import Role


def test_admin_reads_ad_links_but_cannot_change_them() -> None:
    admin = ROLE_PERMISSIONS[Role.ADMIN]

    # The section sits inside the RemnaShop menu, so both are needed to reach it.
    assert Permission.VIEW_REMNASHOP in admin
    assert GetAdLinks.required_permission in admin
    assert GetAdLinkStats.required_permission in admin

    for command in (CreateAdLink, UpdateAdLink, DeleteAdLink):
        assert command.required_permission is Permission.MANAGE_ADVERTISING
        assert command.required_permission not in admin
        assert command.required_permission in ROLE_PERMISSIONS[Role.OWNER]
