#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import os
import logging
import schedule
import time
from datetime import datetime, date
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
TABLE_NAME     = os.environ.get("SUPABASE_TABLE", "leads")
FOURSQUARE_KEY = os.environ["FOURSQUARE_API_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

FS_SEARCH_URL = "https://api.foursquare.com/v3/places/search"
FS_DETAIL_URL = "https://api.foursquare.com/v3/places/{fsq_id}"
FS_HEADERS    = {"Accept": "application/json", "Authorization": FOURSQUARE_KEY}

FALLBACK_JOBS = [
    {"keyword": "roofing contractors", "place": "Dallas,TX"},
    {"keyword": "roofing contractors", "place": "Houston,TX"},
    {"keyword": "roofing contractors", "place": "Atlanta,GA"},
    {"keyword": "plumbers",            "place": "Dallas,TX"},
    {"keyword": "electricians",        "place": "Dallas,TX"},
    {"keyword": "landscaping",         "place": "Dallas,TX"},
]


def load_config():
    defaults = {"SCHEDULE": "weekly", "RUN_HOUR": "06:00", "INTERVAL_HOURS": "24"}
    try:
        resp = supabase.table("scraper_config").select("key,value").execute()
        if resp.data:
            return {row["key"]: row["value"] for row in resp.data}
    except Exception as e:
        log.error(f"Could not load config: {e}")
    return defaults


def load_jobs():
    try:
        resp = supabase.table("scraper_jobs").select("*").eq("active", True).execute()
        if resp.data:
            log.info(f"Loaded {len(resp.data)} jobs from Supabase")
            return [{"keyword": r["keyword"], "place": r["place"], "tags": r.get("tags") or []} for r in resp.data]
        log.warning("No active jobs in scraper_jobs -- using fallback jobs")
        return FALLBACK_JOBS
    except Exception as e:
        log.error(f"Could not load jobs: {e} -- using fallback jobs")
        return FALLBACK_JOBS


def fetch_foursquare_page(keyword, place, cursor=None):
    params = {
        "query":  keyword,
        "near":   place,
        "limit":  50,
        "fields": "fsq_id,name,tel,website,location,categories,rating",
    }
    if cursor:
        params["cursor"] = cursor
    for attempt in range(5):
        try:
            resp = requests.get(FS_SEARCH_URL, headers=FS_HEADERS, params=params, timeout=15)
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                log.warning(f"HTTP {resp.status_code} on attempt {attempt+1}/5")
                time.sleep(2 ** attempt)
                continue
            data = resp.json()
            return data.get("results", []), data.get("context", {}).get("next_cursor")
        except Exception as e:
            log.error(f"Attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return [], None


def fetch_phone(fsq_id):
    try:
        resp = requests.get(
            FS_DETAIL_URL.format(fsq_id=fsq_id),
            headers=FS_HEADERS,
            params={"fields": "tel"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("tel")
    except Exception:
        pass
    return None


def parse_venue(venue, keyword, place, job_tags):
    loc      = venue.get("location", {})
    category = venue.get("categories", [{}])[0].get("name") if venue.get("categories") else None
    tel      = venue.get("tel") or fetch_phone(venue["fsq_id"])
    today    = date.today().isoformat()
    tags     = list(job_tags)
    for t in [keyword, place, today]:
        if t and t not in tags:
            tags.append(t)
    return {
        "business_name":  venue.get("name"),
        "telephone":      tel,
        "website":        venue.get("website"),
        "street":         loc.get("address"),
        "locality":       loc.get("locality"),
        "region":         loc.get("region"),
        "zipcode":        loc.get("postcode"),
        "category":       category,
        "rating":         venue.get("rating"),
        "business_page":  f"https://foursquare.com/v/{venue['fsq_id']}",
        "listing_url":    f"https://foursquare.com/v/{venue['fsq_id']}",
        "search_keyword": keyword,
        "search_place":   place,
        "tags":           tags,
        "scraped_at":     datetime.utcnow().isoformat(),
    }


def scrape_foursquare(keyword, place, job_tags=None):
    log.info(f"Foursquare search: '{keyword}' near '{place}'")
    job_tags, results, cursor, pages = job_tags or [], [], None, 0
    while pages < 5:
        venues, cursor = fetch_foursquare_page(keyword, place, cursor)
        if not venues:
            break
        for v in venues:
            results.append(parse_venue(v, keyword, place, job_tags))
        log.info(f"  page {pages+1}: {len(venues)} venues (total {len(results)})")
        pages += 1
        if not cursor:
            break
        time.sleep(0.5)
    log.info(f"  -> {len(results)} total for '{keyword}' / '{place}'")
    return results


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
        log.info("  -> No new records (all duplicates or no phone numbers)")
        return 0
    try:
        supabase.table(TABLE_NAME).insert(new_records).execute()
        log.info(f"  -> Inserted {len(new_records)} new leads")
        return len(new_records)
    except Exception as e:
        log.error(f"Supabase insert failed: {e}")
        return 0


def run_agent():
    log.info("=" * 60)
    log.info(f"Agent started -- {datetime.utcnow().isoformat()} UTC")
    jobs            = load_jobs()
    existing_phones = get_existing_phones()
    log.info(f"Existing leads in DB: {len(existing_phones)}")
    total_scraped = total_inserted = 0
    for job in jobs:
        records = scrape_foursquare(job["keyword"], job["place"], job_tags=job.get("tags", []))
        total_scraped  += len(records)
        total_inserted += push_to_supabase(records, existing_phones)
        existing_phones.update(r["telephone"] for r in records if r.get("telephone"))
    log.info(f"Done. Scraped: {total_scraped} | Inserted: {total_inserted}")
    log.info("=" * 60)


cfg            = load_config()
SCHEDULE       = os.environ.get("SCHEDULE",        cfg.get("SCHEDULE",        "weekly"))
RUN_HOUR       = os.environ.get("RUN_HOUR",        cfg.get("RUN_HOUR",        "06:00"))
INTERVAL_HOURS = int(os.environ.get("INTERVAL_HOURS", cfg.get("INTERVAL_HOURS", "24")))

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
