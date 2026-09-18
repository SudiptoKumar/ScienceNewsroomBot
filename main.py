import os
import re
import json
import time
import html
import argparse
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, urljoin
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from io import BytesIO

import requests
import trafilatura
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageFile
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from exa_py import Exa
from cerebras.cloud.sdk import Cerebras


# ============================================================
# CONFIGURATION
# ============================================================

EXA_API_KEY = os.environ["EXA_API_KEY"]
CEREBRAS_API_KEY = os.environ["CEREBRAS_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

TELEGRAM_CHANNEL = (os.environ.get("TELEGRAM_CHANNEL") or "@CareerNewsroom").strip()
TELEGRAM_ADMIN_CHAT_ID = (os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or "").strip()

CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b")
POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"
BD_TZ = ZoneInfo("Asia/Dhaka")

# Final publication rule: no fixed lower quota; publish every verified
# candidate up to 15, targeting at least 5 whenever >=5 genuine jobs exist.
MIN_STORIES_PER_RUN = 5
MAX_STORIES_PER_RUN = 15
RANKING_POOL_SIZE = 40
DISCOVERY_LOOKBACK_DAYS = 7
ACTIVE_JOB_RETENTION_DAYS = 30
POST_DELAY_SECONDS = 3.5
FUTURE_TOLERANCE_MINUTES = 20
MAX_EXA_CANDIDATES = 100
MAX_DOHAJ_CANDIDATES = 40
MAX_RICH_CHARACTERS = 32768
MAX_JOB_CONTENT_CHARS = 18000

BDJOBS_DOMAINS = ["bdjobs.com", "jobs.bdjobs.com"]
DOHAJ_DOMAIN = "dohaj.com"

DOHAJ_TOP5_URLS = [
    "https://dohaj.com/category/accounting-finance",
    "https://dohaj.com/category/marketing-sales",
    "https://dohaj.com/category/hr-org-development",
    "https://dohaj.com/category/gen-mgt-admin",
    "https://dohaj.com/category/commercial",
    "https://dohaj.com/category/supply-chain-procurement",
    "https://dohaj.com/category/bank-non-bank-fin-institution",
    "https://dohaj.com/gov-jobs",
]

DOHAJ_CATEGORY_NAMES = {
    "accounting-finance": "Accounting/Finance",
    "marketing-sales": "Marketing/Sales",
    "hr-org-development": "HR/Org. Development",
    "gen-mgt-admin": "General Management/Admin",
    "commercial": "Commercial",
    "supply-chain-procurement": "Supply Chain/Procurement",
    "bank-non-bank-fin-institution": "Bank/Non-Bank Fin. Institution",
    "gov-jobs": "Government Jobs",
}

# The working ScienceNewsroom architecture uses one global ranked pool.
# We retain the same approach, but the candidate universe is now only BDjobs
# and the explicitly allowed Dohaj category pages.
BBA_MBA_TERMS = (
    "bba", "mba", "bachelor of business administration", "master of business administration",
    "business administration", "business studies", "bbs", "mbs", "commerce", "finance",
    "accounting", "marketing", "human resources", "hrm", "management", "banking",
    "business development", "supply chain", "commercial", "operations", "economics",
)
EARLY_CAREER_TERMS = (
    "fresher", "freshers", "no experience", "entry level", "entry-level", "trainee",
    "management trainee", "graduate trainee", "graduate program", "intern", "internship",
    "0-1", "0 to 1", "0-2", "0 to 2", "0-3", "0 to 3", "1-2", "1 to 2", "1-3", "1 to 3",
)
TARGET_FUNCTION_TERMS = (
    "account", "finance", "audit", "tax", "bank", "relationship", "credit", "treasury",
    "marketing", "sales", "brand", "hr", "human resource", "recruitment", "business development",
    "management", "commercial", "procurement", "supply chain", "operations", "admin", "analyst",
    "customer service", "merchandising", "corporate affairs",
)
NOISE_TITLE_TERMS = (
    "calculator", "quiz", "mcq", "question solution", "answer key", "exam result", "admission",
    "scholarship", "career advice", "cv writing", "resume tips", "interview tips", "salary calculator",
    "course", "training course", "webinar", "seminar", "job fair", "how to get a job", "job preparation",
)
SENIOR_TERMS = (
    "chief", "cfo", "ceo", "director", "head of", "general manager", "agm", "dgm", "senior manager",
    "vice president", "vp ", "8 years", "9 years", "10 years", "10+ years", "15 years", "20 years",
)

SOURCE_NAMES = {
    "bdjobs.com": "Bdjobs",
    "jobs.bdjobs.com": "Bdjobs",
    "dohaj.com": "Dohaj",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("career-news-bot")

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 50_000_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)
retry_policy = Retry(
    total=4,
    connect=4,
    read=4,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=True,
)
session.mount(
    "https://",
    HTTPAdapter(max_retries=retry_policy, pool_connections=20, pool_maxsize=20),
)
session.mount(
    "http://",
    HTTPAdapter(max_retries=retry_policy, pool_connections=20, pool_maxsize=20),
)


# ============================================================
# HELPERS
# ============================================================

def safe_text(value):
    return "" if value is None else str(value).strip()


def canonical_url(url):
    raw = safe_text(url)
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path or "/"
    path = path.rstrip("/")
    query = parsed.query
    # Preserve meaningful query parameters for application URLs; discard common tracking noise.
    if query:
        kept = []
        for part in query.split("&"):
            key = part.split("=", 1)[0].lower()
            if key not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}:
                kept.append(part)
        query = "&".join(kept)
    return f"{host}{path}" + (f"?{query}" if query else "")


def normalize_title(title):
    text = safe_text(title).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(text):
    return {x for x in normalize_title(text).split() if len(x) >= 3}


def title_similarity(a, b):
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    aa, bb = title_tokens(na), title_tokens(nb)
    jac = len(aa & bb) / max(1, len(aa | bb))
    return max(seq, jac)


def parse_datetime(value):
    raw = safe_text(value)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BD_TZ)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BD_TZ)
    except Exception:
        return None


def now_iso():
    return datetime.now(BD_TZ).isoformat()


def source_name(url):
    domain = urlparse(safe_text(url)).netloc.lower().removeprefix("www.")
    return SOURCE_NAMES.get(domain, domain or "Source")


def normalized_domain(url):
    raw = safe_text(url).lower()
    if "://" in raw:
        raw = urlparse(raw).netloc
    return raw.split(":")[0].removeprefix("www.").strip().rstrip("/")


def is_domain_allowed(url, domains):
    domain = normalized_domain(url)
    return any(domain == d or domain.endswith("." + d) for d in domains)


def trim_source_text(text, limit):
    text = safe_text(text)
    if len(text) <= limit:
        return text
    text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,:;-/—")
    return text


def clean_generated_text(text):
    text = safe_text(text)
    text = re.sub(r"\*{1,3}", "", text)
    text = re.sub(r"`{1,3}", "", text)
    text = re.sub(r"\.{2,}", ".", text)
    return text.replace("\u2026", "").strip()


def is_noise_title(title, url=""):
    blob = f"{safe_text(title)} {safe_text(url)}".lower()
    return any(term in blob for term in NOISE_TITLE_TERMS)


def is_index_url(url):
    path = urlparse(safe_text(url)).path.lower().rstrip("/")
    if "/category/" in path or path.endswith("/gov-jobs") or "/search" in path:
        return True
    return "/job-details/" not in path and path.count("/") <= 2


def is_vacancy_url(url):
    path = urlparse(safe_text(url)).path.lower()
    if "/job-details/" in path:
        return True
    if "bdjobs" in normalized_domain(url) and any(x in path for x in ("/job", "/jobs", "joblist", "job-details")):
        return True
    return False


def job_family_score(title, text):
    blob = f"{title} {text}".lower()
    score = 0
    score += min(32, sum(4 for x in TARGET_FUNCTION_TERMS if x in blob))
    score += min(30, sum(5 for x in BBA_MBA_TERMS if x in blob))
    score += min(20, sum(4 for x in EARLY_CAREER_TERMS if x in blob))
    if any(x in safe_text(title).lower() for x in SENIOR_TERMS):
        score -= 18
    return max(0, min(100, score))


def job_event_key(job):
    base = "|".join([
        normalize_title(job.get("title", "")),
        normalize_title(job.get("company", "")),
        normalize_title(job.get("location", "")),
    ])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


def likely_same_job(a, b):
    if title_similarity(a.get("title", ""), b.get("title", "")) >= 0.92:
        ca = normalize_title(a.get("company", ""))
        cb = normalize_title(b.get("company", ""))
        if ca and cb and SequenceMatcher(None, ca, cb).ratio() >= 0.85:
            return True
        if a.get("source") == b.get("source"):
            return True
    return False


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "queue": {},
        "events": {},
        "recent_titles": [],
        "last_run": "",
    }


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        base = default_state()
        if isinstance(data, dict):
            base.update(data)
        return base
    except Exception:
        return default_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def load_posted_urls():
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            return {canonical_url(x) for x in f if safe_text(x)}
    except FileNotFoundError:
        return set()


def save_posted_url(canonical):
    if not canonical:
        return
    with open(POSTED_FILE, "a", encoding="utf-8") as f:
        f.write(canonical + "\n")


STATE = load_state()
POSTED_URLS = load_posted_urls()


def prune_state():
    cutoff = datetime.now(BD_TZ) - timedelta(days=ACTIVE_JOB_RETENTION_DAYS)
    queue = {}
    for key, item in STATE.get("queue", {}).items():
        dt = parse_datetime(item.get("last_seen") or item.get("posted_at") or item.get("discovered_at"))
        if dt and dt >= cutoff and item.get("status") != "expired":
            queue[key] = item
    STATE["queue"] = queue
    events = {}
    for key, item in STATE.get("events", {}).items():
        dt = parse_datetime(item.get("published_at") or item.get("selected_at"))
        if dt and dt >= cutoff:
            events[key] = item
    STATE["events"] = events
    STATE["recent_titles"] = STATE.get("recent_titles", [])[-400:]


# ============================================================
# CLIENTS
# ============================================================

exa = None
cerebras = None


def get_exa():
    global exa
    if exa is None:
        exa = Exa(api_key=EXA_API_KEY)
    return exa


def get_cerebras():
    global cerebras
    if cerebras is None:
        cerebras = Cerebras(api_key=CEREBRAS_API_KEY)
    return cerebras


# ============================================================
# SOURCE DISCOVERY: DOHAJ TOP-5 ONLY
# ============================================================

def extract_dohaj_top5(category_url, category_name):
    """Read one exact Dohaj category URL and keep only its first 5 job-details links."""
    try:
        response = session.get(category_url, headers=HEADERS, timeout=30)
        if response.status_code >= 400:
            logger.warning("DOHAJ category failed %s HTTP=%s", category_url, response.status_code)
            return []
        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        seen = set()
        for anchor in soup.find_all("a", href=True):
            href = urljoin(response.url, safe_text(anchor.get("href")))
            if not is_domain_allowed(href, [DOHAJ_DOMAIN]):
                continue
            path = urlparse(href).path.lower()
            if "/job-details/" not in path:
                continue
            canonical = canonical_url(href)
            if not canonical or canonical in seen:
                continue
            title = safe_text(anchor.get_text(" ", strip=True))
            if not title or is_noise_title(title, href):
                continue
            seen.add(canonical)
            results.append({
                "title": title,
                "url": href,
                "canonical": canonical,
                "source": "Dohaj",
                "source_url": href,
                "discovery": "dohaj_top5",
                "dohaj_category": category_name,
                "discovered_at": now_iso(),
            })
            if len(results) >= 5:
                break
        logger.info("DOHAJ TOP5 | %s | %d", category_name, len(results))
        return results
    except Exception as exc:
        logger.warning("DOHAJ category error %s: %s", category_url, exc)
        return []


def discover_dohaj():
    results = []
    for url in DOHAJ_TOP5_URLS:
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        category_name = DOHAJ_CATEGORY_NAMES.get(slug, slug)
        results.extend(extract_dohaj_top5(url, category_name))
    return results[:MAX_DOHAJ_CANDIDATES]


# ============================================================
# SOURCE DISCOVERY: BDJOBS VIA EXA
# ============================================================

BDJOBS_QUERIES = [
    "site:bdjobs.com BBA MBA Bangladesh jobs recent",
    "site:bdjobs.com management trainee graduate trainee Bangladesh",
    "site:bdjobs.com finance accounting banking jobs BBA MBA Bangladesh",
    "site:bdjobs.com marketing sales HR jobs BBA MBA Bangladesh",
    "site:bdjobs.com business development commercial operations supply chain Bangladesh",
    "site:bdjobs.com internship BBA MBA Bangladesh jobs",
    "site:bdjobs.com fresher entry level Bangladesh jobs BBA MBA",
    "site:bdjobs.com government business finance management jobs Bangladesh",
]


def _exa_search(query, num_results=12):
    exa_client = get_exa()
    # Keep the proven reference behavior first. If the installed SDK exposes
    # the modern split API only, use search()+get_contents() compatibility.
    if hasattr(exa_client, "search_and_contents"):
        return exa_client.search_and_contents(
            query,
            type="auto",
            num_results=num_results,
            include_domains=BDJOBS_DOMAINS,
            start_published_date=DISCOVERY_START.isoformat(),
            end_published_date=DISCOVERY_END.isoformat(),
            contents={"text": {"max_characters": 1800}, "highlights": {"max_characters": 900}},
        )

    search_result = exa_client.search(
        query,
        type="auto",
        num_results=num_results,
        include_domains=BDJOBS_DOMAINS,
        start_published_date=DISCOVERY_START.isoformat(),
        end_published_date=DISCOVERY_END.isoformat(),
        contents={"text": {"max_characters": 1800}, "highlights": {"max_characters": 900}},
    )
    return search_result


def discover_bdjobs():
    discovered = []
    seen = set()
    for query in BDJOBS_QUERIES:
        try:
            results = _exa_search(query, num_results=12)
            for result in getattr(results, "results", []) or []:
                url = safe_text(getattr(result, "url", ""))
                title = safe_text(getattr(result, "title", ""))
                if not url or not title or not is_domain_allowed(url, BDJOBS_DOMAINS):
                    continue
                if is_index_url(url) or is_noise_title(title, url):
                    continue
                canonical = canonical_url(url)
                if not canonical or canonical in seen or canonical in POSTED_URLS:
                    continue
                published = parse_datetime(getattr(result, "published_date", ""))
                excerpt_values = getattr(result, "highlights", []) or []
                excerpt = " ".join(excerpt_values) if isinstance(excerpt_values, list) else safe_text(excerpt_values)
                if not excerpt:
                    excerpt = safe_text(getattr(result, "text", ""))
                seen.add(canonical)
                discovered.append({
                    "title": title,
                    "url": url,
                    "canonical": canonical,
                    "source": "Bdjobs",
                    "source_url": url,
                    "published_at_exa": published.isoformat() if published else "",
                    "excerpt": trim_source_text(excerpt, 2400),
                    "research_text": safe_text(getattr(result, "text", ""))[:MAX_JOB_CONTENT_CHARS],
                    "image": safe_text(getattr(result, "image", "")),
                    "discovery": "exa_bdjobs",
                    "discovered_at": now_iso(),
                })
                if len(discovered) >= MAX_EXA_CANDIDATES:
                    return discovered
        except Exception as exc:
            logger.warning("BDJOBS Exa query failed: %s | %s", query, exc)
    logger.info("BDJOBS EXA DISCOVERY: %d", len(discovered))
    return discovered


def discover_all():
    dohaj = discover_dohaj()
    bdjobs = discover_bdjobs()
    all_items = []
    seen = set()
    for item in dohaj + bdjobs:
        canonical = item["canonical"]
        if canonical in seen:
            continue
        seen.add(canonical)
        all_items.append(item)
    logger.info("DISCOVERED | Dohaj=%d | Bdjobs=%d | merged=%d", len(dohaj), len(bdjobs), len(all_items))
    return all_items


# ============================================================
# RETRIEVAL / EXTRACTION
# ============================================================

def cache_key(url):
    return hashlib.sha256((canonical_url(url) + "|job").encode()).hexdigest()[:24]


def _text_from_html(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def _jsonld_objects(page_html):
    objects = []
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        objects.extend(payload if isinstance(payload, list) else [payload])
    return [x for x in objects if isinstance(x, dict)]


def _jobposting_jsonld(page_html):
    for obj in _jsonld_objects(page_html):
        typ = obj.get("@type")
        types = typ if isinstance(typ, list) else [typ]
        if any(safe_text(x).lower() == "jobposting" for x in types):
            return obj
    return {}


def _extract_links(page_html, base_url):
    links = []
    soup = BeautifulSoup(page_html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, safe_text(anchor.get("href")))
        if urlparse(href).scheme not in {"http", "https"}:
            continue
        text = safe_text(anchor.get_text(" ", strip=True))
        links.append({"url": href, "text": text})
    return links


def _apply_link_score(link, source_domain):
    url = safe_text(link.get("url"))
    text = safe_text(link.get("text")).lower()
    blob = f"{text} {url.lower()}"
    score = 0
    exact = [
        "apply now", "apply online", "submit application", "application form",
        "apply", "আবেদন করুন", "আবেদন", "apply for this job", "career apply",
    ]
    for term in exact:
        if term in text:
            score += 80 if term in {"apply now", "apply online", "submit application", "application form"} else 55
            break
    for term in ("/apply", "apply?", "application", "/career", "careers.", "jobs.lever", "greenhouse", "workday", "smartrecruiters"):
        if term in blob:
            score += 35
    domain = normalized_domain(url)
    if domain and domain != source_domain and not domain.endswith("." + source_domain):
        score += 30
    if any(x in domain for x in ("facebook.com", "youtube.com", "instagram.com", "linkedin.com", "t.me")):
        score -= 100
    if "/job-details/" in urlparse(url).path.lower():
        score -= 100
    return score


def extract_apply_url(page_html, page_url, source):
    source_domain = normalized_domain(page_url)
    candidates = _extract_links(page_html, page_url)
    scored = sorted(
        ((
            _apply_link_score(link, source_domain),
            link,
        ) for link in candidates),
        key=lambda x: x[0],
        reverse=True,
    )
    for score, link in scored:
        if score < 70:
            continue
        target = safe_text(link["url"])
        if source == "Dohaj" and is_domain_allowed(target, [DOHAJ_DOMAIN]):
            # Dohaj itself is the details/source layer, not the original third-party application.
            continue
        return target
    return ""


def _label_value(text, labels):
    lines = [x.strip() for x in safe_text(text).splitlines() if x.strip()]
    label_set = {x.lower() for x in labels}
    for i, line in enumerate(lines):
        low = line.lower().rstrip(":")
        for label in labels:
            prefix = label.lower().rstrip(":")
            if low == prefix and i + 1 < len(lines):
                return lines[i + 1]
            if low.startswith(prefix + ":"):
                return line.split(":", 1)[1].strip()
    return ""


def _regex_value(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.M)
        if match:
            return safe_text(match.group(1))
    return ""


def normalize_date_text(value):
    raw = safe_text(value)
    if not raw:
        return ""
    parsed = parse_datetime(raw)
    if parsed:
        return parsed.date().isoformat()
    for fmt in ("%d %b %Y", "%d %B %Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except Exception:
            pass
    return raw


def extract_job_fields(text, page_html, source_url, discovery_item):
    text = safe_text(text)
    jsonld = _jobposting_jsonld(page_html) if page_html else {}

    title = safe_text(jsonld.get("title")) or _label_value(text, ["Title", "Job Title", "Position", "Post Name"])
    company = ""
    hiring = jsonld.get("hiringOrganization")
    if isinstance(hiring, dict):
        company = safe_text(hiring.get("name"))
    company = company or _label_value(text, ["Company Name", "Company", "Organization Name", "Employer"])
    location = _label_value(text, ["Job Location", "Location", "Job Location(s)", "Work Location"])
    salary = _label_value(text, ["Salary", "Salary Range", "Minimum Salary", "Compensation"])
    experience = _label_value(text, ["Experience", "Experience Requirements", "Experience Requirement"])
    education = _label_value(text, ["Education", "Educational Requirements", "Educational Qualification", "Education Requirements"])
    vacancy = _label_value(text, ["Vacancy", "No. of Vacancy", "Number of Vacancy", "Positions"])
    employment = _label_value(text, ["Employment Status", "Job Type", "Employment Type"])
    workplace = _label_value(text, ["Job Work Place", "Workplace", "Work Place"])
    age = _label_value(text, ["Age", "Age Limit", "Age Requirements"])
    category = _label_value(text, ["Category", "Job Category"])
    application_method = _label_value(text, ["Application", "Application Process", "How to Apply", "Read Before Apply"])

    published = _label_value(text, ["Published", "Posted", "Date Posted", "Publication Date"])
    deadline = _label_value(text, ["Application Deadline", "Deadline", "Last Date", "Apply Before"])

    if jsonld:
        if not published and jsonld.get("datePosted"):
            published = safe_text(jsonld.get("datePosted"))
        if not deadline and jsonld.get("validThrough"):
            deadline = safe_text(jsonld.get("validThrough"))
        if not employment and jsonld.get("employmentType"):
            employment = ", ".join(jsonld.get("employmentType")) if isinstance(jsonld.get("employmentType"), list) else safe_text(jsonld.get("employmentType"))
        if not salary and isinstance(jsonld.get("baseSalary"), dict):
            base = jsonld["baseSalary"]
            value = base.get("value") if isinstance(base.get("value"), dict) else base.get("value")
            currency = safe_text(base.get("currency"))
            if value:
                salary = f"{currency} {value}".strip()
        jl = jsonld.get("jobLocation")
        if not location and isinstance(jl, dict):
            addr = jl.get("address")
            if isinstance(addr, dict):
                location = ", ".join(safe_text(x) for x in (addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")) if safe_text(x))
            elif isinstance(addr, str):
                location = addr

    title = title or discovery_item.get("title", "")
    company = company or discovery_item.get("company", "")
    published_iso = normalize_date_text(published or discovery_item.get("published_at_exa", ""))
    deadline_iso = normalize_date_text(deadline)

    return {
        "title": title,
        "company": company,
        "location": location,
        "salary": salary,
        "experience": experience,
        "education": education,
        "vacancy": vacancy,
        "employment_type": employment,
        "workplace": workplace,
        "age": age,
        "category": category,
        "application_method": application_method,
        "posted_date": published_iso,
        "deadline": deadline_iso,
        "source": discovery_item.get("source") or source_name(source_url),
        "source_url": source_url,
        "url": source_url,
        "discovery": discovery_item.get("discovery", ""),
        "dohaj_category": discovery_item.get("dohaj_category", ""),
    }


def retrieve_job_content(item):
    url = item["url"]
    # First use the exact working architecture: direct HTTP + Trafilatura.
    try:
        response = session.get(url, headers={**HEADERS, "Referer": url}, timeout=30)
        if response.status_code < 400:
            page_html = response.text
            text = trafilatura.extract(
                page_html,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
            )
            raw_text = text or _text_from_html(page_html)
            if raw_text and len(raw_text) >= 400:
                apply_url = extract_apply_url(page_html, response.url, item.get("source", ""))
                image_candidates = find_image_candidates(response.url, page_html, response.url)
                return {
                    "text": raw_text[:MAX_JOB_CONTENT_CHARS],
                    "html": page_html,
                    "final_url": response.url,
                    "apply_url": apply_url,
                    "image_candidates": image_candidates,
                    "backend": "direct_http",
                }
    except Exception as exc:
        logger.warning("Direct retrieval failed %s: %s", url, exc)

    # Exa fallback for blocked/thin pages.
    try:
        result_set = get_exa().get_contents(
            [url],
            text={"max_characters": MAX_JOB_CONTENT_CHARS},
            max_age_hours=24,
        )
        if getattr(result_set, "results", None):
            result = result_set.results[0]
            text = safe_text(getattr(result, "text", ""))
            if text:
                image = safe_text(getattr(result, "image", ""))
                return {
                    "text": text[:MAX_JOB_CONTENT_CHARS],
                    "html": "",
                    "final_url": safe_text(getattr(result, "url", "")) or url,
                    "apply_url": "",
                    "image_candidates": [image] if image else [],
                    "backend": "exa_contents",
                }
    except Exception as exc:
        logger.warning("Exa contents fallback failed %s: %s", url, exc)
    return None


def research_job(item):
    cached_text = safe_text(item.get("research_text", ""))
    if cached_text and len(cached_text) >= 500:
        retrieved = {
            "text": cached_text[:MAX_JOB_CONTENT_CHARS],
            "html": "",
            "final_url": item.get("url", ""),
            "apply_url": "",
            "image_candidates": [item.get("image", "")] if item.get("image") else [],
            "backend": "exa_search_contents",
        }
    else:
        retrieved = retrieve_job_content(item)
    if not retrieved:
        return None
    fields = extract_job_fields(
        retrieved["text"],
        retrieved.get("html", ""),
        item["url"],
        item,
    )
    apply_url = retrieved.get("apply_url", "")
    if not apply_url and fields["application_method"]:
        # If the page itself states a direct URL, capture it safely.
        urls = re.findall(r"https?://[^\s<>]+", fields["application_method"])
        for candidate in urls:
            if item.get("source") != "Dohaj" or not is_domain_allowed(candidate, [DOHAJ_DOMAIN]):
                apply_url = candidate.rstrip(".,)")
                break

    fields.update({
        "canonical": item["canonical"],
        "apply_url": apply_url,
        "retrieval_backend": retrieved.get("backend", ""),
        "image_candidates": retrieved.get("image_candidates", []),
        "raw_text": retrieved["text"],
    })
    if not fields["title"] or not fields["company"]:
        # Don't silently fabricate identity. A BDjobs/Dohaj job page title may still
        # be usable if company is visible in a source card, so retain source title fallback.
        fields["title"] = fields["title"] or item.get("title", "")
        fields["company"] = fields["company"] or item.get("company", "")
    fields["audience_pre_score"] = job_family_score(fields["title"], retrieved["text"][:6000])
    fields["event_id"] = job_event_key(fields)
    return fields


# ============================================================
# FRESHNESS / DEADLINE
# ============================================================

DISCOVERY_END = datetime.now(BD_TZ) + timedelta(minutes=FUTURE_TOLERANCE_MINUTES)
DISCOVERY_START = datetime.now(BD_TZ) - timedelta(days=DISCOVERY_LOOKBACK_DAYS)


def deadline_status(job):
    raw = safe_text(job.get("deadline"))
    if not raw:
        return "unknown"
    dt = parse_datetime(raw)
    if not dt:
        try:
            dt = datetime.fromisoformat(raw).replace(tzinfo=BD_TZ)
        except Exception:
            return "unknown"
    return "expired" if dt < datetime.now(BD_TZ) else "active"


def posted_freshness_score(job):
    dt = parse_datetime(job.get("posted_date"))
    if not dt:
        return 20
    age_hours = max(0, (datetime.now(BD_TZ) - dt).total_seconds() / 3600)
    if age_hours <= 24:
        return 30
    if age_hours <= 72:
        return 25
    if age_hours <= 120:
        return 18
    if age_hours <= 168:
        return 12
    return 4


def deterministic_job_gate(job):
    if not job.get("title") or not job.get("company"):
        return False, "missing_identity"
    if is_noise_title(job["title"], job.get("source_url", "")):
        return False, "noise_title"
    if deadline_status(job) == "expired":
        return False, "expired"
    if not (is_domain_allowed(job.get("source_url", ""), BDJOBS_DOMAINS) or is_domain_allowed(job.get("source_url", ""), [DOHAJ_DOMAIN])):
        return False, "source_not_allowed"
    # A job can survive missing salary/education/experience. It only needs enough identity
    # and audience signal to reach the Cerebras editorial judge.
    if job.get("audience_pre_score", 0) < 12:
        return False, "low_business_relevance"
    return True, "ok"


# ============================================================
# CEREBRAS EDITORIAL JUDGE
# ============================================================

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "publish": {"type": "boolean"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "bba_mba_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "early_career_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "role_fit": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string"},
                },
                "required": ["id", "publish", "score", "bba_mba_fit", "early_career_fit", "role_fit", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _judge_prompt():
    return """
You are the final editorial judge for @CareerNewsroom.

Audience: Bangladesh young adults around 20-30, especially BBA/MBA students,
graduates, freshers and early-career professionals.

Judge EACH job independently using only the supplied source-backed facts.
Do not invent missing information. Missing salary, education or experience is allowed.
Hard-reject only a non-vacancy, expired/closed job, non-Bangladesh job, malformed identity,
or a job that is clearly unsuitable for a business/management early-career audience.

Strongly favor:
- BBA/MBA and related business degrees
- Management Trainee / Graduate Trainee
- internships
- fresher / 0-3 year roles
- finance/accounting/banking
- marketing/sales
- HR
- business development
- management/admin
- commercial
- supply chain/procurement
- operations
- analyst / relationship / credit / customer-facing business roles

Scoring guide:
90-100 exceptional direct fit for the target audience
80-89 very strong target fit
70-79 good target fit
60-69 usable but less direct
50-59 borderline
0-49 normally do not publish

The overall score must reflect audience fit, role relevance, accessibility to an early-career candidate,
source-backed quality, active deadline, and useful job information.
Do not rank senior specialist roles highly merely because they mention MBA.
Return every input candidate.
"""


def judge_batch(batch, batch_no):
    payload_parts = []
    for idx, job in enumerate(batch, start=1):
        payload_parts.append("\n".join([
            f"ID: {idx}",
            f"Source: {job.get('source','')}",
            f"Title: {job.get('title','')}",
            f"Company: {job.get('company','')}",
            f"Category: {job.get('category','')}",
            f"Location: {job.get('location','')}",
            f"Employment: {job.get('employment_type','')}",
            f"Education: {trim_source_text(job.get('education',''), 1200)}",
            f"Experience: {trim_source_text(job.get('experience',''), 700)}",
            f"Salary: {job.get('salary','')}",
            f"Vacancy: {job.get('vacancy','')}",
            f"Age: {job.get('age','')}",
            f"Posted: {job.get('posted_date','')}",
            f"Deadline: {job.get('deadline','')}",
            f"Application: {trim_source_text(job.get('application_method',''), 900)}",
            f"Business relevance pre-score: {job.get('audience_pre_score',0)}",
            f"Source text evidence: {trim_source_text(job.get('raw_text',''), 2600)}",
            "",
        ]))
    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": _judge_prompt()},
                {"role": "user", "content": "\n".join(payload_parts)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": f"career_job_judgment_{batch_no}",
                    "strict": True,
                    "schema": JUDGE_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=3500,
        )
        return json.loads(safe_text(response.choices[0].message.content)).get("results", [])
    except Exception as exc:
        logger.error("CEREBRAS judge batch %d failed: %s", batch_no, exc)
        return []


def rank_jobs(jobs):
    if not jobs:
        return []
    # Deterministic pre-ranking keeps the LLM pool bounded without killing valid but incomplete jobs.
    pre_ranked = sorted(
        jobs,
        key=lambda j: (
            -j.get("audience_pre_score", 0),
            -posted_freshness_score(j),
            1 if deadline_status(j) == "active" else 0,
        ),
    )[:RANKING_POOL_SIZE]

    judged = []
    for offset in range(0, len(pre_ranked), 15):
        batch = pre_ranked[offset:offset + 15]
        logger.info("CEREBRAS JUDGE BATCH %d | %d jobs", offset // 15 + 1, len(batch))
        rows = judge_batch(batch, offset // 15 + 1)
        by_id = {i: job for i, job in enumerate(batch, start=1)}
        for row in rows:
            try:
                idx = int(row.get("id"))
            except Exception:
                continue
            if idx not in by_id:
                continue
            job = dict(by_id[idx])
            job.update({
                "judge_publish": bool(row.get("publish")),
                "judge_score": int(row.get("score", 0)),
                "bba_mba_fit": int(row.get("bba_mba_fit", 0)),
                "early_career_fit": int(row.get("early_career_fit", 0)),
                "role_fit": int(row.get("role_fit", 0)),
                "judge_reason": safe_text(row.get("reason")),
            })
            judged.append(job)

    # Stable local tie-break after Cerebras.
    judged.sort(
        key=lambda j: (
            -j.get("judge_score", 0),
            -j.get("bba_mba_fit", 0),
            -j.get("early_career_fit", 0),
            -posted_freshness_score(j),
        )
    )
    return judged


# ============================================================
# STATE / DEDUP / SELECTION
# ============================================================

def save_job_to_queue(job):
    key = job["canonical"]
    existing = STATE["queue"].get(key, {})
    existing.update(job)
    existing.setdefault("status", "pending")
    existing.setdefault("first_seen", now_iso())
    existing["last_seen"] = now_iso()
    STATE["queue"][key] = existing


def candidate_already_posted(job):
    canonical = canonical_url(job.get("source_url", ""))
    if canonical in POSTED_URLS:
        return True
    event_id = job.get("event_id")
    event = STATE.get("events", {}).get(event_id, {})
    return event.get("status") == "published"


def build_unique_job_pool(jobs):
    unique = []
    for job in jobs:
        if candidate_already_posted(job):
            continue
        if any(likely_same_job(job, previous) for previous in unique):
            continue
        unique.append(job)
    return unique


def select_final_jobs(ranked):
    selected = []
    for job in ranked:
        if not job.get("judge_publish"):
            continue
        if job.get("judge_score", 0) < 60:
            continue
        if deadline_status(job) == "expired":
            continue
        selected.append(job)
        if len(selected) >= MAX_STORIES_PER_RUN:
            break
    return selected


def store_selected_event(job, published=False, message_id=None):
    event_id = job.get("event_id") or job_event_key(job)
    STATE["events"][event_id] = {
        "event_id": event_id,
        "canonical_url": job.get("canonical", ""),
        "source_url": job.get("source_url", ""),
        "apply_url": job.get("apply_url", ""),
        "source": job.get("source", ""),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "score": job.get("judge_score", 0),
        "status": "published" if published else "selected",
        "selected_at": now_iso(),
        "published_at": now_iso() if published else "",
        "message_id": message_id,
    }
    return event_id


# ============================================================
# IMAGE PIPELINE: COPIED FROM WORKING NEWSROOM ARCHITECTURE
# ============================================================

def _unique_image_urls(urls, base_url=""):
    seen, result = set(), []
    for value in urls:
        raw = safe_text(value)
        if not raw:
            continue
        absolute = urljoin(base_url or "", raw)
        if urlparse(absolute).scheme not in {"http", "https"}:
            continue
        key = absolute.split("#", 1)[0]
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _jsonld_image_values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out = []
        for x in value:
            out.extend(_jsonld_image_values(x))
        return out
    if isinstance(value, dict):
        out = []
        for key in ("url", "contentUrl", "image"):
            if key in value:
                out.extend(_jsonld_image_values(value[key]))
        return out
    return []


def find_image_candidates(url, page_html=None, final_url=None, initial_url=""):
    candidates = []
    base_url = final_url or url
    try:
        if page_html is None:
            response = session.get(url, headers={**HEADERS, "Referer": url}, timeout=20)
            if response.status_code >= 400:
                return [initial_url] if initial_url else []
            page_html = response.text
            base_url = response.url
        if initial_url:
            candidates.append(initial_url)
        soup = BeautifulSoup(page_html, "html.parser")
        for attrs in (
            {"property": "og:image"}, {"property": "og:image:url"},
            {"name": "twitter:image"}, {"name": "twitter:image:src"}, {"itemprop": "image"},
        ):
            for tag in soup.find_all("meta", attrs=attrs):
                content = safe_text(tag.get("content"))
                if content:
                    candidates.append(content)
        for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            objects = payload if isinstance(payload, list) else [payload]
            for obj in objects:
                if isinstance(obj, dict):
                    candidates.extend(_jsonld_image_values(obj.get("image")))
        for tag in soup.select("article img, main img, figure img, img")[:40]:
            for attr in ("src", "data-src", "data-original", "data-lazy-src"):
                value = safe_text(tag.get(attr))
                if value:
                    candidates.append(value)
        return _unique_image_urls(candidates, base_url)
    except Exception as exc:
        logger.warning("Image candidate extraction failed %s: %s", url, exc)
        return _unique_image_urls(candidates, base_url)


def find_font(bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def crop_cover(photo, size=(1200, 675)):
    image = photo.convert("RGB")
    scale = max(size[0] / image.width, size[1] / image.height)
    resized = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - size[0]) // 2)
    top = max(0, (resized.height - size[1]) // 2)
    return resized.crop((left, top, left + size[0], top + size[1]))


def image_average_brightness(image):
    small = image.resize((1, 1)).convert("RGB")
    r, g, b = small.getpixel((0, 0))
    return (r + g + b) / 3


def branded_card(photo, source_position="left"):
    base = crop_cover(photo).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_path = find_font(True)
    font = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
    channel_text = "@CareerNewsroom"
    bbox = draw.textbbox((0, 0), channel_text, font=font)
    padding_x, padding_y = 18, 9
    margin_x, margin_y = 28, 24
    chip_w = (bbox[2] - bbox[0]) + padding_x * 2
    chip_h = (bbox[3] - bbox[1]) + padding_y * 2
    x2, y2 = 1200 - margin_x, 675 - margin_y
    x1, y1 = x2 - chip_w, y2 - chip_h
    if image_average_brightness(base) < 125:
        chip_bg, chip_fg = (245, 245, 245, 225), (20, 24, 28, 255)
    else:
        chip_bg, chip_fg = (18, 22, 28, 205), (245, 245, 245, 255)
    draw.rounded_rectangle((x1, y1, x2, y2), radius=16, fill=chip_bg)
    draw.text((x1 + padding_x, y1 + padding_y - 1), channel_text, font=font, fill=chip_fg)
    return Image.alpha_composite(base, overlay).convert("RGB")


def make_source_fallback(source, logo=None):
    image = Image.new("RGB", (1200, 675), (235, 235, 235))
    if logo is not None:
        logo = logo.convert("RGBA")
        logo.thumbnail((420, 220), Image.Resampling.LANCZOS)
        x = (1200 - logo.width) // 2
        y = (675 - logo.height) // 2 - 35
        image.paste(logo, (x, y), logo)
        return image
    draw = ImageDraw.Draw(image)
    title_font_path = find_font(True)
    title_font = ImageFont.truetype(title_font_path, 58) if title_font_path else ImageFont.load_default()
    text = source or "Job Source"
    bbox = draw.textbbox((0, 0), text, font=title_font)
    draw.text(((1200 - (bbox[2] - bbox[0])) / 2, 285), text, font=title_font, fill=(30, 30, 30))
    return image


def download_image(url, referer=""):
    try:
        response = session.get(url, headers={**HEADERS, "Referer": referer or url}, timeout=20)
        if response.status_code >= 400 or not response.content:
            return None
        image = Image.open(BytesIO(response.content))
        if image.width < 240 or image.height < 120:
            return None
        return image.convert("RGB")
    except Exception:
        return None


def download_logo(url, referer=""):
    image = download_image(url, referer)
    return image


def source_logo_candidates(source, article_url):
    domain = normalized_domain(article_url)
    candidates = []
    if source == "Dohaj" or domain == DOHAJ_DOMAIN:
        candidates = ["https://dohaj.com/favicon.ico", "https://dohaj.com/images/logo.png"]
    else:
        candidates = [
            "https://bdjobs.com/favicon.ico",
            "https://www.bdjobs.com/favicon.ico",
        ]
    return candidates


def prepare_image(job, index):
    image = None
    for candidate in job.get("image_candidates", []) or []:
        image = download_image(candidate, job.get("source_url", ""))
        if image is not None:
            break
    fallback_kind = "article"
    if image is None:
        for logo_url in source_logo_candidates(job.get("source", ""), job.get("source_url", "")):
            logo = download_logo(logo_url, job.get("source_url", ""))
            if logo is not None:
                image = make_source_fallback(job.get("source", "Source"), logo)
                fallback_kind = "logo"
                break
    if image is None:
        image = make_source_fallback(job.get("source", "Source"))
        fallback_kind = "text"
    branded = branded_card(image)
    path = f"/tmp/career_news_{index}.jpg"
    branded.save(path, "JPEG", quality=88, optimize=True)
    logger.info("Image selected | %s | %s", job.get("source", "Source"), fallback_kind)
    return path


# ============================================================
# TELEGRAM HTTP LAYER: SAME RELIABLE RETRY PATTERN
# ============================================================

def telegram_call(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    last = {"ok": False, "description": "Unknown error"}
    for attempt in range(1, 6):
        try:
            response = session.post(url, data=data or {}, files=files, timeout=90)
            result = response.json()
            if result.get("ok"):
                return result
            last = result
            if response.status_code == 429:
                retry_after = int(result.get("parameters", {}).get("retry_after", 5))
                logger.warning("Telegram 429; waiting %ss", retry_after)
                time.sleep(max(1, retry_after))
                continue
            if response.status_code >= 500:
                time.sleep(2 * attempt)
                continue
            break
        except Exception as exc:
            last = {"ok": False, "description": str(exc)}
            time.sleep(2 * attempt)
    return last


def _button_markup(job):
    url = safe_text(job.get("apply_url"))
    if url and url.startswith(("http://", "https://")):
        label = "APPLY NOW"
        target = url
    else:
        label = "READ MORE"
        target = safe_text(job.get("source_url"))
    return {
        "inline_keyboard": [[{"text": label, "url": target}]]
    }


def send_bot_api_fallback(image_path, job, plain_text):
    text = plain_text
    if len(text) > 900:
        text = text[:900].rsplit(" ", 1)[0].rstrip() + "..."
    data = {
        "chat_id": TELEGRAM_CHANNEL,
        "caption": text,
        "reply_markup": json.dumps(_button_markup(job), ensure_ascii=False),
    }
    try:
        with open(image_path, "rb") as photo:
            return telegram_call("sendPhoto", data=data, files={"photo": photo})
    except Exception as exc:
        return {"ok": False, "description": str(exc)}


def send_rich_photo(image_path, rich_html, job):
    rich_message = {
        "html": rich_html,
        "media": [{"id": "newsphoto", "media": {"type": "photo", "media": "attach://photo"}}],
        "skip_entity_detection": False,
    }
    data = {
        "chat_id": TELEGRAM_CHANNEL,
        "rich_message": json.dumps(rich_message, ensure_ascii=False),
        "reply_markup": json.dumps(_button_markup(job), ensure_ascii=False),
    }
    try:
        with open(image_path, "rb") as photo:
            return telegram_call("sendRichMessage", data=data, files={"photo": photo})
    except Exception as exc:
        return {"ok": False, "description": str(exc)}


# ============================================================
# JOB POST RENDERING
# ============================================================

def display_value(value):
    return clean_generated_text(safe_text(value))


def job_hashtags(job):
    tags = []
    category = safe_text(job.get("category") or job.get("dohaj_category"))
    blob = f"{job.get('title','')} {job.get('education','')} {job.get('category','')}".lower()
    if any(x in blob for x in ("account", "finance", "audit", "tax")):
        tags.append("#Finance")
    if "bank" in blob:
        tags.append("#Banking")
    if any(x in blob for x in ("marketing", "sales", "brand")):
        tags.append("#Marketing")
    if any(x in blob for x in ("human resource", "hr", "recruitment")):
        tags.append("#HR")
    if any(x in blob for x in ("supply chain", "procurement", "commercial")):
        tags.append("#SupplyChain")
    if any(x in blob for x in EARLY_CAREER_TERMS):
        tags.append("#EarlyCareer")
    if any(x in blob for x in BBA_MBA_TERMS):
        tags.append("#BBA_MBA")
    if not tags:
        tags.append("#Career")
    return list(dict.fromkeys(tags))[:4]


def job_snapshot_rows(job):
    rows = []
    mapping = [
        ("Location", "location"),
        ("Type", "employment_type"),
        ("Education", "education"),
        ("Experience", "experience"),
        ("Salary", "salary"),
        ("Vacancies", "vacancy"),
        ("Application", "application_method"),
        ("Application Period", "application_period"),
        ("Deadline", "deadline"),
        ("Posted", "posted_date"),
        ("Workplace", "workplace"),
        ("Age", "age"),
    ]
    for label, key in mapping:
        value = display_value(job.get(key))
        if value:
            rows.append((label, value))
    return rows


def dynamic_rich_html(job):
    parts = [
        '<img src="tg://photo?id=newsphoto">',
        "<h1>" + html.escape(display_value(job.get("title")), quote=False) + "</h1>",
        "<p><b>" + html.escape(display_value(job.get("company")), quote=False) + "</b></p>",
        "<h2>JOB SNAPSHOT</h2>",
        "<table><tr><td><b>FIELD</b></td><td><b>DETAILS</b></td></tr>",
    ]
    for label, value in job_snapshot_rows(job):
        parts.append(
            "<tr><td><b>" + html.escape(label) + "</b></td><td>" + html.escape(value, quote=False) + "</td></tr>"
        )
    parts.append("</table>")
    tags = " ".join(job_hashtags(job))
    if tags:
        parts.append("<p>" + html.escape(tags) + "</p>")
    source = html.escape(job.get("source", "Source"), quote=False)
    source_url = html.escape(job.get("source_url", ""), quote=True)
    parts.append(f'<footer>🔎 Official Source: <a href="{source_url}">{source}</a></footer>')
    return "\n".join(parts)


def plain_job_text(job):
    lines = [display_value(job.get("title")), display_value(job.get("company")), "", "JOB SNAPSHOT"]
    for label, value in job_snapshot_rows(job):
        lines.append(f"{label}: {value}")
    tags = " ".join(job_hashtags(job))
    if tags:
        lines.extend(["", tags])
    lines.append(f"🔎 Official Source: {job.get('source','Source')}")
    return "\n".join(lines)


def rich_visible_length(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def fit_rich_html(job):
    result = dynamic_rich_html(job)
    if rich_visible_length(result) <= MAX_RICH_CHARACTERS:
        return result
    candidate = dict(job)
    for key, limit in (("education", 1200), ("experience", 700), ("application_method", 700), ("location", 500), ("salary", 300)):
        if candidate.get(key):
            candidate[key] = trim_source_text(candidate[key], limit)
    return dynamic_rich_html(candidate)


# ============================================================
# MAIN
# ============================================================

def run():
    logger.info("CAREERNEWSROOM V0.5")
    logger.info("Channel=%s | Sources=Bdjobs+Dohaj | Target=5-15", TELEGRAM_CHANNEL)
    logger.info("BDJOBS DISCOVERY WINDOW=%s -> %s", DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat())
    prune_state()

    discovered = discover_all()
    verified = []
    rejected = 0

    for item in discovered:
        researched = research_job(item)
        if not researched:
            rejected += 1
            logger.info("DROP retrieval: %s | %s", item.get("source", ""), item.get("title", ""))
            continue
        researched["source_url"] = item["url"]
        researched["canonical"] = item["canonical"]
        ok, reason = deterministic_job_gate(researched)
        if not ok:
            rejected += 1
            logger.info("DROP gate: %s | %s | %s", reason, item.get("source", ""), item.get("title", ""))
            continue
        save_job_to_queue(researched)
        if not candidate_already_posted(researched):
            verified.append(researched)

    save_state(STATE)
    unique = build_unique_job_pool(verified)
    logger.info("VERIFIED JOBS=%d | REJECTED=%d | UNIQUE=%d", len(verified), rejected, len(unique))

    # Include eligible unpublished active jobs still in state when today's source pages
    # contain fewer than 5 usable new jobs. This keeps the bot continuous without crawling
    # outside the user-approved source universe.
    if len(unique) < MIN_STORIES_PER_RUN:
        for queued in STATE.get("queue", {}).values():
            if queued.get("status") not in {"pending", "selected"}:
                continue
            if deadline_status(queued) == "expired":
                continue
            if candidate_already_posted(queued):
                continue
            if queued.get("judge_score", 0) >= 60:
                unique.append(queued)
            if len(unique) >= RANKING_POOL_SIZE:
                break
        unique = build_unique_job_pool(unique)

    ranked = rank_jobs(unique)
    logger.info("RANKED=%d", len(ranked))
    for idx, job in enumerate(ranked[:15], start=1):
        logger.info("RANK #%d | %s | score=%s | source=%s", idx, job.get("title", ""), job.get("judge_score", 0), job.get("source", ""))

    selected = select_final_jobs(ranked)
    logger.info("FINAL SELECTED=%d | target_min=%d | max=%d", len(selected), MIN_STORIES_PER_RUN, MAX_STORIES_PER_RUN)

    published_count = 0
    for index, job in enumerate(selected, start=1):
        rich_html = fit_rich_html(job)
        if rich_visible_length(rich_html) > MAX_RICH_CHARACTERS:
            logger.error("DROP render length: %s", job.get("title", ""))
            continue
        store_selected_event(job, published=False)
        image_path = prepare_image(job, index)
        result = send_rich_photo(image_path, rich_html, job)
        if not result.get("ok"):
            logger.warning("Rich Message failed; Bot API fallback: %s", result.get("description"))
            result = send_bot_api_fallback(image_path, job, plain_job_text(job))
        if result.get("ok"):
            published_count += 1
            message = result.get("result", {})
            message_id = message.get("message_id") if isinstance(message, dict) else None
            POSTED_URLS.add(canonical_url(job["source_url"]))
            save_posted_url(canonical_url(job["source_url"]))
            item = STATE["queue"].get(job["canonical"])
            if item:
                item.update({"status": "posted", "posted_at": now_iso(), "judge_score": job.get("judge_score", 0), "apply_url": job.get("apply_url", "")})
            store_selected_event(job, published=True, message_id=message_id)
            STATE["recent_titles"].append(normalize_title(job["title"]))
            logger.info("PUBLISHED %d/%d | %s | %s", published_count, MAX_STORIES_PER_RUN, job.get("source"), job.get("title"))
        else:
            logger.error("Telegram failed: %s", result.get("description"))
        save_state(STATE)
        time.sleep(POST_DELAY_SECONDS)

    STATE["last_run"] = now_iso()
    save_state(STATE)
    logger.info("Finished. Published=%d/%d", published_count, MAX_STORIES_PER_RUN)


# ============================================================
# SELF TEST
# ============================================================

def self_test():
    fixture = """
    <html><head>
    <meta property="og:image" content="/images/job.jpg">
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"Management Trainee",
     "datePosted":"2026-09-17","validThrough":"2026-10-17",
     "hiringOrganization":{"name":"Example Bank"},
     "jobLocation":{"address":{"addressLocality":"Dhaka","addressCountry":"Bangladesh"}},
     "employmentType":"FULL_TIME"}
    </script></head><body>
    <h1>Management Trainee</h1>
    <p>Company Name: Example Bank</p>
    <p>Vacancy: 10</p>
    <p>Education: Bachelor of Business Administration (BBA) or MBA</p>
    <p>Experience: Freshers are encouraged to apply.</p>
    <p>Salary: Tk. 35000 - 45000</p>
    <p>Application Deadline: 2026-10-17</p>
    <a href="https://careers.examplebank.com/jobs/123">Apply Online</a>
    </body></html>
    """
    fake_item = {
        "title": "Management Trainee",
        "url": "https://dohaj.com/job-details/example-123",
        "canonical": canonical_url("https://dohaj.com/job-details/example-123"),
        "source": "Dohaj",
        "discovery": "self_test",
    }
    text = _text_from_html(fixture)
    fields = extract_job_fields(text, fixture, fake_item["url"], fake_item)
    apply_url = extract_apply_url(fixture, fake_item["url"], "Dohaj")
    fields["apply_url"] = apply_url
    fields["canonical"] = fake_item["canonical"]
    fields["audience_pre_score"] = job_family_score(fields["title"], text)
    assert fields["title"] == "Management Trainee"
    assert fields["company"] == "Example Bank"
    assert fields["vacancy"] == "10"
    assert fields["salary"].startswith("Tk")
    assert fields["deadline"] == "2026-10-17"
    assert apply_url == "https://careers.examplebank.com/jobs/123"
    ok, _ = deterministic_job_gate(fields)
    assert ok

    button = _button_markup(fields)
    assert button["inline_keyboard"][0][0]["text"] == "APPLY NOW"
    assert button["inline_keyboard"][0][0]["url"] == apply_url

    fallback = dict(fields)
    fallback["apply_url"] = ""
    fb = _button_markup(fallback)
    assert fb["inline_keyboard"][0][0]["text"] == "READ MORE"
    assert fb["inline_keyboard"][0][0]["url"] == fields["source_url"]

    html_text = dynamic_rich_html(fields)
    assert "JOB SNAPSHOT" in html_text
    assert "Gender" not in html_text
    assert "Suitable For" not in html_text
    assert "Key Highlights" not in html_text
    assert "APPLY NOW" not in html_text
    assert "Official Source" in html_text
    assert rich_visible_length(html_text) < MAX_RICH_CHARACTERS

    noisy = {
        "title": "Salary Calculator for Students",
        "company": "Example",
        "source_url": "https://dohaj.com/job-details/noise",
        "audience_pre_score": 100,
    }
    assert is_noise_title(noisy["title"], noisy["source_url"])

    old_active = dict(fields)
    old_active["posted_date"] = (datetime.now(BD_TZ) - timedelta(days=10)).date().isoformat()
    old_active["deadline"] = (datetime.now(BD_TZ) + timedelta(days=5)).date().isoformat()
    assert deadline_status(old_active) == "active"
    assert posted_freshness_score(old_active) == 4

    assert is_domain_allowed("https://www.bdjobs.com/job/abc", BDJOBS_DOMAINS)
    assert is_domain_allowed("https://dohaj.com/job-details/x", [DOHAJ_DOMAIN])
    assert not is_index_url("https://dohaj.com/job-details/x")
    assert is_index_url("https://dohaj.com/category/accounting-finance")

    # Source-universe contract: only the user-approved Bdjobs and Dohaj domains may enter the queue.
    assert all(is_domain_allowed(url, [DOHAJ_DOMAIN]) for url in DOHAJ_TOP5_URLS)
    assert all(is_domain_allowed(url, BDJOBS_DOMAINS) for url in [
        "https://www.bdjobs.com/job/123",
        "https://hotjobs.bdjobs.com/jobs/example/example1.htm",
    ])
    assert not is_domain_allowed("https://example.com/job/123", BDJOBS_DOMAINS)

    # Native button contract: exactly one button and no emoji in its label.
    markup = _button_markup(fields)
    assert len(markup["inline_keyboard"]) == 1
    assert len(markup["inline_keyboard"][0]) == 1
    assert markup["inline_keyboard"][0][0]["text"] == "APPLY NOW"
    assert not any(ord(ch) > 127 for ch in markup["inline_keyboard"][0][0]["text"])

    # Cerebras ranking contract can be validated without a live API call.
    class _FakeMessage:
        def __init__(self, content):
            self.content = content
    class _FakeChoice:
        def __init__(self, content):
            self.message = _FakeMessage(content)
    class _FakeResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]
    class _FakeCompletions:
        def create(self, **kwargs):
            return _FakeResponse(json.dumps({
                "results": [
                    {"id": 1, "publish": True, "score": 92, "bba_mba_fit": 96, "early_career_fit": 95, "role_fit": 90, "reason": "Direct BBA/MBA management role."},
                    {"id": 2, "publish": False, "score": 42, "bba_mba_fit": 25, "early_career_fit": 20, "role_fit": 50, "reason": "Senior specialist role."},
                ]
            }))
    class _FakeChat:
        def __init__(self):
            self.completions = _FakeCompletions()
    class _FakeCerebras:
        def __init__(self):
            self.chat = _FakeChat()
    global cerebras
    original_cerebras = cerebras
    try:
        cerebras = _FakeCerebras()
        test_jobs = [
            {**fields, "title": "Management Trainee", "company": "Example Bank", "event_id": "one", "audience_pre_score": 85},
            {**fields, "title": "Senior Technical Specialist", "company": "Example Bank", "event_id": "two", "audience_pre_score": 15},
        ]
        judged = rank_jobs(test_jobs)
        assert judged and judged[0]["judge_score"] == 92
        assert select_final_jobs(judged)[0]["title"] == "Management Trainee"
    finally:
        cerebras = original_cerebras

    logger.info("CareerNewsroom V0.5 self-test passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        run()
