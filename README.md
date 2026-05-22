# Indian Cards — Daily Scraper

End-to-end pipeline that runs daily on GitHub Actions, scrapes every Indian
payment-card issuer (banks + neobanks + co-brands + forex + corporate cards),
discovers newly launched cards from finance blogs/press, normalizes them
against a strict JSON schema, and upserts into DynamoDB (`ap-south-1`) with
S3 raw-HTML archival and change tracking (devaluations / fee hikes / new
launches).

## Architecture

```
            ┌─────────────────────────────┐
GH cron ───►│  src/main.py (orchestrator) │
            └──┬──────────────────────┬───┘
               │                      │
               ▼                      ▼
   collect_detail_urls()    discover_candidate_urls()
   (issuers.yaml — 40+      (discovery_sources.yaml —
   bank/neobank/co-brands)  cardinsider, livemint, RBI…)
               │                      │
               └──────────┬───────────┘
                          ▼
                fetch.py  ──► Firecrawl (JS-rendered) → fallback requests
                          ▼
                S3  archive raw HTML  (s3://$S3_BUCKET/raw/YYYY-MM-DD/…)
                          ▼
                extract.py (OpenAI structured output → schema/card.schema.json)
                          ▼
                normalize.py (card_id, dedupe, fuzzy-merge variants)
                          ▼
                diff.py   (devaluation / fee / status detection)
                          ▼
   DynamoDB:  cards_master  ·  cards_sources  ·  cards_changes
```

## DynamoDB tables (create once)

```bash
aws dynamodb create-table --region ap-south-1 --table-name cards_master \
  --attribute-definitions AttributeName=card_id,AttributeType=S \
  --key-schema AttributeName=card_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST

aws dynamodb create-table --region ap-south-1 --table-name cards_sources \
  --attribute-definitions AttributeName=url,AttributeType=S \
  --key-schema AttributeName=url,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST

aws dynamodb create-table --region ap-south-1 --table-name cards_changes \
  --attribute-definitions AttributeName=change_id,AttributeType=S \
  --key-schema AttributeName=change_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

Optionally add a GSI on `cards_changes` (`card_id`, `detected_at`) for
"show me everything that changed for the Magnus this year".

## GitHub Secrets

| Secret | Purpose |
| --- | --- |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | DDB + S3 writer (least-privilege IAM) |
| `S3_BUCKET` | e.g. `plenti-cards-raw` (raw HTML archive) |
| `OPENAI_API_KEY` | structured extraction (gpt-4o-mini default) |
| `FIRECRAWL_API_KEY` | optional but strongly recommended for SPA sites (CRED, OneCard, Jupiter) |

## Running locally

```bash
pip install -r requirements.txt

# scrape only known issuers
RUN_MODE=issuers python -m src.main

# only blog/news discovery
RUN_MODE=discover python -m src.main

# scrape one URL (debugging)
RUN_MODE=single SINGLE_URL=https://www.hdfcbank.com/.../infinia python -m src.main
```

## Adding a new issuer

Edit `config/issuers.yaml`, add a block:

```yaml
- id: new_bank
  name: NewBank
  type: neobank
  list_urls:
    - https://newbank.in/cards
  detail_link_pattern: "/cards/.+"
```

That's it. Next scheduled run will pick it up.

## Change feed

`cards_changes` is the auditable source of truth for:

- `change_type=new_card` — first time we see a card_id
- `change_type=devaluation` — annual fee up, reward rate down, lounge cut, status → invite_only/discontinued
- `change_type=change` — any other watched-field movement

Wire this into Slack/email by reading the table on a 15-min cron.
