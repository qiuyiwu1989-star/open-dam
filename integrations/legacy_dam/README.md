# Legacy DAM production compatibility profile

This directory aligns the public repository with the deployed legacy DAM code,
exported on 2026-11-08. It is an **optional compatibility profile**, not a replacement
for the root Docker Compose application. Production credentials, database rows,
asset inventories, prompts, logs, account IDs and infrastructure configuration
are deliberately excluded.

## Relationship to the default app

| | Default Docker app | This compatibility profile |
|---|---|---|
| Tables | `asset`, `embedding`, `task` | Existing `dam_asset`, `gen_run`, `gen_file`, related legacy tables and `dam_internal` views |
| Image model | OpenCLIP ViT-B/32, configured pretrained weights | OpenAI CLIP ViT-B/32 |
| Input | Browser uploads | Existing COS objects and platform execution APIs |
| Storage | Local content-addressed files | Configured COS buckets; originals retained |

Do not point this profile at the default app database: schemas and model spaces
are different. **Do not mix embeddings from these two model/weight combinations.**
The complete legacy baseline schema and authentication service are not supplied
here. This is source alignment for operators who already have that schema, not a
one-command migration. `sync_dam_asset.sql` is a projection example; replace its
example bucket before use. No production schema dump is included.

## Shipped fixes

- Cursor-based workflow and execution discovery with explicit invalid/repeated
  cursor failure; Artist uses full execution outputs rather than summary-only data.
- Per-output retry ledger so existing execution records do not hide failed downloads.
- Separate `--drain` mode and periodic projection while files are processed.
- COS object inventory, resumable reconciliation, ETag tracking and preservation of
  existing duplicate/rejected/hidden assets. Ownership extracted from paths is an
  **unverified hint**, not a permission decision.
- Bounded local CLIP encoding; COS thumbnail commands use the full command as a
  query key. Error records include safe HTTP/provider details and retry categories.
- API/frontend reliability fixes, including COS-only generated asset details,
  cancel-vote persistence, deterministic browse ordering and image URL encoding.

## Configuration

Use a private configuration directory such as `~/.opendam/` (outside the checkout).
The legacy adapter reads `.aha_pg.json` there with
`host`, `port`, `dbname`, `user`, `password`; COS JSON contains
`region`, `secret_id`, `secret_key`. Populate these yourself, restrict filesystem
permissions, and never commit them. All example values are fictional placeholders.

Copy `cos_sources.example.json` to an untracked `cos_sources.json`, set each bucket,
region and `credential_file`, and export `DAM_SOURCE_CONFIG` to its absolute path.
Use a separate process environment for each platform's `PLATFORM_API_TOKEN`.
See `.env.example`; environment files are not automatically loaded by Python.
Set `DAM_LEGACY_SCRIPTS` to this directory when invoking from another directory.

Examples (require existing legacy schema and private settings):

```sh
cd integrations/legacy_dam
# Preview object discovery (no asset writes)
python cos_ingest.py --config /path/to/private/cos_sources.json
# Register objects only after reviewing the preview
python cos_ingest.py --config /path/to/private/cos_sources.json --apply
# Platform read probe; the token must match the selected platform/team
python platform_sync.py --source app --days 3 --limit 3
# Deliberate write actions, not invoked by root Compose:
python platform_sync.py --source app --days 3 --apply
python platform_sync.py --source app --drain --apply
python cos_encode.py
```

Install dependencies from `requirements.txt` in a separate environment. CLIP weights
are downloaded on first use; subsequent image embedding is local. No cron/systemd
units pointing to a private host are distributed.

## Security and operational limits

- `dam_api.py` is a legacy API without reliable built-in user authorization.
  `dam_access.py` is an optional external-session adapter; its image route reflects
  the anonymous production setting at export time. **It is not an enabled access
  boundary.** Apply your own trusted reverse-proxy authentication before exposing
  private sources. This export does not enable anonymous access anywhere.
- Platform URLs come from upstream execution metadata. Review origins and apply
  network egress restrictions; legacy download paths are not an SSRF-hardened
  public upload endpoint.
- Source discovery success is not full output coverage. `stored` includes existing
  file confirmations, not just newly imported images. Old failures are not reset.
- SVG, oversized, corrupted and expired source objects can remain unencoded.
  No unlimited-size fallback or bulk retry reset is included.
- The legacy Chinese-search translation helper can call a separately configured
  cloud model. Its fallback/credential issue is **not fixed by this export**.
  Local image encoding does not require that service. The root app remains local.
- Upstream CDN/storage outages are external dependencies, not resolved by copying
  this code. No production health/coverage claims or private incident data are
  published.

`production-code-hashes.json` records SHA-256 of exported runtime code for provenance.
Public files differ through sanitization, configuration extraction and minimal
legacy adapter extraction; those hashes are **not** public package checksums.
The old credential-bearing adapter files have no exported source hashes.
