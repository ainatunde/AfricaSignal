# Fixtures

Real files from the sources the adapters read, so adapters can be built and tested without network
access. Each directory has a `manifest.json` with the source URL, retrieval date, checksums and
notes on robots.txt and terms of use.

| Directory | What | Adapter |
|---|---|---|
| `nbs/` | NBS price-watch workbooks (eLibrary, 2024) and ZIP-extracted workbooks (microdata catalog, 2026) | AS-010 (done) |
| `rss/` | 8 Nigerian news feeds | AS-023 |
| `gdelt/` | `lastupdate.txt`, Events, Mentions, GKG rows | AS-024 |
| `nerc/` | the orders listing and two PDFs | AS-026 |
| `nmdpra/` | the site's JavaScript shell (nothing readable without JS) | AS-026 |
| `nnpc/` | the CMS JSON behind the news page | AS-026 |

**This repository is public.** Public government statistics (NBS, NERC) are stored as served. News
text belongs to the outlets, so feed and API bodies are cut to 300 characters (the plan's
quotation limit) and the manifest says so; `original_sha256` identifies the untrimmed response.
Nothing here has been reviewed by a lawyer; the terms notes record what was found on each site.

Fetched with the `AfricaSignalBot/1.0` user agent, one request at a time, honouring each host's
robots.txt crawl-delay. Two outlets (TheCable, Guardian Nigeria) answer bots with a Cloudflare
challenge; that was not bypassed and they have no fixtures.
