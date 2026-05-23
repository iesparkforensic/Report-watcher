# External Pinger Setup

GitHub's scheduled Actions are unreliable — runs are routinely dropped or delayed by 20+ minutes on low-activity / free-tier repos. An external pinger calls the GitHub API hourly to dispatch the workflow manually, bypassing GitHub's flaky scheduler entirely.

The workflow already supports `workflow_dispatch`, so no code change is needed — just the steps below.

---

## Step 1 — Create a fine-grained Personal Access Token

1. Open https://github.com/settings/personal-access-tokens/new
2. Fill in:
   - **Token name**: `report-watcher-pinger`
   - **Expiration**: 1 year (set a calendar reminder to rotate)
   - **Repository access**: *Only select repositories* → choose `iesparkforensic/Report-watcher`
   - **Repository permissions** → expand and set:
     - **Actions**: `Read and write`
     - **Metadata**: `Read-only` (auto-selected, required)
   - Leave everything else as "No access"
3. Click **Generate token**
4. **Copy the token immediately** (starts with `github_pat_…`). You won't see it again.

## Step 2 — Test the token locally (optional but recommended)

Paste this into a terminal, replacing `YOUR_TOKEN`:

```bash
curl -i -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  https://api.github.com/repos/iesparkforensic/Report-watcher/actions/workflows/watcher.yml/dispatches \
  -d '{"ref":"main"}'
```

You should see `HTTP/2 204` and no body. Within ~10s a new run will appear in the Actions tab and you'll get a heartbeat Telegram message.

If you see `401` or `403`, the token doesn't have the right scope. Redo Step 1 and confirm "Actions: Read and write".

## Step 3 — Schedule it on cron-job.org (free)

1. Sign up at https://cron-job.org (free, no credit card)
2. Click **Create cronjob**
3. Fill in:
   - **Title**: `Report Watcher hourly ping`
   - **URL**: `https://api.github.com/repos/iesparkforensic/Report-watcher/actions/workflows/watcher.yml/dispatches`
   - **Schedule**: Every hour, at minute `5` (or any minute you like — pick one that doesn't collide with GitHub's `:17`)
4. Expand **Advanced** → switch **Request method** to `POST`
5. Open the **Headers** tab and add three headers:
   | Key | Value |
   |---|---|
   | `Accept` | `application/vnd.github+json` |
   | `Authorization` | `Bearer YOUR_TOKEN` |
   | `X-GitHub-Api-Version` | `2022-11-28` |
6. Open the **Body** tab → set body to: `{"ref":"main"}` (content type `application/json`)
7. Open **Notifications** → enable "Notify on failure" so you hear if the ping itself breaks
8. Save

cron-job.org will now POST to GitHub every hour, triggering the watcher reliably.

## Step 4 — Verify

After the next scheduled minute:

1. Check https://github.com/iesparkforensic/Report-watcher/actions — a new run with trigger **workflow_dispatch** should appear, not "schedule".
2. Telegram should receive a heartbeat ~30s after.
3. cron-job.org dashboard should show a green tick and HTTP 204.

If all three happen, you're done.

## Step 5 — Optional cleanup

Once the external pinger is proven reliable for a few days, you can remove the unreliable GitHub schedule to stop the duplicated/half-working runs:

In `.github/workflows/watcher.yml`, delete these two lines:

```yaml
  schedule:
    - cron: '17 * * * *'
```

Leave `workflow_dispatch:` in place — that's what the pinger uses.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| cron-job.org shows 401 | Token expired or wrong scope | Regenerate PAT with Actions: Read and write |
| cron-job.org shows 404 | Wrong workflow filename in URL | Workflow file must be `watcher.yml` |
| 204 returned but no run appears | Branch ref wrong | Body must be `{"ref":"main"}` |
| Run starts but no Telegram | Secrets missing | Check repo Settings → Secrets → `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

## Why not GitHub's own schedule?

GitHub's docs state scheduled workflows "may be delayed during periods of high loads" and the [known-issue tracker](https://github.com/orgs/community/discussions/52817) has years of reports of runs being silently dropped on low-activity repos. Empirically on this repo, ~50% of hourly slots are dropped or delayed by 20+ minutes. External pingers are the standard workaround.
