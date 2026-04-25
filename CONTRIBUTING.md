# Contributing

Thanks for contributing to Sianna Financials.

## Project Components

- `square18_signals/`: deterministic options pricing + strategy recommender library
- `square18_signals_web/`: FastAPI backend + static web UI

Please keep changes scoped to the relevant component.

## Local Setup

From repository root:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r square18_signals_web/requirements.txt
```

Run the app:

```bash
bash square18_signals_web/run.sh
```

## Development Guidelines

1. Keep API contracts stable unless explicitly changing frontend/backend together.
2. Preserve deterministic behavior in `square18_signals` (no hidden side effects).
3. Prefer graceful fallbacks for external market/news data failures.
4. Avoid unrelated refactors in the same PR.
5. Keep frontend simple (no new build system unless discussed first).

## Testing Expectations

Run relevant tests before opening a PR:

```bash
# core engine
python3 -m pytest square18_signals/tests -q

# web API E2E
python3 -m pytest square18_signals_web/tests/test_e2e_app.py -q

# market news (CNBC → MarketWatch → internal snapshot)
python3 -m pytest square18_signals_web/tests/test_market_news.py -q
```

If UI behavior changed, run browser E2E too:

```bash
python3 -m playwright install chromium
python3 -m pytest square18_signals_web/tests/test_e2e_ui_playwright.py -q
```

## Commit and PR Guidelines

- Use clear commit messages that explain intent.
- Keep PRs focused and reviewable.
- Include:
  - what changed
  - why it changed
  - how it was tested
  - any known limitations or follow-up work

## PR Checklist

- [ ] Code builds/runs locally
- [ ] Relevant tests pass
- [ ] API/frontend contract changes are coordinated
- [ ] Docs updated (`README.md`, `Claude.md`, or this file) when behavior changed
- [ ] No local artifacts committed (`.venv`, caches, temp files)

