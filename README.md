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
                fetch.py  ──► Jina AI Reader (JS-rendered → markdown) → fallback requests
                          ▼
                S3  archive raw HTML  (s3://$S3_BUCKET/raw/YYYY-MM-DD/…)
                          ▼
                extract.py:  rule-based parser (parse.py, no API)  [primary]
                             → optional local-LLM enrich/fallback (Ollama, FREE)
                             → paid LLMs only if ALLOW_PAID_LLM=1 (default OFF)
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
| `OPENAI_API_KEY` / `GROQ_API_KEY` / `GEMINI_API_KEY` | **optional** — paid LLM fallback, only used when `ALLOW_PAID_LLM=1`. Extraction works without any of these (rule-based + local Ollama). |

## Extraction & cost model

The rule-based parser (`parse.py`) runs first and needs **no API and no money**.
LLMs are optional:

| Env | Default | Effect |
| --- | --- | --- |
| `ALLOW_PAID_LLM` | `0` (off) | Gemini/Groq/OpenAI are **never** called unless set to `1`. |
| `ENRICH_WITH_LLM` | `0` (off) | Depth pass: after rule-based extraction, run the **free local Ollama** and merge its values to fill empty detail fields (fees/rewards/lounge/…). Identity fields stay rule-based; rule-based wins on conflicts. Slow (~15–48s/page). |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Local model. `llama3.2:latest` is ~3–4× faster. |
| `LLM_MAX_CHARS` | `9000` | Cap on page text sent to the local model (small models degrade on long context). |

Ollama must be running locally (`ollama serve`) for the free LLM paths.

## Running locally

```bash
pip install -r requirements.txt

# scrape only known issuers (rule-based only, no LLM, no cost)
RUN_MODE=issuers python -m src.main

# MAX completeness — free local depth pass, never billed, repopulates DDB
env $(grep -v '^#' .env | xargs) \
  ENRICH_WITH_LLM=1 OLLAMA_MODEL=llama3.2:latest FORCE_REFRESH=1 \
  python -m src.main > out/run.log 2>&1 &

# per-issuer discovery diagnostic (no DDB / no LLM / no paid calls)
python -m scripts.coverage            # all issuers → out/coverage.json
python -m scripts.coverage hdfc sbi   # subset

# scrape one URL (debugging)
RUN_MODE=single SINGLE_URL=https://www.hdfc.bank.in/credit-cards/infinia python -m src.main
```

## Adding a new issuer

Edit `config/issuers.yaml`. Two discovery styles:

```yaml
# A) Full issuer with many cards — harvest a listing page or sitemap
- id: new_bank
  name: NewBank
  type: bank
  sitemap_url: "https://newbank.in/sitemap.xml"   # optional, most complete
  list_urls:
    - https://newbank.in/credit-cards             # harvested for card links
  detail_link_pattern: "/credit-cards/[a-z0-9-]+-card$"

# B) Co-brand / single-card issuer — point straight at the card page(s).
#    `detail_urls` are parsed AS detail pages with NO sibling harvesting, so the
#    issuer does NOT vacuum up the parent bank's whole sitemap via nav menus.
- id: brand_x_axis
  name: Brand X Axis Bank Credit Card
  type: cobrand
  detail_urls:
    - https://www.axis.bank.in/cards/credit-card/brand-x-axis-bank-credit-card
```

Run `python -m scripts.coverage <id>` to check how many card URLs discovery finds.

That's it. Next scheduled run will pick it up.

## Change feed

`cards_changes` is the auditable source of truth for:

- `change_type=new_card` — first time we see a card_id
- `change_type=devaluation` — annual fee up, reward rate down, lounge cut, status → invite_only/discontinued
- `change_type=change` — any other watched-field movement

Wire this into Slack/email by reading the table on a 15-min cron.
