"""Pairing help: a QR code notification until Apple Home pairs the accessory."""

from __future__ import annotations

import io
import secrets
from typing import TYPE_CHECKING

from aiohttp import web
import pyqrcode

from homeassistant.components import persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

if TYPE_CHECKING:
    from .driver import HomeKitAccessoryServer

QR_URL = f"/api/{DOMAIN}/pairing_qr"
DATA_QR_TOKENS = f"{DOMAIN}_qr_tokens"
DATA_VIEW = f"{DOMAIN}_qr_view"


def notification_id(entry_id: str) -> str:
    """Return the persistent notification id for an entry."""
    return f"{DOMAIN}_pairing_{entry_id}"


def qr_svg(payload: str) -> bytes:
    """Render a setup payload as an SVG QR code."""
    buffer = io.BytesIO()
    pyqrcode.create(payload).svg(buffer, scale=6, quiet_zone=2, background="#ffffff")
    return buffer.getvalue()


def qr_image_url(hass: HomeAssistant, entry_id: str) -> str:
    """Return a tokenized URL for the entry's pairing QR code."""
    tokens: dict[str, str] = hass.data.setdefault(DATA_QR_TOKENS, {})
    token = tokens.setdefault(entry_id, secrets.token_hex(16))
    return f"{QR_URL}?entry_id={entry_id}&token={token}"


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Register the QR code view once."""
    if not hass.data.get(DATA_VIEW):
        hass.http.register_view(PairingQRView())
        hass.data[DATA_VIEW] = True


def pairing_markdown(hass: HomeAssistant, server: HomeKitAccessoryServer) -> str:
    """Instructions with the setup code and QR code."""
    return (
        f"Open **Apple Home → Add Accessory** and scan this code, or choose "
        f"**More options** and enter `{server.pin}`.\n\n"
        f"![Pairing QR code for {server.entry.title}]"
        f"({qr_image_url(hass, server.entry.entry_id)})"
    )


@callback
def async_show_pairing(hass: HomeAssistant, server: HomeKitAccessoryServer) -> None:
    """Show the pairing notification while the accessory is unpaired."""
    if server.paired:
        async_dismiss_pairing(hass, server.entry.entry_id)
        return
    persistent_notification.async_create(
        hass,
        pairing_markdown(hass, server),
        title=f"Add {server.entry.title} to Apple Home",
        notification_id=notification_id(server.entry.entry_id),
    )


@callback
def async_dismiss_pairing(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the pairing notification."""
    persistent_notification.async_dismiss(hass, notification_id(entry_id))


class PairingQRView(HomeAssistantView):
    """Serve pairing QR codes to the notification's <img> tag."""

    url = QR_URL
    name = f"api:{DOMAIN}:pairing_qr"
    # Browsers don't send auth headers for images; the per-entry token stands in.
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the QR code SVG for a valid entry and token."""
        hass: HomeAssistant = request.app["hass"]
        entry_id = request.query.get("entry_id", "")
        token = request.query.get("token", "")
        expected = hass.data.get(DATA_QR_TOKENS, {}).get(entry_id)
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            not expected
            or not secrets.compare_digest(token, expected)
            or entry is None
            or entry.domain != DOMAIN
            or getattr(entry, "runtime_data", None) is None
            or (uri := entry.runtime_data.setup_uri) is None
        ):
            raise web.HTTPNotFound
        return web.Response(
            body=await hass.async_add_executor_job(qr_svg, uri),
            content_type="image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )
