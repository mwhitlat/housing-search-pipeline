# housing-search-pipeline

Bay Area housing search pipeline for Notion.

## Included
- `scripts/bay_housing_refresh.py` — ingest + dedupe + Notion sync
- `scripts/notion_bay_cleanup.py` — normalize/cleanup Notion DB fields
- `data/bay_housing_latest.json` — latest local run artifact

## Notes
- Zillow is currently paused.
- Primary URL is the single URL field in Notion.
- Data quality gate blocks low-quality rows from sync.
