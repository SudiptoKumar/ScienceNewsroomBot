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
from urllib.parse import urlparse, urljoin, quote
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from io import BytesIO

import requests
import feedparser
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

TELEGRAM_CHANNEL = (os.environ.get("TELEGRAM_CHANNEL") or "@ScienceNewsroom").strip()

# Optional. If set, feed-down alerts go here (a private chat/DM with the
# bot, not the public channel). If empty, alerts only go to the run log.
TELEGRAM_ADMIN_CHAT_ID = (os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or "").strip()

NEWS_MODE = (os.environ.get("NEWS_MODE") or "update").strip().lower()

VALID_NEWS_MODES = {"update"}
if NEWS_MODE not in VALID_NEWS_MODES:
    raise ValueError(
        f"Invalid NEWS_MODE={NEWS_MODE!r}; expected one of {sorted(VALID_NEWS_MODES)}"
    )

CEREBRAS_MODEL = os.environ.get(
    "CEREBRAS_MODEL",
    "gpt-oss-120b",
)

POSTED_FILE = "posted_urls.txt"
STATE_FILE = "news_state.json"

BD_TZ = ZoneInfo("Asia/Dhaka")

# Version 1 editorial target: publish only clearly important science stories.
# All science stories compete in one ranked pool. Six is a safety cap, not a quota.
MAX_STORIES_PER_RUN = 6
RANKING_POOL_SIZE = 24
DISCOVERY_LOOKBACK_HOURS = 24

# Reliability / quality
POST_DELAY_SECONDS = 3.5
ROLLING_DISCOVERY_HOURS = DISCOVERY_LOOKBACK_HOURS
FUTURE_TOLERANCE_MINUTES = 10
QUEUE_RETENTION_DAYS = 4
EVENT_RETENTION_DAYS = 30
MAX_RSS_CANDIDATES = 240
MAX_EXA_CANDIDATES = 60
MAX_GOOGLE_NEWS_CANDIDATES = 40
THIN_EXCERPT_CHARS = 150
MAX_EXCERPT_ENRICH = 12
MAX_SOURCE_PER_RUN = 99
MAX_RICH_CHARACTERS = 32768

# Lightweight English stopwords used only by the conservative event/entity
# deduplication layer. This is deliberately small so technology entities and
# meaningful short terms such as AI, OS, UI, and VR are retained.
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
    "from", "with", "by", "at", "as", "is", "are", "was", "were",
    "be", "been", "being", "has", "have", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can",
    "this", "that", "these", "those", "it", "its", "their", "they",
    "them", "he", "she", "his", "her", "we", "our", "you", "your",
    "new", "after", "before", "over", "into", "than", "about", "from",
}

# RSS-first sources. Exa and Google News remain gap fillers.
RSS_FEEDS = [
    {"name": "Nature News", "region": "Science", "url": "https://www.nature.com/nature.rss"},
    {"name": "Science / AAAS", "region": "Science", "url": "https://www.science.org/rss/news_current.xml"},
    {"name": "Science News", "region": "Science", "url": "https://www.sciencenews.org/feed"},
    {"name": "New Scientist", "region": "Science", "url": "https://www.newscientist.com/feed/home/"},
    {"name": "Scientific American", "region": "Science", "url": "https://www.scientificamerican.com/rss/"},
    {"name": "Quanta Magazine", "region": "Science", "url": "https://www.quantamagazine.org/feed/"},
    {"name": "Phys.org", "region": "Science", "url": "https://phys.org/rss-feed/"},
    {"name": "ScienceDaily", "region": "Science", "url": "https://www.sciencedaily.com/rss/top/science.xml"},
    {"name": "EurekAlert!", "region": "Science", "url": "https://www.eurekalert.org/rss/news.xml"},
    {"name": "Live Science", "region": "Science", "url": "https://www.livescience.com/feeds.xml"},
    {"name": "ScienceAlert", "region": "Science", "url": "https://www.sciencealert.com/feed"},
    {"name": "The Scientist", "region": "Science", "url": "https://www.the-scientist.com/feed"},
    {"name": "Knowable Magazine", "region": "Science", "url": "https://knowablemagazine.org/rss"},
    {"name": "Cosmos Magazine", "region": "Science", "url": "https://cosmosmagazine.com/feed/"},
    {"name": "The Conversation", "region": "Science", "url": "https://theconversation.com/us/topics/science-34/articles.atom"},
    {"name": "Physics World", "region": "Science", "url": "https://physicsworld.com/feed/"},
    {"name": "APS News", "region": "Science", "url": "https://physics.aps.org/rss/recent.xml"},
    {"name": "C&EN", "region": "Science", "url": "https://cen.acs.org/articles.rss"},
    {"name": "Chemistry World", "region": "Science", "url": "https://www.chemistryworld.com/rss"},
    {"name": "Eos / AGU", "region": "Science", "url": "https://eos.org/feed"},
    {"name": "Carbon Brief", "region": "Science", "url": "https://www.carbonbrief.org/feed/"},
    {"name": "Inside Climate News", "region": "Science", "url": "https://insideclimatenews.org/feed/"},
    {"name": "STAT", "region": "Science", "url": "https://www.statnews.com/feed/"},
    {"name": "Science Focus", "region": "Science", "url": "https://www.sciencefocus.com/feed"},
    {"name": "NASA Science", "region": "Science", "url": "https://science.nasa.gov/feed/"},
    {"name": "ESA Science", "region": "Science", "url": "https://www.esa.int/rssfeed/Our_Activities/Space_Science"},
    {"name": "ESO", "region": "Science", "url": "https://www.eso.org/public/news/feed/"},
    {"name": "NOIRLab", "region": "Science", "url": "https://noirlab.edu/public/news/feed/"},
    {"name": "Space Telescope Science Institute", "region": "Science", "url": "https://www.stsci.edu/feeds/news"},
    {"name": "HubbleSite", "region": "Science", "url": "https://hubblesite.org/resource-gallery/news"},
    {"name": "NASA Webb", "region": "Science", "url": "https://webbtelescope.org/news/feed"},
    {"name": "Chandra X-ray Observatory", "region": "Science", "url": "https://chandra.si.edu/resources/rss.xml"},
    {"name": "JPL", "region": "Science", "url": "https://www.jpl.nasa.gov/feeds/news/"},
    {"name": "Space.com", "region": "Science", "url": "https://www.space.com/feeds/all"},
    {"name": "Sky & Telescope", "region": "Science", "url": "https://skyandtelescope.org/feed/"},
    {"name": "Astronomy Magazine", "region": "Science", "url": "https://astronomy.com/feed"},
    {"name": "Universe Today", "region": "Science", "url": "https://universetoday.com/feed/"},
    {"name": "EarthSky", "region": "Science", "url": "https://earthsky.org/feed/"},
    {"name": "Sky at Night", "region": "Science", "url": "https://www.skyatnightmagazine.com/feed"},
    {"name": "arXiv Astronomy", "region": "Science", "url": "https://export.arxiv.org/rss/astro-ph"},
    {"name": "PubMed", "region": "Science", "url": "https://pubmed.ncbi.nlm.nih.gov/rss/search/1/?term=scientific+research&format=pubmed"},
]



# ============================================================
# TAXONOMY: SCIENCE NEWS

TOPICS = {
    "Science": [
        "Astronomy",
        "Astrophysics",
        "Cosmology",
        "Planetary Science",
        "Space Science",
        "Astrobiology",
        "Physics",
        "Quantum Physics",
        "Particle Physics",
        "Nuclear Physics",
        "Condensed Matter",
        "Materials Science",
        "Chemistry",
        "Chemical Biology",
        "Biology",
        "Molecular Biology",
        "Cell Biology",
        "Genetics and Genomics",
        "Evolution",
        "Ecology",
        "Microbiology",
        "Neuroscience",
        "Cognitive Science",
        "Earth Science",
        "Geology",
        "Geophysics",
        "Oceanography",
        "Atmospheric Science",
        "Climate Science",
        "Environmental Science",
        "Paleontology",
        "Biomedical Research",
        "Medicine Research",
        "Mathematics",
        "Scientific Computing",
        "AI for Science",
        "Energy Science",
        "Nanotechnology",
        "Scientific Instruments",
    ]
}

INSTITUTIONS = [
    "NASA", "ESA", "ESO", "NOIRLab", "STScI", "JPL", "CERN", "Fermilab",
    "MIT", "Harvard", "Stanford", "Caltech", "Princeton", "Oxford", "Cambridge",
    "Max Planck", "Broad Institute", "Howard Hughes Medical Institute", "NIH", "NSF",
    "European Molecular Biology Laboratory", "Wellcome Sanger Institute",
]

SOURCE_NAMES = {
    "nature.com": "Nature",
    "science.org": "Science",
    "sciencenews.org": "Science News",
    "newscientist.com": "New Scientist",
    "scientificamerican.com": "Scientific American",
    "quantamagazine.org": "Quanta Magazine",
    "phys.org": "Phys.org",
    "sciencedaily.com": "ScienceDaily",
    "eurekalert.org": "EurekAlert!",
    "livescience.com": "Live Science",
    "sciencealert.com": "ScienceAlert",
    "the-scientist.com": "The Scientist",
    "knowablemagazine.org": "Knowable Magazine",
    "cosmosmagazine.com": "Cosmos Magazine",
    "theconversation.com": "The Conversation",
    "physicsworld.com": "Physics World",
    "aps.org": "American Physical Society",
    "cen.acs.org": "C&EN",
    "chemistryworld.com": "Chemistry World",
    "eos.org": "Eos",
    "carbonbrief.org": "Carbon Brief",
    "insideclimatenews.org": "Inside Climate News",
    "statnews.com": "STAT",
    "sciencefocus.com": "Science Focus",
    "science.nasa.gov": "NASA Science",
    "nasa.gov": "NASA",
    "esa.int": "ESA",
    "eso.org": "ESO",
    "noirlab.edu": "NOIRLab",
    "stsci.edu": "STScI",
    "hubblesite.org": "HubbleSite",
    "webbtelescope.org": "NASA Webb",
    "chandra.si.edu": "Chandra X-ray Observatory",
    "jpl.nasa.gov": "JPL",
    "space.com": "Space.com",
    "skyandtelescope.org": "Sky & Telescope",
    "astronomy.com": "Astronomy Magazine",
    "universetoday.com": "Universe Today",
    "earthsky.org": "EarthSky",
    "skyatnightmagazine.com": "Sky at Night",
    "arxiv.org": "arXiv",
    "pubmed.ncbi.nlm.nih.gov": "PubMed",
    "biorxiv.org": "bioRxiv",
    "medrxiv.org": "medRxiv",
    "chemrxiv.org": "ChemRxiv",
    "eartharxiv.org": "EarthArXiv",
    "adsabs.harvard.edu": "NASA ADS",
    "crossref.org": "Crossref",
    "openalex.org": "OpenAlex",
    "semanticscholar.org": "Semantic Scholar",
}

CATEGORY_HASHTAGS = {
    "Astronomy": ["#Astronomy", "#Space"],
    "Astrophysics": ["#Astrophysics", "#Space"],
    "Cosmology": ["#Cosmology", "#Physics"],
    "Planetary Science": ["#PlanetaryScience", "#Space"],
    "Space Science": ["#SpaceScience", "#Space"],
    "Astrobiology": ["#Astrobiology", "#Space"],
    "Physics": ["#Physics", "#Science"],
    "Quantum Physics": ["#QuantumPhysics", "#Physics"],
    "Particle Physics": ["#ParticlePhysics", "#Physics"],
    "Nuclear Physics": ["#NuclearPhysics", "#Physics"],
    "Condensed Matter": ["#CondensedMatter", "#Physics"],
    "Materials Science": ["#MaterialsScience", "#Science"],
    "Chemistry": ["#Chemistry", "#Science"],
    "Chemical Biology": ["#ChemicalBiology", "#Biology"],
    "Biology": ["#Biology", "#Science"],
    "Molecular Biology": ["#MolecularBiology", "#Biology"],
    "Cell Biology": ["#CellBiology", "#Biology"],
    "Genetics and Genomics": ["#Genetics", "#Genomics"],
    "Evolution": ["#Evolution", "#Biology"],
    "Ecology": ["#Ecology", "#Science"],
    "Microbiology": ["#Microbiology", "#Biology"],
    "Neuroscience": ["#Neuroscience", "#Biology"],
    "Cognitive Science": ["#CognitiveScience", "#Science"],
    "Earth Science": ["#EarthScience", "#Science"],
    "Geology": ["#Geology", "#EarthScience"],
    "Geophysics": ["#Geophysics", "#EarthScience"],
    "Oceanography": ["#Oceanography", "#EarthScience"],
    "Atmospheric Science": ["#AtmosphericScience", "#ClimateScience"],
    "Climate Science": ["#ClimateScience", "#EarthScience"],
    "Environmental Science": ["#EnvironmentalScience", "#Science"],
    "Paleontology": ["#Paleontology", "#Evolution"],
    "Biomedical Research": ["#BiomedicalResearch", "#Science"],
    "Medicine Research": ["#MedicalResearch", "#Science"],
    "Mathematics": ["#Mathematics", "#Science"],
    "Scientific Computing": ["#ScientificComputing", "#Science"],
    "AI for Science": ["#AIforScience", "#AI"],
    "Energy Science": ["#EnergyScience", "#Science"],
    "Nanotechnology": ["#Nanotechnology", "#Science"],
    "Scientific Instruments": ["#ScientificInstruments", "#Science"],
}

CATEGORY_GROUPS = {
    "Astronomy and Space": {"Astronomy", "Astrophysics", "Cosmology", "Planetary Science", "Space Science", "Astrobiology"},
    "Physical Sciences": {"Physics", "Quantum Physics", "Particle Physics", "Nuclear Physics", "Condensed Matter", "Materials Science", "Chemistry", "Chemical Biology"},
    "Life Sciences": {"Biology", "Molecular Biology", "Cell Biology", "Genetics and Genomics", "Evolution", "Ecology", "Microbiology", "Neuroscience", "Cognitive Science", "Biomedical Research", "Medicine Research"},
    "Earth and Environment": {"Earth Science", "Geology", "Geophysics", "Oceanography", "Atmospheric Science", "Climate Science", "Environmental Science", "Paleontology"},
    "Quantitative and Frontier": {"Mathematics", "Scientific Computing", "AI for Science", "Energy Science", "Nanotechnology", "Scientific Instruments"},
}

TOPIC_ALIASES = {
    "astronomy": "Astronomy", "astrophysics": "Astrophysics", "cosmology": "Cosmology",
    "exoplanet": "Astronomy", "black hole": "Astrophysics", "neutron star": "Astrophysics",
    "gravitational wave": "Astrophysics", "fast radio burst": "Astronomy", "space": "Space Science",
    "planetary science": "Planetary Science", "astrobiology": "Astrobiology", "quantum": "Quantum Physics",
    "quantum physics": "Quantum Physics", "particle physics": "Particle Physics", "cern": "Particle Physics",
    "nuclear physics": "Nuclear Physics", "materials": "Materials Science", "chemistry": "Chemistry",
    "biology": "Biology", "genetics": "Genetics and Genomics", "genomics": "Genetics and Genomics",
    "evolution": "Evolution", "ecology": "Ecology", "microbiology": "Microbiology", "neuroscience": "Neuroscience",
    "earth science": "Earth Science", "geology": "Geology", "geophysics": "Geophysics", "oceanography": "Oceanography",
    "climate": "Climate Science", "climate science": "Climate Science", "environment": "Environmental Science",
    "paleontology": "Paleontology", "biomedical": "Biomedical Research", "medical research": "Medicine Research",
    "mathematics": "Mathematics", "math": "Mathematics", "scientific computing": "Scientific Computing",
    "ai for science": "AI for Science", "energy science": "Energy Science", "nanotechnology": "Nanotechnology",
}


def canonical_topic(topic, region="Science"):
    key = safe_text(topic).lower().strip()
    if key in TOPIC_ALIASES:
        return TOPIC_ALIASES[key]
    for item in TOPICS["Science"]:
        if key == item.lower():
            return item
    patterns = [
        (("exoplanet", "planet", "telescope", "galaxy", "star", "supernova", "black hole", "astronomy"), "Astronomy"),
        (("astrophysics", "neutron star", "gravitational wave"), "Astrophysics"),
        (("cosmology", "dark matter", "dark energy", "early universe"), "Cosmology"),
        (("moon", "mars", "asteroid", "comet", "planetary"), "Planetary Science"),
        (("astrobiology", "life beyond earth", "biosignature"), "Astrobiology"),
        (("quantum",), "Quantum Physics"),
        (("particle physics", "cern", "muon", "higgs"), "Particle Physics"),
        (("nuclear",), "Nuclear Physics"),
        (("materials", "metamaterial", "superconductor"), "Materials Science"),
        (("chemistry", "chemical", "molecule", "catalyst"), "Chemistry"),
        (("genetic", "genomics", "dna", "crispr"), "Genetics and Genomics"),
        (("neuroscience", "brain", "neuron"), "Neuroscience"),
        (("evolution", "species", "natural selection"), "Evolution"),
        (("climate", "global warming", "atmosphere"), "Climate Science"),
        (("ocean", "marine", "sea level"), "Oceanography"),
        (("geology", "earthquake", "volcano", "tectonic"), "Geology"),
        (("paleontology", "fossil", "dinosaur"), "Paleontology"),
        (("mathematics", "theorem", "proof"), "Mathematics"),
        (("ai for science", "machine learning for science", "scientific ai"), "AI for Science"),
        (("nanotechnology", "nanoparticle", "nanoscale"), "Nanotechnology"),
    ]
    for needles, canonical in patterns:
        if any(needle in key for needle in needles):
            return canonical
    return "Science"


def category_hashtags(story):
    tags = []
    topic = safe_text(story.get("topic"))
    institution = safe_text(story.get("institution"))
    for tag in CATEGORY_HASHTAGS.get(topic, []):
        if tag not in tags:
            tags.append(tag)
    inst_map = {
        "NASA": "#NASA", "ESA": "#ESA", "ESO": "#ESO", "NOIRLab": "#NOIRLab",
        "STScI": "#STScI", "JPL": "#JPL", "CERN": "#CERN", "Fermilab": "#Fermilab",
        "NIH": "#NIH", "NSF": "#NSF",
    }
    if institution in inst_map and inst_map[institution] not in tags:
        tags.append(inst_map[institution])
    if "#Science" not in tags:
        tags.append("#Science")
    return tags[:3]


def coverage_state():
    return STATE.setdefault("category_coverage", {})


def update_category_coverage(story):
    topic = safe_text(story.get("topic"))
    if topic:
        coverage_state()[topic] = now_iso()


def refresh_category_coverage():
    coverage = coverage_state()
    for event in STATE.get("events", {}).values():
        if event.get("status") != "published":
            continue
        published_at = parse_datetime(event.get("published_at"))
        if not published_at or published_at.date() != NOW_BD.date():
            continue
        topic = safe_text(event.get("topic"))
        if topic:
            coverage[topic] = event.get("selected_at", published_at.isoformat())


# ============================================================
# LOGGING + HTTP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("science-news-bot")

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

adapter = HTTPAdapter(
    max_retries=retry_policy,
    pool_connections=20,
    pool_maxsize=20,
)

session.mount("https://", adapter)
session.mount("http://", adapter)


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

    host = (
        parsed.netloc.lower()
        .removeprefix("www.")
        .removeprefix("amp.")
    )

    path = parsed.path or "/"
    path = path.rstrip("/")
    path = re.sub(r"/amp$", "", path, flags=re.I)
    path = re.sub(r"\.amp$", "", path, flags=re.I)

    return f"{host}{path}"


def normalize_title(title):
    text = safe_text(title).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(text):
    text = normalize_title(text)
    return {
        token
        for token in text.split()
        if len(token) >= 3
    }


def token_jaccard(a, b):
    aa = title_tokens(a)
    bb = title_tokens(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / max(1, len(aa | bb))


def title_similarity(a, b):
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    sequence = SequenceMatcher(None, na, nb).ratio()
    jaccard = token_jaccard(na, nb)
    return max(sequence, jaccard)


def event_similarity(a, b):
    """Cheap event-level similarity without an embedding dependency."""
    sequence = SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()
    jaccard = token_jaccard(a, b)
    return (0.55 * sequence) + (0.45 * jaccard)


def likely_same_event(a, b):
    return (
        title_similarity(a, b) >= 0.90
        or event_similarity(a, b) >= 0.80
    )


def parse_datetime(value):
    raw = safe_text(value)
    if not raw:
        return None

    try:
        dt = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )
        return dt.astimezone(BD_TZ)
    except Exception:
        pass

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )
        return dt.astimezone(BD_TZ)
    except Exception:
        return None


def feed_entry_datetime(entry):
    for key in (
        "published_parsed",
        "updated_parsed",
        "created_parsed",
    ):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(
                    *parsed[:6],
                    tzinfo=timezone.utc,
                ).astimezone(BD_TZ)
            except Exception:
                pass

    for key in (
        "published",
        "updated",
        "created",
    ):
        dt = parse_datetime(
            entry.get(key)
        )
        if dt:
            return dt

    return None


def trim_source_text(text, limit):
    """Trim source text before rendering. Never appends ellipses."""
    text = safe_text(text)
    if len(text) <= limit:
        return text

    trimmed = text[:limit].rstrip()
    if " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0]

    return trimmed.rstrip(" ,:;-/—")


def clean_generated_text(text):
    text = safe_text(text)

    # Prevent visible Markdown artifacts. The Telegram renderer uses its own
    # HTML layer, so generated Markdown emphasis markers must never survive.
    text = re.sub(r"\*{1,3}", "", text)
    text = re.sub(r"`{1,3}", "", text)

    # Prevent visible truncation artifacts.
    text = re.sub(r"\.{2,}", ".", text)
    text = text.replace("\u2026", "")

    # Remove incomplete endings.
    text = re.sub(
        r"\s*[,;:]\s*$",
        "",
        text,
    )
    text = re.sub(
        r"\s*[-—]\s*$",
        "",
        text,
    )

    return text.strip()


def complete_text(text):
    raw = safe_text(text)
    if not raw:
        return False

    # A text that clean_generated_text() would mutilate
    # (trailing dash/comma/colon) is INCOMPLETE.
    if re.search(r"[\s,;:\-—…]+$", raw):
        return False

    text = clean_generated_text(raw)
    if not text:
        return False

    return not text.endswith(
        (",", ";", ":", "-", "—", "…")
    )


def source_name(url):
    domain = (
        urlparse(
            safe_text(url)
        )
        .netloc
        .lower()
        .removeprefix("www.")
    )

    return SOURCE_NAMES.get(
        domain,
        domain or "Source",
    )


def article_region(url):
    return "Science"


def now_iso():
    return datetime.now(
        BD_TZ
    ).isoformat()


# ============================================================
# STATE: QUEUE + EVENTS + KNOWLEDGE
# ============================================================

def default_state():
    return {
        "feeds": {},
        "queue": {},
        "events": {},
        "event_clusters": {},
        "posted_event_ids": [],
        "recent_titles": [],
    }


def load_state():
    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(
            data,
            dict,
        ):
            return default_state()

        base = default_state()
        base.update(data)

        return base

    except Exception:
        return default_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        tmp,
        STATE_FILE,
    )


def load_posted_urls():
    try:
        with open(
            POSTED_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            return {
                canonical_url(line)
                for line in f
                if safe_text(line)
            }
    except FileNotFoundError:
        return set()


def save_posted_url(canonical):
    if not canonical:
        return

    with open(
        POSTED_FILE,
        "a",
        encoding="utf-8",
    ) as f:
        f.write(
            canonical
            + "\n"
        )


STATE = load_state()
POSTED_URLS = load_posted_urls()


def prune_state():
    cutoff_queue = (
        datetime.now(BD_TZ)
        - timedelta(
            days=QUEUE_RETENTION_DAYS
        )
    )

    cutoff_events = (
        datetime.now(BD_TZ)
        - timedelta(
            days=EVENT_RETENTION_DAYS
        )
    )

    queue = STATE.get(
        "queue",
        {},
    )

    keep_queue = {}

    for key, item in queue.items():
        dt = parse_datetime(
            item.get("last_seen")
            or item.get("published_date")
        )

        if (
            dt
            and dt >= cutoff_queue
        ):
            keep_queue[key] = item

    STATE["queue"] = keep_queue

    events = STATE.get(
        "events",
        {},
    )

    keep_events = {}

    for key, event in events.items():
        dt = parse_datetime(
            event.get("published_at")
            or event.get("selected_at")
        )

        if (
            dt
            and dt >= cutoff_events
        ):
            keep_events[key] = event

    STATE["events"] = keep_events

    titles = STATE.get(
        "recent_titles",
        [],
    )

    STATE["recent_titles"] = titles[-400:]


# ============================================================
# TIME WINDOWS
# ============================================================

NOW_BD = datetime.now(
    BD_TZ
)

TODAY_START = NOW_BD.replace(
    hour=0,
    minute=0,
    second=0,
    microsecond=0,
)

YESTERDAY_START = (
    TODAY_START
    - timedelta(days=1)
)

DISCOVERY_START = (
    NOW_BD
    - timedelta(
        hours=ROLLING_DISCOVERY_HOURS
    )
)
DISCOVERY_END = (
    NOW_BD
    + timedelta(
        minutes=FUTURE_TOLERANCE_MINUTES
    )
)

DISCOVERY_TARGET_PER_REGION = 18


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
# CANDIDATE FILTERING
# ============================================================

BAD_PATH_RE = re.compile(
    r"/(opinion|editorial|sponsored|"
    r"tag|topic|live-blog|liveblog|"
    r"photo|photos|video)(/|$)",
    re.I,
)

BAD_TITLE_RE = re.compile(
    r"\b(sponsored|advertisement|"
    r"promo|opinion|editorial)\b",
    re.I,
)

SCIENCE_REJECTION_TITLE_RE = re.compile(
    r"\b(click here|miracle cure|shocking|you won't believe|proof of aliens|aliens are here)\b",
    re.I,
)


def candidate_basic_allowed(item):
    url = safe_text(
        item.get("url")
    )
    title = safe_text(
        item.get("title")
    )
    published = item.get(
        "published_dt"
    )

    if (
        not url
        or not title
        or not published
    ):
        return False

    if BAD_PATH_RE.search(
        urlparse(url).path
    ):
        return False

    if BAD_TITLE_RE.search(
        title
    ) or SCIENCE_REJECTION_TITLE_RE.search(title):
        return False

    if not (
        DISCOVERY_START
        <= published
        <= DISCOVERY_END
    ):
        return False

    region = safe_text(item.get("region"))
    if region and not allowed_source_for_region(url, region):
        return False

    canonical = canonical_url(
        url
    )

    return bool(
        canonical
    )


def title_duplicate_against_state(title):
    for previous in STATE.get(
        "recent_titles",
        [],
    )[-250:]:
        if title_similarity(
            title,
            previous,
        ) >= 0.88:
            return True

    return False


def title_duplicate_against_list(
    title,
    candidates,
    threshold=0.88,
):
    for candidate in candidates:
        if title_similarity(
            title,
            candidate["title"],
        ) >= threshold:
            return True

    return False


# ============================================================
# RSS INGESTION + PERSISTENT QUEUE
# ============================================================

def extract_entry_image(
    entry,
    page_url,
):
    for key in (
        "media_content",
        "media_thumbnail",
    ):
        for item in entry.get(
            key,
            [],
        ):
            image_url = safe_text(
                item.get("url")
            )

            if image_url:
                return urljoin(
                    page_url,
                    image_url,
                )

    for enclosure in entry.get(
        "enclosures",
        [],
    ):
        href = safe_text(
            enclosure.get("href")
        )

        mime = safe_text(
            enclosure.get("type")
        ).lower()

        if (
            href
            and (
                not mime
                or mime.startswith(
                    "image/"
                )
            )
        ):
            return urljoin(
                page_url,
                href,
            )

    return ""


def queue_candidate(item):
    canonical = item["canonical"]

    existing = STATE["queue"].get(
        canonical
    )

    if existing:
        existing.update(
            {
                "last_seen": now_iso(),
                "image": (
                    item.get("image")
                    or existing.get("image", "")
                ),
            }
        )
        return

    STATE["queue"][canonical] = {
        **item,
        "status": "pending",
        "first_seen": now_iso(),
        "last_seen": now_iso(),
    }


def fetch_rss_feed(
    feed_def,
):
    url = feed_def["url"]

    old = STATE["feeds"].get(
        url,
        {},
    )

    headers = dict(
        HEADERS
    )

    if old.get("etag"):
        headers["If-None-Match"] = old[
            "etag"
        ]

    if old.get(
        "last_modified"
    ):
        headers["If-Modified-Since"] = old[
            "last_modified"
        ]

    try:
        response = session.get(
            url,
            headers=headers,
            timeout=20,
        )

        # 304 means the queue remains intact. The feed is reachable,
        # so this counts as healthy and clears any fail streak.
        if response.status_code == 304:
            logger.info(
                "RSS 304: %s",
                feed_def["name"],
            )
            mark_feed_healthy(feed_def, old)
            return 0

        if response.status_code >= 400:
            logger.warning(
                "RSS %s returned %s",
                feed_def["name"],
                response.status_code,
            )
            mark_feed_failed(feed_def, old)
            return 0

        STATE["feeds"][url] = {
            "etag": response.headers.get(
                "ETag",
                old.get("etag"),
            ),
            "last_modified": response.headers.get(
                "Last-Modified",
                old.get("last_modified"),
            ),
            "last_checked": now_iso(),
            "fail_count": 0,
            "alerted": False,
        }

        parsed = feedparser.parse(
            response.content
        )

        added = 0

        for entry in parsed.entries:
            published_dt = feed_entry_datetime(
                entry
            )

            date_estimated = False

            if not published_dt:
                # Some feeds send a date format we cannot parse.
                # Do not throw the story away: use fetch time instead,
                # and mark it so downstream code knows it is a guess.
                published_dt = datetime.now(
                    BD_TZ
                )
                date_estimated = True

            article_url = urljoin(
                url,
                safe_text(
                    entry.get("link")
                ),
            )

            title = safe_text(
                entry.get("title")
            )

            if not article_url or not title:
                continue

            item = {
                "title": title,
                "url": article_url,
                "canonical": canonical_url(
                    article_url
                ),
                "published_dt": published_dt.isoformat(),
                "published_date": published_dt.isoformat(),
                "source": feed_def["name"],
                "region": feed_def["region"],
                "excerpt": BeautifulSoup(
                    safe_text(
                        entry.get(
                            "summary"
                        )
                        or entry.get(
                            "description"
                        )
                    ),
                    "html.parser",
                ).get_text(
                    " ",
                    strip=True,
                )[:2000],
                "image": extract_entry_image(
                    entry,
                    article_url,
                ),
                "discovery": "rss",
                "date_estimated": date_estimated,
            }

            if not candidate_basic_allowed(
                {
                    **item,
                    "published_dt": published_dt,
                }
            ):
                continue

            if (
                item["canonical"]
                in POSTED_URLS
            ):
                continue

            before = item["canonical"] in STATE[
                "queue"
            ]

            queue_candidate(
                item
            )

            if not before:
                added += 1

        return added

    except Exception as exc:
        logger.warning(
            "RSS failed %s: %s",
            feed_def["name"],
            exc,
        )
        mark_feed_failed(feed_def, old)
        return 0


# Consecutive failed runs before we alert about a broken feed.
FEED_FAIL_ALERT_THRESHOLD = 3


def mark_feed_healthy(feed_def, old):
    STATE["feeds"][feed_def["url"]] = {
        **old,
        "last_checked": now_iso(),
        "fail_count": 0,
        "alerted": False,
    }


def mark_feed_failed(feed_def, old):
    fail_count = int(old.get("fail_count", 0)) + 1

    STATE["feeds"][feed_def["url"]] = {
        **old,
        "last_checked": now_iso(),
        "fail_count": fail_count,
    }

    if fail_count >= FEED_FAIL_ALERT_THRESHOLD and not old.get("alerted"):
        alert_feed_down(feed_def, fail_count)
        STATE["feeds"][feed_def["url"]]["alerted"] = True


def alert_feed_down(feed_def, fail_count):
    """Tell the admin a source has gone quiet, instead of failing silently forever."""
    message = (
        f"Feed down: {feed_def['name']} ({feed_def['region']})\n"
        f"Failed {fail_count} runs in a row.\n"
        f"URL: {feed_def['url']}\n"
        f"It will keep retrying, but this source is not feeding the bot right now."
    )

    if TELEGRAM_ADMIN_CHAT_ID:
        try:
            telegram_call(
                "sendMessage",
                data={
                    "chat_id": TELEGRAM_ADMIN_CHAT_ID,
                    "text": message,
                },
            )
        except Exception as exc:
            logger.warning(
                "Feed-down alert failed to send: %s",
                exc,
            )

    logger.error(message)


def collect_rss():
    added = 0

    for feed_def in RSS_FEEDS:
        added += fetch_rss_feed(
            feed_def
        )

    # Critical: queue is saved together with feed validators.
    # A later 304 cannot erase unposted queued stories.
    save_state(
        STATE
    )

    logger.info(
        "RSS queue additions: %d",
        added,
    )

    return added


# ============================================================
# EXA GAP-FILL DISCOVERY
# ============================================================

# ============================================================
# SOURCE UNIVERSE
PRIMARY_SCIENCE_DOMAINS = [
    "nature.com", "science.org", "sciencenews.org", "newscientist.com", "scientificamerican.com",
    "quantamagazine.org", "phys.org", "sciencedaily.com", "eurekalert.org", "livescience.com",
    "sciencealert.com", "the-scientist.com", "knowablemagazine.org", "cosmosmagazine.com", "theconversation.com",
    "physicsworld.com", "aps.org", "cen.acs.org", "chemistryworld.com", "eos.org",
    "carbonbrief.org", "insideclimatenews.org", "statnews.com", "sciencefocus.com", "science.nasa.gov",
    "nasa.gov", "esa.int", "eso.org", "noirlab.edu", "stsci.edu", "hubblesite.org", "webbtelescope.org",
    "chandra.si.edu", "jpl.nasa.gov", "space.com", "skyandtelescope.org", "astronomy.com", "universetoday.com",
    "earthsky.org", "skyatnightmagazine.com", "arxiv.org", "biorxiv.org", "medrxiv.org", "chemrxiv.org",
    "eartharxiv.org", "pubmed.ncbi.nlm.nih.gov", "ui.adsabs.harvard.edu", "crossref.org",
    "openalex.org", "semanticscholar.org",
]
FALLBACK_SCIENCE_DOMAINS = []
ALL_PRIMARY_DOMAINS = PRIMARY_SCIENCE_DOMAINS
ALL_FALLBACK_DOMAINS = FALLBACK_SCIENCE_DOMAINS
ALL_ALLOWED_DOMAINS = ALL_PRIMARY_DOMAINS


def normalized_domain(url_or_source):
    raw = safe_text(url_or_source).lower()
    if "://" in raw:
        raw = urlparse(raw).netloc
    return raw.split(":")[0].removeprefix("www.").strip().rstrip("/")


def is_domain_allowed(url, domains):
    domain = normalized_domain(url)
    return any(domain == d or domain.endswith("." + d) for d in domains)


def primary_domain_allowed(url, region=None):
    return is_domain_allowed(url, ALL_PRIMARY_DOMAINS)


def fallback_domain_allowed(url, region=None):
    return is_domain_allowed(url, ALL_FALLBACK_DOMAINS)


def allowed_source_for_region(url, region=None):
    return primary_domain_allowed(url, region) or fallback_domain_allowed(url, region)


GOOGLE_NEWS_QUERIES = {
    "Science": [
        "major science discovery research breakthrough",
        "important new scientific paper breakthrough",
        "major physics research discovery",
        "major biology genetics neuroscience research discovery",
        "major chemistry materials science discovery",
        "major climate earth ocean science research",
        "major astronomy astrophysics cosmology discovery",
        "major exoplanet black hole gravitational wave discovery",
        "major space science planetary science research",
        "major medical biomedical research discovery",
        "major mathematics scientific computing discovery",
    ],
}

GOOGLE_NEWS_LOCALE = {"Science": ("en-US", "US", "US:en")}

def resolve_google_news_url(link):
    """Google News RSS gives a redirect link, not the publisher URL.
    Follow it once (without downloading the full page) to get the
    real article URL. Return "" if it cannot be resolved safely."""
    try:
        response = session.get(
            link,
            timeout=10,
            allow_redirects=True,
            headers=HEADERS,
            stream=True,
        )
        real_url = safe_text(response.url)
        response.close()

        if not real_url or "news.google.com" in real_url:
            return ""

        return real_url

    except Exception:
        return ""


def google_news_gap_fill(
    region,
    existing_count,
    needed,
):
    # Same thin-coverage trigger as Exa, tried first because it is free.
    if existing_count >= max(
        6,
        needed * 3,
    ):
        return 0

    queries = GOOGLE_NEWS_QUERIES.get("Science", [])
    hl, gl, ceid = GOOGLE_NEWS_LOCALE.get("Science", ("en-US", "US", "US:en"))

    added = 0

    for query in queries:
        try:
            feed_url = (
                "https://news.google.com/rss/search?q="
                + quote(f"{query} when:2d")
                + f"&hl={hl}&gl={gl}&ceid={ceid}"
            )

            response = session.get(
                feed_url,
                timeout=15,
                headers=HEADERS,
            )

            if response.status_code >= 400:
                continue

            parsed = feedparser.parse(
                response.content
            )

            for entry in parsed.entries[:6]:
                title = safe_text(
                    entry.get("title")
                )
                link = safe_text(
                    entry.get("link")
                )

                if not title or not link:
                    continue

                real_url = resolve_google_news_url(
                    link
                )

                if not real_url:
                    continue

                published_dt = feed_entry_datetime(
                    entry
                )
                date_estimated = False

                if not published_dt:
                    published_dt = datetime.now(
                        BD_TZ
                    )
                    date_estimated = True

                item = {
                    "title": title,
                    "url": real_url,
                    "canonical": canonical_url(
                        real_url
                    ),
                    "published_dt": published_dt.isoformat(),
                    "published_date": published_dt.isoformat(),
                    "source": source_name(
                        real_url
                    ),
                    "region": region,
                    "excerpt": BeautifulSoup(
                        safe_text(
                            entry.get("summary")
                        ),
                        "html.parser",
                    ).get_text(
                        " ",
                        strip=True,
                    )[:2000],
                    "image": "",
                    "discovery": "google_news",
                    "date_estimated": date_estimated,
                }

                if not primary_domain_allowed(real_url, region):
                    continue

                if not candidate_basic_allowed(
                    {
                        **item,
                        "published_dt": published_dt,
                    }
                ):
                    continue

                if item["canonical"] in POSTED_URLS:
                    continue

                if item["canonical"] in STATE["queue"]:
                    continue

                queue_candidate(
                    item
                )
                added += 1

                if added >= MAX_GOOGLE_NEWS_CANDIDATES:
                    return added

        except Exception as exc:
            logger.warning(
                "Google News gap fill failed %s: %s",
                region,
                exc,
            )

    return added


def exa_gap_fill(region, existing_count, needed, fallback=False):
    if existing_count >= max(12, needed * 3):
        return 0

    domains = FALLBACK_SCIENCE_DOMAINS if fallback else PRIMARY_SCIENCE_DOMAINS
    if not domains:
        return 0
    queries = [
        "latest major science discovery research breakthrough",
        "latest important new scientific paper breakthrough",
        "latest major physics quantum particle materials research",
        "latest major biology genetics neuroscience research",
        "latest major chemistry discovery research",
        "latest climate earth ocean environmental science research",
        "latest astronomy astrophysics cosmology discovery",
        "latest exoplanet black hole gravitational wave discovery",
        "latest space science planetary science discovery",
        "latest major biomedical medicine research discovery",
        "latest mathematics scientific computing AI for science research",
    ]

    added = 0
    for query in queries:
        try:
            results = get_exa().search_and_contents(
                query, type="auto", category="news", num_results=8,
                include_domains=domains,
                start_published_date=DISCOVERY_START.isoformat(),
                end_published_date=DISCOVERY_END.isoformat(),
                contents={"highlights": {"max_characters": 900}},
            )
            for result in results.results:
                url = safe_text(getattr(result, "url", ""))
                title = safe_text(getattr(result, "title", ""))
                published_dt = parse_datetime(getattr(result, "published_date", ""))
                if not url or not title or not published_dt:
                    continue
                if fallback:
                    if not fallback_domain_allowed(url, region):
                        continue
                elif not primary_domain_allowed(url, region):
                    continue
                item = {
                    "title": title, "url": url, "canonical": canonical_url(url),
                    "published_dt": published_dt.isoformat(), "published_date": published_dt.isoformat(),
                    "source": source_name(url), "region": "Science",
                    "excerpt": safe_text(" ".join(getattr(result, "highlights", []) if isinstance(getattr(result, "highlights", []), list) else str(getattr(result, "highlights", ""))))[:2000],
                    "image": safe_text(getattr(result, "image", "")),
                    "discovery": "exa_fallback" if fallback else "exa",
                    "source_pool": "fallback" if fallback else "primary",
                }
                if not candidate_basic_allowed({**item, "published_dt": published_dt}):
                    continue
                if item["canonical"] in POSTED_URLS or item["canonical"] in STATE["queue"]:
                    continue
                queue_candidate(item)
                added += 1
                if added >= MAX_EXA_CANDIDATES:
                    return added
        except Exception as exc:
            logger.warning("Exa %s discovery failed: %s", "fallback" if fallback else "primary", exc)
    return added


def queue_candidates_for_region(
    region,
):
    count = 0

    for item in STATE[
        "queue"
    ].values():
        if (
            item.get("region")
            == region
            and item.get("status")
            == "pending"
        ):
            published = parse_datetime(
                item.get(
                    "published_date"
                )
            )

            if (
                published
                and DISCOVERY_START
                <= published
                <= DISCOVERY_END
            ):
                count += 1

    return count


# ============================================================
# CANDIDATE NORMALIZATION
# ============================================================

# ============================================================
# VERSION 1 EDITORIAL RANKING
# ============================================================

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "rank": {"type": "integer", "minimum": 1},
                    "score": {"type": "integer", "minimum": 0, "maximum": 10},
                    "important": {"type": "boolean"},
                    "topic": {"type": "string"},
                    "institution": {"type": "string"},
                    "event_key": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "rank", "score", "important", "topic", "institution", "event_key", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranked"],
    "additionalProperties": False,
}


def enrich_thin_excerpt(item):
    """Best-effort article enrichment. Failure never removes a candidate."""
    try:
        downloaded = trafilatura.fetch_url(item["url"])
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded)
        return safe_text(text)[:1200] if text else None
    except Exception:
        return None


def enrich_thin_excerpts(regional):
    enriched = 0
    for item in regional:
        if enriched >= MAX_EXCERPT_ENRICH:
            break
        excerpt = safe_text(item.get("excerpt", ""))
        if len(excerpt) >= THIN_EXCERPT_CHARS:
            continue
        fuller = enrich_thin_excerpt(item)
        if fuller and len(fuller) > len(excerpt):
            item["excerpt"] = fuller
            enriched += 1
    return regional


def _rank_prompt(region):
    topic_list = ", ".join(TOPICS[region])
    return f"""
You are the editor-in-chief of @ScienceNewsroom.

Rank this batch of science news candidates by REAL scientific and editorial importance in the previous 24 hours.
The channel is science-only. Prefer discoveries, research results, observations, measurements and scientific developments
with meaningful evidence or direct consequences. Do not rank by sensational wording or novelty alone.
Do not invent facts. Return EVERY candidate in this batch.

There is NO requirement to publish a story from every discipline. Diversity is a selection preference only after scientific
importance is established. Never lower a score just because another story covers the same field.

High-priority areas when genuinely important:
1. Major new scientific discoveries or observations that materially advance knowledge.
2. Breakthrough experimental or theoretical results with strong evidence.
3. Important astronomy/astrophysics findings: exoplanets, black holes, gravitational waves, early universe, unusual objects,
   major telescope observations and other results with real scientific significance.
4. Major biology, genetics, evolution, neuroscience, ecology or biomedical research findings.
5. Major physics, quantum, particle, nuclear, chemistry or materials discoveries.
6. Important climate, Earth, ocean, environmental and planetary science results.
7. Significant mathematics and scientific-computing results when they materially advance a field.
8. High-impact preprints when the underlying evidence and result are genuinely notable, but clearly label them as preprints.
9. Important new instruments, observatories, datasets or methods that materially expand scientific capability.

Evidence hierarchy:
- Peer-reviewed published research: strongest.
- Accepted/in-press research: strong.
- Credible preprint: eligible when genuinely important, but never treat it as established consensus.
- University/institutional research release: useful evidence, but check the underlying research status.
- Science journalism: useful for discovery and context, not proof by itself.

Normally score low or reject:
- press-release-only hype without a substantive result
- weakly supported claims of "cure", "proof", "alien life", "revolutionary", or "solved"
- ordinary telescope/rocket/mission updates with no important scientific result
- routine medical advice or consumer health news
- product launches and corporate business news without a substantive scientific result
- conference promotions, podcasts, webinars, opinion/commentary and generic explainers
- duplicate coverage of a previously posted research event
- minor incremental results with limited field impact

Scoring:
9-10 exceptional scientific significance; rare, major advances or discoveries
7-8 clearly important and publishable
4-6 interesting but normally not publishable
0-3 low-value, promotional, speculative, routine or non-scientific

A score of 7+ is required for publication. The important field MUST be true only when score >= 7.

Return EVERY candidate with:
id, rank, score, important, topic, institution, event_key, reason.

Allowed topics:
{topic_list}
"""


def _rank_batch(batch, region, batch_no):
    lines = []
    for idx, item in enumerate(batch, start=1):
        published = item.get("published_date", "")
        age_note = ""
        dt = parse_datetime(published)
        if dt:
            age_hours = max(0.0, (NOW_BD - dt).total_seconds() / 3600)
            age_note = f"Age: {age_hours:.1f} hours"
        lines.append("\n".join([
            f"ID: {idx}",
            f"Title: {item.get('title','')}",
            f"Source: {item.get('source','')}",
            f"Published: {published}",
            age_note,
            f"Description/Excerpt: {trim_source_text(item.get('excerpt',''), 650)}",
            "",
        ]))

    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": _rank_prompt(region)},
                {"role": "user", "content": "\n".join(lines)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": f"science_news_rank_batch_{batch_no}",
                    "strict": True,
                    "schema": RANK_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=3500,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return data.get("ranked", [])
    except Exception as exc:
        logger.error("Ranking batch %d failed: %s", batch_no, exc)
        return []


def rank_candidates(candidates, region):
    """Rank the discovery pool in bounded LLM batches, then merge globally.

    Batching prevents a large structured response from being truncated. The merged result
    is globally ordered by editorial score, then model rank, then freshness.
    """
    if not candidates:
        return []

    regional = sorted(
        candidates,
        key=lambda x: parse_datetime(x.get("published_date")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:80]

    batch_size = 15
    ranked_rows = []
    for offset in range(0, len(regional), batch_size):
        batch = regional[offset:offset + batch_size]
        logger.info("%s RANK BATCH %d: %d candidates", region, offset // batch_size + 1, len(batch))
        rows = _rank_batch(batch, region, offset // batch_size + 1)
        by_id = {idx: item for idx, item in enumerate(batch, start=1)}
        for row in rows:
            try:
                idx = int(row["id"])
            except Exception:
                continue
            if idx not in by_id:
                continue
            item = dict(by_id[idx])
            score = max(0, min(10, int(row.get("score", 0))))
            item.update({
                "importance_score": score,
                "important": bool(row.get("important")) and score >= 7,
                "topic": canonical_topic(safe_text(row.get("topic")), region),
                "institution": safe_text(row.get("institution")),
                "event_key": safe_text(row.get("event_key")),
                "rank_reason": safe_text(row.get("reason")),
                "batch_rank": int(row.get("rank", 9999)),
            })
            ranked_rows.append(item)

    # A failed/partial batch is recoverable, but never receives an invented importance score.
    # It remains available only for diagnostics, not eligibility.
    ranked_rows.sort(key=lambda x: (
        -x.get("importance_score", 0),
        x.get("batch_rank", 9999),
        -(parse_datetime(x.get("published_date")).timestamp() if parse_datetime(x.get("published_date")) else 0),
    ))

    for rank, item in enumerate(ranked_rows, start=1):
        item["editor_rank"] = rank

    logger.info("%s RANK MODEL ROWS: %d/%d", region, len(ranked_rows), len(regional))
    return ranked_rows


# ============================================================
# VERSION 1 EVENT DEDUPLICATION
# ============================================================

def extract_entities(text):
    words = re.findall(r"[A-Za-z][A-Za-z&'-]{1,}", safe_text(text).lower())
    return {w for w in words if w not in STOPWORDS}


def entity_overlap(a, b):
    ea = extract_entities(f"{a.get('title','')} {a.get('excerpt','')}")
    eb = extract_entities(f"{b.get('title','')} {b.get('excerpt','')}")
    if not ea or not eb:
        return 0.0
    return len(ea & eb) / max(1, min(len(ea), len(eb)))


def event_similarity_v04(a, b):
    title_score = title_similarity(a.get("title", ""), b.get("title", ""))
    entity_score = entity_overlap(a, b)
    return (0.75 * title_score) + (0.25 * entity_score)


def same_event_window(a, b, hours=30):
    da = parse_datetime(a.get("published_date"))
    db = parse_datetime(b.get("published_date"))
    if not da or not db:
        return False
    return abs((da - db).total_seconds()) <= hours * 3600


def cluster_ranked_events(ranked):
    """Conservative event clustering. Uncertain items are always kept."""
    clusters = []
    ordered = sorted(
        ranked,
        key=lambda x: x.get("editor_rank", 9999),
    )
    for item in ordered:
        placed = False
        for cluster in clusters:
            representative = cluster[0]
            item_event_key = safe_text(item.get("event_key"))
            rep_event_key = safe_text(representative.get("event_key"))
            same_key = bool(
                item_event_key
                and rep_event_key
                and item_event_key == rep_event_key
                and (
                    entity_overlap(item, representative) >= 0.25
                    or title_similarity(item.get("title", ""), representative.get("title", "")) >= 0.55
                )
            )
            if same_key or (
                same_event_window(item, representative)
                and event_similarity_v04(item, representative) >= 0.88
            ):
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    output = []
    for index, cluster in enumerate(clusters, start=1):
        representative = cluster[0]
        stable_key = normalize_title(representative.get("title", "")) or representative.get("canonical", "")
        digest = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:10]
        cluster_id = f"evt_{digest}"
        sources = sorted({safe_text(x.get("source")) for x in cluster if safe_text(x.get("source"))})
        for member in cluster:
            row = dict(member)
            row.update({
                "event_cluster_id": cluster_id,
                "event_cluster_size": len(cluster),
                "event_sources": sources,
                "event_source_count": len(sources),
                "event_confidence": 1.0 if len(cluster) > 1 else 0.6,
            })
            output.append(row)
    return output


def collapse_event_clusters(ranked):
    clustered = cluster_ranked_events(ranked)
    winners = {}
    for item in clustered:
        key = item.get("event_cluster_id") or item.get("canonical")
        old = winners.get(key)
        if old is None:
            winners[key] = item
            continue
        # Preserve the highest editorial rank, then newest story.
        item_key = (
            item.get("editor_rank", 9999),
            -(parse_datetime(item.get("published_date")).timestamp() if parse_datetime(item.get("published_date")) else 0),
        )
        old_key = (
            old.get("editor_rank", 9999),
            -(parse_datetime(old.get("published_date")).timestamp() if parse_datetime(old.get("published_date")) else 0),
        )
        if item_key < old_key:
            winners[key] = item
    return sorted(winners.values(), key=lambda x: x.get("editor_rank", 9999))


def persist_event_cluster_state(ranked):
    clusters = STATE.setdefault("event_clusters", {})
    for item in ranked:
        event_id = item.get("event_cluster_id")
        if not event_id:
            continue
        clusters[event_id] = {
            "event_id": event_id,
            "topic": item.get("topic", ""),
            "region": item.get("region", ""),
            "sources": item.get("event_sources", []),
            "source_count": item.get("event_source_count", 0),
            "confidence": item.get("event_confidence", 0),
            "last_seen": now_iso(),
            "headline": item.get("title", ""),
        }


def remember_posted_event(story):
    event_id = story.get("event_cluster_id") or make_event_id(story)
    ids = STATE.setdefault("posted_event_ids", [])
    if event_id and event_id not in ids:
        ids.append(event_id)
    STATE["posted_event_ids"] = ids[-500:]
    return event_id


# ============================================================
# ARTICLE EXTRACTION
# ============================================================

def _unique_image_urls(urls, base_url=""):
    seen = set()
    result = []
    for value in urls:
        raw = safe_text(value)
        if not raw:
            continue
        absolute = urljoin(base_url or "", raw)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        key = absolute.split("#", 1)[0]
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _jsonld_image_values(value):
    values = []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        for item in value:
            values.extend(_jsonld_image_values(item))
        return values
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "image"):
            item = value.get(key)
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, (dict, list)):
                values.extend(_jsonld_image_values(item))
    return values


def find_image_candidates(
    url,
    page_html=None,
    final_url=None,
    initial_url="",
):
    candidates = []
    base_url = final_url or url

    try:
        if page_html is None:
            response = session.get(
                url,
                headers={
                    **HEADERS,
                    "Referer": url,
                },
                timeout=20,
            )
            if response.status_code >= 400:
                return [initial_url] if initial_url else []
            page_html = response.text
            base_url = response.url

        if initial_url:
            candidates.append(initial_url)

        soup = BeautifulSoup(page_html, "html.parser")

        # Highest-signal article image metadata first.
        for attrs in (
            {"property": "og:image"},
            {"property": "og:image:url"},
            {"name": "twitter:image"},
            {"name": "twitter:image:src"},
            {"itemprop": "image"},
        ):
            for tag in soup.find_all("meta", attrs=attrs):
                content = safe_text(tag.get("content"))
                if content:
                    candidates.append(content)

        for tag in soup.find_all("link"):
            rel = {safe_text(x).lower() for x in tag.get("rel", [])}
            if {"image_src"} & rel:
                href = safe_text(tag.get("href"))
                if href:
                    candidates.append(href)

        # JSON-LD article image metadata, including nested ImageObject values.
        for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\\+json", re.I)}):
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
                    main_entity = obj.get("mainEntity")
                    if isinstance(main_entity, dict):
                        candidates.extend(_jsonld_image_values(main_entity.get("image")))

        # Last HTML fallback: article/content images with srcset support.
        for tag in soup.select("article img, main img, figure img, img")[:40]:
            src = safe_text(tag.get("src"))
            if src:
                candidates.append(src)
            for attr in ("data-src", "data-original", "data-lazy-src"):
                value = safe_text(tag.get(attr))
                if value:
                    candidates.append(value)
            srcset = safe_text(tag.get("srcset") or tag.get("data-srcset"))
            if srcset:
                parts = [x.strip().split(" ")[0] for x in srcset.split(",") if x.strip()]
                candidates.extend(parts)

        return _unique_image_urls(candidates, base_url)
    except Exception as exc:
        logger.warning("Image candidate extraction failed %s: %s", url, exc)
        return _unique_image_urls(candidates, base_url)


def find_og_image(
    url,
    page_html=None,
    final_url=None,
):
    candidates = find_image_candidates(url, page_html, final_url)
    return candidates[0] if candidates else ""


def extract_article(
    item,
):
    url = item["url"]

    try:
        response = session.get(
            url,
            headers={
                **HEADERS,
                "Referer": url,
            },
            timeout=25,
        )

        if response.status_code < 400:
            page_html = response.text

            text = trafilatura.extract(
                page_html,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )

            image_candidates = find_image_candidates(
                url,
                page_html,
                response.url,
                initial_url=item.get("image", ""),
            )
            item["_image_candidates"] = image_candidates
            image_url = image_candidates[0] if image_candidates else ""

            if text and len(safe_text(text)) >= 500:
                return (
                    safe_text(text),
                    image_url,
                )

    except Exception as exc:
        logger.warning(
            "Local extraction failed %s: %s",
            url,
            exc,
        )

    try:
        result_set = get_exa().get_contents(
            [url],
            text={
                "max_characters": 12000,
            },
        )

        if result_set.results:
            result = result_set.results[0]

            text = safe_text(
                getattr(
                    result,
                    "text",
                    "",
                )
            )

            exa_image = safe_text(getattr(result, "image", ""))
            prior = item.get("_image_candidates", [])
            candidates = _unique_image_urls(
                [*prior, item.get("image", ""), exa_image]
            )
            item["_image_candidates"] = candidates
            image_url = candidates[0] if candidates else ""

            if text:
                return (
                    text,
                    image_url,
                )

    except Exception as exc:
        logger.warning(
            "Exa article fallback failed %s: %s",
            url,
            exc,
        )

    return (
        "",
        item.get("image", ""),
    )


# ============================================================
# STORY + KNOWLEDGE GENERATION
# ============================================================

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "highlights": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 5,
        },
        "the_context": {"type": "string"},
        "bottom_line": {"type": "string"},
        "bold_terms": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 16,
        },
    },
    "required": [
        "headline",
        "summary",
        "highlights",
        "the_context",
        "bottom_line",
        "bold_terms",
    ],
    "additionalProperties": False,
}


def first_sentence(text):
    text = clean_generated_text(
        text
    )

    # Conservative sentence extraction. Avoids common tech
    # abbreviations and decimals splitting incorrectly.
    protected = {
        "U.S.": "US_SENTINEL",
        "U.K.": "UK_SENTINEL",
        "E.U.": "EU_SENTINEL",
        "No.": "NO_SENTINEL",
        "Inc.": "INC_SENTINEL",
        "Ltd.": "LTD_SENTINEL",
        "Dr.": "DR_SENTINEL",
        "Mr.": "MR_SENTINEL",
        "Mrs.": "MRS_SENTINEL",
        "Ms.": "MS_SENTINEL",
    }

    working = text

    for old, marker in protected.items():
        working = working.replace(
            old,
            marker,
        )

    match = re.search(
        r"(.+?[.!?])(?:\s|$)",
        working,
    )

    if match:
        sentence = match.group(1)
    else:
        sentence = working

    for old, marker in protected.items():
        sentence = sentence.replace(
            marker,
            old,
        )

    return clean_generated_text(
        sentence
    )


def generate_story(
    item,
    article_text,
):
    topic_hint = item.get(
        "topic",
        "",
    )

    prompt = f"""
You are a senior newspaper tech editor and knowledge editor for @TheScienceNewsroom.

Create a compact Telegram science news card from the source article.

Primary topic:
{topic_hint}

Return ONLY valid JSON matching the schema.

PUBLIC CONTENT:
- Headline: 6-14 words, accurate, newspaper style.
- Summary: exactly ONE complete sentence, about 18-28 words.
- Highlights: 3-5 short factual points, choosing the number that best fits the story.
- The Context: 2-4 complete sentences of background explaining how the story came about.
  This must explain the scientific context: prior work, the research question, what changed, and why the result matters.
  State research status when relevant (peer-reviewed, accepted, preprint, observation, etc.).
- Bottom Line: exactly ONE complete sentence giving the central takeaway or "so what" of the story.
- No repetition between sections.
- No "..." or "…".
- Never end a headline or highlight with an ellipsis.
- No hashtags in generated fields.
- No Markdown or HTML in JSON fields.

BOLD TERMS:
- Include important institutions, researchers, instruments, missions, organisms, species, measurements, dates,
  sample sizes, percentages, statistical values, physical quantities, paper status, and scientific terms appearing in the generated
  headline, summary or highlights.

The public post must follow this exact order:
Photo
Headline
1-sentence summary
## KEY HIGHLIGHTS
3-5 bullets
## THE CONTEXT (collapsed by default)
2-4 sentences of background
## BOTTOM LINE (collapsed by default)
1 sentence takeaway
#hashtags
**Source:** Publication
"""

    user = (
        f"REGION: {item['region']}\n"
        f"SOURCE: {item['source']}\n"
        f"TITLE: {item['title']}\n"
        f"DATE: {item['published_date']}\n\n"
        f"ARTICLE:\n{article_text[:12000]}"
    )

    for attempt in range(3):
        try:
            response = get_cerebras().chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": prompt,
                    },
                    {
                        "role": "user",
                        "content": user,
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "science_news_story_v1_0",
                        "strict": True,
                        "schema": STORY_SCHEMA,
                    },
                },
                reasoning_effort="low",
                temperature=0.2,
                max_completion_tokens=1400,
            )

            data = json.loads(
                safe_text(
                    response.choices[0]
                    .message
                    .content
                )
            )

            headline = clean_generated_text(
                data.get(
                    "headline"
                )
            )

            summary = first_sentence(
                data.get(
                    "summary"
                )
            )

            highlights = [
                clean_generated_text(x)
                for x in data.get("highlights", [])
                if clean_generated_text(x)
            ]
            if not (3 <= len(highlights) <= 5):
                raise ValueError("Highlights must contain 3-5 points")

            the_context = clean_generated_text(data.get("the_context"))
            bottom_line = clean_generated_text(data.get("bottom_line"))
            context_count = len(re.findall(r"(?<=[.!?])\s+", the_context)) + (1 if the_context and the_context[-1] in ".!?" else 0)
            bottom_count = len(re.findall(r"(?<=[.!?])\s+", bottom_line)) + (1 if bottom_line and bottom_line[-1] in ".!?" else 0)
            if not the_context or not bottom_line or not (2 <= context_count <= 4) or bottom_count != 1:
                raise ValueError("Invalid The Context or Bottom Line")

            if (
                not headline
                or not summary
                or not complete_text(headline)
                or not complete_text(summary)
                or any(not complete_text(x) for x in highlights)
                or not complete_text(the_context)
                or not complete_text(bottom_line)
            ):
                raise ValueError("Incomplete story")

            story = {
                **item,
                "headline": trim_source_text(headline, 110),
                "summary": trim_source_text(summary, 260),
                "highlights": [trim_source_text(x, 130) for x in highlights],
                "the_context": trim_source_text(the_context, 520),
                "bottom_line": trim_source_text(bottom_line, 220),
                "bold_terms": [safe_text(x) for x in data.get("bold_terms", []) if safe_text(x)],
            }

            return story

        except Exception as exc:
            logger.warning(
                "Story generation attempt %d failed: %s",
                attempt + 1,
                exc,
            )

            if attempt == 0:
                time.sleep(1)

    return None


# ============================================================
# NUMERIC GROUNDING
# ============================================================

NUMBER_RE = re.compile(
    r"""
    (?:
        (?:US|U\.S\.|HK|HK\$|Tk|BDT|USD|EUR|GBP|JPY|CNY|INR|৳|\$|€|£|¥)
        \s*
    )?
    \d[\d,]*(?:\.\d+)?
    \s*
    (?:
        million|billion|trillion|
        crore|lakh|bn|mn|b|m|k|%
    )?
    """,
    re.I | re.X,
)

YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")

_NUMERIC_SCALES = {
    "": 1.0,
    "k": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
    "trillion": 1e12,
    "t": 1e12,
    "crore": 1e7,
    "lakh": 1e5,
}
_NUMERIC_CURRENCIES = {
    "$": "usd",
    "usd": "usd",
    "tk": "bdt",
    "bdt": "bdt",
    "৳": "bdt",
    "€": "eur",
    "eur": "eur",
    "£": "gbp",
    "gbp": "gbp",
    "¥": "jpy",
    "jpy": "jpy",
    "cny": "cny",
    "inr": "inr",
    "₹": "inr",
    "hk$": "hkd",
    "hk": "hkd",
}

def _numeric_signature(raw):
    text = safe_text(raw).strip().lower()
    if not text:
        return None

    currency = None
    for symbol in sorted(_NUMERIC_CURRENCIES, key=len, reverse=True):
        if text.startswith(symbol):
            currency = _NUMERIC_CURRENCIES[symbol]
            text = text[len(symbol):].strip()
            break

    if text.endswith("%"):
        unit = "%"
        text = text[:-1].strip()
    else:
        m = re.search(r"(trillion|billion|million|crore|lakh|bn|mn|[kmbt])$", text)
        unit = m.group(1) if m else ""
        if m:
            text = text[:m.start()].strip()

    text = text.replace(",", "")
    try:
        value = float(text)
    except ValueError:
        return None

    if unit == "%":
        scale = 1.0
        percent = True
    else:
        scale = _NUMERIC_SCALES.get(unit, 1.0)
        percent = False

    return {
        "value": value * scale,
        "percent": percent,
        "currency": currency,
    }

def normalize_number(raw):
    sig = _numeric_signature(raw)
    if not sig:
        return ""
    value = sig["value"]
    value_key = f"{value:.12g}"
    currency = sig["currency"] or ""
    percent = "%" if sig["percent"] else ""
    return f"{currency}|{percent}|{value_key}"

def numeric_tokens(text):
    tokens = []
    for match in NUMBER_RE.finditer(safe_text(text)):
        token = safe_text(match.group(0)).strip()
        sig = _numeric_signature(token)
        if not sig:
            continue

        # Ignore bare years, but retain years with a financial/percent/unit marker.
        numeric_value = sig["value"]
        if (
            numeric_value.is_integer()
            and YEAR_RE.match(str(int(numeric_value)))
            and not sig["percent"]
            and not sig["currency"]
        ):
            continue

        if token:
            tokens.append(token)
    return tokens

def _numeric_equivalent(source_sig, generated_sig):
    if not source_sig or not generated_sig:
        return False
    if source_sig["percent"] != generated_sig["percent"]:
        return False

    # If both explicitly name currencies, they must agree.
    # If only one names a currency, accept the numeric equivalent because
    # source extraction often drops currency symbols around abbreviated forms.
    if (
        source_sig["currency"]
        and generated_sig["currency"]
        and source_sig["currency"] != generated_sig["currency"]
    ):
        return False

    return abs(source_sig["value"] - generated_sig["value"]) <= max(
        1e-9, abs(source_sig["value"]) * 1e-9
    )

def numeric_grounded(story, article_text):
    source_sigs = [
        _numeric_signature(x)
        for x in numeric_tokens(article_text)
    ]
    source_sigs = [x for x in source_sigs if x]

    generated_text = " ".join(
        [
            story.get("headline", ""),
            story.get("summary", ""),
            *story.get("highlights", []),
        ]
    )

    for token in numeric_tokens(generated_text):
        generated_sig = _numeric_signature(token)
        if not generated_sig:
            continue

        if not any(
            _numeric_equivalent(source_sig, generated_sig)
            for source_sig in source_sigs
        ):
            return False, token

    return True, ""


# ============================================================
# BOLD TERMS
# ============================================================

def derive_bold_terms(
    story,
):
    terms = [
        safe_text(x)
        for x in story.get(
            "bold_terms",
            [],
        )
        if safe_text(x)
    ]

    combined = " ".join(
        [
            story.get(
                "summary",
                "",
            ),
            *story.get(
                "highlights",
                [],
            ),
        ]
    )

    # Financial figures, but do not bold bare years.
    for match in NUMBER_RE.finditer(
        combined
    ):
        token = safe_text(
            match.group(0)
        )

        numeric_only = re.sub(
            r"[^\d.]",
            "",
            token,
        )

        if (
            YEAR_RE.match(
                numeric_only
            )
            and not re.search(
                r"(Tk|BDT|USD|EUR|GBP|JPY|CNY|INR|৳|\$|€|£|¥|%|million|billion|crore|lakh)",
                token,
                re.I,
            )
        ):
            continue

        if token:
            terms.append(
                token
            )

    unique = []
    seen = set()

    for term in sorted(
        terms,
        key=len,
        reverse=True,
    ):
        key = term.lower()

        if (
            len(term) >= 2
            and key not in seen
        ):
            seen.add(key)
            unique.append(term)

    return unique[:16]


def escape_rich_html(
    text,
):
    return html.escape(
        clean_generated_text(text),
        quote=False,
    )


def bold_terms_html(
    text,
    terms,
):
    text = clean_generated_text(
        text
    )

    if not text:
        return ""

    result = text

    # Use letter-only markers to avoid collisions with numeric terms.
    replacements = []

    for index, term in enumerate(
        sorted(
            {
                safe_text(x)
                for x in terms
                if safe_text(x)
            },
            key=len,
            reverse=True,
        )
    ):
        marker = (
            f"__RICHBOLD_{chr(65 + (index % 26))}"
            f"{index // 26}__"
        )

        pattern = re.compile(
            re.escape(term),
            re.I,
        )

        match = pattern.search(
            result
        )

        if match:
            original = match.group(
                0
            )
            result = (
                result[:match.start()]
                + marker
                + result[match.end():]
            )
            replacements.append(
                (
                    marker,
                    original,
                )
            )

    escaped = html.escape(
        result,
        quote=False,
    )

    for marker, original in replacements:
        escaped = escaped.replace(
            marker,
            "<b>"
            + html.escape(
                original,
                quote=False,
            )
            + "</b>",
        )

    return escaped


# ============================================================
# DYNAMIC RICH MESSAGE HTML
# ============================================================

def dynamic_rich_html(story):
    terms = derive_bold_terms(story)
    parts = [
        '<img src="tg://photo?id=newsphoto">',
        "<h1>" + escape_rich_html(story["headline"]) + "</h1>",
        "<p>" + bold_terms_html(story["summary"], terms) + "</p>",
        "<h2>KEY HIGHLIGHTS</h2>",
        "<p>" + "<br>".join(
            "• " + bold_terms_html(point, terms)
            for point in story.get("highlights", [])
        ) + "</p>",
        "<blockquote expandable><b>THE CONTEXT</b><br>"
        + bold_terms_html(story.get("the_context", ""), terms)
        + "</blockquote>",
        "<blockquote expandable><b>BOTTOM LINE</b><br>"
        + bold_terms_html(story.get("bottom_line", ""), terms)
        + "</blockquote>",
    ]

    hashtags = " ".join(category_hashtags(story))
    if hashtags:
        parts.append("<p>" + escape_rich_html(hashtags) + "</p>")

    source = escape_rich_html(story["source"])
    url = html.escape(story["url"], quote=True)
    parts.append(
        "<footer><b>Source:</b> "
        f'<a href="{url}">{source}</a>'
        "</footer>"
    )

    return "\n".join(parts)


def rich_visible_length(text):
    """Return Telegram-visible character count for Rich HTML text.

    Telegram limits the rendered text, not the raw HTML markup, so strip
    tags and decode HTML entities before counting characters.
    """
    no_tags = re.sub(r"<[^>]+>", "", text)
    no_attrs = re.sub(
        r"\[[^\]]+\]\([^)]+\)",
        lambda m: m.group(0).split("]")[0][1:],
        no_tags,
    )
    return len(html.unescape(no_attrs))


def fit_rich_html(story):
    variants = [
        (260, 130, 520, 220),
        (220, 115, 440, 190),
        (190, 100, 380, 170),
        (160, 85, 320, 150),
    ]

    for summary_len, highlight_len, context_len, bottom_len in variants:
        candidate = dict(story)
        candidate["summary"] = trim_source_text(story["summary"], summary_len)
        candidate["highlights"] = [trim_source_text(x, highlight_len) for x in story.get("highlights", [])]
        candidate["the_context"] = trim_source_text(story.get("the_context", ""), context_len)
        candidate["bottom_line"] = trim_source_text(story.get("bottom_line", ""), bottom_len)
        html_text = dynamic_rich_html(candidate)
        if rich_visible_length(html_text) <= MAX_RICH_CHARACTERS:
            return html_text

    return dynamic_rich_html(story)



# ============================================================
# IMAGE BRANDING: ONLY @TheScienceNewsroom
# ============================================================

def find_font(
    bold=False,
):
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )

    for path in candidates:
        if os.path.exists(path):
            return path

    return None



def crop_cover(
    image,
    size=(1200, 675),
):
    target_w, target_h = size

    ratio = max(
        target_w / image.width,
        target_h / image.height,
    )

    resized = image.resize(
        (
            int(
                image.width
                * ratio
            ),
            int(
                image.height
                * ratio
            ),
        ),
        Image.Resampling.LANCZOS,
    )

    left = (
        resized.width
        - target_w
    ) // 2

    top = (
        resized.height
        - target_h
    ) // 2

    return resized.crop(
        (
            left,
            top,
            left + target_w,
            top + target_h,
        )
    )


def image_average_brightness(
    image,
):
    small = image.resize(
        (1, 1)
    ).convert(
        "L"
    )
    return small.getpixel(
        (0, 0)
    )


def display_source_name(source):
    """Return a reader-friendly publication label for the image fallback."""
    raw = safe_text(source).strip()
    return raw or "Science News"


def _source_domain_from_story(source, article_url):
    article_host = urlparse(safe_text(article_url)).netloc.lower().removeprefix("www.")
    if article_host:
        return article_host

    source_lower = safe_text(source).lower().strip()
    for domain, name in SOURCE_NAMES.items():
        if source_lower == safe_text(name).lower():
            return domain
    return ""


def source_logo_candidates(source, article_url):
    domain = _source_domain_from_story(source, article_url)
    if not domain:
        return []

    homepage = f"https://{domain}/"
    candidates = []

    try:
        response = session.get(
            homepage,
            headers={**HEADERS, "Referer": article_url or homepage},
            timeout=15,
        )
        if response.status_code < 400:
            soup = BeautifulSoup(response.text, "html.parser")
            base_url = response.url

            for tag in soup.find_all("link"):
                rel = {safe_text(x).lower() for x in tag.get("rel", [])}
                if rel & {"icon", "shortcut", "apple-touch-icon", "apple-touch-icon-precomposed"}:
                    href = safe_text(tag.get("href"))
                    if href:
                        candidates.append(urljoin(base_url, href))

            for attrs in (
                {"property": "og:logo"},
                {"name": "og:logo"},
                {"itemprop": "logo"},
            ):
                for tag in soup.find_all("meta", attrs=attrs):
                    content = safe_text(tag.get("content"))
                    if content:
                        candidates.append(urljoin(base_url, content))

            for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\\+json", re.I)}):
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
                        publisher = obj.get("publisher")
                        if isinstance(publisher, dict):
                            candidates.extend(_jsonld_image_values(publisher.get("logo")))

    except Exception as exc:
        logger.warning("Source logo discovery failed %s: %s", source, exc)

    candidates.extend([
        f"https://{domain}/favicon.ico",
        f"https://{domain}/favicon.png",
        f"https://www.google.com/s2/favicons?domain={quote(domain)}&sz=256",
    ])

    return _unique_image_urls(candidates, homepage)


def download_image(
    url,
    referer,
    min_width=400,
    min_height=250,
):
    if not url:
        return None

    try:
        response = session.get(
            url,
            headers={
                **HEADERS,
                "Referer": referer,
            },
            timeout=20,
            stream=True,
        )

        if response.status_code >= 400:
            return None

        content_type = (
            response.headers.get(
                "content-type",
                "",
            )
            .lower()
        )

        if (
            content_type
            and not content_type.startswith(
                "image/"
            )
        ):
            return None

        buf = BytesIO()

        for chunk in response.iter_content(
            65536
        ):
            if not chunk:
                continue

            buf.write(
                chunk
            )

            if buf.tell() > 8_000_000:
                return None

        buf.seek(0)

        image = Image.open(
            buf
        )
        image.load()

        if (
            image.width < min_width
            or image.height < min_height
        ):
            return None

        return image.convert(
            "RGB"
        )

    except Exception as exc:
        logger.warning(
            "Image download failed: %s",
            exc,
        )
        return None


def download_logo(
    url,
    referer,
):
    """Download a source logo, accepting small favicon-sized assets."""
    image = download_image(
        url,
        referer,
        min_width=24,
        min_height=24,
    )
    if image is None:
        return None

    if image.width < 512 or image.height < 512:
        scale = min(768 / image.width, 768 / image.height)
        if scale > 1:
            image = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
    return image


def _rounded_logo_on_fallback(logo, size=(420, 260)):
    target_w, target_h = size
    if logo.width <= 0 or logo.height <= 0:
        return None
    ratio = min(target_w / logo.width, target_h / logo.height)
    new_size = (max(1, int(logo.width * ratio)), max(1, int(logo.height * ratio)))
    return logo.resize(new_size, Image.Resampling.LANCZOS).convert("RGBA")


def make_source_fallback(source, logo=None):
    """Create a polished source-branded fallback when article imagery is unavailable."""
    canvas = Image.new("RGB", (1200, 675), (25, 35, 47))
    draw = ImageDraw.Draw(canvas)

    # Subtle visual hierarchy without depending on external assets.
    draw.rectangle((40, 40, 1160, 635), outline=(55, 74, 92), width=3)

    if logo is not None:
        logo_rgba = _rounded_logo_on_fallback(logo)
        if logo_rgba is not None:
            x = (1200 - logo_rgba.width) // 2
            y = 155
            canvas_rgba = canvas.convert("RGBA")
            canvas_rgba.alpha_composite(logo_rgba, (x, y))
            canvas = canvas_rgba.convert("RGB")
            return canvas

    source_text = display_source_name(source)
    font_path = find_font(bold=True)
    font = ImageFont.truetype(font_path, 76) if font_path else ImageFont.load_default()

    words = source_text.split()
    lines = []
    current = ""
    max_chars = 18
    for word in words:
        trial = f"{current} {word}".strip()
        if current and len(trial) > max_chars:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    if not lines:
        lines = ["Science News"]

    line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_heights = [box[3] - box[1] for box in line_boxes]
    total_h = sum(line_heights) + 18 * (len(lines) - 1)
    y = (675 - total_h) / 2 - 4

    for line, box, line_h in zip(lines, line_boxes, line_heights):
        w = box[2] - box[0]
        x = (1200 - w) / 2
        draw.text((x, y), line, font=font, fill=(245, 248, 250))
        y += line_h + 18

    return canvas


def branded_card(
    photo,
    source,
    source_position="left",
):
    """Crop the image and add only the channel chip."""
    base = crop_cover(
        photo
    ).convert(
        "RGBA"
    )

    brightness = image_average_brightness(
        base
    )

    if brightness < 125:
        chip_bg = (245, 245, 245, 225)
        chip_fg = (20, 24, 28, 255)
    else:
        chip_bg = (18, 22, 28, 205)
        chip_fg = (245, 245, 245, 255)

    overlay = Image.new(
        "RGBA",
        base.size,
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(overlay)

    font_path = find_font(
        bold=True
    )
    if font_path:
        font = ImageFont.truetype(
            font_path,
            24,
        )
    else:
        font = ImageFont.load_default()

    channel_text = "@ScienceNewsroom"
    bbox = draw.textbbox(
        (0, 0),
        channel_text,
        font=font,
    )
    padding_x = 18
    padding_y = 9
    margin_x = 28
    margin_y = 24
    chip_w = (bbox[2] - bbox[0]) + padding_x * 2
    chip_h = (bbox[3] - bbox[1]) + padding_y * 2
    x2 = 1200 - margin_x
    y2 = 675 - margin_y
    x1 = x2 - chip_w
    y1 = y2 - chip_h

    draw.rounded_rectangle(
        (x1, y1, x2, y2),
        radius=16,
        fill=chip_bg,
    )
    draw.text(
        (x1 + padding_x, y1 + padding_y - 1),
        channel_text,
        font=font,
        fill=chip_fg,
    )

    return Image.alpha_composite(
        base,
        overlay,
    ).convert(
        "RGB"
    )


def prepare_image(
    story,
    index,
):
    image = None
    image_candidates = []
    image_candidates.extend(story.get("_image_candidates", []) or [])
    image_candidates.extend(story.get("image_candidates", []) or [])
    if story.get("image_url"):
        image_candidates.append(story.get("image_url"))
    if story.get("image"):
        image_candidates.append(story.get("image"))
    image_candidates = _unique_image_urls(image_candidates)

    for candidate in image_candidates:
        image = download_image(candidate, story["url"])
        if image is not None:
            break

    fallback_kind = "article"

    if image is None:
        for logo_url in source_logo_candidates(
            story.get("source", "Science News"),
            story.get("url", ""),
        ):
            logo = download_logo(logo_url, story.get("url", ""))
            if logo is not None:
                image = make_source_fallback(story.get("source", "Science News"), logo)
                fallback_kind = "logo"
                break

    if image is None:
        image = make_source_fallback(story.get("source", "Science News"))
        fallback_kind = "text"

    branded = branded_card(
        image,
        story.get("source", "Source"),
        source_position="center" if fallback_kind != "article" else "left",
    )

    path = f"/tmp/news_{index}.jpg"

    branded.save(
        path,
        "JPEG",
        quality=88,
        optimize=True,
    )

    logger.info(
        "Image selected for %s: %s",
        story.get("source", "Source"),
        fallback_kind,
    )
    return path


# ============================================================
# TELEGRAM HTTP LAYER
# ============================================================

def telegram_call(
    method,
    data=None,
    files=None,
):
    """Call the Telegram Bot API with bounded retries for transient failures."""
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/"
        f"{method}"
    )

    last = {
        "ok": False,
        "description": "Unknown error",
    }

    for attempt in range(1, 6):
        try:
            response = session.post(
                url,
                data=data or {},
                files=files,
                timeout=90,
            )
            result = response.json()

            if result.get("ok"):
                return result

            last = result

            if response.status_code == 429:
                retry_after = int(
                    result.get("parameters", {}).get("retry_after", 5)
                )
                logger.warning(
                    "Telegram 429; waiting %ss",
                    retry_after,
                )
                time.sleep(max(1, retry_after))
                continue

            if response.status_code >= 500:
                time.sleep(2 * attempt)
                continue

            break

        except Exception as exc:
            last = {
                "ok": False,
                "description": str(exc),
            }
            time.sleep(2 * attempt)

    return last


def send_bot_api_fallback(image_path, rich_html):
    """Last-resort Bot API photo send with a safe caption length."""
    text = re.sub(r"<br\s*/?>", "\n", rich_html, flags=re.I)
    text = re.sub(r"</(p|h1|h2|h3|footer|summary|details|tr|td)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > 900:
        text = text[:900].rsplit(" ", 1)[0].rstrip() + "..."

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as photo:
            response = session.post(
                url,
                data={"chat_id": TELEGRAM_CHANNEL, "caption": text},
                files={"photo": photo},
                timeout=90,
            )
        return response.json()
    except Exception as exc:
        return {"ok": False, "description": str(exc)}

def send_rich_photo(
    image_path,
    rich_html,
):
    rich_message = {
        "html": rich_html,
        "media": [
            {
                "id": "newsphoto",
                "media": {
                    "type": "photo",
                    "media": "attach://photo",
                },
            }
        ],
        "skip_entity_detection": False,
    }

    with open(
        image_path,
        "rb",
    ) as photo:

        return telegram_call(
            "sendRichMessage",
            data={
                "chat_id": TELEGRAM_CHANNEL,
                "rich_message": json.dumps(
                    rich_message,
                    ensure_ascii=False,
                ),
            },
            files={
                "photo": photo
            },
        )


# ============================================================
# KNOWLEDGE / EVENT RECORD
# ============================================================

def make_event_id(
    story,
):
    event_key = safe_text(
        story.get(
            "event_key"
        )
    )

    if event_key:
        return (
            re.sub(
                r"[^a-z0-9]+",
                "_",
                event_key.lower(),
            ).strip("_")
        )

    return canonical_url(
        story["url"]
    )


def store_event(
    story,
    published=False,
    message_id=None,
):
    event_id = make_event_id(
        story
    )

    event = {
        "event_id": event_id,
        "canonical_url": story[
            "canonical"
        ],
        "original_url": story[
            "url"
        ],
        "source": story[
            "source"
        ],
        "region": story[
            "region"
        ],
        "topic": story.get(
            "topic",
            "",
        ),
        "institution": story.get(
            "institution",
            "",
        ),
        "event_cluster_id": story.get(
            "event_cluster_id",
            event_id,
        ),
        "event_confidence": story.get(
            "event_confidence",
            0,
        ),
        "event_source_count": story.get(
            "event_source_count",
            0,
        ),
        "headline": story[
            "headline"
        ],
        "summary": story[
            "summary"
        ],
        "highlights": story[
            "highlights"
        ],
        "concepts": story.get(
            "concepts",
            [],
        ),
        "key_numbers": story.get(
            "key_numbers",
            [],
        ),
        "published_at": story[
            "published_date"
        ],
        "selected_at": now_iso(),
        "status": (
            "published"
            if published
            else "selected"
        ),
        "message_id": message_id,
    }

    STATE["events"][
        event_id
    ] = event

    return event_id


# ============================================================
# ============================================================



# ============================================================
# VERSION 1 FALLBACK POOLS
# ============================================================

def build_candidate_pool(ranked, needed):
    """Build a verification pool that favors important stories and broad scientific coverage.

    Diversity is a soft preference. High scientific significance always beats a low-significance story.
    """
    if not ranked:
        return []

    eligible = [dict(x) for x in ranked if x.get("importance_score", 0) >= 7 and x.get("important") is True]
    target = max(RANKING_POOL_SIZE, needed * 2)
    target = min(target, len(eligible))

    selected = []
    used_groups = set()
    remaining = list(eligible)

    def group_for(topic):
        for group, topics in CATEGORY_GROUPS.items():
            if topic in topics:
                return group
        return topic or "Other"

    for item in remaining:
        group = group_for(item.get("topic", ""))
        if group not in used_groups and len(selected) < target:
            selected.append(item)
            used_groups.add(group)

    for item in remaining:
        if len(selected) >= target:
            break
        if item not in selected:
            selected.append(item)

    selected.sort(key=lambda x: (
        -x.get("importance_score", 0),
        x.get("editor_rank", 9999),
    ))
    return selected


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "unsupported_claims": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
    },
    "required": ["supported", "unsupported_claims"],
    "additionalProperties": False,
}


def claims_grounded(story, article_text):
    """Second-pass editorial verification for non-numeric factual claims."""
    claims = [
        story.get("headline", ""),
        story.get("summary", ""),
        *story.get("highlights", []),
    ]
    claims = [safe_text(x) for x in claims if safe_text(x)]

    prompt = """
You are a strict science fact-checking editor. Compare the generated claims with the source article.
Mark supported=true only if every material factual claim in the headline, summary and highlights is directly supported by the source article, either explicitly or by a faithful paraphrase. Reject invented facts, unsupported causal claims, wrong dates, wrong institutions, wrong researchers, wrong figures, wrong study status, exaggerated certainty, and claims stronger than the source. Be especially strict about changing "suggests" into "proves", treating a preprint as peer-reviewed, or presenting an association as causation.
Return only the JSON schema.
"""

    user = (
        "SOURCE ARTICLE:\n" + article_text[:12000]
        + "\n\nGENERATED CLAIMS:\n- " + "\n- ".join(claims)
    )

    try:
        response = get_cerebras().chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "story_claim_verification",
                    "strict": True,
                    "schema": VERIFY_SCHEMA,
                },
            },
            reasoning_effort="low",
            temperature=0.0,
            max_completion_tokens=500,
        )
        data = json.loads(safe_text(response.choices[0].message.content))
        return bool(data.get("supported")), data.get("unsupported_claims", [])
    except Exception as exc:
        logger.warning("Claim verification failed: %s", exc)
        # Verification infrastructure failure must not silently become a hard drop.
        # Numeric grounding remains mandatory; this pass is advisory on verifier outage.
        return True, []


def process_story_candidate(item):
    """
    Extract, generate, ground and normalize one candidate.
    Returns a publishable story or None.
    """
    article_text, image_url = extract_article(item)

    if not article_text:
        logger.warning(
            "DROP extraction: %s",
            item.get("title"),
        )
        return None

    story = generate_story(
        item,
        article_text,
    )

    if not story:
        logger.warning(
            "DROP generation: %s",
            item.get("title"),
        )
        return None

    region = item.get(
        "region",
        "Science",
    )

    story["topic"] = canonical_topic(
        story.get("topic") or item.get("topic"),
        region,
    )

    story["image_url"] = (
        image_url
        or item.get("image")
    )
    story["_image_candidates"] = list(item.get("_image_candidates", []))

    grounded, bad_number = numeric_grounded(
        story,
        article_text,
    )

    if not grounded:
        logger.warning(
            "Numeric grounding failed: %s (%s)",
            story.get("headline"),
            bad_number,
        )

        retry_story = generate_story(
            {
                **item,
                "grounding_warning": bad_number,
            },
            article_text,
        )

        if not retry_story:
            return None

        retry_story["topic"] = canonical_topic(
            retry_story.get("topic") or item.get("topic"),
            region,
        )
        retry_story["image_url"] = (
            image_url
            or item.get("image")
        )

        grounded_retry, _ = numeric_grounded(
            retry_story,
            article_text,
        )

        if not grounded_retry:
            logger.warning(
                "DROP numeric grounding: %s",
                story.get("headline"),
            )
            return None

        story = retry_story

    verified, unsupported_claims = claims_grounded(story, article_text)
    if not verified:
        logger.warning(
            "Claim verification failed: %s | claims=%s",
            story.get("headline"),
            unsupported_claims,
        )

        retry_story = generate_story(
            {**item, "grounding_warning": ", ".join(unsupported_claims[:3])},
            article_text,
        )
        if not retry_story:
            return None

        retry_story["topic"] = canonical_topic(
            retry_story.get("topic") or item.get("topic"),
            region,
        )
        retry_story["image_url"] = image_url or item.get("image")

        grounded_retry, _ = numeric_grounded(retry_story, article_text)
        if not grounded_retry:
            return None

        verified_retry, _ = claims_grounded(retry_story, article_text)
        if not verified_retry:
            logger.warning("DROP claim grounding: %s", story.get("headline"))
            return None
        story = retry_story

    story["topic"] = canonical_topic(
        story.get("topic") or item.get("topic"),
        region,
    )
    story["institution"] = item.get(
        "institution",
        "",
    )
    story["event_key"] = item.get(
        "event_key",
        "",
    )
    story["event_cluster_id"] = item.get(
        "event_cluster_id",
        "",
    )
    story["event_confidence"] = item.get(
        "event_confidence",
        0,
    )
    story["event_source_count"] = item.get(
        "event_source_count",
        0,
    )
    story["category_hashtags"] = category_hashtags(
        story
    )

    return story


# ============================================================
# MAIN
# ============================================================

def is_already_published_candidate(item):
    canonical = safe_text(item.get("canonical"))
    if canonical and canonical in POSTED_URLS:
        return True

    title = safe_text(item.get("title"))
    if not title:
        return False

    for event in STATE.get("events", {}).values():
        if event.get("status") != "published":
            continue
        if event.get("region") != item.get("region"):
            continue
        published_at = parse_datetime(event.get("published_at"))
        if not published_at or (NOW_BD - published_at).total_seconds() > EVENT_RETENTION_DAYS * 86400:
            continue
        previous_title = safe_text(event.get("headline"))
        if previous_title and title_similarity(title, previous_title) >= 0.90:
            return True
    return False


def available_candidates(region, source_pool=None):
    candidates = []
    seen = set()

    for item in STATE.get("queue", {}).values():
        if item.get("region") != region:
            continue
        if item.get("status") not in {"pending", "selected"}:
            continue

        published = parse_datetime(item.get("published_date"))
        if not published or not (DISCOVERY_START <= published <= DISCOVERY_END):
            continue

        url = safe_text(item.get("url"))
        canonical = safe_text(item.get("canonical"))
        if not canonical or canonical in seen:
            continue

        if source_pool == "primary" and not primary_domain_allowed(url, region):
            continue
        if source_pool == "fallback" and not fallback_domain_allowed(url, region):
            continue
        if source_pool is None and not allowed_source_for_region(url, region):
            continue

        if is_already_published_candidate(item):
            continue
        if title_duplicate_against_list(item.get("title", ""), candidates, threshold=0.94):
            continue

        candidates.append(dict(item))
        seen.add(canonical)

    candidates.sort(
        key=lambda x: parse_datetime(x.get("published_date"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return candidates[:MAX_RSS_CANDIDATES]


def prepare_ranked_region(region, candidates):
    ranked = rank_candidates(candidates, region)
    logger.info("%s RANKED RETURNED: %d", region, len(ranked))

    clustered = collapse_event_clusters(ranked)
    logger.info("%s AFTER EVENT DEDUP: %d", region, len(clustered))

    eligible = [
        item for item in clustered
        if item.get("importance_score", 0) >= 7
        and item.get("important") is True
    ]
    logger.info("%s IMPORTANCE PASS (score>=7): %d", region, len(eligible))

    persist_event_cluster_state(eligible)
    return eligible


def process_ranked_region(region, ranked):
    pool = build_candidate_pool(ranked, MAX_STORIES_PER_RUN)
    valid = []
    attempted = 0
    rejected = 0

    for item in pool:
        if len(valid) >= MAX_STORIES_PER_RUN:
            break
        attempted += 1
        story = process_story_candidate(item)
        if not story:
            rejected += 1
            continue

        # Final duplicate check after generation.
        if is_already_published_candidate({**item, "title": story.get("headline", item.get("title"))}):
            logger.info("DROP already published event: %s", story.get("headline", ""))
            rejected += 1
            continue

        story["topic"] = canonical_topic(story.get("topic"), region)
        story["category_hashtags"] = category_hashtags(story)
        valid.append(story)
        logger.info(
            "ACCEPT %s #%d: rank=%s title=%s",
            region,
            len(valid),
            item.get("editor_rank", "?"),
            story.get("headline", ""),
        )

    logger.info(
        "%s FINAL VALID: %d/%d | pool=%d attempted=%d rejected=%d",
        region,
        len(valid),
        MAX_STORIES_PER_RUN,
        len(pool),
        attempted,
        rejected,
    )
    return valid


def run():
    logger.info("THE SCIENCE NEWSROOM V1 UPDATE-ONLY")
    logger.info("Channel=%s Mode=%s", TELEGRAM_CHANNEL, NEWS_MODE)
    logger.info("LOOKBACK=%d hours | %s -> %s", DISCOVERY_LOOKBACK_HOURS, DISCOVERY_START.isoformat(), DISCOVERY_END.isoformat())

    prune_state()
    refresh_category_coverage()
    collect_rss()

    science_count = queue_candidates_for_region("Science")
    science_count += google_news_gap_fill("Science", science_count, DISCOVERY_TARGET_PER_REGION)
    exa_gap_fill("Science", science_count, DISCOVERY_TARGET_PER_REGION)

    save_state(STATE)

    candidates = available_candidates("Science", source_pool="primary")
    logger.info("DISCOVERY CANDIDATES: SCIENCE=%d", len(candidates))

    ranked = prepare_ranked_region("Science", candidates)
    logger.info("UNIQUE EVENTS: SCIENCE=%d", len(ranked))

    for item in ranked[:12]:
        logger.info("RANK SCIENCE #%s | %s | %s", item.get("editor_rank", "?"), item.get("title", ""), item.get("rank_reason", ""))

    stories = process_ranked_region("Science", ranked)
    logger.info("FINAL: SCIENCE=%d MAX=%d", len(stories), MAX_STORIES_PER_RUN)

    if not stories:
        logger.info("No science story cleared the importance and verification bar this run.")

    published_count = 0
    for index, story in enumerate(stories, start=1):
        rich_html = fit_rich_html(story)
        if rich_visible_length(rich_html) > MAX_RICH_CHARACTERS:
            logger.error("Rich message exceeds Telegram limit: %s", story["headline"])
            continue

        image_path = prepare_image(story, index)
        result = send_rich_photo(image_path, rich_html)
        if not result.get("ok"):
            logger.warning("Rich Message publish failed; trying Bot API fallback: %s", result.get("description"))
            result = send_bot_api_fallback(image_path, rich_html)

        if result.get("ok"):
            published_count += 1
            message = result.get("result", {})
            message_id = message.get("message_id") if isinstance(message, dict) else None
            canonical = story["canonical"]
            POSTED_URLS.add(canonical)
            save_posted_url(canonical)

            queue_item = STATE["queue"].get(canonical)
            if queue_item:
                queue_item["status"] = "posted"
                queue_item["posted_at"] = now_iso()

            store_event(story, published=True, message_id=message_id)
            remember_posted_event(story)
            update_category_coverage(story)
            STATE["recent_titles"].append(normalize_title(story["headline"]))
            logger.info("Published %d/%d: [%s] %s", published_count, MAX_STORIES_PER_RUN, story.get("region", ""), story["headline"])
        else:
            logger.error("Telegram failed: %s", result.get("description"))
        save_state(STATE)
        time.sleep(POST_DELAY_SECONDS)

    save_state(STATE)
    logger.info("Finished. Published=%d/%d", published_count, MAX_STORIES_PER_RUN)


# ============================================================
# SELF TEST
# ============================================================

def self_test():
    global session
    sample = {
        "headline": "Telescope Finds Evidence of a New Planetary System",
        "summary": "New observations reveal a planetary system with an unusual configuration that offers fresh evidence about how such systems form.",
        "highlights": [
            "Astronomers detected the system with a modern space telescope.",
            "The observations reveal an unusual planetary configuration.",
            "The result gives researchers new evidence about planetary system formation.",
            "The research was reported from a newly published scientific study.",
        ],
        "the_context": "Researchers have used increasingly sensitive telescopes to study how planets assemble around stars. The new observations add a useful case for testing models of planetary formation.",
        "bottom_line": "The finding matters because the system gives scientists a new test case for understanding how planets form.",
        "bold_terms": ["telescope", "planetary system", "observations"],
        "source": "Nature", "url": "https://example.com/story", "region": "Science",
        "topic": "Astronomy", "institution": "NASA",
    }
    rendered = dynamic_rich_html(sample)
    assert complete_text("A normal sentence.")
    assert "**bold**" not in clean_generated_text("**bold** study result")
    assert "bold" in clean_generated_text("**bold** study result")
    assert complete_text("An incomplete sentence—") is False
    assert "THE CONTEXT" in rendered
    assert "BOTTOM LINE" in rendered
    assert rendered.count('<blockquote expandable>') == 2
    assert "<aside>" not in rendered
    assert rendered.count("• ") == 4
    assert rendered.index("<h1>Telescope Finds") < rendered.index("KEY HIGHLIGHTS") < rendered.index("THE CONTEXT") < rendered.index("BOTTOM LINE")

    sample_three = dict(sample)
    sample_three["highlights"] = sample_three["highlights"][:3]
    rendered_three = dynamic_rich_html(sample_three)
    assert rendered_three.count("• ") == 3

    sample_five = dict(sample)
    sample_five["highlights"] = sample_five["highlights"] + ["The result offers another test of current planet-formation models."]
    rendered_five = dynamic_rich_html(sample_five)
    assert rendered_five.count("• ") == 5
    assert rendered.index("#Astronomy") > rendered.index("BOTTOM LINE")
    assert "<footer><b>Source:</b>" in rendered
    import inspect
    assert "@ScienceNewsroom" in inspect.getsource(branded_card)
    assert "display_source_name" not in inspect.getsource(branded_card)
    assert "source_text =" not in inspect.getsource(branded_card)
    assert likely_same_event("Astronomers announce new exoplanet", "Astronomers announce new exoplanet")
    assert canonical_url("https://www.example.com/story/?utm_source=x") == "example.com/story"
    assert "exoplanet" in extract_entities("Astronomers announce an exoplanet discovery")
    clustered = cluster_ranked_events([
        {"title": "Astronomers announce new exoplanet", "source": "Nature", "url": "https://nature.com/a", "published_date": now_iso(), "region": "Science"},
        {"title": "Astronomers announce new exoplanet", "source": "Science News", "url": "https://sciencenews.org/a", "published_date": now_iso(), "region": "Science"},
    ])
    assert len(clustered) >= 1
    assert clustered[0]["event_cluster_size"] >= 1
    assert canonical_topic("black hole") == "Astrophysics"
    assert "#Astronomy" in category_hashtags(sample) and "#Science" in category_hashtags({**sample, "topic": "Physics", "institution": ""})

    fallback = make_source_fallback("Nature")
    assert fallback.size == (1200, 675)
    text_fallback = make_source_fallback("An Example Science Publication")
    assert text_fallback.size == (1200, 675)
    candidate_urls = find_image_candidates(
        "https://example.com/story",
        page_html='<html><head><meta property="og:image" content="/images/story.jpg"><meta name="twitter:image" content="/images/tw.jpg"></head><body></body></html>',
        final_url="https://example.com/story",
    )
    assert candidate_urls[:2] == [
        "https://example.com/images/story.jpg",
        "https://example.com/images/tw.jpg",
    ]

    # Discovery/source contract regression tests. These functions are part of
    # the proven Tech Newsroom architecture and must exist before any network
    # discovery path is allowed to run.
    assert normalized_domain("https://www.nature.com/news/article") == "nature.com"
    assert is_domain_allowed("https://science.nasa.gov/science-research/", ["science.nasa.gov"])
    assert primary_domain_allowed("https://science.nasa.gov/news/article", "Science")
    assert primary_domain_allowed("https://www.nature.com/articles/test", "Science")
    assert allowed_source_for_region("https://pubmed.ncbi.nlm.nih.gov/12345678/", "Science")
    assert not allowed_source_for_region("https://example.com/science/story", "Science")

    regression_item = {
        "title": "New telescope observations reveal a distant exoplanet",
        "url": "https://www.nature.com/articles/example",
        "published_dt": NOW_BD - timedelta(hours=2),
        "region": "Science",
    }
    assert candidate_basic_allowed(regression_item), "Valid primary science candidate was rejected"

    # Keep the source universe contract explicit: V1 promises 50 primary
    # science domains even though only a subset may expose working RSS feeds.
    assert len(PRIMARY_SCIENCE_DOMAINS) == 50

    # Telegram runtime contract regression tests. The publishing layer must
    # never reach send_rich_photo/send_bot_api_fallback with an undefined
    # telegram_call symbol again.
    assert callable(telegram_call)

    class _FakeTelegramResponse:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    class _FakeTelegramSession:
        def __init__(self):
            self.calls = 0

        def post(self, url, data=None, files=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                return _FakeTelegramResponse(
                    {
                        "ok": False,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 0},
                    },
                    status_code=429,
                )
            return _FakeTelegramResponse(
                {"ok": True, "result": {"message_id": 123}},
                status_code=200,
            )

    original_session = session
    try:
        session = _FakeTelegramSession()
        telegram_result = telegram_call(
            "sendMessage",
            data={"chat_id": TELEGRAM_CHANNEL, "text": "self-test"},
        )
        assert telegram_result.get("ok") is True
        assert session.calls == 2
    finally:
        session = original_session

    logger.info("TheScienceNewsroom V1 self-test passed.")


def visible_text_for_test(
    rendered,
):
    text = re.sub(
        r"<[^>]+>",
        "",
        rendered,
    )
    return html.unescape(
        text
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--self-test",
        action="store_true",
    )

    args = parser.parse_args()

    if args.self_test:
        self_test()
    else:
        run()
