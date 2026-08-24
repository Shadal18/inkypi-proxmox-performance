from collections import Counter, defaultdict
from datetime import datetime

import requests

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


GUEST_BAR_COLORS = ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]


class ProxmoxPerformance(BasePlugin):
    DEFAULT_TIMEOUT = 10

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

        raw_nodes = [r for r in resources if r.get("type") == "node"]
        raw_guests = [r for r in resources if r.get("type") in ("qemu", "lxc")]
        raw_storage = [r for r in resources if r.get("type") == "storage"]

        if not raw_nodes and not raw_guests:
            raise RuntimeError(
                "Proxmox returned no nodes or guests. Make sure both the user "
                "AND the API token have a PVEAuditor (or higher) role granted "
                "on path '/' with Propagate enabled."
            )

        storage_used = defaultdict(int)
        storage_total = defaultdict(int)
        seen_shared = set()
        for s in raw_storage:
            node_name = s.get("node")
            if not node_name:
                continue
            storage_id = s.get("storage")
            if s.get("shared") and storage_id in seen_shared:
                continue
            if s.get("shared"):
                seen_shared.add(storage_id)
            storage_used[node_name] += s.get("disk", 0) or 0
            storage_total[node_name] += s.get("maxdisk", 0) or 0

        status_counts = Counter(g.get("status", "unknown") for g in raw_guests)
        running = status_counts["running"]
        stopped = status_counts["stopped"]
        other = len(raw_guests) - running - stopped

        cpu_pcts = []
        mem_used_total = mem_total_total = 0
        disk_used_total = disk_total_total = 0
        primary_node = None

        for node in sorted(raw_nodes, key=lambda n: n.get("node", "")):
            name = node.get("node", "?")
            mem_used = node.get("mem", 0)
            mem_total = node.get("maxmem", 0)
            disk_used = storage_used.get(name, node.get("disk", 0))
            disk_total = storage_total.get(name, node.get("maxdisk", 0))

            cpu_pcts.append((node.get("cpu") or 0) * 100)
            mem_used_total += mem_used
            mem_total_total += mem_total
            disk_used_total += disk_used
            disk_total_total += disk_total

            if primary_node is None:
                primary_node = {
                    "name": name,
                    "status": node.get("status", "unknown"),
                    "uptime": _human_uptime(node.get("uptime")),
                }

        cpu_pct = round(sum(cpu_pcts) / len(cpu_pcts)) if cpu_pcts else 0
        mem_pct = round((mem_used_total / mem_total_total * 100) if mem_total_total else 0)
        disk_pct = round((disk_used_total / disk_total_total * 100) if disk_total_total else 0)

        visible_guests = [
            g for g in raw_guests if show_stopped or g.get("status") != "stopped"
        ]
        visible_guests.sort(key=lambda g: (g.get("mem", 0) or 0), reverse=True)

        max_rows = 10 if show_guest_stats else 14
        omitted = max(0, len(visible_guests) - max_rows)
        trimmed = visible_guests[:max_rows]

        max_mem = max((g.get("mem", 0) or 0 for g in trimmed), default=0)

        guests = []
        for i, guest in enumerate(trimmed):
            status = guest.get("status", "unknown")
            mem_bytes = guest.get("mem", 0) or 0
            g_cpu = round((guest.get("cpu") or 0) * 100) if status == "running" else None
            g_maxmem = guest.get("maxmem", 0) if status == "running" else None

            guests.append({
                "vmid": guest.get("vmid", "?"),
                "name": guest.get("name") or f"unnamed-{guest.get('vmid', '?')}",
                "type": "CT" if guest.get("type") == "lxc" else "VM",
                "status": status,
                "status_class": (
                    "running" if status == "running"
                    else "stopped" if status == "stopped"
                    else "other"
                ),
                "bar_color": GUEST_BAR_COLORS[i % len(GUEST_BAR_COLORS)],
                "bar_pct": round((mem_bytes / max_mem * 100) if max_mem else 0),
                "cpu_pct": g_cpu,
                "mem_used_h": _human_bytes(mem_bytes) if status == "running" else "-",
                "mem_total_h": _human_bytes(g_maxmem) if g_maxmem is not None else None,
            })

        template_params = {
            "primary_node": primary_node or {"name": "n/a", "status": "unknown", "uptime": "n/a"},
            "running": running,
            "stopped": stopped,
            "other": other,
            "cpu_pct": cpu_pct,
            "mem_pct": mem_pct,
            "disk_pct": disk_pct,
            "guests": guests,
            "show_guest_stats": show_guest_stats,
            "omitted": omitted,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "plugin_settings": settings,
        }

        dimensions = device_config.get_resolution()

        return self.render_image(
            dimensions,
            "proxmox_performance.html",
            "proxmox_performance.css",
            template_params,
        )
