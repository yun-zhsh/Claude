# Project rules

## Language (important)

- **Discussion with the user may be in Chinese. The software we build must be English-only.**
  Anyone running it sees English only — no Chinese anywhere at runtime, **including stock names**
  (use English names in `config.yaml`, e.g. Tencent Holdings, Hikvision).
- "Runtime-visible" means: web pages (text, buttons, tooltips, charts), CLI output and error messages,
  downloaded files (file names, CSV column names and contents, zip), generated data files under `data/`
  (CSV/JSON, including filing titles and source notes), GitHub Actions step names, and everything in `config.yaml`.
- The only Python file allowed to contain Chinese is `src/valuation_monitor/cn_patterns.py`: patterns used to
  *read* Chinese-language A-share filings. They are never displayed.
- Code comments, docstrings, README and tests may be written in Chinese.
- `tests/test_english_only.py` enforces this (string literals in `src/`, templates, static files, config, workflow,
  `data/`, rendered pages and downloads). Keep it green; extend it when adding new output channels.
- Secrets such as `SEC_USER_AGENT` are provided through environment variables only; never commit them.
