from collections import Counter
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin.base_plugin import BasePlugin


class ProxmoxPerformance(BasePlugin):
    DEFAULT_TIMEOUT = 10

    def _font(self, size, bold=False):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold else
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
            if bold else
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]

        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass

        return ImageFont.load_default()

    def _pve_request(self, host, token_id, token_secret, verify_ssl):
        url = f"{host.rstrip('/')}/api2/json/cluster/resources?type=vm"
        headers = {
            "Authorization": f"PVEAPIToken={token_id}={token_secret}",
            "Accept": "application/json",
        }

        try:
            response = requests.get(
                url, headers=headers, timeout=self.DEFAULT_TIMEOUT, verify=verify_ssl
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Unable to query Proxmox: {exc}") from exc

        try:
            return response.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Proxmox returned an unexpected API response.") from exc

    def generate_image(self, settings, device_config):
        host = settings.get("proxmox_host", "").strip()
        verify_ssl = settings.get("verify_ssl") == "true"
        show_stopped = settings.get("show_stopped") == "true"

        if not host:
            raise RuntimeError("Proxmox host is required.")

        token_id = device_config.load_env_key("PROXMOX_TOKEN_ID")
        token_secret = device_config.load_env_key("PROXMOX_TOKEN_SECRET")

        if not token_id or not token_secret:
            raise RuntimeError(
                "Proxmox API token not found. Add PROXMOX_TOKEN_ID and "
                "PROXMOX_TOKEN_SECRET using the key icon on the main screen."
            )

        if not host.startswith(("https://", "http://")):
            host = f"https://{host}"

        guests = self._pve_request(host, token_id, token_secret, verify_ssl)

        width = device_config.width
        height = device_config.height

        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)

        title_font = self._font(max(18, width // 24), bold=True)
        summary_font = self._font(max(15, width // 34), bold=True)
        row_font = self._font(max(12, width // 48))
        small_font = self._font(max(10, width // 64))

        status_counts = Counter(vm.get("status", "unknown") for vm in guests)
        running = status_counts["running"]
        stopped = status_counts["stopped"]
        other = len(guests) - running - stopped

        margin = max(12, width // 40)
        y = margin

        draw.text((margin, y), "PROXMOX VM STATUS", font=title_font, fill="black")
        y += title_font.getbbox("Ag")[3] + margin

        summary = f"Running: {running}    Stopped: {stopped}    Other: {other}"
        draw.text((margin, y), summary, font=summary_font, fill="black")
        y += summary_font.getbbox("Ag")[3] + margin

        draw.line((margin, y, width - margin, y), fill="black", width=2)
        y += margin // 2

        visible_guests = [
            g for g in guests if show_stopped or g.get("status") != "stopped"
        ]
        visible_guests.sort(
            key=lambda vm: (
                vm.get("status") != "running",
                vm.get("node", ""),
                vm.get("vmid", 0),
            )
        )

        line_height = row_font.getbbox("Ag")[3] + max(5, height // 110)
        footer_height = small_font.getbbox("Ag")[3] + margin
        available_height = height - y - footer_height
        max_rows = max(1, available_height // line_height)

        for guest in visible_guests[:max_rows]:
            vmid = guest.get("vmid", "?")
            name = guest.get("name") or f"unnamed-{vmid}"
            node = guest.get("node", "?")
            guest_type = "CT" if guest.get("type") == "lxc" else "VM"
            status = guest.get("status", "unknown").upper()
            marker = "●" if guest.get("status") == "running" else (
                "○" if guest.get("status") == "stopped" else "!"
            )

            left = f"{marker} {guest_type} {vmid}  {name}"
            right = f"{node}  {status}"

            draw.text((margin, y), left, font=row_font, fill="black")
            right_width = draw.textbbox((0, 0), right, font=row_font)[2]
            draw.text((width - margin - right_width, y), right, font=row_font, fill="black")
            y += line_height

        omitted = len(visible_guests) - max_rows
        if omitted > 0:
            draw.text(
                (margin, y), f"+ {omitted} additional guest(s)", font=small_font, fill="black"
            )

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        footer = f"Updated {timestamp}"
        footer_width = draw.textbbox((0, 0), footer, font=small_font)[2]
        draw.text(
            (width - margin - footer_width, height - margin - small_font.getbbox("Ag")[3]),
            footer, font=small_font, fill="black",
        )

        return image