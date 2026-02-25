#!/usr/bin/env python3
"""Cyber Threat Feed Observer — auto-publishes threat briefings to cyber.puretensor.ai + cyber.varangian.ai.

Runs every 6 hours. Collects data from:
  - NVD (recent critical/high CVEs)
  - CISA KEV (newly added known exploited vulnerabilities)
  - abuse.ch (URLhaus, ThreatFox, MalwareBazaar, Feodo Tracker)
  - Cyber RSS feeds (CISA alerts, Krebs, Bleeping Computer, Hacker News, etc.)

Sends raw intelligence to Ollama for analysis, generates styled HTML briefing,
deploys to GCP e2-micro, and sends a Telegram summary.

Schedule: 0 * * * *  (every hour)
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from observers.base import ALERT_BOT_TOKEN, Observer, ObserverContext, ObserverResult

log = logging.getLogger("nexus")

# GCP e2-micro for static site deployment
GCP_HOST = "puretensorai@GCP_TAILSCALE_IP"
WEBROOT = "/var/www/cyber.puretensor.ai"
WEBROOT_VARANGIAN = "/var/www/cyber.varangian.ai"

# Ollama config
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-235b-a22b-q4km")

# NVD API
NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"

# CISA KEV
CISA_KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"

# abuse.ch endpoints
URLHAUS_API = "https://urlhaus-api.abuse.ch/v1"
THREATFOX_API = "https://threatfox-api.abuse.ch/api/v1"
MALWARE_API = "https://mb-api.abuse.ch/api/v1"
FEODO_API = "https://feodotracker.abuse.ch/downloads/ipblocklist_recommended.json"

# Cyber RSS feeds
CYBER_RSS_FEEDS = {
    "CISA Alerts": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
    "Krebs on Security": "https://krebsonsecurity.com/feed/",
    "Bleeping Computer": "https://www.bleepingcomputer.com/feed/",
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "Dark Reading": "https://www.darkreading.com/rss.xml",
    "Schneier on Security": "https://www.schneier.com/feed/atom/",
}


class CyberThreatFeedObserver(Observer):
    """Collects cyber threat intelligence and publishes briefings to cyber.puretensor.ai + cyber.varangian.ai."""

    name = "cyber_threat_feed"
    schedule = "0 * * * *"  # every hour

    STATE_FILE = Path(
        os.environ.get("OBSERVER_STATE_DIR", str(Path(__file__).parent / ".state"))
    ) / "cyber_threat_feed.json"

    # -------------------------------------------------------------------
    # State management (track what we've already published)
    # -------------------------------------------------------------------

    def _load_state(self) -> dict:
        if self.STATE_FILE.exists():
            try:
                return json.loads(self.STATE_FILE.read_text())
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    def _save_state(self, state: dict) -> None:
        self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.STATE_FILE.write_text(json.dumps(state, indent=2))

    # -------------------------------------------------------------------
    # Delta computation (what's new since last briefing)
    # -------------------------------------------------------------------

    def compute_delta(self, intel: dict, state: dict) -> dict:
        """Compare current intelligence against previous run's data.
        Returns dict with new items not seen in the previous briefing."""
        prev = state.get("previous_intel_ids", {})

        # Extract current IDs
        current_cve_ids = {c["cve_id"] for c in intel.get("nvd_critical", []) + intel.get("nvd_high", [])}
        current_kev_ids = {k["cve_id"] for k in intel.get("cisa_kev", [])}
        current_malware = {m.get("signature", m.get("filename", "")) for m in intel.get("malware_bazaar", [])} - {""}
        current_c2_ips = {f["ip"] for f in intel.get("feodo_tracker", [])}
        current_rss_titles = {a["title"][:60] for a in intel.get("rss_articles", [])}

        # Previous IDs
        prev_cves = set(prev.get("cve_ids", []))
        prev_kevs = set(prev.get("kev_ids", []))
        prev_malware = set(prev.get("malware", []))
        prev_c2s = set(prev.get("c2_ips", []))
        prev_rss = set(prev.get("rss_titles", []))

        delta = {
            "new_cves": sorted(current_cve_ids - prev_cves),
            "new_kevs": sorted(current_kev_ids - prev_kevs),
            "new_malware": sorted(current_malware - prev_malware),
            "new_c2s": sorted(current_c2_ips - prev_c2s),
            "new_articles": sorted(current_rss_titles - prev_rss),
            "is_first_run": not prev,
        }

        # Store current IDs for next run's comparison
        state["previous_intel_ids"] = {
            "cve_ids": sorted(current_cve_ids),
            "kev_ids": sorted(current_kev_ids),
            "malware": sorted(current_malware),
            "c2_ips": sorted(current_c2_ips),
            "rss_titles": sorted(current_rss_titles),
        }

        return delta

    # -------------------------------------------------------------------
    # Data collection: NVD
    # -------------------------------------------------------------------

    def fetch_nvd_recent(self, hours: int = 12, severity: str = "HIGH") -> list[dict]:
        """Fetch recent CVEs from NVD. Returns list of simplified CVE dicts."""
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)

        params = {
            "pubStartDate": start.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "pubEndDate": now.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "resultsPerPage": 20,
        }
        if severity:
            params["cvssV3Severity"] = severity

        try:
            qs = urllib.parse.urlencode(params)
            req = urllib.request.Request(
                f"{NVD_API}?{qs}",
                headers={"User-Agent": "PureTensor-CyberFeed/1.0"},
            )
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
        except Exception as e:
            log.warning("NVD fetch failed: %s", e)
            return []

        results = []
        for vuln in data.get("vulnerabilities", []):
            cve = vuln.get("cve", {})
            cve_id = cve.get("id", "?")
            published = cve.get("published", "")[:10]

            # English description
            desc = ""
            for d in cve.get("descriptions", []):
                if d.get("lang") == "en":
                    desc = d.get("value", "")
                    break

            # CVSS score
            metrics = cve.get("metrics", {})
            score, sev = 0, "UNKNOWN"
            for ver in ["cvssMetricV31", "cvssMetricV30"]:
                m = metrics.get(ver, [])
                if m:
                    cvss = m[0].get("cvssData", {})
                    score = cvss.get("baseScore", 0)
                    sev = cvss.get("baseSeverity", "UNKNOWN")
                    break

            results.append({
                "cve_id": cve_id,
                "score": score,
                "severity": sev,
                "published": published,
                "description": desc[:300],
            })

        return results

    def fetch_nvd_critical(self) -> list[dict]:
        """Fetch critical CVEs from last 24 hours."""
        return self.fetch_nvd_recent(hours=24, severity="CRITICAL")

    def fetch_nvd_high(self) -> list[dict]:
        """Fetch high-severity CVEs from last 12 hours."""
        return self.fetch_nvd_recent(hours=12, severity="HIGH")

    # -------------------------------------------------------------------
    # Data collection: CISA KEV
    # -------------------------------------------------------------------

    def fetch_cisa_kev(self, days: int = 7) -> list[dict]:
        """Fetch recently added CISA Known Exploited Vulnerabilities."""
        try:
            req = urllib.request.Request(
                CISA_KEV_URL,
                headers={"User-Agent": "PureTensor-CyberFeed/1.0"},
            )
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
        except Exception as e:
            log.warning("CISA KEV fetch failed: %s", e)
            return []

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        vulns = data.get("vulnerabilities", [])
        recent = [v for v in vulns if v.get("dateAdded", "") >= cutoff]
        recent.sort(key=lambda v: v.get("dateAdded", ""), reverse=True)

        return [
            {
                "cve_id": v.get("cveID", "?"),
                "vendor": v.get("vendorProject", "?"),
                "product": v.get("product", "?"),
                "name": v.get("vulnerabilityName", ""),
                "description": v.get("shortDescription", "")[:300],
                "date_added": v.get("dateAdded", ""),
                "due_date": v.get("dueDate", ""),
                "action": v.get("requiredAction", ""),
            }
            for v in recent[:15]
        ]

    # -------------------------------------------------------------------
    # Data collection: abuse.ch
    # -------------------------------------------------------------------

    def _curl_post(self, url: str, data: dict = None, json_data: dict = None) -> dict:
        """HTTP POST via curl (abuse.ch blocks Python TLS fingerprints)."""
        args = ["curl", "-sL", "--max-time", "30", "-X", "POST"]
        if json_data:
            args += ["-H", "Content-Type: application/json", "-d", json.dumps(json_data)]
        elif data:
            for k, v in data.items():
                args += ["-d", f"{k}={v}"]
        args.append(url)

        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=35)
            return json.loads(result.stdout)
        except Exception as e:
            log.warning("curl POST to %s failed: %s", url, e)
            return {}

    def _curl_get(self, url: str) -> dict:
        """HTTP GET via curl."""
        try:
            result = subprocess.run(
                ["curl", "-sL", "--max-time", "30", url],
                capture_output=True, text=True, timeout=35,
            )
            return json.loads(result.stdout)
        except Exception as e:
            log.warning("curl GET %s failed: %s", url, e)
            return {}

    def fetch_urlhaus_recent(self, n: int = 10) -> list[dict]:
        """Recent malicious URLs from URLhaus."""
        data = self._curl_post(f"{URLHAUS_API}/urls/recent/", data={"limit": str(n)})
        urls = data.get("urls", [])
        return [
            {
                "url": u.get("url", "?")[:80],
                "host": u.get("host", "?"),
                "threat": u.get("threat", "?"),
                "tags": ", ".join(u.get("tags", []) or []),
                "status": u.get("url_status", "?"),
                "added": u.get("dateadded", ""),
            }
            for u in urls[:n]
        ]

    def fetch_threatfox_recent(self, n: int = 10) -> list[dict]:
        """Recent IOCs from ThreatFox."""
        data = self._curl_post(THREATFOX_API, json_data={"query": "get_iocs", "days": 1})
        iocs = data.get("data", [])
        if not isinstance(iocs, list):
            return []
        return [
            {
                "ioc": ioc.get("ioc", "?")[:60],
                "type": ioc.get("ioc_type", "?"),
                "threat_type": ioc.get("threat_type", "?"),
                "malware": ioc.get("malware_printable", "?"),
                "confidence": ioc.get("confidence_level", "?"),
                "tags": ", ".join(ioc.get("tags", []) or []),
            }
            for ioc in iocs[:n]
        ]

    def fetch_malware_recent(self, n: int = 10) -> list[dict]:
        """Recent malware samples from MalwareBazaar."""
        data = self._curl_post(f"{MALWARE_API}/", data={"query": "get_recent", "selector": str(n)})
        samples = data.get("data", [])
        if not isinstance(samples, list):
            return []
        return [
            {
                "filename": s.get("file_name", "?"),
                "file_type": s.get("file_type", "?"),
                "signature": s.get("signature", ""),
                "tags": ", ".join(s.get("tags", []) or []),
                "first_seen": s.get("first_seen", ""),
            }
            for s in samples[:n]
        ]

    def fetch_feodo(self, n: int = 15) -> list[dict]:
        """Active botnet C2 IPs from Feodo Tracker."""
        data = self._curl_get(FEODO_API)
        entries = data if isinstance(data, list) else data.get("data", [])
        if not isinstance(entries, list):
            return []
        return [
            {
                "ip": e.get("ip_address", e.get("dst_ip", "?")),
                "port": e.get("dst_port", e.get("port", "?")),
                "malware": e.get("malware", "?"),
                "status": e.get("status", "?"),
                "first_seen": str(e.get("first_seen", e.get("date_added", "")))[:10],
            }
            for e in entries[:n]
        ]

    # -------------------------------------------------------------------
    # Data collection: RSS feeds
    # -------------------------------------------------------------------

    def fetch_rss(self, name: str, url: str) -> list[dict]:
        """Fetch and parse an RSS/Atom feed. Falls back to curl if urllib fails."""
        xml_bytes = None
        # Try urllib first
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PureTensor/1.0)"})
            resp = urllib.request.urlopen(req, timeout=15)
            xml_bytes = resp.read()
        except Exception:
            pass

        # Fallback to curl for sites that block Python
        if xml_bytes is None:
            try:
                result = subprocess.run(
                    ["curl", "-sL", "--max-time", "15",
                     "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                     url],
                    capture_output=True, timeout=20,
                )
                if result.returncode == 0 and result.stdout:
                    xml_bytes = result.stdout
            except Exception:
                pass

        if xml_bytes is None:
            log.warning("[%s] RSS fetch failed (urllib + curl)", name)
            return []

        try:
            root = ET.fromstring(xml_bytes)
        except Exception as e:
            log.warning("[%s] RSS parse failed: %s", name, e)
            return []

        items = []
        # RSS
        for item in root.findall(".//item"):
            title = item.find("title")
            desc = item.find("description")
            link = item.find("link")
            pub_date = item.find("pubDate")
            if title is not None and title.text:
                summary = ""
                if desc is not None and desc.text:
                    summary = re.sub(r"<[^>]+>", "", desc.text)[:300]
                items.append({
                    "title": title.text.strip(),
                    "summary": summary.strip(),
                    "source": name,
                    "link": link.text.strip() if link is not None and link.text else "",
                    "date": pub_date.text.strip() if pub_date is not None and pub_date.text else "",
                })

        # Atom fallback
        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall(".//atom:entry", ns):
                title = entry.find("atom:title", ns)
                summary = entry.find("atom:summary", ns)
                link = entry.find("atom:link", ns)
                if title is not None and title.text:
                    s = ""
                    if summary is not None and summary.text:
                        s = re.sub(r"<[^>]+>", "", summary.text)[:300]
                    href = link.get("href", "") if link is not None else ""
                    items.append({
                        "title": title.text.strip(),
                        "summary": s.strip(),
                        "source": name,
                        "link": href,
                        "date": "",
                    })

        return items[:10]

    def fetch_all_cyber_rss(self) -> list[dict]:
        """Fetch all cyber RSS feeds."""
        all_items = []
        for name, url in CYBER_RSS_FEEDS.items():
            items = self.fetch_rss(name, url)
            all_items.extend(items)
            log.info("  [%s] %d items", name, len(items))

        # Deduplicate by normalized title
        seen = set()
        unique = []
        for item in all_items:
            norm = re.sub(r"[^a-z0-9 ]", "", item["title"].lower())[:60]
            if norm not in seen:
                seen.add(norm)
                unique.append(item)

        return unique

    # -------------------------------------------------------------------
    # Ollama LLM analysis
    # -------------------------------------------------------------------

    # PureTensor: technical, developer-facing, vulnerability-centric
    SYSTEM_PROMPT = (
        "You are a cybersecurity threat analyst. Produce clear, concise, "
        "technically accurate threat briefings. Output only the requested HTML "
        "content, no preamble or commentary. Do not use markdown code fences "
        "around the output."
    )

    # Varangian: risk-focused, executive-facing, threat-actor-centric
    VARANGIAN_SYSTEM_PROMPT = (
        "You are a senior security and risk intelligence analyst at Varangian Group. "
        "You produce executive-level threat briefings focused on operational risk, "
        "threat actor attribution, nation-state campaigns, and business impact. "
        "Emphasise who is attacking, why, what sectors are targeted, and what "
        "organisational leaders need to know. Use British English. "
        "Output only the requested HTML content, no preamble or commentary. "
        "Do not use markdown code fences around the output."
    )

    def call_llm_for_briefing(self, prompt: str, timeout: int = 300,
                               system_prompt: str | None = None) -> tuple[str, str]:
        """Call LLM via shared module — Gemini-first to avoid burning GPU on
        routine threat summaries.  Falls back to Ollama if Gemini is unavailable.

        Returns:
            (content, backend_name) — the generated HTML and which backend was used.
        """
        from observers.llm import call_llm
        return call_llm(
            system_prompt=system_prompt or self.SYSTEM_PROMPT,
            user_prompt=prompt,
            timeout=timeout,
            num_predict=8192,
            temperature=0.4,
            preferred_backend="gemini",
            override_gemini_model="gemini-2.5-flash-lite",
        )

    # -------------------------------------------------------------------
    # Build the intelligence bundle for LLM analysis
    # -------------------------------------------------------------------

    def collect_all_intelligence(self) -> dict:
        """Collect intelligence from all sources. Returns structured dict."""
        log.info("Collecting NVD critical CVEs...")
        nvd_critical = self.fetch_nvd_critical()
        log.info("  %d critical CVEs", len(nvd_critical))

        # Small delay to avoid NVD rate limiting
        time.sleep(2)

        log.info("Collecting NVD high-severity CVEs...")
        nvd_high = self.fetch_nvd_high()
        log.info("  %d high CVEs", len(nvd_high))

        log.info("Collecting CISA KEV...")
        kev = self.fetch_cisa_kev()
        log.info("  %d KEV entries", len(kev))

        log.info("Collecting abuse.ch URLhaus...")
        urlhaus = self.fetch_urlhaus_recent()
        log.info("  %d URLs", len(urlhaus))

        log.info("Collecting abuse.ch ThreatFox...")
        threatfox = self.fetch_threatfox_recent()
        log.info("  %d IOCs", len(threatfox))

        log.info("Collecting abuse.ch MalwareBazaar...")
        malware = self.fetch_malware_recent()
        log.info("  %d samples", len(malware))

        log.info("Collecting Feodo Tracker...")
        feodo = self.fetch_feodo()
        log.info("  %d C2 IPs", len(feodo))

        log.info("Collecting cyber RSS feeds...")
        rss = self.fetch_all_cyber_rss()
        log.info("  %d articles", len(rss))

        return {
            "nvd_critical": nvd_critical,
            "nvd_high": nvd_high,
            "cisa_kev": kev,
            "urlhaus": urlhaus,
            "threatfox": threatfox,
            "malware_bazaar": malware,
            "feodo_tracker": feodo,
            "rss_articles": rss,
        }

    # -------------------------------------------------------------------
    # LLM prompt construction
    # -------------------------------------------------------------------

    def build_analysis_prompt(self, intel: dict, timestamp: str, delta: dict = None) -> str:
        """Build the LLM prompt from collected intelligence."""
        sections = []

        # NVD Critical
        if intel["nvd_critical"]:
            lines = []
            for c in intel["nvd_critical"]:
                lines.append(f"  {c['cve_id']} (CVSS {c['score']} {c['severity']}) — {c['description']}")
            sections.append("CRITICAL CVEs (last 24h):\n" + "\n".join(lines))

        # NVD High
        if intel["nvd_high"]:
            lines = []
            for c in intel["nvd_high"]:
                lines.append(f"  {c['cve_id']} (CVSS {c['score']} {c['severity']}) — {c['description']}")
            sections.append("HIGH-SEVERITY CVEs (last 12h):\n" + "\n".join(lines))

        # CISA KEV
        if intel["cisa_kev"]:
            lines = []
            for k in intel["cisa_kev"]:
                lines.append(f"  {k['cve_id']} — {k['vendor']}/{k['product']} — {k['name']} (Added: {k['date_added']}, Remediation due: {k['due_date']})")
            sections.append("CISA KEV (recently added):\n" + "\n".join(lines))

        # URLhaus
        if intel["urlhaus"]:
            lines = []
            for u in intel["urlhaus"]:
                lines.append(f"  [{u['status']}] {u['host']} — Threat: {u['threat']} — Tags: {u['tags']}")
            sections.append("URLhaus — Active Malicious URLs:\n" + "\n".join(lines))

        # ThreatFox
        if intel["threatfox"]:
            lines = []
            for t in intel["threatfox"]:
                lines.append(f"  [{t['type']}] {t['ioc']} — {t['malware']} ({t['threat_type']}) — Confidence: {t['confidence']}%")
            sections.append("ThreatFox — Recent IOCs:\n" + "\n".join(lines))

        # MalwareBazaar
        if intel["malware_bazaar"]:
            lines = []
            for m in intel["malware_bazaar"]:
                sig = f" — Signature: {m['signature']}" if m["signature"] else ""
                lines.append(f"  {m['filename']} ({m['file_type']}){sig} — Tags: {m['tags']}")
            sections.append("MalwareBazaar — Recent Malware Samples:\n" + "\n".join(lines))

        # Feodo
        if intel["feodo_tracker"]:
            lines = []
            for f in intel["feodo_tracker"]:
                lines.append(f"  {f['ip']}:{f['port']} — {f['malware']} — {f['status']} (first seen: {f['first_seen']})")
            sections.append("Feodo Tracker — Active C2 Infrastructure:\n" + "\n".join(lines))

        # RSS articles
        if intel["rss_articles"]:
            lines = []
            for a in intel["rss_articles"]:
                lines.append(f"  [{a['source']}] {a['title']}")
                if a["summary"]:
                    lines.append(f"    {a['summary'][:200]}")
            sections.append("Cyber Security News:\n" + "\n".join(lines))

        intel_text = "\n\n".join(sections) if sections else "(No intelligence data collected)"

        # Build delta section for the prompt
        delta_text = ""
        if delta and not delta.get("is_first_run"):
            delta_parts = []
            if delta["new_cves"]:
                delta_parts.append(f"NEW CVEs (not in previous briefing): {', '.join(delta['new_cves'])}")
            if delta["new_kevs"]:
                delta_parts.append(f"NEW KEV additions: {', '.join(delta['new_kevs'])}")
            if delta["new_malware"]:
                delta_parts.append(f"NEW malware families: {', '.join(delta['new_malware'][:10])}")
            if delta["new_c2s"]:
                delta_parts.append(f"NEW C2 infrastructure: {', '.join(delta['new_c2s'][:10])}")
            if delta["new_articles"]:
                delta_parts.append(f"NEW news stories: {len(delta['new_articles'])} articles not in previous briefing")
            if not delta_parts:
                delta_parts.append("No significant changes from previous briefing.")
            delta_text = "\n\nDELTA (changes since last briefing):\n" + "\n".join(delta_parts)

        return f"""Timestamp: {timestamp}

RAW THREAT INTELLIGENCE DATA:

{intel_text}{delta_text}

---

Generate a CYBER THREAT BRIEFING as an HTML fragment (no <html>, <head>, or <body> tags — just the inner content that will go inside a styled page wrapper). The briefing content section should follow this structure:

1. DELTA — What changed since the last briefing. This section goes FIRST, immediately after the threat level badge. Use heading <h2 class="section-header">DELTA &mdash; CHANGES SINCE LAST BRIEFING</h2>. Summarise in 2-4 bullet points: new CVEs appeared, new KEV entries, new malware families, new C2 infrastructure, notable new stories. If this is the first briefing or nothing changed, say so briefly. Use a <ul> list with concise items. Mark genuinely new critical items with <strong>.

2. THREAT LEVEL ASSESSMENT — A one-paragraph executive summary of the current threat landscape. Assign an overall threat level: CRITICAL, HIGH, ELEVATED, GUARDED, or LOW. Base this on the severity and volume of active threats.

3. CRITICAL VULNERABILITIES — Highlight the most dangerous CVEs. For each: CVE ID, affected product, CVSS score, why it matters, and whether active exploitation is confirmed (check CISA KEV). Use an HTML table with columns: CVE, Product, CVSS, Status, Impact.

4. ACTIVE EXPLOITS & KEV — Any CISA Known Exploited Vulnerabilities added recently. Include remediation deadlines. Use an HTML table.

5. MALWARE & THREAT ACTORS — Summarize notable malware campaigns, IOCs, and C2 infrastructure from abuse.ch data. Group by malware family where possible. Mention specific malware names, their types, and the infrastructure they use.

6. CYBER NEWS DIGEST — Summarize the top 5-8 most significant stories from the RSS feeds. One paragraph per story with the source attribution in brackets.

FORMATTING RULES:
- Use semantic HTML: <h2> for section headers, <p> for paragraphs, <table> for data, <strong> for emphasis
- Tables should have class="threat-table" with <thead> and <tbody>
- Section headers should be <h2 class="section-header">
- The threat level badge should be: <div class="threat-level threat-level-[level]"> where [level] is critical/high/elevated/guarded/low
- Wrap the threat level text in <span class="level-text">[LEVEL]</span>
- The DELTA section should come immediately after the threat level badge, before everything else.
- Keep it factual, technical, concise. No filler, no hedging, no marketing language.
- Do NOT add any CSS styles inline — the page template handles all styling.
- Total output should be 800-1500 words of content.
- Output only the HTML fragment, no markdown fences, no preamble."""

    # -------------------------------------------------------------------
    # HTML page template
    # -------------------------------------------------------------------

    def build_full_page(self, briefing_html: str, timestamp: str, intel_stats: dict) -> str:
        """Wrap the LLM-generated briefing in a full styled page."""
        now_display = datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cyber Threat Briefing | PureTensor</title>
<meta name="description" content="Automated cyber threat intelligence briefing. CVEs, active exploits, malware campaigns, and threat actor activity.">
<meta name="robots" content="noindex, nofollow">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><polygon points='16,2 28,28 16,22 4,28' fill='%2300E5FF'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root {{
    --bg-primary: #09090b;
    --bg-surface: #0f0f12;
    --bg-elevated: #16161a;
    --bg-card: #1A1F2B;
    --gold: #c9b97a;
    --gold-bright: #ddd0a0;
    --gold-dim: #8b7f52;
    --gold-border: rgba(201, 185, 122, 0.2);
    --cyan: #00E5FF;
    --cyan-dim: #0088A3;
    --cyan-border: rgba(0, 229, 255, 0.15);
    --cyan-glow: rgba(0, 229, 255, 0.06);
    --red: #FF3B3B;
    --red-dim: rgba(255, 59, 59, 0.15);
    --orange: #FF9500;
    --orange-dim: rgba(255, 149, 0, 0.15);
    --yellow: #FFD600;
    --yellow-dim: rgba(255, 214, 0, 0.15);
    --green: #34C759;
    --green-dim: rgba(52, 199, 89, 0.15);
    --blue: #5AC8FA;
    --blue-dim: rgba(90, 200, 250, 0.15);
    --text-primary: #e8e4dc;
    --text-secondary: #9a958b;
    --text-muted: #5a564e;
    --border: rgba(255, 255, 255, 0.06);
    --font-serif: 'Cormorant Garamond', Georgia, 'Times New Roman', serif;
    --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --font-mono: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;
}}

*, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ scroll-behavior: smooth; font-size: 16px; }}
body {{
    font-family: var(--font-sans);
    background: var(--bg-primary);
    color: var(--text-primary);
    line-height: 1.7;
    font-weight: 300;
    -webkit-font-smoothing: antialiased;
}}

/* Navigation */
nav {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 1000;
    padding: 1.25rem 2rem;
    background: rgba(9, 9, 11, 0.85);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
}}
.nav-inner {{
    max-width: 1140px;
    margin: 0 auto;
    display: flex; align-items: center; justify-content: space-between; gap: 3rem;
}}
.nav-brand {{
    display: flex; align-items: center; gap: 0.6rem;
    font-family: var(--font-serif);
    font-weight: 600; font-size: 1.4rem;
    color: var(--gold); letter-spacing: 0.15em;
    text-transform: uppercase; text-decoration: none;
}}
.nav-brand svg {{ width: 24px; height: 24px; flex-shrink: 0; opacity: 0.35; }}
.nav-brand span {{ color: var(--text-secondary); font-weight: 300; }}
.nav-links {{ display: flex; gap: 1.5rem; }}
.nav-links a {{
    font-family: var(--font-mono);
    color: var(--text-secondary); text-decoration: none;
    font-size: 0.72rem; font-weight: 400;
    text-transform: uppercase; letter-spacing: 0.1em;
    transition: color 0.2s;
}}
.nav-links a:hover {{ color: var(--gold); }}

/* Main */
main {{
    max-width: 960px; margin: 0 auto;
    padding: 6rem 2rem 3rem;
}}

/* Header */
.briefing-header {{
    margin-bottom: 2.5rem;
    padding-bottom: 1.5rem;
    border-bottom: 1px solid var(--border);
}}
.briefing-header h1 {{
    font-size: 1.8rem; font-weight: 600;
    letter-spacing: -0.02em;
    margin-bottom: 0.5rem;
}}
.briefing-header h1 span {{ color: var(--cyan); }}
.briefing-meta {{
    display: flex; gap: 2rem; flex-wrap: wrap;
    color: var(--text-secondary); font-size: 0.85rem;
    font-family: var(--font-mono);
}}
.briefing-meta .meta-item {{ display: flex; align-items: center; gap: 0.4rem; }}
.briefing-meta .label {{ color: var(--text-muted); }}

/* Stats bar */
.stats-bar {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 1px; margin-bottom: 2rem;
    background: var(--border); border-radius: 8px; overflow: hidden;
}}
.stat-item {{
    background: var(--bg-surface); padding: 1rem 1.2rem;
    text-align: center;
}}
.stat-value {{
    font-size: 1.5rem; font-weight: 600;
    font-family: var(--font-mono); color: var(--cyan);
}}
.stat-label {{
    font-size: 0.7rem; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 0.1em;
    margin-top: 0.25rem;
}}

/* Threat level badges */
.threat-level {{
    display: inline-block; padding: 0.5rem 1.5rem;
    border-radius: 4px; font-weight: 600;
    font-family: var(--font-mono); font-size: 0.9rem;
    letter-spacing: 0.1em; margin: 0.5rem 0 1rem;
}}
.threat-level-critical {{ background: var(--red-dim); color: var(--red); border: 1px solid rgba(255,59,59,0.3); }}
.threat-level-high {{ background: var(--orange-dim); color: var(--orange); border: 1px solid rgba(255,149,0,0.3); }}
.threat-level-elevated {{ background: var(--yellow-dim); color: var(--yellow); border: 1px solid rgba(255,214,0,0.3); }}
.threat-level-guarded {{ background: var(--blue-dim); color: var(--blue); border: 1px solid rgba(90,200,250,0.3); }}
.threat-level-low {{ background: var(--green-dim); color: var(--green); border: 1px solid rgba(52,199,89,0.3); }}

/* Section headers */
.section-header {{
    font-size: 1.1rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.1em;
    color: var(--cyan); margin: 2rem 0 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--cyan-border);
}}

/* Content */
.briefing-content p {{
    margin-bottom: 1rem; color: var(--text-primary);
    font-size: 0.95rem;
}}
.briefing-content strong {{ color: var(--text-primary); font-weight: 500; }}

/* Tables */
.threat-table {{
    width: 100%; border-collapse: collapse;
    margin: 1rem 0 1.5rem; font-size: 0.85rem;
}}
.threat-table thead th {{
    background: var(--bg-elevated);
    color: var(--cyan); font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.05em;
    font-size: 0.75rem; padding: 0.75rem 1rem;
    text-align: left; border-bottom: 1px solid var(--cyan-border);
}}
.threat-table tbody td {{
    padding: 0.65rem 1rem; border-bottom: 1px solid var(--border);
    color: var(--text-secondary); font-family: var(--font-mono);
    font-size: 0.8rem;
}}
.threat-table tbody tr:hover {{ background: var(--bg-surface); }}

/* Footer */
.briefing-footer {{
    margin-top: 3rem; padding-top: 1.5rem;
    border-top: 1px solid var(--border);
    color: var(--text-muted); font-size: 0.75rem;
    font-family: var(--font-mono);
}}
.briefing-footer a {{ color: var(--cyan-dim); text-decoration: none; }}
.briefing-footer a:hover {{ color: var(--cyan); }}

@media (max-width: 640px) {{
    main {{ padding: 5rem 1rem 2rem; }}
    .briefing-header h1 {{ font-size: 1.3rem; }}
    .briefing-meta {{ flex-direction: column; gap: 0.5rem; }}
    .stats-bar {{ grid-template-columns: repeat(2, 1fr); }}
    .threat-table {{ font-size: 0.75rem; }}
    .threat-table thead th, .threat-table tbody td {{ padding: 0.5rem 0.5rem; }}
}}
</style>
</head>
<body>

<nav>
    <div class="nav-inner">
    <a class="nav-brand" href="/"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><defs><linearGradient id="ag" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" style="stop-color:#00D4AA"/><stop offset="100%" style="stop-color:#0080FF"/></linearGradient><linearGradient id="df" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" style="stop-color:#1a252f"/><stop offset="100%" style="stop-color:#2C3E50"/></linearGradient><linearGradient id="mf" x1="0%" y1="100%" x2="100%" y2="0%"><stop offset="0%" style="stop-color:#34495E"/><stop offset="100%" style="stop-color:#4A6278"/></linearGradient></defs><g transform="translate(10,5)"><polygon points="40,0 80,20 80,65 40,85 0,65 0,20" fill="url(#df)"/><polygon points="40,85 80,65 80,20 40,40" fill="url(#mf)"/><polygon points="40,85 0,65 0,20 40,40" fill="#2C3E50"/><polygon points="40,0 80,20 40,40 0,20" fill="#3D566E"/><polygon points="40,0 58,10 40,20 22,10" fill="url(#ag)"/></g></svg>PURETENSOR <span>// CYBER</span></a>
    <div class="nav-links">
        <a href="/">Main Site</a>
        <a href="/briefings/">Archive</a>
    </div>
    </div>
</nav>

<main>
    <div class="briefing-header">
        <h1>Cyber Threat <span>Briefing</span></h1>
        <div class="briefing-meta">
            <div class="meta-item"><span class="label">PUBLISHED</span> {now_display}</div>
            <div class="meta-item"><span class="label">CLASSIFICATION</span> TLP:CLEAR</div>
            <div class="meta-item"><span class="label">AUTO-GENERATED</span> PureTensor Cyber Intelligence</div>
        </div>
    </div>

    <div class="stats-bar">
        <div class="stat-item">
            <div class="stat-value">{intel_stats.get('critical_cves', 0)}</div>
            <div class="stat-label">Critical CVEs</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{intel_stats.get('high_cves', 0)}</div>
            <div class="stat-label">High CVEs</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{intel_stats.get('kev_count', 0)}</div>
            <div class="stat-label">KEV Added</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{intel_stats.get('ioc_count', 0)}</div>
            <div class="stat-label">New IOCs</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{intel_stats.get('malware_count', 0)}</div>
            <div class="stat-label">Malware Samples</div>
        </div>
        <div class="stat-item">
            <div class="stat-value">{intel_stats.get('c2_count', 0)}</div>
            <div class="stat-label">Active C2s</div>
        </div>
    </div>

    <div class="briefing-content">
        {briefing_html}
    </div>

    <div class="briefing-footer">
        <p>Auto-generated by PureTensor Cyber Intelligence Pipeline.</p>
        <p>Sources: NVD, CISA KEV, abuse.ch (URLhaus, ThreatFox, MalwareBazaar, Feodo Tracker),
           CISA Advisories, Krebs on Security, Bleeping Computer, The Hacker News, Dark Reading, Schneier on Security.</p>
        <p>This briefing is generated automatically and may contain inaccuracies. Verify critical findings independently.</p>
        <p style="margin-top: 1rem;"><a href="https://puretensor.ai">PureTensor Inc</a></p>
    </div>
</main>

</body>
</html>"""

    # -------------------------------------------------------------------
    # Build briefings archive index
    # -------------------------------------------------------------------

    def build_archive_section(self, briefings: list[dict]) -> str:
        """Build an HTML snippet of previous briefings to append to the latest briefing page.
        Skips the first entry (that's the current briefing being displayed)."""
        previous = briefings[1:30]  # up to 29 previous briefings
        if not previous:
            return ""

        rows = ""
        for b in previous:
            rows += f"""<tr>
                <td>{b['date']}</td>
                <td><a href="/briefings/{b['filename']}">{b['title']}</a></td>
            </tr>\n"""

        return f"""
<div style="margin-top: 3rem; padding-top: 2rem; border-top: 1px solid var(--border);">
    <h2 class="section-header">Previous Briefings</h2>
    <table class="threat-table">
        <thead><tr><th>Date</th><th>Briefing</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>
</div>"""

    def build_latest_briefing_page(self, briefing_html: str, timestamp: str,
                                    intel_stats: dict, briefings: list[dict]) -> str:
        """Build the /briefings/index.html — the latest briefing with archive below it.
        This is the page people land on when clicking 'Threat Briefings'."""
        full_page = self.build_full_page(briefing_html, timestamp, intel_stats)
        archive_section = self.build_archive_section(briefings)

        if archive_section:
            # Insert archive section before the closing </div> of briefing-footer's parent (</main>)
            full_page = full_page.replace("</main>", f"{archive_section}\n</main>")

        return full_page

    # -------------------------------------------------------------------
    # Rebrand HTML for Varangian deployment
    # -------------------------------------------------------------------

    @staticmethod
    def _rebrand_for_varangian(html: str) -> str:
        """Transform PureTensor/cyan-branded HTML to Varangian/gold-branded HTML."""
        replacements = [
            # CSS colour variables: cyan → gold
            ("--cyan: #00E5FF;", "--gold: #c9b97a;"),
            ("--cyan-dim: #0088A3;", "--gold-dim: #8b7f52;"),
            ("--cyan-border: rgba(0, 229, 255, 0.15);", "--gold-border: rgba(201, 185, 122, 0.2);"),
            ("--cyan-glow: rgba(0, 229, 255, 0.06);", "--gold-glow: rgba(201, 185, 122, 0.06);"),
            # CSS variable references
            ("var(--cyan-dim)", "var(--gold-dim)"),
            ("var(--cyan-border)", "var(--gold-border)"),
            ("var(--cyan-glow)", "var(--gold-glow)"),
            ("var(--cyan)", "var(--gold)"),
            # Favicon
            ("%2300E5FF", "%23c9b97a"),
            # Nav brand
            ('PURETENSOR <span>// CYBER</span>', 'VARANGIAN <span>// CYBER</span>'),
            # Title
            ("Cyber Threat Briefing | PureTensor", "Cyber Threat Briefing | Varangian"),
            # Meta / footer text
            ("PureTensor Cyber Intelligence", "Varangian Cyber Intelligence"),
            ("Auto-generated by PureTensor Cyber Intelligence Pipeline.",
             "Auto-generated by Varangian Cyber Intelligence Pipeline."),
            ('<a href="https://puretensor.ai">PureTensor Inc</a>',
             '<a href="https://varangian.ai">Varangian Group</a>'),
            # Links to puretensor cyber → varangian cyber
            ("cyber.puretensor.ai", "cyber.varangian.ai"),
        ]
        for old, new in replacements:
            html = html.replace(old, new)

        # Strip PureTensor hex logo SVG from nav-brand (Varangian has no logo)
        html = re.sub(
            r'(<a[^>]*class="nav-brand"[^>]*>)\s*<svg[^>]*>.*?</svg>\s*',
            r'\1',
            html,
            flags=re.DOTALL,
        )
        # Remove .nav-brand svg CSS rule
        html = re.sub(r'\.nav-brand\s+svg\s*\{[^}]*\}\s*', '', html)

        return html

    # -------------------------------------------------------------------
    # Deploy to GCP
    # -------------------------------------------------------------------

    def _deploy_to_webroot(self, webroot: str, latest_page_html: str,
                           briefing_html: str, briefing_filename: str) -> bool:
        """Deploy briefing files to a single webroot on GCP e2-micro."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = os.path.join(tmpdir, "archive_index.html")
            briefing_path = os.path.join(tmpdir, briefing_filename)

            with open(archive_path, "w") as f:
                f.write(latest_page_html)
            with open(briefing_path, "w") as f:
                f.write(briefing_html)

            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", GCP_HOST,
                 f"sudo mkdir -p {webroot}/briefings"],
                check=True, timeout=15,
            )
            subprocess.run(
                ["scp", "-o", "ConnectTimeout=10",
                 archive_path, f"{GCP_HOST}:/tmp/_briefing_index.html"],
                check=True, timeout=30,
            )
            subprocess.run(
                ["scp", "-o", "ConnectTimeout=10",
                 briefing_path, f"{GCP_HOST}:/tmp/_briefing_{briefing_filename}"],
                check=True, timeout=30,
            )
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", GCP_HOST,
                 f"sudo mv /tmp/_briefing_index.html {webroot}/briefings/index.html && "
                 f"sudo mv /tmp/_briefing_{briefing_filename} {webroot}/briefings/{briefing_filename} && "
                 f"sudo chown -R www-data:www-data {webroot}/briefings/"],
                check=True, timeout=15,
            )
            log.info("Deployed to %s", webroot)
        return True

    def deploy_to_gcp(self, latest_page_html: str, briefing_html: str,
                       briefing_filename: str,
                       varangian_latest_html: str | None = None,
                       varangian_briefing_html: str | None = None) -> bool:
        """Deploy briefing files to GCP e2-micro — both PureTensor and Varangian sites.

        latest_page_html: goes to /briefings/index.html (latest briefing + previous list)
        briefing_html: goes to /briefings/<filename> (individual permalink)

        If varangian_latest_html / varangian_briefing_html are provided, Varangian
        gets independently generated content (different analytical lens).  Otherwise
        falls back to CSS-only rebranding of the PureTensor content.

        IMPORTANT: Only deploys to /briefings/ subdirectory.
        Never touches the root index.html — that's the landing page managed by gcp-sites repo.
        """
        ok = True
        # Deploy to cyber.puretensor.ai (original, cyan branding)
        try:
            self._deploy_to_webroot(WEBROOT, latest_page_html, briefing_html, briefing_filename)
        except Exception as e:
            log.error("PureTensor deployment failed: %s", e)
            ok = False

        # Deploy to cyber.varangian.ai (gold/Varangian branding)
        try:
            if varangian_latest_html and varangian_briefing_html:
                # Independent Varangian content — just rebrand the CSS/template
                var_page = self._rebrand_for_varangian(varangian_latest_html)
                var_briefing = self._rebrand_for_varangian(varangian_briefing_html)
            else:
                # Fallback: rebrand PureTensor content (legacy behaviour)
                var_page = self._rebrand_for_varangian(latest_page_html)
                var_briefing = self._rebrand_for_varangian(briefing_html)
            self._deploy_to_webroot(WEBROOT_VARANGIAN, var_page, var_briefing, briefing_filename)
        except Exception as e:
            log.error("Varangian deployment failed: %s", e)
            ok = False

        return ok

    # -------------------------------------------------------------------
    # Landing page feed
    # -------------------------------------------------------------------

    def _update_landing_feed(self, intel_stats: dict, briefing_filename: str, timestamp: str):
        """Update varangian.ai landing-page activity ticker feed (cyber section)."""
        feed_path = "/var/www/varangian.ai/html/api/feed.json"
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", GCP_HOST, f"cat {feed_path}"],
                capture_output=True, text=True, timeout=15,
            )
            feed = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {
                "updated": "", "intel": [], "cyber": [],
            }
        except Exception:
            feed = {"updated": "", "intel": [], "cyber": []}

        parts = []
        if intel_stats["critical_cves"]:
            parts.append(f"{intel_stats['critical_cves']} critical CVEs")
        if intel_stats["high_cves"]:
            parts.append(f"{intel_stats['high_cves']} high CVEs")
        if intel_stats["malware_count"]:
            parts.append(f"{intel_stats['malware_count']} malware samples")
        if intel_stats["c2_count"]:
            parts.append(f"{intel_stats['c2_count']} C2 servers")
        if intel_stats["ioc_count"]:
            parts.append(f"{intel_stats['ioc_count']} IOCs")
        title = ", ".join(parts) if parts else "Threat scan complete — no significant activity"

        now = datetime.now(timezone.utc).isoformat()
        level = "critical" if intel_stats["critical_cves"] else (
            "elevated" if intel_stats["high_cves"] or intel_stats["malware_count"] else "monitoring"
        )
        feed["cyber"].insert(0, {
            "title": title,
            "level": level,
            "url": f"https://cyber.varangian.ai/briefings/{briefing_filename}",
            "time": now,
        })
        feed["cyber"] = feed["cyber"][:12]
        feed["updated"] = now

        feed_json = json.dumps(feed, indent=2)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(feed_json)
            tmp = f.name
        try:
            subprocess.run(["scp", "-q", tmp, f"{GCP_HOST}:/tmp/_landing_feed.json"],
                           check=True, timeout=10)
            subprocess.run(["ssh", "-o", "ConnectTimeout=10", GCP_HOST,
                            f"sudo cp /tmp/_landing_feed.json {feed_path} && "
                            f"sudo chown www-data:www-data {feed_path} && "
                            f"rm /tmp/_landing_feed.json"],
                           check=True, timeout=10)
            log.info("cyber_threat_feed: landing feed updated (%d cyber items)", len(feed["cyber"]))
        except Exception as e:
            log.warning("cyber_threat_feed: landing feed update failed: %s", e)
        finally:
            os.unlink(tmp)

    # -------------------------------------------------------------------
    # Observer entry point
    # -------------------------------------------------------------------

    def run(self, ctx: ObserverContext) -> ObserverResult:
        """Execute the cyber threat feed pipeline."""
        timestamp = ctx.now.strftime("%Y-%m-%d %H:%M UTC")
        date_str = ctx.now.strftime("%Y-%m-%d")
        time_str = ctx.now.strftime("%H%M")
        briefing_filename = f"{date_str}_{time_str}.html"

        log.info("Cyber Threat Feed — starting collection at %s", timestamp)

        # 1. Collect intelligence
        intel = self.collect_all_intelligence()

        intel_stats = {
            "critical_cves": len(intel["nvd_critical"]),
            "high_cves": len(intel["nvd_high"]),
            "kev_count": len(intel["cisa_kev"]),
            "ioc_count": len(intel["threatfox"]) + len(intel["urlhaus"]),
            "malware_count": len(intel["malware_bazaar"]),
            "c2_count": len(intel["feodo_tracker"]),
        }

        total_items = sum(intel_stats.values()) + len(intel["rss_articles"])
        if total_items == 0:
            log.warning("No intelligence collected from any source")
            return ObserverResult(
                success=False,
                error="No intelligence data collected from any source",
            )

        log.info("Collection complete: %d total intelligence items", total_items)

        # 2. Compute delta (what's new since last briefing)
        state = self._load_state()
        delta = self.compute_delta(intel, state)
        if delta["is_first_run"]:
            log.info("First run — no previous data for delta comparison")
        else:
            new_count = len(delta["new_cves"]) + len(delta["new_kevs"]) + len(delta["new_malware"]) + len(delta["new_c2s"])
            log.info("Delta: %d new CVEs, %d new KEV, %d new malware, %d new C2, %d new articles",
                     len(delta["new_cves"]), len(delta["new_kevs"]),
                     len(delta["new_malware"]), len(delta["new_c2s"]),
                     len(delta["new_articles"]))

        # 3. Generate PureTensor briefing (tech/cyber focus)
        log.info("Generating PureTensor briefing via LLM...")
        prompt = self.build_analysis_prompt(intel, timestamp, delta=delta)
        try:
            briefing_html, backend_used = self.call_llm_for_briefing(prompt, timeout=300)
        except RuntimeError as e:
            msg = f"LLM generation failed: {e}"
            self.send_telegram(f"[cyber_threat_feed] ERROR: {msg}", token=ALERT_BOT_TOKEN)
            return ObserverResult(success=False, error=msg)

        if not briefing_html:
            msg = "LLM returned empty response"
            self.send_telegram(f"[cyber_threat_feed] ERROR: {msg}", token=ALERT_BOT_TOKEN)
            return ObserverResult(success=False, error=msg)

        briefing_html = re.sub(r"^```(?:html)?\s*", "", briefing_html)
        briefing_html = re.sub(r"\s*```$", "", briefing_html)
        log.info("PureTensor briefing generated via %s: %d chars", backend_used, len(briefing_html))

        # 3b. Generate Varangian briefing (security/risk focus, same data)
        var_briefing_html = None
        var_backend = None
        try:
            log.info("Generating Varangian briefing via LLM...")
            var_briefing_html, var_backend = self.call_llm_for_briefing(
                prompt, timeout=300, system_prompt=self.VARANGIAN_SYSTEM_PROMPT,
            )
            if var_briefing_html:
                var_briefing_html = re.sub(r"^```(?:html)?\s*", "", var_briefing_html)
                var_briefing_html = re.sub(r"\s*```$", "", var_briefing_html)
                log.info("Varangian briefing generated via %s: %d chars", var_backend, len(var_briefing_html))
            else:
                log.warning("Varangian LLM returned empty — will rebrand PureTensor content")
                var_briefing_html = None
        except Exception as e:
            log.warning("Varangian briefing generation failed (%s) — will rebrand PureTensor content", e)
            var_briefing_html = None

        # 4. Build individual briefing pages (permalinks in /briefings/)
        individual_briefing = self.build_full_page(briefing_html, timestamp, intel_stats)
        var_individual = None
        if var_briefing_html:
            var_individual = self.build_full_page(var_briefing_html, timestamp, intel_stats)

        # 5. Update archive state (state already loaded for delta computation above)
        archive = state.get("archive", [])
        archive.insert(0, {
            "date": timestamp,
            "filename": briefing_filename,
            "title": f"Cyber Threat Briefing — {timestamp}",
        })
        # Keep last 90 entries
        archive = archive[:90]
        state["archive"] = archive
        state["last_run"] = timestamp

        # 5b. Build /briefings/index.html — latest briefing + previous list
        latest_page = self.build_latest_briefing_page(
            briefing_html, timestamp, intel_stats, archive
        )
        var_latest = None
        if var_briefing_html:
            var_latest = self.build_latest_briefing_page(
                var_briefing_html, timestamp, intel_stats, archive
            )

        # 6. Deploy to GCP (Varangian gets independent content if available)
        log.info("Deploying to GCP...")
        deployed = self.deploy_to_gcp(
            latest_page, individual_briefing, briefing_filename,
            varangian_latest_html=var_latest,
            varangian_briefing_html=var_individual,
        )

        if not deployed:
            msg = "GCP deployment failed"
            self.send_telegram(f"[cyber_threat_feed] ERROR: {msg}", token=ALERT_BOT_TOKEN)
            return ObserverResult(success=False, error=msg)

        # 6. Save state
        self._save_state(state)

        # 6b. Update varangian.ai landing page feed
        try:
            self._update_landing_feed(intel_stats, briefing_filename, timestamp)
        except Exception as e:
            log.warning("cyber_threat_feed: landing feed update failed: %s", e)

        # 7. Telegram notification
        summary_parts = []
        if intel_stats["critical_cves"]:
            summary_parts.append(f"{intel_stats['critical_cves']} critical CVEs")
        if intel_stats["high_cves"]:
            summary_parts.append(f"{intel_stats['high_cves']} high CVEs")
        if intel_stats["kev_count"]:
            summary_parts.append(f"{intel_stats['kev_count']} KEV additions")
        if intel_stats["ioc_count"]:
            summary_parts.append(f"{intel_stats['ioc_count']} IOCs")
        if intel_stats["malware_count"]:
            summary_parts.append(f"{intel_stats['malware_count']} malware samples")
        if intel_stats["c2_count"]:
            summary_parts.append(f"{intel_stats['c2_count']} active C2s")

        summary = ", ".join(summary_parts) if summary_parts else "no significant threats"

        tg_msg = (
            f"[CYBER THREAT BRIEFING] {timestamp}\n\n"
            f"{summary}\n\n"
            f"Engine: {backend_used}\n"
            f"https://cyber.puretensor.ai/briefings/\n"
            f"https://cyber.varangian.ai/briefings/\n"
            f"Permalink: https://cyber.puretensor.ai/briefings/{briefing_filename}"
        )
        self.send_telegram(tg_msg, token=ALERT_BOT_TOKEN)

        log.info("Cyber Threat Feed complete — published to cyber.puretensor.ai + cyber.varangian.ai")

        return ObserverResult(
            success=True,
            message=tg_msg,
            data={
                "timestamp": timestamp,
                "intel_stats": intel_stats,
                "briefing_filename": briefing_filename,
                "deployed": True,
            },
        )


# ---------------------------------------------------------------------------
# Standalone testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Load .env from nexus project root
    project_dir = Path(__file__).parent.parent
    env_path = project_dir / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    sys.path.insert(0, str(project_dir))

    import argparse
    parser = argparse.ArgumentParser(description="Cyber Threat Feed (standalone)")
    parser.add_argument("--collect-only", action="store_true", help="Only collect intel, skip LLM/deploy")
    parser.add_argument("--no-deploy", action="store_true", help="Generate but don't deploy to GCP")
    parser.add_argument("--output", type=str, help="Write HTML to local file instead of deploying")
    args = parser.parse_args()

    observer = CyberThreatFeedObserver()

    if args.collect_only:
        print("Collecting intelligence...")
        intel = observer.collect_all_intelligence()
        print(json.dumps(intel, indent=2, default=str))
        sys.exit(0)

    if args.no_deploy or args.output:
        from observers.base import ObserverContext
        ctx = ObserverContext()
        timestamp = ctx.now.strftime("%Y-%m-%d %H:%M UTC")

        print("Collecting intelligence...")
        intel = observer.collect_all_intelligence()
        intel_stats = {
            "critical_cves": len(intel["nvd_critical"]),
            "high_cves": len(intel["nvd_high"]),
            "kev_count": len(intel["cisa_kev"]),
            "ioc_count": len(intel["threatfox"]) + len(intel["urlhaus"]),
            "malware_count": len(intel["malware_bazaar"]),
            "c2_count": len(intel["feodo_tracker"]),
        }
        print(f"Stats: {intel_stats}")

        print("Generating briefing via LLM (Ollama → Gemini fallback)...")
        prompt = observer.build_analysis_prompt(intel, timestamp)
        try:
            briefing_html, backend = observer.call_llm_for_briefing(prompt, timeout=300)
            print(f"  Backend: {backend}")
        except RuntimeError as e:
            print(f"ERROR: LLM generation failed: {e}", file=sys.stderr)
            sys.exit(1)

        if not briefing_html:
            print("ERROR: LLM returned empty response", file=sys.stderr)
            sys.exit(1)

        briefing_html = re.sub(r"^```(?:html)?\s*", "", briefing_html)
        briefing_html = re.sub(r"\s*```$", "", briefing_html)

        page = observer.build_full_page(briefing_html, timestamp, intel_stats)

        out = args.output or "/tmp/cyber_briefing_preview.html"
        with open(out, "w") as f:
            f.write(page)
        print(f"Written to {out}")
        sys.exit(0)

    # Full run
    from observers.base import ObserverContext
    ctx = ObserverContext()
    result = observer.run(ctx)

    if result.success:
        print(f"SUCCESS: {result.data}")
    else:
        print(f"FAILED: {result.error}", file=sys.stderr)
        sys.exit(1)
