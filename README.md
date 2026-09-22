# Portfolio: E-Commerce Analytics Platform

A personal portfolio project demonstrating a modern analytics stack end to
end, built on real Brazilian e-commerce data:

1. **Dimensionalized data lake** — raw → staged → conformed star schema
2. **Semantic layer** — reusable, governed metric definitions on top of the
   warehouse
3. **Agentic Q&A** — an agent that answers natural-language business
   questions by querying the semantic layer
4. **BI dashboards** — Tableau (or an alternative) on top of the same
   warehouse/semantic layer

## Status

Early scaffolding. Data sources are chosen and documented; the pipeline,
semantic layer, agent, and dashboards are not yet built.

## Data

See [`docs/data-sources.md`](docs/data-sources.md) for the full writeup.
Short version: primary dataset is the
[Olist Brazilian E-Commerce Public Dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
(~100k real orders, 2016–2018), enriched with IBGE state/city reference data,
Brazilian public holidays, and USD/BRL exchange rates.

```
data/
  raw/         # untouched source extracts (gitignored)
  staging/     # cleaned/typed, one table per source file (gitignored)
  warehouse/   # dimensional model (facts + dimensions) (gitignored)
```

## Planned architecture

- **Lake / warehouse:** dbt-style layered SQL (raw → staging → marts) over
  DuckDB or Postgres for local dev
- **Semantic layer:** metric definitions (dbt Metrics / Cube / similar)
  reused by both the agent and the dashboards
- **Agent:** LLM agent with a tool to query the semantic layer, so answers
  are grounded in governed metrics rather than free-form SQL
- **Visualization:** Tableau workbook(s) connected to the warehouse

## Next steps

- [ ] Download raw CSVs into `data/raw/`
- [ ] Build staging models (typed, deduplicated, conformed keys)
- [ ] Build the star schema (`fact_order_items` + dimensions)
- [ ] Define semantic-layer metrics
- [ ] Build the agent's query tool against the semantic layer
- [ ] Build Tableau dashboards
