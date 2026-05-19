#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import os
import re
import logging
import schedule
import time
import urllib3
from datetime import datetime, date
from urllib.parse import urljoin, urlparse
from supabase import create_client, Client

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
TABLE_NAME      = os.environ.get("SUPABASE_TABLE", "leads")
PROXY_URL       = os.environ.get("PROXY_URL", "https://blubalances.com/api/foursquare-proxy")
YELP_KEY        = os.environ.get("YELP_API_KEY", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

FALLBACK_JOBS = [
    {"keyword": "roofing contractors", "place": "Dallas,TX"},
    {"keyword": "roofing contractors", "place": "Houston,TX"},
    {"keyword": "plumbers",            "place": "Dallas,TX"},
    {"keyword": "electricians",        "place": "Dallas,TX"},
    {"keyword": "landscaping",         "place": "Dallas,TX"},
]

EMAIL_SKIP_WORDS   = {"noreply", "no-reply", "donotreply", "mailer", "bounce",
                      "support", "help", "admin", "webmaster", "postmaster"}
EMAIL_SKIP_DOMAINS = {"sentry.io", "wixpress.com", "squarespace.com",
                      "shopify.com", "wordpress.com", "example.com"}
CRAWL_SKIP_DOMAINS = {"yelp.com", "www.yelp.com", "foursquare.com",
                      "facebook.com", "instagram.com", "google.com",
                      "maps.google.com", "linkedin.com", "twitter.com",
                      "tripadvisor.com", "yellowpages.com"}
EMAIL_PATHS = [
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
        log.error(f"Could not fetch existing leads: {e}")
        return set()


def push_to_supabase(records, existing_phones):
    new_records = [r for r in records if r.get("telephone") and r["telephone"] not in existing_phones]
    if not new_records:
        log.info("  -> No new records (all duplicates or no phone numbers)")
        return [], 0
    try:
        resp = supabase.table(TABLE_NAME).insert(new_records).execute()
        inserted = resp.data if resp.data else []
        log.info(f"  -> Inserted {len(inserted)} new leads")
        return inserted, len(inserted)
    except Exception as e:
        log.error(f"Supabase insert failed: {e}")
        return [], 0


def update_email(telephone, email):
    try:
        supabase.table(TABLE_NAME).update({"email": email}).eq("telephone", telephone).execute()
    except Exception as e:
        log.error(f"Email update failed for {telephone}: {e}")


# ── Email crawler ─────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
OBFUS_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*[\[\(]?\s*at\s*[\]\)]?\s*"
    r"([a-zA-Z0-9.\-]+)\s*[\[\(]?\s*dot\s*[\]\)]?\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (compatible; LeadBot/1.0)",
    "Accept":     "text/html,application/xhtml+xml",
}


def _is_valid_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[-1]
    if not re.match(r"^[a-zA-Z]{2,6}$", tld):  # rejects "handleredirect", etc.
        return False
    if any(w in local.lower() for w in EMAIL_SKIP_WORDS):
        return False
    if domain.lower() in EMAIL_SKIP_DOMAINS:
        return False
    if "example" in domain.lower():
        return False
    return True


def _extract_from_html(html: str) -> set:
    found = set()
    for href in re.findall(r'href=["\']mailto:([^"\'?\s]+)', html, re.IGNORECASE):
        email = href.split("?")[0].strip()
        if EMAIL_RE.match(email) and _is_valid_email(email):
            found.add(email.lower())
    for email in EMAIL_RE.findall(html):
        if _is_valid_email(email):
            found.add(email.lower())
    for m in OBFUS_RE.finditer(html):
        email = f"{m.group(1)}@{m.group(2)}.{m.group(3)}"
        if _is_valid_email(email):
            found.add(email.lower())
    return found


def crawl_email(website: str) -> str | None:
    if not website:
        return None
    base = website.rstrip("/")
    parsed = urlparse(base)
    if not parsed.scheme:
        base = "https://" + base
        parsed = urlparse(base)
    if not parsed.netloc:
        return None
    if parsed.netloc in CRAWL_SKIP_DOMAINS or any(parsed.netloc.endswith('.'+d) for d in CRAWL_SKIP_DOMAINS):
        return None

    found: set = set()
    session = requests.Session()
    session.headers.update(HEADERS)

    for path in EMAIL_PATHS:
        url = base + path
        try:
            resp = session.get(url, timeout=8, allow_redirects=True)
            if resp.status_code == 200:
                emails = _extract_from_html(resp.text)
                found.update(emails)
                if found:
                    break
        except Exception:
            pass
        time.sleep(0.1)

    if not found:
        return None
    preferred = [e for e in found if not any(w in e.split("@")[0] for w in {"info", "contact", "hello", "support"})]
    pool = preferred or list(found)
    return min(pool, key=len)


def enrich_emails(inserted_records):
    if not inserted_records:
        return
    log.info(f"  [Email] Enriching {len(inserted_records)} new leads...")
    found_count = 0
    for rec in inserted_records:
        website   = rec.get("website")
        telephone = rec.get("telephone")
        if not website or not telephone:
            continue
        email = crawl_email(website)
        if email:
            update_email(telephone, email)
            found_count += 1
            log.info(f"  [Email] {rec.get('business_name','?')} -> {email}")
        time.sleep(0.2)
    log.info(f"  [Email] Found emails for {found_count}/{len(inserted_records)} leads")


# ── Provider: Foursquare (via Vercel proxy) ───────────────────────────────────
def fetch_foursquare(keyword, place):
    params = {"query": keyword, "near": place}
    for attempt in range(3):
        try:
            resp = requests.get(PROXY_URL, params=params, timeout=20)
            if resp.status_code == 401:
                log.error("Foursquare proxy: invalid API key")
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
    loc      = venue.get("location", {})
    category = venue.get("categories", [{}])[0].get("name") if venue.get("categories") else None
    today    = date.today().isoformat()
    tags     = list(job_tags)
    for t in [keyword, place, today, "foursquare"]:
        if t and t not in tags:
            tags.append(t)
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
        "tags":           tags,
        "scraped_at":     datetime.utcnow().isoformat(),
    }


def scrape_foursquare(keyword, place, job_tags):
    if not PROXY_URL:
        return None
    log.info(f"  [Foursquare] '{keyword}' near '{place}'")
    venues = fetch_foursquare(keyword, place)
    if venues is None:
        log.warning("  [Foursquare] failed -- will try next provider")
        return None
    results = [parse_foursquare(v, keyword, place, job_tags) for v in venues]
    log.info(f"  [Foursquare] -> {len(results)} results")
    return results


# ── Provider: Yelp ────────────────────────────────────────────────────────────
YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
YELP_HEADERS    = {"Authorization": f"Bearer {YELP_KEY}", "Accept": "application/json"}


def fetch_yelp(keyword, place, offset=0):
    params = {"term": keyword, "location": place, "limit": 50, "offset": offset}
    for attempt in range(3):
        try:
            resp = requests.get(YELP_SEARCH_URL, headers=YELP_HEADERS, params=params, timeout=15)
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
    loc   = biz.get("location", {})
    today = date.today().isoformat()
    tags  = list(job_tags)
    for t in [keyword, place, today, "yelp"]:
        if t and t not in tags:
            tags.append(t)
    return {
        "business_name":  biz.get("name"),
        "telephone":      biz.get("phone") or biz.get("display_phone"),
        "website":        None,  # Yelp search doesn't return the real business website
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
        "tags":           tags,
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
            log.warning("  [Yelp] failed -- will try next provider")
            return None
        if not businesses:
            break
        all_results.extend(parse_yelp(b, keyword, place, job_tags) for b in businesses)
        offset += 50
        if offset >= total:
            break
        time.sleep(0.3)
    log.info(f"  [Yelp] -> {len(all_results)} results")
    return all_results


# ── Provider cycling ──────────────────────────────────────────────────────────
PROVIDERS = [
    ("Foursquare", scrape_foursquare),
    ("Yelp",       scrape_yelp),
]


def scrape_with_fallback(keyword, place, job_tags):
    """Try each provider in order; return first non-empty result."""
    for name, fn in PROVIDERS:
        try:
            results = fn(keyword, place, job_tags)
        except Exception as e:
            log.error(f"  [{name}] crashed: {e}")
            results = None
        if results:
            return results
        log.info(f"  [{name}] empty/failed -- trying next provider")
    log.error(f"  All providers failed for '{keyword}' / '{place}'")
    return []


# ── Main agent ────────────────────────────────────────────────────────────────
def run_agent():
    log.info("=" * 60)
    log.info(f"Agent started -- {datetime.utcnow().isoformat()} UTC")
    log.info(f"Providers configured: Foursquare proxy={bool(PROXY_URL)} | Yelp={'yes' if YELP_KEY else 'no'}")
    jobs            = load_jobs()
    existing_phones = get_existing_phones()
    log.info(f"Existing leads in DB: {len(existing_phones)}")
    total_scraped = total_inserted = 0

    for job in jobs:
        log.info(f"Job: '{job['keyword']}' / '{job['place']}'")
        records = scrape_with_fallback(job["keyword"], job["place"], job.get("tags", []))
        inserted_records, count = push_to_supabase(records, existing_phones)
        total_scraped  += len(records)
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
