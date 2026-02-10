"""Infrastructure quick-action commands — direct SSH/subprocess, no Claude."""

import asyncio
import logging
import os

from dispatcher.apis import get_session, DispatchError

log = logging.getLogger("claude-telegram")

# Node SSH targets — map friendly names to SSH hosts
# These must match ~/.ssh/config or ~/.ssh/config.puretensor
NODES = {
    "tensor-core": "localhost",
    "mon1": "mon1",
    "mon2": "mon2",
    "mon3": "mon3",
    "e2-micro": "e2-micro",
    "gcp-medium": "gcp-medium",
    "arx1": "arx1",
    "arx2": "arx2",
    "arx3": "arx3",
    "arx4": "arx4",
    "fox-n0": "fox-n0",
    "fox-n1": "fox-n1",
}

# Services that can be restarted (whitelist for safety)
ALLOWED_SERVICES = {
    "claude-telegram-bot",
    "whatsapp-translator",
    "bretalon-report-bot",
    "nginx",
    "prometheus",
    "grafana-server",
    "loki",
    "alertmanager",
    "node_exporter",
    "prometheus-node-exporter",
    "openclaw-gateway",
}

DEPLOY_WEBHOOK_URL = os.environ.get(
    "DEPLOY_WEBHOOK_URL",
    "http://e2-micro:8888/webhook.php?secret=gitea_deploy_2024",
)

# Websites to check in check_sites()
MONITORED_SITES = [
    "https://bretalon.com",
    "https://nesdia.com",
    "https://cerebral.chat",
    "https://varangian.ai",
    "https://puretensor.ai",
]


async def run_ssh(host: str, command: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a command via SSH (or locally for localhost).

    Returns (returncode, stdout, stderr).
    """
    if host == "localhost":
        args = ["bash", "-c", command]
    else:
        args = [
            "ssh",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=no",
            host,
            command,
        ]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return (-1, "", f"Timed out after {timeout}s")

    return (
        proc.returncode,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


async def check_nodes() -> str:
    """Ping all nodes in parallel and return formatted status."""

    async def _check_one(name: str, host: str) -> str:
        rc, stdout, stderr = await run_ssh(host, "uptime", timeout=10)
        if rc == 0:
            return f"{name:14s} UP  {stdout}"
        else:
            return f"{name:14s} UNREACHABLE  {stderr[:60]}"

    tasks = [_check_one(name, host) for name, host in NODES.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    lines = []
    for r in results:
        if isinstance(r, Exception):
            lines.append(f"{'???':14s} ERROR  {r}")
        else:
            lines.append(r)
    return "\n".join(lines)


async def check_sites() -> str:
    """Check key websites via HTTP HEAD and return status + response time."""
    import time

    session = await get_session()
    lines = []

    for url in MONITORED_SITES:
        try:
            t0 = time.monotonic()
            async with session.head(
                url,
                allow_redirects=True,
                timeout=__import__("aiohttp").ClientTimeout(total=10),
            ) as resp:
                elapsed = (time.monotonic() - t0) * 1000
                lines.append(f"{url:30s}  {resp.status}  {elapsed:.0f}ms")
        except Exception as e:
            lines.append(f"{url:30s}  DOWN  {str(e)[:40]}")

    return "\n".join(lines)


async def restart_service(node: str, service: str) -> str:
    """Restart a whitelisted service on a known node."""
    if node not in NODES:
        raise ValueError(f"Unknown node: {node}")
    if service not in ALLOWED_SERVICES:
        raise ValueError(f"Service not in whitelist: {service}")

    host = NODES[node]
    # mon1 services under user mon1 need special handling for openclaw
    if service == "openclaw-gateway" and node == "mon1":
        cmd = (
            'su - mon1 -c "XDG_RUNTIME_DIR=/run/user/1000 '
            'systemctl --user restart openclaw-gateway"'
        )
    elif host == "localhost":
        cmd = f"sudo systemctl restart {service}"
    else:
        # mon nodes use root, so no sudo needed
        cmd = f"systemctl restart {service}"

    rc, stdout, stderr = await run_ssh(host, cmd)
    if rc == 0:
        return f"Restarted {service} on {node} successfully."
    else:
        return f"Failed to restart {service} on {node}:\n{stderr or stdout}"


async def get_logs(node: str, service: str, lines: int = 50) -> str:
    """Fetch recent journal logs for a service."""
    if node not in NODES:
        raise ValueError(f"Unknown node: {node}")
    if service not in ALLOWED_SERVICES:
        raise ValueError(f"Service not in whitelist: {service}")

    lines = min(lines, 200)
    host = NODES[node]
    cmd = f"journalctl -u {service} -n {lines} --no-pager"

    rc, stdout, stderr = await run_ssh(host, cmd, timeout=15)
    if rc != 0:
        return f"Error fetching logs: {stderr or stdout}"

    # Truncate to 3500 chars for Telegram
    if len(stdout) > 3500:
        stdout = stdout[:3497] + "..."
    return stdout


async def get_disk(node: str | None = None) -> str:
    """Get disk usage for a node (or tensor-core if not specified)."""
    host = NODES.get(node, "localhost") if node else "localhost"
    if node and node not in NODES:
        raise ValueError(f"Unknown node: {node}")

    cmd = "df -h --output=target,size,used,avail,pcent -x tmpfs -x devtmpfs"
    rc, stdout, stderr = await run_ssh(host, cmd)
    if rc != 0:
        return f"Error: {stderr or stdout}"

    label = node or "tensor-core"
    return f"Disk usage ({label}):\n{stdout}"


async def get_top(node: str | None = None) -> str:
    """Get system overview: uptime, memory, root disk, GPU."""
    host = NODES.get(node, "localhost") if node else "localhost"
    if node and node not in NODES:
        raise ValueError(f"Unknown node: {node}")

    cmd = (
        'uptime && echo "---" && free -h && echo "---" && df -h / && echo "---" && '
        'nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total '
        '--format=csv,noheader 2>/dev/null || echo "No GPU"'
    )
    rc, stdout, stderr = await run_ssh(host, cmd)
    if rc != 0:
        return f"Error: {stderr or stdout}"

    label = node or "tensor-core"
    return f"System overview ({label}):\n{stdout}"


async def trigger_deploy(site: str) -> str:
    """Trigger the deploy webhook for a site/repo."""
    import aiohttp

    session = await get_session()
    payload = {"repository": {"name": site}}

    try:
        async with session.post(
            DEPLOY_WEBHOOK_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            body = await resp.text()
            if resp.status == 200:
                return f"Deploy triggered for {site}.\n{body[:500]}"
            else:
                return f"Webhook returned {resp.status}: {body[:500]}"
    except Exception as e:
        return f"Deploy request failed: {e}"
