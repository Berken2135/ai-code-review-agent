# Optional: real GitHub webhooks through a tunnel

By default the app listens on `127.0.0.1:8000` only, and you trigger reviews with
`scripts/send_test_webhook.py`. To have GitHub call the app on every PR event, expose the local
port through a tunnel. The app itself does not change.

## Before you open a tunnel

Everything reachable through the tunnel is public. Check these first:

1. `GITHUB_WEBHOOK_SECRET` is set. Without it the webhook answers 503, and every delivery must be
   signed with it.
2. `RUNS_API_TOKEN` is set to a long random value. `/runs` answers 503 without it and 401 without
   the right `Authorization: Bearer` header. Generate one straight to the clipboard (PowerShell):

   ```powershell
   $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); (($b | ForEach-Object { $_.ToString('x2') }) -join '') | Set-Clipboard
   ```

3. After editing `.env`, recreate the container: `docker compose up -d --force-recreate app`.

Still public, by design: `/health`, `/docs` and `/openapi.json`. They show the API schema but no
data.

## 1. Start a tunnel

Either tool works. Both connect out from your machine to `localhost:8000`, so the Compose port
binding stays local-only.

**Cloudflare quick tunnel** (no account needed; the URL changes every time you start it):

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

**ngrok** (free account and auth token needed):

```powershell
winget install --id Ngrok.Ngrok
ngrok config add-authtoken <your-ngrok-token>
ngrok http 8000
```

Copy the `https://...` URL it prints and check it: `Invoke-RestMethod https://<tunnel-url>/health`.

## 2. Add the webhook on GitHub

In the test repository go to **Settings → Webhooks → Add webhook**:

| Field | Value |
|---|---|
| Payload URL | `https://<tunnel-url>/webhooks/github` |
| Content type | `application/json` (required: form-encoded payloads are rejected with 400) |
| Secret | the same value as `GITHUB_WEBHOOK_SECRET` in `.env` |
| Events | "Let me select individual events" → **Pull requests** only |

GitHub sends a `ping` right away. Under **Recent Deliveries** it should show `200` with
`{"status":"pong"}`.

## 3. Trigger a review

Open a PR, push a commit to it, reopen it, or mark a draft as ready for review. Then:

```powershell
docker compose logs -f app
$h = @{ Authorization = "Bearer <RUNS_API_TOKEN>" }
Invoke-RestMethod http://localhost:8000/runs -Headers $h | ConvertTo-Json -Depth 6
```

GitHub's **Recent Deliveries** page shows each response (`202` queued, `200` ignored or
duplicate) and has a **Redeliver** button. Redeliveries reuse the delivery id, so the app treats
them as duplicates and does not review twice.

## 4. Close it again

- Stop the tunnel (Ctrl+C). A quick-tunnel URL stops working at that point.
- Disable or delete the webhook on GitHub, otherwise deliveries fail and pile up there.
- Rotate `RUNS_API_TOKEN` if you shared it anywhere.

## Troubleshooting

| Symptom on "Recent Deliveries" | Cause |
|---|---|
| `401 invalid signature` | The secret on GitHub differs from `.env`, or the container was not recreated. |
| `503 GITHUB_WEBHOOK_SECRET is not configured` | The secret is empty in `.env`. |
| `400 invalid pull_request payload` | Content type is `application/x-www-form-urlencoded`. Switch it to JSON. |
| `502` / timeout | The tunnel is down or the app container is not running (`docker compose ps`). |
| `202`, but the run fails | Open `/runs/{id}` and read `error` (token scopes, LLM key, and so on). |
