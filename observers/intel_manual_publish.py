#!/usr/bin/env python3
"""Intel Manual Publish Pipeline — inject a manually-sourced article into the intel pipeline.

Runs AI Council verification (accuracy/coherence/depth, threshold 6.0), rewrites
the source article into both PureTensor and Varangian branded versions, deploys
to all four touchpoints (article page, main index, briefings index, feed.json),
and syncs local git repos.

Usage:
    python3 intel_manual_publish.py --file article.txt
    python3 intel_manual_publish.py --text "..."
    python3 intel_manual_publish.py --dry-run --file article.txt  # preview only
"""

import json
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import sys as _sys
_nexus_root = str(Path(__file__).resolve().parent.parent)
if _nexus_root not in _sys.path:
    _sys.path.insert(0, _nexus_root)

from observers.cloud_llm import (
    call_gemini_flash, call_xai_grok, call_claude_haiku, call_deepseek, extract_json,
)
from observers.intel_deep_analysis import IntelDeepAnalysisObserver, BRANDS, GCP_SSH_HOST

log = logging.getLogger("nexus")

# Lower than automated pipeline's 7.0 — manual articles are human-curated for
# topic relevance; council only verifies quality/veracity.
COUNCIL_THRESHOLD = 6.0

COUNCIL_VERIFY_PROMPT = """\
You are a fact-checking editor. Score this article on three dimensions (1-10):
- Accuracy: Are the factual claims verifiable and internally consistent?
- Coherence: Is the reasoning logical, sourcing credible, conclusions supported?
- Depth: Is the analysis substantive enough to publish as intelligence output?

Respond as JSON: {{"accuracy": N, "coherence": N, "depth": N, "verdict": "pass|fail", "notes": "..."}}

ARTICLE:
{text}
"""


class IntelManualPublisher:
    """Runs a manually-sourced article through the intel pipeline."""

    def __init__(self):
        self._observer = IntelDeepAnalysisObserver()

    # ── Council Verification ──────────────────────────────────────────────

    def _council_verify(self, text: str) -> tuple[bool, float, str]:
        """Run AI Council to verify article quality. Returns (passed, score, notes)."""
        truncated = text[:8000]
        prompt = COUNCIL_VERIFY_PROMPT.format(text=truncated)
        system = "You are a fact-checking editor. Respond only with valid JSON."

        callers = {
            "gemini":   lambda: call_gemini_flash(system, prompt, timeout=45),
            "grok":     lambda: call_xai_grok(system, prompt, timeout=45),
            "claude":   lambda: call_claude_haiku(system, prompt, timeout=45),
            "deepseek": lambda: call_deepseek(system, prompt, timeout=45),
        }

        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(self._observer._call_with_retry, fn, name): name
                for name, fn in callers.items()
            }
            for future in as_completed(futures, timeout=120):
                name = futures[future]
                try:
                    raw = future.result()
                    parsed = extract_json(raw)
                    if isinstance(parsed, dict) and "accuracy" in parsed:
                        results[name] = parsed
                        log.info("intel_manual_publish: council %s responded", name)
                    else:
                        log.warning("intel_manual_publish: council %s returned invalid JSON", name)
                except Exception as e:
                    log.warning("intel_manual_publish: council %s failed: %s", name, e)

        if not results:
            log.error("intel_manual_publish: council quorum failed — no models responded")
            return False, 0.0, "Council quorum failed — no models responded"

        log.info("intel_manual_publish: council quorum %d/4", len(results))

        # Aggregate scores across models
        all_scores: list[float] = []
        all_notes: list[str] = []
        for model_name, data in results.items():
            accuracy = float(data.get("accuracy", 0))
            coherence = float(data.get("coherence", 0))
            depth = float(data.get("depth", 0))
            avg = (accuracy + coherence + depth) / 3
            all_scores.append(avg)
            note = data.get("notes", "")
            if note:
                all_notes.append(f"[{model_name}] {note}")

        composite = sum(all_scores) / len(all_scores)
        notes = " | ".join(all_notes) if all_notes else "No detailed notes"
        passed = composite >= COUNCIL_THRESHOLD
        return passed, round(composite, 2), notes

    # ── Article Rewrite ───────────────────────────────────────────────────

    def _rewrite_for_brand(self, source_text: str, brand: str) -> dict | None:
        """Rewrite source article as a branded intel analysis.

        Passes the source article as the 'research' content to _generate_article,
        which uses the brand-specific writer prompt and outputs a full analysis.
        """
        raw_articles = [
            {"source": "Manual Intelligence Source", "title": "Source Article", "url": ""}
        ]
        # _generate_article uses WRITER_PROMPTS[brand] as the system prompt,
        # treats research as the primary content, and parses TITLE/SUBTITLE/CATEGORY
        # from the model output.
        return self._observer._generate_article(
            topic="Manual Intel",
            research=source_text[:15000],
            angles=[],
            raw_articles=raw_articles,
            brand=brand,
        )

    # ── Local Repo Sync ───────────────────────────────────────────────────

    def _sync_local_article(self, article: dict, html: str, brand: str):
        """Write the article HTML to the local git repo for the brand's intel site."""
        local_repo = Path.home() / "gcp-sites" / f"intel.{brand}.ai"
        if not local_repo.exists():
            log.warning("intel_manual_publish: local repo not found: %s", local_repo)
            return
        analysis_dir = local_repo / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{article['slug']}.html"
        (analysis_dir / filename).write_text(html)
        log.info("intel_manual_publish: synced article to local repo: %s/analysis/%s", brand, filename)

    def _sync_local_index(self, article: dict, score: float, brand: str):
        """Insert an analysis card into the local copy of index.html."""
        local_repo = Path.home() / "gcp-sites" / f"intel.{brand}.ai"
        index_file = local_repo / "index.html"
        if not index_file.exists():
            log.warning("intel_manual_publish: local index.html not found for %s", brand)
            return
        index_html = index_file.read_text()
        article_url = f"/analysis/{article['slug']}.html"
        if article_url in index_html:
            return
        card_html = self._observer._generate_card_html(article, score)
        marker = '<div class="analysis-grid reveal">'
        if marker not in index_html:
            log.warning("intel_manual_publish: analysis-grid marker not found in local %s index", brand)
            return
        pos = index_html.index(marker) + len(marker)
        updated = index_html[:pos] + "\n" + card_html + "\n" + index_html[pos:]
        index_file.write_text(updated)
        log.info("intel_manual_publish: updated local index.html for %s", brand)

    def _sync_local_briefings(self, article: dict, score: float, summary: str, brand: str):
        """Insert a briefing card into the local copy of briefings/index.html."""
        local_repo = Path.home() / "gcp-sites" / f"intel.{brand}.ai"
        briefings_file = local_repo / "briefings" / "index.html"
        if not briefings_file.exists():
            log.warning("intel_manual_publish: local briefings/index.html not found for %s", brand)
            return
        index_html = briefings_file.read_text()
        article_url = f"/analysis/{article['slug']}.html"
        if article_url in index_html:
            return
        card_html = self._observer._generate_briefing_card_html(article, score, summary)
        marker = '<div class="briefing-list">'
        if marker not in index_html:
            log.warning("intel_manual_publish: briefing-list marker not found in local %s briefings", brand)
            return
        pos = index_html.index(marker) + len(marker)
        updated = index_html[:pos] + "\n" + card_html + "\n" + index_html[pos:]
        briefings_file.write_text(updated)
        log.info("intel_manual_publish: updated local briefings/index.html for %s", brand)

    # ── Main Pipeline ─────────────────────────────────────────────────────

    def run(self, source_text: str, dry_run: bool = False) -> dict:
        """Run the full manual publish pipeline.

        Returns a dict with keys:
          passed (bool), score (float), notes (str), urls (list[str]),
          council_quorum (bool), dry_run (bool, optional), elapsed (float, optional),
          error (str, optional)
        """
        start_time = time.time()

        # 1. Council verification
        log.info("intel_manual_publish: running council verification...")
        passed, score, notes = self._council_verify(source_text)
        log.info("intel_manual_publish: council score=%.1f passed=%s", score, passed)

        if not passed:
            return {
                "passed": False,
                "score": score,
                "notes": notes,
                "urls": [],
                "council_quorum": True,
            }

        # 2. Dry-run: generate previews, don't deploy
        if dry_run:
            log.info("intel_manual_publish: dry-run mode — generating previews only")
            preview_paths: list[str] = []
            for brand in ("puretensor", "varangian"):
                article = self._rewrite_for_brand(source_text, brand)
                if article:
                    html = self._observer._generate_html(
                        article, brand, score,
                        ["gemini", "grok", "claude", "deepseek"],
                    )
                    preview_path = f"/tmp/intel_preview_{brand}.html"
                    Path(preview_path).write_text(html)
                    preview_paths.append(preview_path)
                    log.info("intel_manual_publish: dry-run preview saved: %s", preview_path)
            return {
                "passed": True,
                "score": score,
                "notes": notes,
                "urls": [],
                "dry_run": True,
                "preview_paths": preview_paths,
                "council_quorum": True,
                "elapsed": round(time.time() - start_time, 1),
            }

        # 3. Generate + deploy both brands
        published_urls: list[str] = []
        all_published: list[dict] = []

        for brand in ("puretensor", "varangian"):
            log.info("intel_manual_publish: generating %s article...", brand)
            article = self._rewrite_for_brand(source_text, brand)
            if not article or len(article.get("body", "")) < 500:
                log.warning("intel_manual_publish: %s article too short or failed", brand)
                continue

            html = self._observer._generate_html(
                article, brand, score,
                ["gemini", "grok", "claude", "deepseek"],
            )

            # Deploy article HTML to GCP
            try:
                url = self._observer._deploy(article, html, brand)
                published_urls.append(url)
                log.info("intel_manual_publish: deployed %s: %s", brand, url)
            except Exception as e:
                log.error("intel_manual_publish: deploy failed (%s): %s", brand, e)
                continue

            # Update main index
            try:
                self._observer._update_index(article, score, brand)
            except Exception as e:
                log.error("intel_manual_publish: index update failed (%s): %s", brand, e)

            # Generate briefing summary and update briefings index
            summary = ""
            try:
                summary = self._observer._generate_briefing_summary(article)
                self._observer._update_briefings_index(article, score, summary, brand)
            except Exception as e:
                log.error("intel_manual_publish: briefings update failed (%s): %s", brand, e)

            # Sync local git repo
            try:
                self._sync_local_article(article, html, brand)
                self._sync_local_index(article, score, brand)
                self._sync_local_briefings(article, score, summary, brand)
            except Exception as e:
                log.warning("intel_manual_publish: local repo sync failed (%s): %s", brand, e)

            all_published.append({
                "domain": article.get("category", "Multi-Domain"),
                "topic": article.get("title", ""),
                "score": score,
                "urls": [url],
            })

        # 4. Update varangian.ai landing feed (ticker)
        if all_published:
            try:
                self._observer._update_landing_feed(all_published)
            except Exception as e:
                log.warning("intel_manual_publish: landing feed update failed: %s", e)

        elapsed = time.time() - start_time
        return {
            "passed": True,
            "score": score,
            "notes": notes,
            "urls": published_urls,
            "council_quorum": True,
            "elapsed": round(elapsed, 1),
        }


# ── Standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Intel Manual Publish Pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", metavar="PATH", help="Path to source article text file")
    group.add_argument("--text", metavar="TEXT", help="Source article text (inline)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run council + generate previews, do not deploy")
    args = parser.parse_args()

    if args.file:
        source = Path(args.file).read_text(encoding="utf-8")
    else:
        source = args.text

    print(f"[intel_manual_publish] Source text: {len(source)} characters")
    print(f"[intel_manual_publish] Mode: {'dry-run' if args.dry_run else 'live deploy'}")
    print()

    publisher = IntelManualPublisher()
    result = publisher.run(source, dry_run=args.dry_run)

    print()
    if not result["passed"]:
        print(f"REJECTED — Score: {result['score']:.1f}/10 (threshold: {COUNCIL_THRESHOLD})")
        print(f"Council notes: {result['notes']}")
        _sys.exit(1)

    print(f"PASSED — Council score: {result['score']:.1f}/10")
    print(f"Notes: {result['notes']}")

    if result.get("dry_run"):
        print("\nDry-run previews:")
        for path in result.get("preview_paths", []):
            print(f"  {path}")
        print(f"\nElapsed: {result.get('elapsed', 0):.1f}s")
    else:
        print("\nDeployed URLs:")
        for url in result.get("urls", []):
            print(f"  {url}")
        print(f"\nElapsed: {result.get('elapsed', 0):.1f}s")
