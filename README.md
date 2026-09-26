# Vera AI Challenge — Grounded Deterministic Composer

## Approach
This submission uses a deterministic, context-grounded composer rather than an external LLM. Every outbound message is generated from the pushed Category, Merchant, Trigger and optional Customer contexts.

The routing has three layers:

1. **Surface selection** — customer context present → merchant-on-behalf-of-customer; otherwise Vera-to-merchant.
2. **Trigger family** — research/compliance, performance, seasonal/event, planning, reviews, competitor, renewal, recall, win-back, refill, etc.
3. **Category-aware phrasing** — dentists, salons, restaurants, gyms and pharmacies use different vocabulary and risk boundaries.

Messages deliberately prefer:
- concrete facts already present in context;
- active offers only;
- real dates/slots/counts/percentages from the trigger;
- one low-friction CTA;
- a concise rationale that matches the body;
- no URLs in message bodies;
- suppression-key deduplication.

The bot stores context versions and conversation state in memory, which is sufficient for the challenge process because the judge keeps the process alive. For production, Redis would replace the in-memory stores.

## Tradeoffs
A deterministic composer is less stylistically flexible than an LLM, but it guarantees repeatability, latency, and grounding. The main risk is lower language variety on unseen trigger kinds. The fallback names the trigger and asks before making unsupported claims rather than hallucinating.

## Additional context that would help
The most useful additional production context would be:
- canonical merchant offer/source-of-truth;
- exact consent scope for each customer channel;
- real appointment/stock availability at send time;
- a larger library of category-specific approved message patterns.

## Run
```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

The submission URL should expose:
`/v1/healthz`, `/v1/metadata`, `/v1/context`, `/v1/tick`, `/v1/reply`.
