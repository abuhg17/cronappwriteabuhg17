# cronappwriteabuhg17

This repository exports Appwrite database data every hour with GitHub Actions and stores the result in versioned JSON files.

## What gets created

- `data/appwrite/latest.json`: the newest full export
- `data/appwrite/history/snapshot-YYYYMMDDTHHMMSSZ.json`: timestamped history snapshots

## Required GitHub Secrets

Add these repository secrets in GitHub:

- `APPWRITE_ENDPOINT`
- `APPWRITE_PROJECT_ID`
- `APPWRITE_DATABASE_ID`
- `APPWRITE_API_KEY`

The workflow also accepts the same names used by Appwrite frontend projects:

- `NEXT_PUBLIC_APPWRITE_ENDPOINT`
- `NEXT_PUBLIC_APPWRITE_PROJECT_ID`
- `NEXT_PUBLIC_APPWRITE_DATABASE_ID`
- `NEXT_PUBLIC_APPWRITE_API_KEY`

Use these values from your Appwrite project:

- Endpoint: your Appwrite API endpoint
- Project ID: your Appwrite project ID
- Database ID: the database to export
- API key: an Appwrite server key with permission to read the target database and collections

## Workflow behavior

- Runs automatically every hour at minutes `33` and `37` via GitHub Actions cron
- Can also be started manually from the Actions tab with `workflow_dispatch`
- Also supports external trigger via `repository_dispatch`
- Commits backup changes back into the repository
- Uses workflow concurrency to avoid overlapping runs on the same branch
- If Appwrite reports `project_paused`, the workflow logs a warning and skips that run
- If the primary `APPWRITE_*` configuration returns `project_not_found`, the script tries the `NEXT_PUBLIC_APPWRITE_*` configuration before failing

## Important note about schedule time

GitHub Actions cron uses UTC. The current schedules `33 * * * *` and `37 * * * *` mean it runs at minutes `33` and `37` of every UTC hour.

## More reliable external trigger

Keep the GitHub schedule enabled, then add a second trigger from any external cron service that can send an HTTP `POST` request.

Recommended setup:

- Keep this workflow file active in GitHub
- In the external cron service, run the request every hour at minute `11`
- Use GitHub's `repository_dispatch` API endpoint
- Send the event type `external-hourly-sync`

Example request:

```bash
curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer YOUR_GITHUB_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/abuhg17/cronappwriteabuhg17/dispatches \
  -d '{"event_type":"external-hourly-sync"}'
```

Token notes:

- Use a GitHub token that can trigger repository events for this repo
- Store that token in the external cron service, not in this repository
- If both GitHub schedule and external trigger fire close together, concurrency prevents overlapping runs

## Local run

You can test locally with PowerShell:

```powershell
$env:APPWRITE_ENDPOINT="https://tor.cloud.appwrite.io/v1"
$env:APPWRITE_PROJECT_ID="your-project-id"
$env:APPWRITE_DATABASE_ID="your-database-id"
$env:APPWRITE_API_KEY="your-api-key"
python scripts/fetch_appwrite_backup.py
```
