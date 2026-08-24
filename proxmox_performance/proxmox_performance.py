from collections import Counter
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin.base_plugin import BasePlugin


def _human_bytes(n):
    if n is None:
        return "n/a"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _human_uptime(seconds):
    if not seconds:
        return "n/a"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


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
        url = f"{host.rstrip('/')}/api2/json/cluster/resources"
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
        show_guest_stats = settings.get("show_guest_stats", "true") == "true"

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

        resources = self._pve_request(host, token_id, token_secret, verify_ssl)

        nodes = [r for r in resources if r.get("type") == "node"]
        guests = [r for r in resources if r.get("type") in ("qemu", "lxc")]

        if not nodes and not guests:
            raise RuntimeError(
                "Proxmox returned no nodes or guests. This usually means the API "
                "token itself (not just the user) is missing a PVEAuditor "
                "permission with Propagate enabled on path '/'."
            )

        width, height = device_config.get_resolution()
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)

        title_font = self._font(max(18, width // 24), bold=True)
        summary_font = self._font(max(15, width // 34), bold=True)
        section_font = self._font(max(14, width // 40), bold=True)
        row_font = self._font(max(12, width // 48))
        small_font = self._font(max(10, width // 64))

        margin = max(12, width // 40)
        y = margin

        draw.text((margin, y), "PROXMOX STATUS", font=title_font, fill="black")
        y += title_font.getbbox("Ag")[3] + margin

        status_counts = Counter(g.get("status", "unknown") for g in guests)
        running = status_counts["running"]
        stopped = status_counts["stopped"]
        other = len(guests) - running - stopped

        summary = f"Running: {running}    Stopped: {stopped}    Other: {other}"
        draw.text((margin, y), summary, font=summary_font, fill="black")
        y += summary_font.getbbox("Ag")[3] + margin

        # ---- Node health section ----
        draw.text((margin, y), "NODE HEALTH", font=section_font, fill="black")
        y += section_font.getbbox("Ag")[3] + max(6, margin // 2)

        total_netin = sum(g.get("netin", 0) or 0 for g in guests)
        total_netout = sum(g.get("netout", 0) or 0 for g in guests)

        for node in sorted(nodes, key=lambda n: n.get("node", "")):
            name = node.get("node", "?")
            status = node.get("status", "unknown")
            cpu_pct = (node.get("cpu") or 0) * 100
            maxcpu = node.get("maxcpu", "?")
            mem_used = node.get("mem", 0)
            mem_total = node.get("maxmem", 0)
            mem_pct = (mem_used / mem_total * 100) if mem_total else 0
            disk_used = node.get("disk", 0)
            disk_total = node.get("maxdisk", 0)
            disk_pct = (disk_used / disk_total * 100) if disk_total else 0
            uptime = _human_uptime(node.get("uptime"))

            line1 = f"{name} [{status}]  CPU {cpu_pct:.0f}% ({maxcpu} cores)  up {uptime}"
            line2 = (
                f"  Mem {_human_bytes(mem_used)}/{_human_bytes(mem_total)} "
                f"({mem_pct:.0f}%)   Disk {_human_bytes(disk_used)}/"
                f"{_human_bytes(disk_total)} ({disk_pct:.0f}%)"
            )

            draw.text((margin, y), line1, font=row_font, fill="black")
            y += row_font.getbbox("Ag")[3] + 4
            draw.text((margin, y), line2, font=row_font, fill="black")
            y += row_font.getbbox("Ag")[3] + max(8, margin // 2)

        net_line = (
            f"  Guest network I/O (since boot): "
            f"in {_human_bytes(total_netin)} / out {_human_bytes(total_netout)}"
        )
        draw.text((margin, y), net_line, font=small_font, fill="black")
        y += small_font.getbbox("Ag")[3] + margin

        draw.line((margin, y, width - margin, y), fill="black", width=2)
        y += margin // 2

        # ---- Guest list section ----
        draw.text((margin, y), "GUESTS", font=section_font, fill="black")
        y += section_font.getbbox("Ag")[3] + max(6, margin // 2)

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
        if show_guest_stats:
            line_height *= 2
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
            y += row_font.getbbox("Ag")[3] + 4

            if show_guest_stats:
                if guest.get("status") == "running":
                    g_cpu = (guest.get("cpu") or 0) * 100
                    g_mem = guest.get("mem", 0)
                    g_maxmem = guest.get("maxmem", 0)
                    stat_line = (
                        f"    CPU {g_cpu:.0f}%   Mem {_human_bytes(g_mem)}/"
                        f"{_human_bytes(g_maxmem)}"
                    )
                else:
                    stat_line = "    -"
                draw.text((margin, y), stat_line, font=small_font, fill="black")
                y += small_font.getbbox("Ag")[3] + max(6, margin // 3)

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