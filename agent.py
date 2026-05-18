#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
from bs4 import BeautifulSoup
import os
import logging
import schedule
import time
from datetime import datetime
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TABLE_NAME   = os.environ.get("SUPABASE_TABLE", "leads")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Host": "www.yellowpages.com",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Fallback jobs used only if scraper_jobs table is empty or unreachable
FALLBACK_JOBS = [
    {"keyword": "roofing contractors", "place": "Dallas,TX"},
    {"keyword": "roofing contractors", "place": "Houston,TX"},
    {"keyword": "roofing contractors", "place": "Atlanta,GA"},
    {"keyword": "plumbers",            "place": "Dallas,TX"},
    {"keyword": "electricians",        "place": "Dallas,TX"},
    {"keyword": "landscaping",         "place": "Dallas,TX"},
]


def load_jobs():
    """Load active jobs from Supabase scraper_jobs table."""
    try:
        resp = supabase.table("scraper_jobs").select("*").eq("active", True).execute()
        if resp.data:
            log.info(f"Loaded {len(resp.data)} jobs from Supabase")
            return [{"keyword": r["keyword"], "place": r["place"]} for r in resp.data]
        log.warning("No active jobs in scraper_jobs table — using fallback jobs")
        return FALLBACK_JOBS
    except Exception as e:
        log.error(f"Could not load jobs from Supabase: {e} — using fallback jobs")
        return FALLBACK_JOBS


def scrape_yellowpages(keyword, place):
    url = (
        f"https://www.yellowpages.com/search"
        f"?search_terms={requests.utils.quote(keyword)}"
        f"&geo_location_terms={requests.utils.quote(place)}"
    )
    log.info(f"Scraping: {url}")

    for attempt in range(5):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
            if resp.status_code == 404:
                log.warning(f"404 — no results for: {place}")
                return []
            if resp.status_code != 200:
                log.warning(f"HTTP {resp.status_code} — retry {attempt+1}/5")
                time.sleep(2 ** attempt)
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            listings = soup.select("div.search-results.organic div.v-card")
            results = []

            for item in listings:
                def t(selector, attr=None):
                    el = item.select_one(selector)
                    if not el:
                        return None
                    return (el.get(attr, "") or "").strip() or None if attr else el.get_text(strip=True) or None

                raw_locality = t("div.locality") or ""
                try:
                    locality, rest = raw_locality.split(",", 1)
                    parts = rest.strip().split()
                    region  = parts[0] if parts else ""
                    zipcode = parts[1] if len(parts) > 1 else ""
                except ValueError:
                    locality = raw_locality
                    region = zipcode = ""

                business_page = t("a.business-name", attr="href")
                if business_page and not business_page.startswith("http"):
                    business_page = "https://www.yellowpages.com" + business_page

                results.append({
                    "business_name":  t("a.business-name"),
                    "telephone":      t("div.phones.phone.primary"),
                    "business_page":  business_page,
                    "rank":           t("h2.n"),
                    "category":       t("div.categories"),
                    "website":        t("div.links a.website", attr="href"),
                    "rating":         t("div.result-rating span"),
                    "street":         t("div.street-address"),
                    "locality":       locality.strip() or None,
                    "region":         region or None,
                    "zipcode":        zipcode or None,
                    "listing_url":    resp.url,
                    "search_keyword": keyword,
                    "search_place":   place,
                    "scraped_at":     datetime.utcnow().isoformat(),
                })

            log.info(f"  → {len(results)} listings found")
            return results

        except Exception as e:
            log.error(f"Attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)

    return []


def get_existing_phones():
    try:
        resp = supabase.table(TABLE_NAME).select("telephone").execute()
        return {row["telephone"] for row in resp.data if row.get("telephone")}
    except Exception as e:
        log.error(f"Could not fetch existing leads: {e}")
        return set()


def push_to_supabase(records, existing_phones):
    new_records = [r for r in records if r.get("telephone") not in existing_phones]
    if not new_records:
        log.info("  → No new records (all duplicates)")
        return 0
    try:
        supabase.table(TABLE_NAME).insert(new_records).execute()
        log.info(f"  → Inserted {len(new_records)} new leads")
        return len(new_records)
    except Exception as e:
        log.error(f"Supabase insert failed: {e}")
        return 0


def run_agent():
    log.info("=" * 60)
    log.info(f"Agent started — {datetime.utcnow().isoformat()} UTC")
    jobs = load_jobs()
    existing_phones = get_existing_phones()
    log.info(f"Existing leads in DB: {len(existing_phones)}")
    total_scraped = total_inserted = 0
    for job in jobs:
        records = scrape_yellowpages(job["keyword"], job["place"])
        total_scraped += len(records)
        total_inserted += push_to_supabase(records, existing_phones)
        existing_phones.update(r["telephone"] for r in records if r.get("telephone"))
    log.info(f"Done. Scraped: {total_scraped} | Inserted: {total_inserted}")
    log.info("=" * 60)


SCHEDULE       = os.environ.get("SCHEDULE", "weekly")
RUN_HOUR       = os.environ.get("RUN_HOUR", "06:00")
INTERVAL_HOURS = int(os.environ.get("INTERVAL_HOURS", "24"))

if SCHEDULE == "daily":
    schedule.every().day.at(RUN_HOUR).do(run_agent)
elif SCHEDULE == "interval":
    schedule.every(INTERVAL_HOURS).hours.do(run_agent)
else:
    schedule.every().monday.at(RUN_HOUR).do(run_agent)

log.info(f"Scheduler: {SCHEDULE} | Next run at {RUN_HOUR} UTC")
log.info("Running once on startup...")
run_agent()

while True:
    schedule.run_pending()
    time.sleep(60)
