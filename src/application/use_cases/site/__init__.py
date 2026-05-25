from typing import Final

from src.application.common import Interactor

from .commands.manage import (
    AddSiteKeyword,
    AddSiteKeywordDto,
    CreateSite,
    CreateSiteDto,
    CreateSubdomain,
    CreateSubdomainDto,
    ToggleSiteActive,
    ToggleSiteKeywordActive,
    ToggleSubdomainActive,
)

SITE_USE_CASES: Final[tuple[type[Interactor], ...]] = (
    CreateSite,
    CreateSubdomain,
    AddSiteKeyword,
    ToggleSiteActive,
    ToggleSubdomainActive,
    ToggleSiteKeywordActive,
)

__all__ = [
    "AddSiteKeyword",
    "AddSiteKeywordDto",
    "CreateSite",
    "CreateSiteDto",
    "CreateSubdomain",
    "CreateSubdomainDto",
    "ToggleSiteActive",
    "ToggleSiteKeywordActive",
    "ToggleSubdomainActive",
    "SITE_USE_CASES",
]
