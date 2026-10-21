# 📡 PM News Radar

Scans worldwide news for product-management topics and builds an interactive HTML digest. Pure Python 3 stdlib — no installs.

## Run
```bash
./pm-news                       # last 7 days, opens report in your browser
./pm-news --days 2 --max 120    # shorter window, fewer stories
./pm-news --topics "AI agents product" "usage-based pricing"   # custom searches
./pm-news --regions US India --no-open --out ~/Desktop/pm.html
```
Reports are saved to `reports/pm-news-<timestamp>.html`.

## Sources
- Google News + Bing News across US, UK, India, Australia, Singapore, Canada editions
- PM blogs: Mind the Product, Lenny's Newsletter, Product Talk, SVPG
- Hacker News discussions

Topics: Product Management, AI Products, Strategy/Roadmaps, Growth & PLG, Pricing & Monetization, User Research & UX, Launches, Careers & Org.

## Summaries
- Default: extractive (article snippets + top story per topic + trending terms).
- AI digest: `export OPENAI_API_KEY=...` or `export ANTHROPIC_API_KEY=...` (override model via `OPENAI_MODEL` / `ANTHROPIC_MODEL`). Use `--no-llm` to skip.

## Report features
Search (`/` to focus), topic chips, region/type filters, sort by relevance/newest/coverage, ★ save stories, hide-read, trending-term chips, dark mode. Saved/read state persists in localStorage.
