# Bambi Knowledge Agent

Local Dockerized knowledge agent for Bambi course and FAQ data.

## Quick start

1. Set `LLM_PROVIDER=openai` and `OPENAI_API_KEY`, or set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`, in `.env`.
2. Run `docker compose up --build`.
3. Open `http://localhost:8000/`.

## CLI maintenance

- `python -m app.cli status`
- `python -m app.cli list-tools`
- `python -m app.cli conflicts`

## WordPress knowledge sync

Export a full raw WordPress snapshot and mark only new/changed records compared with the latest previous snapshot:

```powershell
python scripts\export_wordpress_raw.py --sleep 2.5
```

The export writes `manifest.json`, `index.jsonl`, and `changed_files.json` under `data/website_raw/<timestamp>/`. `changed_files.json` contains only pages/posts with `change_status` of `new` or `changed`.

Build knowledge tools only from the new/changed WordPress records:

```powershell
python scripts\build_knowledge_tools.py --only-changed
```

To compare against a specific older snapshot instead of the latest sibling export:

```powershell
python scripts\export_wordpress_raw.py --previous-dir data\website_raw\20260618T214803Z --sleep 2.5
```

## One-time Drive quote rebuild

The approved Drive quote rebuild uses `data/drive_quotes_raw/inventory.json`, downloads the listed DOCX/PDF files, excludes every `ישן` folder, removes dynamic commercial and personal data, and builds an audited candidate before replacing active tools.

Prepare and inspect extraction without calling the LLM:

```powershell
python scripts\update_knowledge_from_drive_quotes.py --prepare-only
```

Build a candidate without changing active tools:

```powershell
python scripts\update_knowledge_from_drive_quotes.py --skip-download
```

Apply only after the audit passes. A full tool and manifest backup is created automatically:

```powershell
python scripts\update_knowledge_from_drive_quotes.py --skip-download --apply
```

Use `--refresh-tool course_example` to invalidate one cached course decision and synthesis. Reports and checkpoints are written under `data/drive_quotes_build/`.

## Included features

- FastAPI chat API and local test UI
- SQLite-backed chat and tool-call history
- File-backed knowledge tools from `app/tools-knowleage`
- Selectable OpenAI Agents SDK or Anthropic Claude runtime with the same approved tools and guardrails

## Local Claude evaluation

Claude runs behind a provider flag so the approved OpenAI demo remains the default and available as a rollback:

```powershell
$env:LLM_PROVIDER="anthropic"
$env:ANTHROPIC_API_KEY="..."
$env:ANTHROPIC_MODEL="claude-sonnet-5"
docker compose up --build
```

Anthropic response logs include total duration, time to first streamed text, tool rounds, and token usage. Do not commit the API key or place it in benchmark output.

Run the read-only latency suite after exporting the Anthropic key:

```powershell
$env:ANTHROPIC_API_KEY="..."
python scripts\benchmark_anthropic.py
```

## AWS WhatsApp production runtime

The production runtime is serverless and is defined in `infra/production.yaml`. It uses API Gateway, two Lambda functions, SQS with a dead-letter queue, DynamoDB with point-in-time recovery, Secrets Manager, CloudWatch logs, and a dead-letter alarm.

Deploy or update it from PowerShell after authenticating the AWS CLI:

```powershell
.\infra\deploy-production.ps1 -Region eu-central-1
```

The runtime secret `bambi-bot/production/runtime` must contain the Anthropic and MyBusiness credentials. Keep the four `meta_*` values empty until the Meta test number is ready. Configure Meta with the `WebhookUrl` stack output only after setting `meta_verify_token`, `meta_app_secret`, `meta_access_token`, and `meta_phone_number_id`.

## Useful endpoints

- `GET /health`
- `POST /chat/sessions`
- `POST /chat/sessions/{session_id}/messages`
- `GET /chat/sessions/{session_id}`
- `POST /admin/reload-sources` returns disabled status because online ingestion is off
- `GET /admin/sources/status`
- `GET /admin/conflicts`
