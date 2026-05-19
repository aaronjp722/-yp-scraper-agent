#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import os
import re
import logging
import schedule
import time
from datetime import datetime, date
from urllib.parse import urljoin, urlparse
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
TABLE_NAME   = os.environ.get("SUPABASE_TABLE", "leads")
YELP_KEY     = os.environ.get("YELP_API_KEY", "").strip()
PROXY_URL    = os.environ.get("PROXY_URL", "https://blubalances.com/api/foursquare-proxy")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

FALLBACK_JOBS = [
    {"keyword": "roofing contractors", "place": "Dallas,TX"},
    {"keyword": "roofing contractors", "place": "Houston,TX"},
    {"keyword": "plumbers",            "place": "Dallas,TX"},
    {"keyword": "electricians",        "place": "Dallas,TX"},
    {"keyword": "landscaping",         "place": "Dallas,TX"},
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
OBFUSCATED_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*[\[\(]?\s*(?:at|@)\s*[\]\)]?\s*([a-zA-Z0-9.\-]+)\s*[\[\(]?\s*(?:dot|\.)\s*[\]\)]?\s*([a-zA-Z]{2,})"
)
SKIP_PREFIXES = ("noreply","no-reply","donotreply","mailer","bounce",
                 "support","help","admin","webmaster","postmaster","info@example")
SKIP_DOMAINS  = ("sentry.io","wixpress.com","squarespace.com","shopify.com",
                 "wordpress.com","example.com","domain.com","yourdomain.com",
                 "email.com","test.com")
SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CONTACT_PATHS = [
    "", "/contact", "/contact-us", "/contactus", "/contact_us",
    "/about", "/about-us", "/aboutus", "/about_us",
    "/get-in-touch", "/reach-us", "/connect", "/hello",
    "/team", "/our-team", "/staff", "/people",
    "/info", "/information", "/support", "/help",
    "/services", "/hire-us", "/work-with-us",
]


# ── Supabase helpers ──────────────────────────────────────────────────────────

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
        log.warning("No active jobs -- using fallback jobs")
        return FALLBACK_JOBS
    except Exception as e:
        log.error(f"Could not load jobs: {e} -- using fallback jobs")
        return FALLBACK_JOBS


def get_existing_phones():
    try:
        resp = supabase.table(TABLE_NAME).select("telephone").execute()
        return {row["telephone"] for row in resp.data if row.get("telephone")}
    except Exception as e:
        log.error(f"Could not fetch existing phones: {e}")
        return set()


def push_to_supabase(records, existing_phones):
    new_records = [r for r in records if r.get("telephone") and r["telephone"] not in existing_phones]
    if not new_records:
        log.info("  -> No new records (all duplicates or no phone numbers)")
        return [], 0
    try:
        result = supabase.table(TABLE_NAME).insert(new_records).execute()
        log.info(f"  -> Inserted {len(new_records)} new leads")
        return new_records, len(new_records)
    except Exception as e:
        log.error(f"Supabase insert failed: {e}")
        return [], 0


def update_email(telephone, email):
    try:
        supabase.table(TABLE_NAME).update({"email": email}).eq("telephone", telephone).execute()
    except Exception as e:
        log.error(f"Email update failed: {e}")


def make_tags(keyword, place, job_tags, source):
    tags = list(job_tags)
    for t in [keyword, place, date.today().isoformat(), source]:
        if t and t not in tags:
            tags.append(t)
    return tags


# ── Email extraction ──────────────────────────────────────────────────────────

def clean_emails(raw_emails):
    clean = []
    for e in raw_emails:
        e = e.lower().strip(".").strip()
        if not e or len(e) > 100:
            continue
        if any(e.startswith(p) for p in SKIP_PREFIXES):
            continue
        if e.endswith((".png", ".jpg", ".gif", ".css", ".js", ".svg", ".woff")):
            continue
        domain = e.split("@")[-1] if "@" in e else ""
        if any(d in domain for d in SKIP_DOMAINS):
            continue
        if e not in clean:
            clean.append(e)
    return clean


def extract_emails_from_html(html):
    # 1. mailto: links — most reliable
    mailto = re.findall(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', html)
    # 2. plain email pattern
    plain = EMAIL_RE.findall(html)
    # 3. obfuscated: name [at] domain [dot] com
    obfuscated = []
    for m in OBFUSCATED_RE.finditer(html):
        obfuscated.append(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
    all_found = mailto + plain + obfuscated
    return clean_emails(all_found)


def get_domain_email_guesses(website):
    """Generate common email patterns from domain as last resort."""
    try:
        domain = urlparse(website).netloc.replace("www.", "")
        if not domain:
            return []
        return [f"info@{domain}", f"contact@{domain}", f"hello@{domain}"]
    except Exception:
        return []


def fetch_page(url, timeout=8):
    try:
        resp = requests.get(url, headers=SCRAPE_HEADERS, timeout=timeout,
                            allow_redirects=True, verify=False)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


def scrape_email_from_site(website):
    if not website:
        return None

    base = website.rstrip("/")
    # parse base domain for absolute URL building
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    found_emails = []

    for path in CONTACT_PATHS:
        url = urljoin(origin, path) if path else base
        html = fetch_page(url)
        if not html:
            continue
        emails = extract_emails_from_html(html)
        if emails:
            found_emails.extend(emails)
            # if we found a mailto email already, stop early
            if any(f"mailto:{e}" in html.lower() for e in emails):
                break
        time.sleep(0.15)

        # Stop once we have candidates — don't crawl all pages unnecessarily
        if len(found_emails) >= 3:
            break

    if found_emails:
        return found_emails[0]

    return None


def enrich_emails(inserted_records):
    if not inserted_records:
        return
    with_sites = [(r.get("telephone"), r.get("website"), r.get("business_name"))
                  for r in inserted_records if r.get("website")]
    if not with_sites:
        log.info("  [Email] No websites to crawl")
        return

    log.info(f"  [Email] Crawling {len(with_sites)} websites for emails...")
    found = 0
    for telephone, website, name in with_sites:
        email = scrape_email_from_site(website)
        if email:
            update_email(telephone, email)
            log.info(f"  [Email] {name}: {email}")
            found += 1
        time.sleep(0.2)

    log.info(f"  [Email] Found {found}/{len(with_sites)} emails")


# ── Foursquare via Vercel proxy ───────────────────────────────────────────────

def fetch_foursquare(keyword, place):
    params = {"query": keyword, "near": place}
    for attempt in range(3):
        try:
            resp = requests.get(PROXY_URL, params=params, timeout=20)
            if resp.status_code == 401:
                log.error("Foursquare proxy: invalid API key in Vercel")
                return None
            if resp.status_code != 200:
                log.warning(f"Foursquare proxy HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(2 ** attempt)
                continue
            return resp.json().get("results", [])
        except Exception as e:
            log.warning(f"Foursquare proxy attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None


def parse_foursquare(venue, keyword, place, job_tags):
    loc = venue.get("location", {})
    category = venue.get("categories", [{}])[0].get("name") if venue.get("categories") else None
    return {
        "business_name":  venue.get("name"),
        "telephone":      venue.get("tel"),
        "website":        venue.get("website"),
        "street":         loc.get("address"),
        "locality":       loc.get("locality"),
        "region":         loc.get("region"),
        "zipcode":        loc.get("postcode"),
        "category":       category,
        "rating":         venue.get("rating"),
        "business_page":  f"https://foursquare.com/v/{venue.get('fsq_id','')}",
        "listing_url":    f"https://foursquare.com/v/{venue.get('fsq_id','')}",
        "search_keyword": keyword,
        "search_place":   place,
        "tags":           make_tags(keyword, place, job_tags, "foursquare"),
        "scraped_at":     datetime.utcnow().isoformat(),
    }


def scrape_foursquare(keyword, place, job_tags):
    log.info(f"  [Foursquare] '{keyword}' near '{place}'")
    venues = fetch_foursquare(keyword, place)
    if venues is None:
        log.warning("  [Foursquare] failed -- trying next provider")
        return None
    results = [parse_foursquare(v, keyword, place, job_tags) for v in venues]
    log.info(f"  [Foursquare] -> {len(results)} results")
    return results if results else None


# ── Yelp ─────────────────────────────────────────────────────────────────────

YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"


def fetch_yelp(keyword, place, offset=0):
    if not YELP_KEY:
        return None, 0
    headers = {"Authorization": f"Bearer {YELP_KEY}", "Accept": "application/json"}
    params  = {"term": keyword, "location": place, "limit": 50, "offset": offset}
    for attempt in range(3):
        try:
            resp = requests.get(YELP_SEARCH_URL, headers=headers, params=params, timeout=15)
            if resp.status_code == 401:
                log.error("Yelp: invalid API key")
                return None, 0
            if resp.status_code != 200:
                log.warning(f"Yelp HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(2 ** attempt)
                continue
            data = resp.json()
            return data.get("businesses", []), data.get("total", 0)
        except Exception as e:
            log.warning(f"Yelp attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None, 0


def parse_yelp(biz, keyword, place, job_tags):
    loc = biz.get("location", {})
    return {
        "business_name":  biz.get("name"),
        "telephone":      biz.get("phone") or biz.get("display_phone"),
        "website":        biz.get("url"),
        "street":         (loc.get("display_address") or [None])[0],
        "locality":       loc.get("city"),
        "region":         loc.get("state"),
        "zipcode":        loc.get("zip_code"),
        "category":       (biz.get("categories") or [{}])[0].get("title"),
        "rating":         biz.get("rating"),
        "business_page":  biz.get("url"),
        "listing_url":    biz.get("url"),
        "search_keyword": keyword,
        "search_place":   place,
        "tags":           make_tags(keyword, place, job_tags, "yelp"),
        "scraped_at":     datetime.utcnow().isoformat(),
    }


def scrape_yelp(keyword, place, job_tags):
    if not YELP_KEY:
        return None
    log.info(f"  [Yelp] '{keyword}' near '{place}'")
    all_results, offset = [], 0
    while offset < 200:
        businesses, total = fetch_yelp(keyword, place, offset)
        if businesses is None:
            log.warning("  [Yelp] failed -- trying next provider")
            return None
        if not businesses:
            break
        all_results.extend(parse_yelp(b, keyword, place, job_tags) for b in businesses)
        offset += 50
        if offset >= total:
            break
        time.sleep(0.3)
    log.info(f"  [Yelp] -> {len(all_results)} results")
    return all_results if all_results else None


# ── Provider fallback chain ───────────────────────────────────────────────────

def scrape_with_fallback(keyword, place, job_tags):
    for name, fn in [("Foursquare", scrape_foursquare), ("Yelp", scrape_yelp)]:
        try:
            results = fn(keyword, place, job_tags)
        except Exception as e:
            log.error(f"  [{name}] crashed: {e}")
            results = None
        if results:
            return results
        log.info(f"  [{name}] no results -- trying next provider")
    log.error(f"  All providers failed for '{keyword}' / '{place}'")
    return []


# ── Main agent ────────────────────────────────────────────────────────────────

def run_agent():
    log.info("=" * 60)
    log.info(f"Agent started -- {datetime.utcnow().isoformat()} UTC")
    log.info(f"Providers: Foursquare=proxy | Yelp={'yes' if YELP_KEY else 'no'}")
    jobs            = load_jobs()
    existing_phones = get_existing_phones()
    log.info(f"Existing leads in DB: {len(existing_phones)}")
    total_scraped = total_inserted = 0

    for job in jobs:
        log.info(f"Job: '{job['keyword']}' / '{job['place']}'")
        records = scrape_with_fallback(job["keyword"], job["place"], job.get("tags", []))
        total_scraped += len(records)
        inserted_records, count = push_to_supabase(records, existing_phones)
        total_inserted += count
        existing_phones.update(r["telephone"] for r in records if r.get("telephone"))
        enrich_emails(inserted_records)

    log.info(f"Done. Scraped: {total_scraped} | Inserted: {total_inserted}")
    log.info("=" * 60)


# ── Scheduler ─────────────────────────────────────────────────────────────────

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
