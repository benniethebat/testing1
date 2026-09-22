# Data Sources

This project uses the **Brazilian E-Commerce Public Dataset by Olist** as its
primary dataset, enriched with a few small public reference datasets. Together
they're rich enough to support a real dimensional model, a semantic layer with
non-trivial business metrics, an agent that can answer natural-language
questions, and a set of BI dashboards — while staying small enough (a few
hundred MB) to build and iterate on quickly.

## 1. Primary dataset: Olist Brazilian E-Commerce

- **Source:** https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce
- **Publisher:** Olist Store (a Brazilian multi-vendor marketplace)
- **License:** CC BY-NC-SA 4.0 (non-commercial, share-alike — fine for a
  portfolio, not for a commercial product)
- **Size / coverage:** ~100k real, anonymized orders placed between 2016 and
  2018 across multiple marketplaces in Brazil
- **Format:** 9 relational CSV files, joined by order/customer/product/seller
  IDs

| File | Contents |
|---|---|
| `olist_orders_dataset.csv` | Order header: status, purchase/approval/delivery timestamps |
| `olist_order_items_dataset.csv` | Line items per order: product, seller, price, freight |
| `olist_order_payments_dataset.csv` | Payment method, installments, payment value |
| `olist_order_reviews_dataset.csv` | Review score, comment title/message, timestamps |
| `olist_customers_dataset.csv` | Customer ID, city, state, zip prefix |
| `olist_sellers_dataset.csv` | Seller ID, city, state, zip prefix |
| `olist_products_dataset.csv` | Category, dimensions, weight, photo count |
| `olist_geolocation_dataset.csv` | Zip-prefix to lat/lng |
| `product_category_name_translation.csv` | Portuguese → English category names |

**Why this dataset:** it's real (not synthetic), already shaped like a set of
OLTP tables rather than a pre-flattened CSV, and naturally supports a clean
star schema plus genuinely interesting business questions (delivery SLAs,
review scores vs. freight cost, seller performance, regional demand).

**Download:** requires a free Kaggle account + API token
(`~/.kaggle/kaggle.json`), then:
```bash
kaggle datasets download -d olistbr/brazilian-ecommerce -p data/raw --unzip
```

## 2. Enrichment datasets

These aren't required, but each one turns a single flat fact table into a
genuine dimensional model with conformed dimensions, and gives the agent more
interesting things to reason about.

### 2a. Brazilian state & municipality (IBGE) reference data
- **Source:** https://github.com/datasets-br/state-codes and
  https://github.com/datasets-br/city-codes
- **Contents:** ISO 3166-2:BR state codes and IBGE municipality codes/names
- **Use:** cleans and standardizes `dim_geography` (Olist only ships city name
  + state abbreviation + zip prefix; this lets you roll up to region/state
  reliably and join to any other IBGE-coded dataset later)
- **License:** open (public domain / ODbL-style, check repo)

### 2b. Brazilian public holidays
- **Source:** BrasilAPI (`https://brasilapi.com.br/api/feriados/v1/{ano}`) or
  Nager.Date (`https://date.nager.at/api/v3/publicholidays/{year}/BR`)
- **Contents:** national holiday dates by year
- **Use:** adds an `is_holiday` flag to `dim_date`, enabling questions like
  "does order volume/delivery time change around holidays?"
- **License:** free public API, no key required

### 2c. USD/BRL exchange rate
- **Source:** Banco Central do Brasil SGS API, series 1:
  `https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/dados?formato=csv&dataInicial=01/01/2016&dataFinal=31/12/2018`
- **Contents:** daily USD/BRL free-market rate
- **Use:** a small `fact_exchange_rate` / `dim_currency` companion, mostly to
  demonstrate joining an external time series into the lake and to let the
  semantic layer expose metrics in USD as well as BRL
- **License:** open government data, no key required

## 3. How these map to the dimensional model

- **Fact:** `fact_order_items` — grain = one row per order line item.
  Measures: `price`, `freight_value`, `payment_value`, `review_score`.
- **Dimensions:** `dim_customer`, `dim_seller`, `dim_product` (+ English
  category via translation file), `dim_date` (+ holiday flag), `dim_geography`
  (+ IBGE codes and lat/lng), `dim_payment_type`, `dim_order_status`.
- **Conformed/reference:** `dim_currency` / exchange-rate fact for BRL↔USD.

## 4. Candidate semantic-layer metrics

`GMV`, `average_order_value`, `freight_to_price_ratio`, `on_time_delivery_rate`,
`average_review_score`, `repeat_customer_rate`, `revenue_by_category`,
`seller_fulfillment_time`.

## 5. Not used, considered and rejected

- **NYC TLC taxi trip records** — great for showing raw data-lake scale
  (tens of millions of rows/month, partitioned Parquet), but the schema is
  a single wide fact with almost no dimensional richness. Worth adding later
  as a second, higher-volume domain if the portfolio wants a "big data"
  story, but not needed to start.
- **NYC/Chicago 311 service requests, FRED/World Bank economic indicators** —
  good data, but don't naturally dimensionalize into a retail-style star
  schema and don't support as interesting an agent Q&A surface for a first
  build.
