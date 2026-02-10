"""Infrastructure status client using Prometheus API."""

import os
from dispatcher.apis import get_session, ttl_cache, DispatchError

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://100.80.213.1:9090")

# Display-friendly names for Prometheus instance labels
NODE_NAMES = {
    "192.168.4.158:9100": "tensor-core",
    "192.168.4.160:9100": "arx1",
    "192.168.4.161:9100": "arx2",
    "192.168.4.162:9100": "arx3",
    "192.168.4.163:9100": "arx4",
    "192.168.4.170:9100": "fox-n0",
    "192.168.4.171:9100": "fox-n1",
    "192.168.4.186:9100": "mon1",
    "192.168.4.180:9100": "mon2",
    "192.168.4.185:9100": "mon3",
}

# Preferred display order
NODE_ORDER = [
    "tensor-core", "arx1", "arx2", "arx3", "arx4",
    "fox-n0", "fox-n1", "mon1", "mon2", "mon3",
]


@ttl_cache(seconds=15)
async def fetch_status() -> dict:
    """Query Prometheus for all 'up' metrics.

    Returns dict suitable for cards.render_status().
    """
    url = f"{PROMETHEUS_URL}/api/v1/query"
    params = {"query": "up"}

    session = await get_session()
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise DispatchError(f"Prometheus returned {resp.status}")
            data = await resp.json()
    except DispatchError:
        raise
    except Exception as e:
        raise DispatchError(f"Prometheus query failed: {e}")

    try:
        results = data["data"]["result"]
        # Deduplicate by node — keep node_exporter (port 9100) entries
        seen = {}
        for item in results:
            instance = item["metric"].get("instance", "")
            job = item["metric"].get("job", "")
            value = item["value"][1]  # "1" = up, "0" = down

            name = NODE_NAMES.get(instance)
            if name is None:
                # Skip non-node-exporter targets (ceph, etc.)
                if ":9100" in instance:
                    # Unknown node — use IP
                    name = instance.split(":")[0]
                else:
                    continue

            # First node_exporter entry wins
            if name not in seen:
                seen[name] = {
                    "name": name,
                    "status": "up" if value == "1" else "down",
                    "job": job,
                }

        # Sort by preferred order
        targets = []
        for node in NODE_ORDER:
            if node in seen:
                targets.append(seen.pop(node))
        # Append any remaining unknown nodes
        for t in sorted(seen.values(), key=lambda x: x["name"]):
            targets.append(t)

        return {"targets": targets}
    except (KeyError, IndexError) as e:
        raise DispatchError(f"Unexpected Prometheus response: {e}")
