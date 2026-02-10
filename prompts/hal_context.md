# NEXUS Quick Reference

## User
REDACTED_NAME — runs PureTensor AI infrastructure from London.

## Infrastructure
- **tensor-core**: Main workstation, Ubuntu, 128GB RAM, RTX 4090 + A6000. Runs Ollama, Claude Code.
- **Proxmox cluster**: arx1-4 (4 nodes), fox-n0/n1. HA VMs.
- **mon1** (MON1_LAN_IP): Gitea, Uptime Kuma, Nextcloud, NEXUS bot, WhatsApp translator
- **mon2** (MON2_LAN_IP): Grafana, Prometheus, Loki, Alertmanager
- **mon3** (198.51.100.10): Raspberry Pi 5, node exporter
- **e2-micro** (GCP): 12 static sites, nginx
- **gcp-medium** (GCP): bretalon.com, nesdia.com, cerebral.chat (WordPress)

## Common Tasks
- Deploy static site: push to Gitea → webhook auto-deploys
- Check node health: query Prometheus via mon2
- Restart service: `ssh <node> systemctl restart <service>`
- Check logs: `ssh <node> journalctl -u <service> -n 50`

## User Preferences
- Direct, no fluff. One-liner if it answers the question.
- Prefers Sonnet for speed, Opus for complex tasks.
- Working hours: London timezone (UTC/BST).
