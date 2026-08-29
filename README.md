# TheScienceNewsroom V1

> Automated science-news intelligence for Telegram, powered by GitHub Actions, Exa, and Cerebras, using the same proven runtime architecture as the Tech Newsroom bot with a science-specific editorial intelligence layer.

TheScienceNewsroom discovers, filters, ranks, verifies, clusters, and publishes the most important science stories to **@ScienceNewsroom**. The bot is designed to find meaningful scientific developments rather than simply collect everything containing the word "science".

## Mission

The channel is science-only and covers the full scientific landscape:

- Astronomy, astrophysics, cosmology, planetary science, space science, and astrobiology
- Physics, quantum physics, particle physics, nuclear physics, condensed matter, and materials science
- Chemistry and chemical biology
- Biology, molecular biology, cell biology, genetics/genomics, evolution, ecology, microbiology, and neuroscience
- Earth science, geology, geophysics, oceanography, atmospheric science, climate science, and environmental science
- Paleontology
- Biomedical and medical research when the underlying story is scientific research
- Mathematics and scientific computing
- AI for science, scientific datasets, methods, instruments, observatories, and research infrastructure

The bot prioritizes actual scientific results, observations, measurements, methods, discoveries, and research papers. A source being science-themed does not make a story publishable.

## Technical Architecture

This V1 intentionally preserves the core technical process of the proven Tech Newsroom implementation.

```text
RSS feeds
   ↓
Google News RSS gap fill
   ↓
Exa gap fill
   ↓
Source validation
   ↓
24-hour filtering
   ↓
URL deduplication
   ↓
Event / research-event deduplication
   ↓
LLM scientific editorial ranking in bounded batches
   ↓
Global score merge + soft discipline diversity
   ↓
Top science events
   ↓
Article / source extraction
   ↓
Science news story generation
   ↓
Numeric grounding + scientific claim verification
   ↓
Branded image
   ↓
Telegram Rich Message
   ↓
Persistent state
   ↓
GitHub Actions state commit
```

The bot remains a single Python service. No database, Docker layer, Redis queue, or external scheduler is introduced.

## Runtime

- Python 3.12
- GitHub Actions
- RSS / Atom via `feedparser`
- Article extraction via `trafilatura` with BeautifulSoup fallback
- HTTP via `requests` + `urllib3` retry adapter
- Exa for discovery gap filling
- Cerebras for bounded-batch editorial ranking and story generation
- Pillow for image processing
- Telegram publishing through the existing Rich Message path with Bot API fallback

## Required Secrets

```text
EXA_API_KEY
CEREBRAS_API_KEY
TELEGRAM_BOT_TOKEN
```

Optional:

```text
TELEGRAM_ADMIN_CHAT_ID
CEREBRAS_MODEL
```

The workflow sets:

```text
TELEGRAM_CHANNEL=@ScienceNewsroom
NEWS_MODE=update
```

## Source Universe: 50 Major Science Sources

The editorial allowlist covers a 50-source science universe. RSS is attempted first where a source exposes a usable feed. Google News RSS and Exa are gap-fill mechanisms restricted to the allowed science domain universe.

| # | Source | Domain | Primary use |
|---:|---|---|---|
| 1 | Nature | `nature.com` | Broad research news |
| 2 | Science / AAAS | `science.org` | Broad research news |
| 3 | Science News | `sciencenews.org` | Research journalism |
| 4 | New Scientist | `newscientist.com` | Broad science |
| 5 | Scientific American | `scientificamerican.com` | Broad science |
| 6 | Quanta Magazine | `quantamagazine.org` | Fundamental science and mathematics |
| 7 | Phys.org | `phys.org` | Broad research discovery |
| 8 | ScienceDaily | `sciencedaily.com` | Research discovery |
| 9 | EurekAlert! | `eurekalert.org` | Institutional research releases |
| 10 | Live Science | `livescience.com` | Broad science and space |
| 11 | ScienceAlert | `sciencealert.com` | Research discovery |
| 12 | The Scientist | `the-scientist.com` | Life sciences |
| 13 | Knowable Magazine | `knowablemagazine.org` | Research explainers |
| 14 | Cosmos Magazine | `cosmosmagazine.com` | Broad science |
| 15 | The Conversation | `theconversation.com` | Academic research reporting |
| 16 | Physics World | `physicsworld.com` | Physics |
| 17 | American Physical Society | `aps.org` | Physics |
| 18 | C&EN | `cen.acs.org` | Chemistry |
| 19 | Chemistry World | `chemistryworld.com` | Chemistry |
| 20 | Eos / AGU | `eos.org` | Earth and space science |
| 21 | Carbon Brief | `carbonbrief.org` | Climate science |
| 22 | Inside Climate News | `insideclimatenews.org` | Climate/environment |
| 23 | STAT | `statnews.com` | Biomedical research |
| 24 | Science Focus | `sciencefocus.com` | Broad science |
| 25 | NASA Science | `science.nasa.gov` | Space and planetary science |
| 26 | NASA | `nasa.gov` | Space and Earth science |
| 27 | ESA | `esa.int` | Space science |
| 28 | European Southern Observatory | `eso.org` | Astronomy |
| 29 | NOIRLab | `noirlab.edu` | Astronomy |
| 30 | Space Telescope Science Institute | `stsci.edu` | Hubble/JWST science |
| 31 | HubbleSite | `hubblesite.org` | Hubble discoveries |
| 32 | NASA Webb | `webbtelescope.org` | JWST discoveries |
| 33 | Chandra X-ray Observatory | `chandra.si.edu` | X-ray astronomy |
| 34 | JPL | `jpl.nasa.gov` | Planetary science |
| 35 | Space.com | `space.com` | Space/astronomy journalism |
| 36 | Sky & Telescope | `skyandtelescope.org` | Astronomy |
| 37 | Astronomy Magazine | `astronomy.com` | Astronomy |
| 38 | Universe Today | `universetoday.com` | Astronomy/space |
| 39 | EarthSky | `earthsky.org` | Astronomy/Earth science |
| 40 | Sky at Night | `skyatnightmagazine.com` | Astronomy |
| 41 | arXiv | `arxiv.org` | Preprints, especially physics/astronomy |
| 42 | bioRxiv | `biorxiv.org` | Biology preprints |
| 43 | medRxiv | `medrxiv.org` | Medical preprints |
| 44 | ChemRxiv | `chemrxiv.org` | Chemistry preprints |
| 45 | EarthArXiv | `eartharxiv.org` | Earth-science preprints |
| 46 | PubMed | `pubmed.ncbi.nlm.nih.gov` | Biomedical literature |
| 47 | NASA ADS | `ui.adsabs.harvard.edu` | Astronomy/astrophysics literature |
| 48 | Crossref | `crossref.org` | Scholarly metadata |
| 49 | OpenAlex | `openalex.org` | Research discovery/metadata |
| 50 | Semantic Scholar | `semanticscholar.org` | Research discovery/citations |

Additional allowed API/subdomain hosts are kept in the runtime allowlist where necessary to support source resolution and research metadata without changing the 50-source editorial universe.

## Science Taxonomy

The classifier maps candidates into a controlled taxonomy covering:

```text
Astronomy
Astrophysics
Cosmology
Planetary Science
Space Science
Astrobiology
Physics
Quantum Physics
Particle Physics
Nuclear Physics
Condensed Matter
Materials Science
Chemistry
Chemical Biology
Biology
Molecular Biology
Cell Biology
Genetics and Genomics
Evolution
Ecology
Microbiology
Neuroscience
Cognitive Science
Earth Science
Geology
Geophysics
Oceanography
Atmospheric Science
Climate Science
Environmental Science
Paleontology
Biomedical Research
Medicine Research
Mathematics
Scientific Computing
AI for Science
Energy Science
Nanotechnology
Scientific Instruments
```

## Scientific Relevance Gate

The first question is not "Is this interesting?" It is:

> **Is this actually a scientific development worth reporting as science news?**

The bot rejects or heavily downgrades routine corporate space news, consumer health content, generic technology news, unsupported sensational claims, opinion/commentary, and ordinary mission updates without a substantive scientific result.

## Editorial Ranking

Every candidate competes in one ranked pool. There are no fixed discipline quotas.

```text
9-10  Exceptional scientific significance
7-8   Clearly important and publishable
4-6   Interesting but normally not publishable
0-3   Low-value, promotional, speculative, routine or non-scientific
```

The model considers:

```text
Scientific novelty            0-20
Strength of evidence          0-20
Potential scientific impact   0-15
Breadth of implications       0-15
Research credibility          0-10
Independent confirmation      0-10
Methodological quality        0-5
Timeliness                     0-5
```

A score of **7+** is required for publication.

The bot prefers the lower score when uncertain. Skipping a weak story is safer than publishing an exaggerated one.

## Evidence Hierarchy

```text
Peer-reviewed published research
        ↓
Accepted / in-press research
        ↓
Credible preprint
        ↓
University / scientific-institution release
        ↓
Science journalism
```

A preprint can be important and publishable, but it must not be presented as established scientific consensus.

## Research Paper Discovery

Research sources are not treated as a second-class feed. They provide early signals for stories that may not yet have mainstream coverage.

The bot can discover:

- New arXiv research, especially astronomy/astrophysics and physics
- New bioRxiv biology research
- New medRxiv biomedical research
- New ChemRxiv chemistry research
- New EarthArXiv Earth-science research
- PubMed literature
- NASA ADS astronomy literature
- Scholarly metadata through Crossref/OpenAlex/Semantic Scholar

The editorial ranker then determines whether a paper represents a meaningful scientific development or merely incremental research.

## Astronomy Pipeline

Astronomy receives dedicated high-signal discovery coverage through:

```text
NASA
ESA
ESO
NOIRLab
STScI
JWST / NASA Webb
HubbleSite
Chandra
JPL
NASA ADS
arXiv
```

Useful event labels include:

```text
Exoplanet
Black Hole
Neutron Star
Supernova
Gravitational Wave
Fast Radio Burst
Galaxy
Dark Matter
Dark Energy
Early Universe
Interstellar Object
Astrobiology
Planetary Discovery
Solar Science
JWST
Hubble
New Telescope Result
```

The existence of a telescope or mission in a story does not automatically make it important. The scientific result must clear the same importance bar.

## Duplicate and Event Handling

The bot preserves the proven URL and event-deduplication approach:

```text
Canonical URL
   +
Title similarity
   +
Entity overlap
   +
Event similarity
   +
Time window
   +
Persistent posted-event history
```

Multiple reports of the same paper or discovery are collapsed into one scientific event so the channel does not repeat the same research finding simply because several publications covered it.

## Recency

Default discovery window:

```text
24 hours
```

Age handling:

```text
<24h       strong freshness signal
24-72h     acceptable when still relevant
>72h       normally downgraded unless important/developing
Unknown    not automatically rejected
```

## Scientific Anti-Hype Rules

The verifier is specifically instructed to catch overclaiming such as:

```text
"Scientists prove..."
"Scientists discovered alien life..."
"Researchers found a cure..."
"This solves..."
"This changes everything..."
```

unless the source actually supports that level of certainty.

The bot also checks the difference between:

```text
suggests
supports
is consistent with
demonstrates
establishes
proves
```

It must not upgrade the strength of a scientific claim during summarization.

## Research Status

When relevant, the article generation layer should preserve whether the research is:

```text
peer-reviewed
accepted / in press
preprint
conference result
observational result
institutional release
```

## Story Generation

The Telegram card preserves the proven output structure:

```text
Photo
Headline
1-sentence summary
## KEY HIGHLIGHTS
• 3-5 factual points
## THE CONTEXT (collapsed)
2-4 sentences
## BOTTOM LINE (collapsed)
1 sentence
#hashtags
**Source:** Publication
```

Content requirements:

- Headline: 6-14 words, newspaper style, evidence-faithful.
- Summary: exactly one complete sentence.
- Highlights: 3-5 concise factual points, chosen dynamically.
- Context: 2-4 sentences explaining the research/scientific background.
- Bottom Line: exactly one sentence explaining why the result matters.
- No unsupported claims, fake certainty, or invented figures.

## Verification

Two verification passes remain mandatory before publication:

1. **Numeric grounding** checks measurements, dates, percentages, sample sizes, distances, ages, energies, and other numeric claims against the source article.
2. **Scientific claim verification** checks the headline, summary, and highlights against the source and is especially strict about causal claims, study status, certainty, and researcher/institution attribution.

Verification failure triggers regeneration or rejection.

## Image Pipeline

The image layer preserves the existing architecture:

```text
article image
   ↓
1200×675 crop
   ↓
@ScienceNewsroom chip
   ↓
JPEG
   ↓
Telegram Rich Message
```

If no usable source image exists, a branded fallback Science News card is generated.

## Persistent State

The same state files are used:

```text
news_state.json
posted_urls.txt
```

State survives GitHub Actions runs and is committed back to the repository after execution.

## GitHub Actions

The included workflow runs on the same cadence as the working Tech bot:

```text
07:00-23:00 Asia/Dhaka
hourly
```

Execution order:

```text
Checkout
↓
Python 3.12
↓
pip install -r requirements.txt
↓
py_compile
↓
--self-test
↓
main.py
↓
commit state
↓
push
```

## Self-Test

Run locally:

```bash
EXA_API_KEY=dummy \
CEREBRAS_API_KEY=dummy \
TELEGRAM_BOT_TOKEN=dummy \
python main.py --self-test
```

Before release, the source is also checked with:

```bash
python -m py_compile main.py
```

The self-test covers HTML rendering, sentence completeness, science taxonomy, hashtags, canonical URLs, event clustering, and the `@ScienceNewsroom` image-branding contract.

## Normal Run

```bash
EXA_API_KEY=... \
CEREBRAS_API_KEY=... \
TELEGRAM_BOT_TOKEN=... \
TELEGRAM_CHANNEL=@ScienceNewsroom \
python main.py
```

## Release/Test Standard

Every updated ZIP should pass:

```text
1. Python compile check
2. Dependency contract check against requirements.txt
3. Self-test
4. Science identity / channel check
5. Workflow validation
6. State-file load check
7. ZIP integrity check
```

A release should not be packaged as final when these checks fail.
