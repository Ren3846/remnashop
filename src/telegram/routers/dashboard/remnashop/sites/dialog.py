from aiogram_dialog import Dialog, StartMode, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, ListGroup, Row, ScrollingGroup, Start, SwitchTo
from aiogram_dialog.widgets.text import Format

from src.core.enums import BannerName
from src.telegram.keyboards import main_menu_button
from src.telegram.states import DashboardRemnashop, RemnashopSites
from src.telegram.widgets import Banner, I18nFormat, IgnoreUpdate

from .getters import site_getter, sites_getter, subdomain_getter
from .handlers import (
    on_keyword_active_toggle,
    on_keyword_input,
    on_site_active_toggle,
    on_site_domain_input,
    on_site_name_input,
    on_site_select,
    on_subdomain_active_toggle,
    on_subdomain_host_input,
    on_subdomain_select,
)

sites = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-sites-main"),
    ScrollingGroup(
        ListGroup(
            Row(
                Button(
                    text=Format("{item[title]}"),
                    id="site_select",
                    on_click=on_site_select,
                ),
            ),
            id="sites_list",
            item_id_getter=lambda item: item["id"],
            items="sites",
        ),
        id="scroll",
        width=1,
        height=8,
        hide_on_single_page=True,
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-sites.add-site"),
            id="add_site",
            state=RemnashopSites.SITE_NAME,
        ),
    ),
    Row(
        Start(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=DashboardRemnashop.MAIN,
            mode=StartMode.RESET_STACK,
        ),
        *main_menu_button,
    ),
    IgnoreUpdate(),
    state=RemnashopSites.MAIN,
    getter=sites_getter,
)

site = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-sites-site"),
    Format("🌐 <b>{name}</b>\n<code>{base_domain}</code>"),
    ScrollingGroup(
        ListGroup(
            Row(
                Button(
                    text=Format("{item[title]}"),
                    id="subdomain_select",
                    on_click=on_subdomain_select,
                ),
            ),
            id="subdomains_list",
            item_id_getter=lambda item: item["id"],
            items="subdomains",
        ),
        id="scroll",
        width=1,
        height=6,
        hide_on_single_page=True,
    ),
    Row(
        Button(
            text=I18nFormat("btn-sites.toggle-active"),
            id="toggle_site",
            on_click=on_site_active_toggle,
        ),
        SwitchTo(
            text=I18nFormat("btn-sites.add-subdomain"),
            id="add_subdomain",
            state=RemnashopSites.SUBDOMAIN_HOST,
        ),
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=RemnashopSites.MAIN,
        ),
    ),
    IgnoreUpdate(),
    state=RemnashopSites.SITE,
    getter=site_getter,
)

subdomain = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-sites-subdomain"),
    Format("🔗 <code>{host}</code>"),
    ScrollingGroup(
        ListGroup(
            Row(
                Button(
                    text=Format("{item[title]}"),
                    id="keyword_toggle",
                    on_click=on_keyword_active_toggle,
                ),
            ),
            id="keywords_list",
            item_id_getter=lambda item: item["id"],
            items="keywords",
        ),
        id="scroll",
        width=1,
        height=6,
        hide_on_single_page=True,
    ),
    Row(
        Button(
            text=I18nFormat("btn-sites.toggle-active"),
            id="toggle_subdomain",
            on_click=on_subdomain_active_toggle,
        ),
        SwitchTo(
            text=I18nFormat("btn-sites.add-keyword"),
            id="add_keyword",
            state=RemnashopSites.KEYWORD,
        ),
    ),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=RemnashopSites.SITE,
        ),
    ),
    IgnoreUpdate(),
    state=RemnashopSites.SUBDOMAIN,
    getter=subdomain_getter,
)

site_name = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-sites-site-name"),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=RemnashopSites.MAIN,
        ),
    ),
    MessageInput(func=on_site_name_input),
    IgnoreUpdate(),
    state=RemnashopSites.SITE_NAME,
)

site_domain = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-sites-site-domain"),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=RemnashopSites.SITE_NAME,
        ),
    ),
    MessageInput(func=on_site_domain_input),
    IgnoreUpdate(),
    state=RemnashopSites.SITE_DOMAIN,
)

subdomain_host = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-sites-subdomain-host"),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=RemnashopSites.SITE,
        ),
    ),
    MessageInput(func=on_subdomain_host_input),
    IgnoreUpdate(),
    state=RemnashopSites.SUBDOMAIN_HOST,
)

keyword = Window(
    Banner(BannerName.DASHBOARD),
    I18nFormat("msg-sites-keyword"),
    Row(
        SwitchTo(
            text=I18nFormat("btn-back.general"),
            id="back",
            state=RemnashopSites.SUBDOMAIN,
        ),
    ),
    MessageInput(func=on_keyword_input),
    IgnoreUpdate(),
    state=RemnashopSites.KEYWORD,
)

router = Dialog(
    sites,
    site,
    subdomain,
    site_name,
    site_domain,
    subdomain_host,
    keyword,
)
