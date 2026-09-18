# CareerNewsroom V0.5

A production-style Bangladesh job newsroom built from the proven Newsroom runtime pattern.

## Sources

Only these source universes are used:

- **Bdjobs**: researched through Exa with a 7-day discovery window, then read and ranked as individual vacancy pages.
- **Dohaj**: only the first five jobs from each of the eight approved category/government URLs are considered on each run.

Dohaj source/details URL stays in the Official Source section. When the Dohaj page exposes the original external application URL, that exact destination is used for the native `APPLY NOW` button. When no verified external application URL is found, the button becomes `READ MORE` and opens the Dohaj details page.

## Audience

The editorial judge is optimized for Bangladesh users around 20-30, especially BBA/MBA students, graduates, freshers and early-career professionals.

## Publication

A run publishes every verified, judged candidate that clears the quality threshold, up to 15 jobs. The target minimum is 5 when at least 5 genuine candidates are available. Jobs are never invented to meet the target.

## State

`news_state.json` retains unpublished jobs and published event information. `posted_urls.txt` prevents direct URL republishing.

## Required GitHub Actions secrets

- `EXA_API_KEY`
- `CEREBRAS_API_KEY`
- `TELEGRAM_BOT_TOKEN`
