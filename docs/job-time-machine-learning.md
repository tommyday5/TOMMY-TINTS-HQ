# Job Time Memory and Machine Learning — Design v0.1

Status: DESIGN ONLY; no ML training or prediction service has been deployed.

## Event capture
Store each job's immutable events: timer start/pause/resume, manual adjustments with reasons, stage tags, estimate revisions, materials, consumables, travel, customer quote, approval, completion and payment. Server timestamps in UTC; display UK local time. Support offline-first sync with idempotent event IDs and conflict detection. Preserve history rather than overwriting adjustments.

## Features
Service, specific operation (prep/sanding/primer/paint/polish/tint/wrap), vehicle body style and size, panel count, damage extent, film/vinyl quantity, access difficulty, condition, mobile travel, technician, experience, labour rate, customer type and date. Never use protected personal characteristics to price.

## Progressive learning
1. First 10–30 comparable jobs: deterministic rate card + conservative robust median and interquartile range; show sample count and uncertainty.
2. When enough verified jobs accumulate: regularised regression / gradient boosting with time-based validation and segment checks; avoid leakage from post-job information.
3. Estimate labour hours and materials separately, with 50/80% prediction intervals. Derive sell price from controlled cost and margin rules, not direct black-box price prediction.
4. Analyse quote conversion and actual contribution margin separately; never optimise only for conversion.
5. Flag outliers, data entry errors, unpaid breaks, multi-technician time, rework and bundled jobs for review.

## Human oversight
Show baseline, ML suggestion, confidence, comparable jobs and explanation. Never auto-adjust final approved quotes, override minimum margins, or publish unreviewed prices. Maintain model versions, training dates, data counts, opt-out and rollback.

## Data governance
Consent/transparency for AI photo processing; keep customer contact details out of training features. Store data in private authenticated PostgreSQL; audit accesses and revisions. No sensitive records in public GitHub or browser-only storage. Maintain retention and export/deletion policies.

## Integration
The mobile timer is currently browser-local. First deliver authenticated HQ jobs, database and safe sync. Then attach timer events to job IDs, validate actuals, implement baseline predictions and evaluate against historical holdout jobs. The public OpenAI assistant may gather structured scope but cannot independently set final prices.
