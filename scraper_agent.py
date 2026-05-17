#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
from lxml import html
import os
import logging
import schedule
import time
from datetime import datetime
from supabase import create_client, Client

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TABLE_NAME   = os.environ.get("SUPABASE_TABLE", "leads")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Jobs — edit these to control what gets scraped ───────────────────────────
JOBS = [
    {"keyword": "roofing contractors", "place": "Dallas,TX"},
    {"keyword": "roofing contractors", "place": "Houston,TX"},
    {"keyword": "roofing contractors", "place": "Atlanta,GA"},
    {"keyword": "plumbers",            "place": "Dallas,TX"},
    {"keyword": "electricians",        "place": "Dallas,TX"},
    {"keyword": "landscaping",         "place": "Dallas,TX"},
]

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Host": "www.yellowpages.com",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# ── Scraper ───────────────────────────────────────────────────────────────────
def scrape_yellowpages(keyword, place):
    url = f"https://www.yellowpages.com/search?search_terms={keyword}&geo_location_terms={place}"
    log.info(f"Scraping: {url}")

    for attempt in range(5):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
            if resp.status_code == 404:
                log.warning(f"404 for: {place}")
                return []
            if resp.status_code != 200:
                log.warning(f"HTTP {resp.status_code} — retry {attempt+1}/5")
                continue

            parser = html.fromstring(resp.text)
            parser.make_links_absolute("https://www.yellowpages.com")
            listings = parser.xpath("//div[@class='search-results organic']//div[@class='v-card']")
            results = []

            for item in listings:
                def x(path): return item.xpath(path)
                raw_locality = ''.join(x(".//div[@class='locality']//text()")).replace(',\xa0', '').strip()
                try:
                    locality, rest = raw_locality.split(',')
                    parts = rest.strip().split(' ')
                    region  = parts[0] if len(parts) > 0 else ""
                    zipcode = parts[1] if len(parts) > 1 else ""
                except Exception:
                    locality = raw_locality
                    region = zipcode = ""

                results.append({
                    "business_name":  ''.join(x(".//a[@class='business-name']//text()")).strip() or None,
                    "telephone":      ''.join(x(".//div[@class='phones phone primary']//text()")).strip() or None,
                    "business_page":  ''.join(x(".//a[@class='business-name']//@href")).strip() or None,
                    "rank":           ''.join(x(".//div[@class='info']//h2[@class='n']/text()")).replace('.\xa0','') or None,
                    "category":       ','.join(x(".//div[@class='categories']//text()")).strip() or None,
                    "website":        ''.join(x(".//div[@class='links']//a[contains(@class,'website')]/@href")).strip() or None,
                    "rating":         ''.join(x(".//div[contains(@class,'result-rating')]//span//text()")).replace("(","").replace(")","").strip() or None,
                    "street":         ''.join(x(".//div[@class='street-address']//text()")).strip() or None,
                    "locality":       locality or None,
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

    return []

# ── Dedup + Push ──────────────────────────────────────────────────────────────
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

# ── Agent ─────────────────────────────────────────────────────────────────────
def run_agent():
    log.info("=" * 60)
    log.info(f"Agent started — {datetime.utcnow().isoformat()} UTC")
    existing_phones = get_existing_phones()
    log.info(f"Existing leads in DB: {len(existing_phones)}")
    total_scraped = total_inserted = 0
    for job in JOBS:
        records = scrape_yellowpages(job["keyword"], job["place"])
        total_scraped += len(records)
        total_inserted += push_to_supabase(records, existing_phones)
        existing_phones.update(r["telephone"] for r in records if r.get("telephone"))
    log.info(f"Done. Scraped: {total_scraped} | Inserted: {total_inserted}")
    log.info("=" * 60)

# ── Schedule ──────────────────────────────────────────────────────────────────
SCHEDULE       = os.environ.get("SCHEDULE", "weekly")
RUN_HOUR       = os.environ.get("RUN_HOUR", "06:00")
INTERVAL_HOURS = int(os.environ.get("INTERVAL_HOURS", "24"))

if SCHEDULE == "daily":
    schedule.every().day.at(RUN_HOUR).do(run_agent)
elif SCHEDULE == "weekly":
    schedule.every().monday.at(RUN_HOUR).do(run_agent)
elif SCHEDULE == "interval":
    schedule.every(INTERVAL_HOURS).hours.do(run_agent)
else:
    schedule.every().monday.at(RUN_HOUR).do(run_agent)

log.info(f"Scheduler: {SCHEDULE} at {RUN_HOUR} UTC")
log.info("Running once on startup...")
run_agent()

while True:
    schedule.run_pending()
    time.sleep(60)
