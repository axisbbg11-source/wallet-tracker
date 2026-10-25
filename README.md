# Solana Wallet Tracker

A Flask webhook receiver for Helius enhanced `SWAP` transactions. It watches the
addresses in `wallets.txt`, stores qualifying buys in SQLite, and sends Telegram
buy and one-time cluster alerts.

## Setup

1. Use Python 3.10 or newer and install dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env`, then configure the Telegram bot token, chat
   ID, Helius API key, and a long random `WEBHOOK_SECRET`. `MIN_BUY_SOL`
   defaults to `1`. The Helius webhook sends this secret in its `Authorization`
   header; all webhook requests without an exact match receive HTTP 401.
3. Create `wallets.txt` in the project directory, with one Solana wallet
   address per line. Keep this file private; it is excluded from Git.
4. Start the server:

   ```powershell
   python app.py
   ```

   The webhook endpoint is `http://localhost:5000/webhook`; health is available
   at `http://localhost:5000/health`. Deploy behind a production WSGI server and
   HTTPS for public use.
5. Make the webhook URL publicly reachable, then create or update the Helius
   webhook (the script matches an existing webhook by URL and configures its
   `authHeader` from `WEBHOOK_SECRET`):

   ```powershell
   python register_webhook.py --webhook-url https://your-host.example/webhook
   ```

The SQLite database path defaults to `./tracker.db` and can be changed with
`DB_PATH`. The app uses `/etc/secrets/wallets.txt` when it exists (for a Render
secret file), otherwise it reads `./wallets.txt`. Set `PORT` to override the
local default of `5000`; the server listens on `0.0.0.0`.

## Render deployment

Deploy this repository as a Render Web Service. The included `Procfile` runs
Gunicorn with one worker and four threads. Configure the environment variables
from `.env.example` in the Render dashboard, and add `wallets.txt` as a secret
file mounted at `/etc/secrets/wallets.txt`. Use a persistent disk and set
`DB_PATH` to a path on that disk if database contents must survive deployments.

`DEBUG_LOG_PAYLOADS=true` logs every raw webhook request body. Enable it only
temporarily for debugging because payloads may contain sensitive wallet
activity; set it to `false` for normal operation.

Helius and Telegram request failures are logged; unsuccessful webhook processing
returns HTTP 500 so Helius can retry.

## Alert behavior

Only Helius transactions with type `SWAP` are considered. A tracked wallet must
receive a non-SOL/non-USDC/non-USDT token and spend at least `MIN_BUY_SOL` of
native SOL or wrapped SOL. Buys are recorded once per transaction, wallet, and
token. Three distinct tracked wallets buying the same token within ten minutes
trigger one cluster alert for that token.
