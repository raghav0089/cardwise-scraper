# CardWise Card Scraper

## Run once (now)
```bash
cd cardwise_scraper
pip install -r requirements.txt
playwright install chromium

# Create DynamoDB tables first
python setup_tables.py

# Run all scrapers
python main.py

# Run one issuer only
python main.py --issuer hdfc
```

## Enable daily cron
Uncomment the `schedule` block in `.github/workflows/scraper.yml`

Add these secrets to GitHub → Settings → Secrets:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

## Add a new bank scraper
1. Create `scrapers/newbank.py` extending `BaseScraper`
2. Implement `async def scrape() -> list[dict]`
3. Register in `main.py` SCRAPER_MAP
4. Add to `config/sources.py` SOURCES list

## Tables created
- `cards_master` — latest state of every card
- `cards_versions` — full version history
- `card_change_events` — devaluations, new cards, discontinued
- `scraper_runs` — log of every run

## S3 Snapshots
Raw JSON saved to `s3://plenti-card-snapshots/{issuer}/{date}.json`
Create the bucket manually in AWS S3 (ap-south-1) before running.
