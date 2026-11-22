"""The calibration sidebar panel."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN, PANEL_ELEMENT, PANEL_ICON, PANEL_TITLE, PANEL_URL, STATIC_URL

_LOGGER = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).parent / "frontend"


async def async_register_panel(hass: HomeAssistant) -> bool:
    """Serve the panel module and add the panel to the sidebar (admins only).

    Skipped when the frontend is not loaded (e.g. in tests or headless setups).
    """
    if hass.http is None or "frontend" not in hass.config.components:
        _LOGGER.debug("Frontend not loaded; calibration panel not registered")
        return False

    from homeassistant.components import panel_custom  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    integration = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(FRONTEND_DIR), cache_headers=False)]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{STATIC_URL}/light-match-panel.js?v={integration.version}",
        require_admin=True,
        config={},
    )
    return True
