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

        nodes = []
        for node in sorted(raw_nodes, key=lambda n: n.get("node", "")):
            name = node.get("node", "?")
            mem_used = node.get("mem", 0)
            mem_total = node.get("maxmem", 0)

            disk_used = storage_used.get(name, node.get("disk", 0))
            disk_total = storage_total.get(name, node.get("maxdisk", 0))

            nodes.append({
                "name": name,
                "status": node.get("status", "unknown"),
                "cpu_pct": round((node.get("cpu") or 0) * 100),
                "maxcpu": node.get("maxcpu", "?"),
                "mem_used_h": _human_bytes(mem_used),
                "mem_total_h": _human_bytes(mem_total),
                "mem_pct": round((mem_used / mem_total * 100) if mem_total else 0),
                "disk_used_h": _human_bytes(disk_used),
                "disk_total_h": _human_bytes(disk_total),
                "disk_pct": round((disk_used / disk_total * 100) if disk_total else 0),
                "uptime": _human_uptime(node.get("uptime")),
            })

        # Sort guests purely by current memory usage, highest first. Stopped
        # guests report no live mem figure and sink to the bottom naturally.
        visible_guests = [
            g for g in raw_guests if show_stopped or g.get("status") != "stopped"
        ]
        visible_guests.sort(key=lambda g: (g.get("mem", 0) or 0), reverse=True)

        max_rows = 12 if show_guest_stats else 18
        omitted = max(0, len(visible_guests) - max_rows)

        guests = []
        for guest in visible_guests[:max_rows]:
            status = guest.get("status", "unknown")
            g_cpu = round((guest.get("cpu") or 0) * 100) if status == "running" else None
            g_mem = guest.get("mem", 0) if status == "running" else None
            g_maxmem = guest.get("maxmem", 0) if status == "running" else None

            guests.append({
                "vmid": guest.get("vmid", "?"),
                "name": guest.get("name") or f"unnamed-{guest.get('vmid', '?')}",
                "node": guest.get("node", "?"),
                "type": "CT" if guest.get("type") == "lxc" else "VM",
                "status": status,
                "status_class": (
                    "running" if status == "running"
                    else "stopped" if status == "stopped"
                    else "other"
                ),
                "cpu_pct": g_cpu,
                "mem_used_h": _human_bytes(g_mem) if g_mem is not None else None,
                "mem_total_h": _human_bytes(g_maxmem) if g_maxmem is not None else None,
            })

        template_params = {
            "running": running,
            "stopped": stopped,
            "other": other,
            "nodes": nodes,
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
