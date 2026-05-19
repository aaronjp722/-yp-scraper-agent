#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import os
import re
import logging
import schedule
import time
import urllib3
from datetime import datetime, timezone, date
from urllib.parse import urlparse, unquote
from supabase import create_client, Client

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
TABLE_NAME    = os.environ.get("SUPABASE_TABLE", "leads")
PROXY_URL     = os.environ.get("PROXY_URL", "https://blubalances.com/api/foursquare-proxy")
YELP_KEY      = os.environ.get("YELP_API_KEY", "").strip()
HERE_KEY      = os.environ.get("HERE_API_KEY", "").strip()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def utcnow():
    return datetime.now(timezone.utc).isoformat()

FALLBACK_JOBS = [
    {"keyword": "roofing contractors", "place": "Dallas,TX"},
    {"keyword": "roofing contractors", "place": "Houston,TX"},
    {"keyword": "plumbers",            "place": "Dallas,TX"},
    {"keyword": "electricians",        "place": "Dallas,TX"},
    {"keyword": "landscaping",         "place": "Dallas,TX"},
]

EMAIL_SKIP_WORDS = {"noreply", "no-reply", "donotreply", "mailer", "bounce",
                    "support", "help", "admin", "webmaster", "postmaster"}
EMAIL_SKIP_DOMAINS = {"sentry.io", "wixpress.com", "squarespace.com",
                      "shopify.com", "wordpress.com", "example.com"}
DIRECTORY_DOMAINS  = {
    "yelp.com", "yellowpages.com", "bbb.org", "facebook.com", "instagram.com",
    "linkedin.com", "twitter.com", "google.com", "tripadvisor.com", "angi.com",
    "angieslist.com", "homeadvisor.com", "thumbtack.com", "houzz.com",
    "nextdoor.com", "manta.com", "superpages.com", "whitepages.com",
    "foursquare.com", "mapquest.com", "merchantcircle.com", "here.com",
    "bing.com", "yahoo.com", "duckduckgo.com", "chamberofcommerce.com",
}
EMAIL_PATHS = [
    "", "/contact", "/contact-us", "/contactus", "/contact_us",
    "/about", "/about-us", "/aboutus", "/about_us",
    "/get-in-touch", "/reach-us", "/connect", "/hello",
    "/team", "/our-team", "/staff", "/people",
    "/info", "/information", "/services", "/hire-us", "/work-with-us",
]

_geocode_cache: dict = {}

CRAWL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


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
            return [{"keyword": r["keyword"], "place": r["place"], "tags": r.get("tags") or []}
                    for r in resp.data]
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
    new_records = [r for r in records
                   if r.get("telephone") and r["telephone"] not in existing_phones]
    if not new_records:
        log.info("  -> No new records (all duplicates or missing phone)")
        return [], 0
    try:
        resp = supabase.table(TABLE_NAME).insert(new_records).execute()
        inserted = resp.data if resp.data else []
        log.info(f"  -> Inserted {len(inserted)} new leads")
        return inserted, len(inserted)
    except Exception as e:
        log.error(f"Supabase insert failed: {e}")
        return [], 0


def _db_update(telephone, fields: dict):
    try:
        supabase.table(TABLE_NAME).update(fields).eq("telephone", telephone).execute()
    except Exception as e:
        log.error(f"DB update failed for {telephone}: {e}")


# ── Website discovery via DuckDuckGo ─────────────────────────────────────────
def _is_business_url(url: str) -> bool:
    try:
        netloc = urlparse(url).netloc.lower().replace("www.", "")
        return bool(netloc) and not any(
            netloc == d or netloc.endswith("." + d) for d in DIRECTORY_DOMAINS
        )
    except Exception:
        return False


def discover_website(business_name: str, locality: str, region: str = "") -> str | None:
    query = f'"{business_name}" {locality} {region}'.strip()
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=CRAWL_HEADERS,
            timeout=12,
        )
        if resp.status_code != 200:
            return None
        for encoded in re.findall(r'uddg=([^&"\']+)', resp.text)[:8]:
            url = unquote(encoded)
            if url.startswith("http") and _is_business_url(url):
                return url.split("?")[0].rstrip("/")
    except Exception as e:
        log.debug(f"DDG discover failed for '{business_name}': {e}")
    return None


def enrich_websites(inserted_records):
    no_site = [r for r in inserted_records
               if not r.get("website") and r.get("telephone") and r.get("business_name")]
    if not no_site:
        return
    log.info(f"  [WebDiscover] Searching websites for {len(no_site)} leads...")
    found = 0
    for rec in no_site:
        site = discover_website(rec["business_name"],
                                rec.get("locality", ""),
                                rec.get("region", ""))
        if site:
            _db_update(rec["telephone"], {"website": site})
            rec["website"] = site
            found += 1
            log.info(f"  [WebDiscover] {rec['business_name']} -> {site}")
        time.sleep(0.6)
    log.info(f"  [WebDiscover] Found {found}/{len(no_site)} websites")


# ── Email crawler ─────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
OBFUS_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*[\[\(]?\s*at\s*[\]\)]?\s*"
    r"([a-zA-Z0-9.\-]+)\s*[\[\(]?\s*dot\s*[\]\)]?\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)


def _is_valid_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[-1]
    if not re.match(r"^[a-zA-Z]{2,6}$", tld):
        return False
    if any(w in local.lower() for w in EMAIL_SKIP_WORDS):
        return False
    if domain.lower() in EMAIL_SKIP_DOMAINS:
        return False
    if "example" in domain.lower():
        return False
    return True


def _extract_emails(html: str) -> set:
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
    base   = website if "://" in website else "https://" + website
    parsed = urlparse(base.rstrip("/"))
    if not parsed.netloc:
        return None
    netloc = parsed.netloc.lower()
    if any(netloc == d or netloc.endswith("." + d) for d in DIRECTORY_DOMAINS):
        return None

    session = requests.Session()
    session.headers.update(CRAWL_HEADERS)
    found: set = set()

    for path in EMAIL_PATHS:
        try:
            r = session.get(f"{parsed.scheme}://{parsed.netloc}{path}",
                            timeout=8, allow_redirects=True)
            if r.status_code == 200:
                found.update(_extract_emails(r.text))
                if found:
                    break
        except Exception:
            pass
        time.sleep(0.1)

    if not found:
        return None
    preferred = [e for e in found
                 if not any(w in e.split("@")[0]
                            for w in {"info", "contact", "hello", "support"})]
    return min(preferred or list(found), key=len)


def enrich_emails(inserted_records):
    eligible = [r for r in inserted_records if r.get("website") and r.get("telephone")]
    if not eligible:
        return
    log.info(f"  [Email] Crawling {len(eligible)} leads with websites...")
    found = 0
    for rec in eligible:
        email = crawl_email(rec["website"])
        if email:
            _db_update(rec["telephone"], {"email": email})
            found += 1
            log.info(f"  [Email] {rec.get('business_name', '?')} -> {email}")
        time.sleep(0.2)
    log.info(f"  [Email] Found {found}/{len(eligible)} emails")


# ── Provider: Foursquare (Vercel proxy) ──────────────────────────────────────
def scrape_foursquare(keyword, place, job_tags):
    if not PROXY_URL:
        return None
    log.info(f"  [Foursquare] '{keyword}' near '{place}'")
    for attempt in range(3):
        try:
            resp = requests.get(PROXY_URL,
                                params={"query": keyword, "near": place},
                                timeout=20)
            if resp.status_code == 401:
                log.error("  [Foursquare] invalid API key"); return None
            if resp.status_code != 200:
                log.warning(f"  [Foursquare] HTTP {resp.status_code}")
                time.sleep(2 ** attempt); continue
            venues  = resp.json().get("results", [])
            today   = date.today().isoformat()
            results = []
            for v in venues:
                loc  = v.get("location", {})
                tags = list(job_tags)
                for t in [keyword, place, today, "foursquare"]:
                    if t and t not in tags: tags.append(t)
                results.append({
                    "business_name":  v.get("name"),
                    "telephone":      v.get("tel"),
                    "website":        v.get("website"),
                    "street":         loc.get("address"),
                    "locality":       loc.get("locality"),
                    "region":         loc.get("region"),
                    "zipcode":        loc.get("postcode"),
                    "category":       (v.get("categories") or [{}])[0].get("name"),
                    "rating":         v.get("rating"),
                    "business_page":  f"https://foursquare.com/v/{v.get('fsq_id','')}",
                    "listing_url":    f"https://foursquare.com/v/{v.get('fsq_id','')}",
                    "search_keyword": keyword,
                    "search_place":   place,
                    "tags":           tags,
                    "scraped_at":     utcnow(),
                })
            log.info(f"  [Foursquare] -> {len(results)} results")
            return results or None
        except Exception as e:
            log.warning(f"  [Foursquare] attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None


# ── Provider: HERE Maps ───────────────────────────────────────────────────────
HERE_GEOCODE_URL  = "https://geocode.search.hereapi.com/v1/geocode"
HERE_DISCOVER_URL = "https://discover.search.hereapi.com/v1/discover"


def _here_geocode(place: str) -> tuple | None:
    if place in _geocode_cache:
        return _geocode_cache[place]
    try:
        r = requests.get(HERE_GEOCODE_URL,
                         params={"q": place, "apiKey": HERE_KEY}, timeout=10)
        items = r.json().get("items", [])
        if items:
            pos    = items[0]["position"]
            coords = (pos["lat"], pos["lng"])
            _geocode_cache[place] = coords
            return coords
    except Exception as e:
        log.warning(f"  [HERE] geocode failed for '{place}': {e}")
    return None


def scrape_here(keyword, place, job_tags):
    if not HERE_KEY:
        return None
    log.info(f"  [HERE] '{keyword}' near '{place}'")
    coords = _here_geocode(place)
    if not coords:
        log.warning(f"  [HERE] could not geocode '{place}'"); return None

    results, today = [], date.today().isoformat()
    for offset in range(0, 500, 100):
        try:
            r = requests.get(HERE_DISCOVER_URL, params={
                "q":      keyword,
                "at":     f"{coords[0]},{coords[1]}",
                "limit":  100,
                "apiKey": HERE_KEY,
            }, timeout=15)
            if r.status_code != 200:
                log.warning(f"  [HERE] HTTP {r.status_code}"); break
            items = r.json().get("items", [])
            if not items:
                break
            for item in items:
                addr     = item.get("address", {})
                contacts = (item.get("contacts") or [{}])[0]
                phones   = contacts.get("phone", [])
                websites = contacts.get("www", [])
                phone    = phones[0].get("value") if phones else None
                website  = websites[0].get("value") if websites else None
                tags     = list(job_tags)
                for t in [keyword, place, today, "here"]:
                    if t and t not in tags: tags.append(t)
                results.append({
                    "business_name":  item.get("title"),
                    "telephone":      phone,
                    "website":        website,
                    "street":         f"{addr.get('houseNumber','')} {addr.get('street','')}".strip() or None,
                    "locality":       addr.get("city"),
                    "region":         addr.get("stateCode") or addr.get("state"),
                    "zipcode":        addr.get("postalCode"),
                    "category":       (item.get("categories") or [{}])[0].get("name"),
                    "rating":         None,
                    "business_page":  None,
                    "listing_url":    None,
                    "search_keyword": keyword,
                    "search_place":   place,
                    "tags":           tags,
                    "scraped_at":     utcnow(),
                })
            if len(items) < 100:
                break
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"  [HERE] failed: {e}"); break

    log.info(f"  [HERE] -> {len(results)} results "
             f"({sum(1 for r in results if r['website'])} with websites)")
    return results or None


# ── Provider: Yelp ────────────────────────────────────────────────────────────
YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"


def scrape_yelp(keyword, place, job_tags):
    if not YELP_KEY:
        return None
    log.info(f"  [Yelp] '{keyword}' near '{place}'")
    all_biz, offset, today = [], 0, date.today().isoformat()

    while offset < 200:
        for attempt in range(3):
            try:
                resp = requests.get(
                    YELP_SEARCH_URL,
                    headers={"Authorization": f"Bearer {YELP_KEY}"},
                    params={"term": keyword, "location": place,
                            "limit": 50, "offset": offset},
                    timeout=15,
                )
                if resp.status_code == 401:
                    log.error("  [Yelp] invalid API key"); return None
                if resp.status_code != 200:
                    log.warning(f"  [Yelp] HTTP {resp.status_code}")
                    time.sleep(2 ** attempt); continue
                data       = resp.json()
                businesses = data.get("businesses", [])
                total      = data.get("total", 0)
                all_biz.extend(businesses)
                offset += len(businesses)
                if offset >= total or not businesses:
                    offset = 9999
                break
            except Exception as e:
                log.warning(f"  [Yelp] attempt {attempt+1}: {e}")
                time.sleep(2 ** attempt)
        time.sleep(0.3)

    results = []
    for biz in all_biz:
        loc  = biz.get("location", {})
        tags = list(job_tags)
        for t in [keyword, place, today, "yelp"]:
            if t and t not in tags: tags.append(t)
        results.append({
            "business_name":  biz.get("name"),
            "telephone":      biz.get("phone") or biz.get("display_phone"),
            "website":        None,  # Yelp search API doesn't return business websites
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
            "scraped_at":     utcnow(),
        })

    log.info(f"  [Yelp] -> {len(results)} results")
    return results or None


# ── Provider cycling ──────────────────────────────────────────────────────────
PROVIDERS = [
    ("Foursquare", scrape_foursquare),
    ("HERE",       scrape_here),
    ("Yelp",       scrape_yelp),
]


def scrape_with_fallback(keyword, place, job_tags):
    all_results, seen_phones = [], set()
    for name, fn in PROVIDERS:
        try:
            results = fn(keyword, place, job_tags)
        except Exception as e:
            log.error(f"  [{name}] crashed: {e}"); results = None
        if not results:
            log.info(f"  [{name}] empty/failed -- trying next provider")
            continue
        new = [r for r in results
               if r.get("telephone") and r["telephone"] not in seen_phones]
        seen_phones.update(r["telephone"] for r in new if r.get("telephone"))
        all_results.extend(new)
        log.info(f"  [{name}] contributed {len(new)} unique results")
    if not all_results:
        log.error(f"  All providers failed for '{keyword}' / '{place}'")
    return all_results


# ── Main agent ────────────────────────────────────────────────────────────────
def run_agent():
    log.info("=" * 60)
    log.info(f"Agent started -- {utcnow()} UTC")
    log.info(f"Providers: Foursquare={bool(PROXY_URL)} | HERE={'yes' if HERE_KEY else 'no'} | Yelp={'yes' if YELP_KEY else 'no'}")
    jobs            = load_jobs()
    existing_phones = get_existing_phones()
    log.info(f"Existing leads in DB: {len(existing_phones)}")
    total_scraped = total_inserted = 0

    for job in jobs:
        log.info(f"Job: '{job['keyword']}' / '{job['place']}'")
        records                    = scrape_with_fallback(job["keyword"], job["place"], job.get("tags", []))
        inserted_records, count    = push_to_supabase(records, existing_phones)
        total_scraped  += len(records)
        total_inserted += count
        existing_phones.update(r["telephone"] for r in records if r.get("telephone"))
        enrich_websites(inserted_records)   # find websites for leads missing them
        enrich_emails(inserted_records)     # crawl websites for emails

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
