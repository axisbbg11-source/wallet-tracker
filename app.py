import html
import json
import logging
import math
import os
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
LOCAL_WALLETS_PATH = BASE_DIR / "wallets.txt"
RENDER_WALLETS_PATH = Path("/etc/secrets/wallets.txt")
LAMPORTS_PER_SOL = 1_000_000_000
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
IGNORED_TOKEN_MINTS = {WRAPPED_SOL_MINT, USDC_MINT, USDT_MINT}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _db_connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database(database_path: str | Path) -> None:
    with closing(_db_connect(database_path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS buys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signature TEXT NOT NULL,
                wallet TEXT NOT NULL,
                token TEXT NOT NULL,
                amount_sol REAL NOT NULL,
                token_amount REAL NOT NULL,
                timestamp INTEGER NOT NULL,
                alert_sent INTEGER NOT NULL DEFAULT 0,
                UNIQUE(signature, wallet, token)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_buys_token_timestamp
            ON buys(token, timestamp)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cluster_alerts (
                token TEXT PRIMARY KEY,
                detected_at INTEGER NOT NULL,
                wallets_json TEXT NOT NULL,
                alert_sent INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_claims (
                signature TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY(signature, alert_type)
            )
            """
        )


def _default_wallets_path() -> Path:
    if RENDER_WALLETS_PATH.is_file():
        return RENDER_WALLETS_PATH
    return LOCAL_WALLETS_PATH


def load_wallets(wallets_path: str | Path | None = None) -> set[str]:
    if wallets_path is None:
        wallets_path = _default_wallets_path()
    path = Path(wallets_path)
    with path.open("r", encoding="utf-8") as wallets_file:
        wallets = {
            line.strip()
            for line in wallets_file
            if line.strip() and not line.lstrip().startswith("#")
        }
    if not wallets:
        raise ValueError(f"No wallet addresses found in {path}")
    return wallets


def _as_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _token_transfer_amount(transfer: dict[str, Any]) -> float:
    amount = transfer.get("tokenAmount")
    if amount is not None:
        return _as_number(amount)

    raw_amount = transfer.get("rawTokenAmount")
    if isinstance(raw_amount, dict):
        raw_value = _as_number(raw_amount.get("tokenAmount"))
        decimals = _as_number(raw_amount.get("decimals"))
        if raw_value and decimals >= 0:
            return raw_value / (10 ** int(decimals))
    return 0.0


def _is_ignored_token(mint: str) -> bool:
    return mint in IGNORED_TOKEN_MINTS


def _swap_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    return []


def _swap_account(entry: dict[str, Any]) -> str | None:
    for key in ("userAccount", "account", "owner"):
        account = entry.get(key)
        if isinstance(account, str):
            return account
    return None


def _swap_amount(entry: dict[str, Any]) -> float:
    return _as_number(entry.get("tokenAmount", entry.get("amount")))


def _transaction_timestamp(transaction: dict[str, Any]) -> int:
    value = transaction.get("timestamp")
    if value is None:
        return int(time.time())
    if isinstance(value, (int, float)):
        return int(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        logger.warning("Invalid transaction timestamp %r; using receive time", value)
        return int(time.time())


def extract_wallet_buys(
    transaction: dict[str, Any],
    tracked_wallets: set[str],
    minimum_buy_sol: float,
) -> list[dict[str, Any]]:
    if transaction.get("type") != "SWAP":
        return []

    events = transaction.get("events")
    swap_event = events.get("swap") if isinstance(events, dict) else None
    if isinstance(swap_event, dict):
        token_outputs = _swap_entries(swap_event.get("tokenOutputs"))
        sol_spent: dict[str, float] = {}
        native_input = swap_event.get("nativeInput")
        for entry in _swap_entries(native_input):
            wallet = _swap_account(entry)
            if wallet in tracked_wallets:
                sol_spent[wallet] = sol_spent.get(wallet, 0.0) + _as_number(
                    entry.get("amount")
                )

        token_inputs = _swap_entries(swap_event.get("tokenInputs"))
        for entry in token_inputs:
            wallet = _swap_account(entry)
            if wallet in tracked_wallets and entry.get("mint") == WRAPPED_SOL_MINT:
                sol_spent[wallet] = sol_spent.get(wallet, 0.0) + _swap_amount(entry)

        timestamp = _transaction_timestamp(transaction)
        signature = transaction.get("signature")
        if not isinstance(signature, str) or not signature:
            logger.warning("Ignoring SWAP transaction without a signature")
            return []

        buys = []
        for output in token_outputs:
            wallet = _swap_account(output)
            mint = output.get("mint")
            amount = _swap_amount(output)
            if (
                wallet not in tracked_wallets
                or not isinstance(mint, str)
                or not mint
                or _is_ignored_token(mint)
                or amount <= 0
            ):
                continue
            spent_sol = sol_spent.get(wallet, 0.0)
            if spent_sol < minimum_buy_sol:
                continue
            buys.append(
                {
                    "signature": signature,
                    "wallet": wallet,
                    "token": mint,
                    "amount_sol": spent_sol,
                    "token_amount": amount,
                    "timestamp": timestamp,
                }
            )
        return buys

    transfers = transaction.get("tokenTransfers")
    if not isinstance(transfers, list):
        transfers = []

    token_deltas: dict[tuple[str, str], float] = {}
    for transfer in transfers:
        if not isinstance(transfer, dict):
            continue
        mint = transfer.get("mint")
        if not isinstance(mint, str) or not mint:
            continue
        amount = _token_transfer_amount(transfer)
        sender = transfer.get("fromUserAccount")
        recipient = transfer.get("toUserAccount")
        if isinstance(sender, str) and sender in tracked_wallets:
            key = (sender, mint)
            token_deltas[key] = token_deltas.get(key, 0.0) - amount
        if isinstance(recipient, str) and recipient in tracked_wallets:
            key = (recipient, mint)
            token_deltas[key] = token_deltas.get(key, 0.0) + amount

    native_outgoing: dict[str, float] = {}
    wrapped_sol_outgoing: dict[str, float] = {}
    for transfer in transfers:
        if (
            isinstance(transfer, dict)
            and transfer.get("mint") == WRAPPED_SOL_MINT
            and transfer.get("fromUserAccount") in tracked_wallets
        ):
            wallet = transfer["fromUserAccount"]
            wrapped_sol_outgoing[wallet] = wrapped_sol_outgoing.get(wallet, 0.0) + (
                _token_transfer_amount(transfer)
            )

    for account_data in transaction.get("accountData", []):
        if not isinstance(account_data, dict):
            continue
        wallet = account_data.get("account")
        if wallet not in tracked_wallets or native_outgoing.get(wallet, 0.0) > 0:
            continue
        balance_change = _as_number(account_data.get("nativeBalanceChange"))
        if balance_change < 0:
            lamports = -balance_change
            if transaction.get("feePayer") == wallet:
                lamports = max(0.0, lamports - _as_number(transaction.get("fee")))
            native_outgoing[wallet] = lamports / LAMPORTS_PER_SOL

    timestamp = _transaction_timestamp(transaction)
    signature = transaction.get("signature")
    if not isinstance(signature, str) or not signature:
        logger.warning("Ignoring SWAP transaction without a signature")
        return []

    buys = []
    for (wallet, mint), amount in token_deltas.items():
        if amount <= 0 or _is_ignored_token(
            mint
        ):
            continue

        sol_amount = wrapped_sol_outgoing.get(wallet, 0.0)
        if sol_amount < minimum_buy_sol:
            continue
        buys.append(
            {
                "signature": signature,
                "wallet": wallet,
                "token": mint,
                "amount_sol": sol_amount,
                "token_amount": amount,
                "timestamp": timestamp,
            }
        )
    return buys


def _short_address(address: str) -> str:
    if len(address) <= 8:
        return address
    return f"{address[:4]}...{address[-4:]}"


def _buy_message(buy: dict[str, Any]) -> str:
    token = html.escape(buy["token"])
    wallet = html.escape(_short_address(buy["wallet"]))
    signature = html.escape(buy["signature"])
    mint_url = html.escape(buy["token"], quote=True)
    signature_url = html.escape(buy["signature"], quote=True)
    return (
        "🟢 <b>Wallet BUY</b>\n"
        f"Token: <code>{token}</code>\n"
        f"Wallet: <code>{wallet}</code>\n"
        f"Buy size: <b>{buy['amount_sol']:.4f} SOL</b>\n"
        f'<a href="https://dexscreener.com/solana/{mint_url}">DexScreener</a> | '
        f'<a href="https://solscan.io/token/{mint_url}">Solscan</a> | '
        f'<a href="https://solscan.io/tx/{signature_url}">Transaction</a>'
    )


def _cluster_message(token: str, wallets: list[str]) -> str:
    escaped_token = html.escape(token)
    displayed_wallets = wallets[:10]
    wallet_list = ", ".join(
        f"<code>{html.escape(_short_address(wallet))}</code>"
        for wallet in displayed_wallets
    )
    if len(wallets) > len(displayed_wallets):
        wallet_list += f", and {len(wallets) - len(displayed_wallets)} more"
    mint_url = html.escape(token, quote=True)
    return (
        "🚨 <b>CLUSTER ALERT</b>\n"
        f"Token: <code>{escaped_token}</code>\n"
        f"Tracked wallets buying in 10 minutes: <b>{len(wallets)}</b>\n"
        f"Wallets: {wallet_list}\n"
        f'<a href="https://dexscreener.com/solana/{mint_url}">DexScreener</a> | '
        f'<a href="https://solscan.io/token/{mint_url}">Solscan</a>'
    )


def send_telegram_message(token: str, chat_id: str, message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or not body.get("ok"):
        raise RuntimeError("Telegram rejected the message")


def _http_status(exception: Exception) -> int | None:
    response = getattr(exception, "response", None)
    return getattr(response, "status_code", None)


def _claim_alert(
    database_path: str | Path, signature: str, alert_type: str
) -> bool:
    with closing(_db_connect(database_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT OR IGNORE INTO alert_claims (signature, alert_type, status)
            VALUES (?, ?, 'sending')
            """,
            (signature, alert_type),
        )
        if connection.execute("SELECT changes()").fetchone()[0] == 1:
            return True
        cursor = connection.execute(
            """
            UPDATE alert_claims SET status = 'sending'
            WHERE signature = ? AND alert_type = ? AND status = 'failed'
            """,
            (signature, alert_type),
        )
        return cursor.rowcount == 1


def _set_alert_status(
    database_path: str | Path, signature: str, alert_type: str, status: str
) -> None:
    with closing(_db_connect(database_path)) as connection, connection:
        connection.execute(
            """
            UPDATE alert_claims SET status = ?
            WHERE signature = ? AND alert_type = ?
            """,
            (status, signature, alert_type),
        )


def _send_claimed_alert(
    database_path: str | Path,
    signature: str,
    alert_type: str,
    telegram_token: str,
    chat_id: str,
    message: str,
) -> bool:
    if not _claim_alert(database_path, signature, alert_type):
        return False
    try:
        send_telegram_message(telegram_token, chat_id, message)
    except Exception as exc:
        _set_alert_status(database_path, signature, alert_type, "failed")
        logger.error(
            "Telegram send failed (%s; HTTP status: %s)",
            type(exc).__name__,
            _http_status(exc),
        )
        return False
    _set_alert_status(database_path, signature, alert_type, "sent")
    return True


def _required_telegram_settings() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured")
    return token, chat_id


def _save_buy_and_detect_cluster(
    database_path: str | Path, buy: dict[str, Any]
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    with closing(_db_connect(database_path)) as connection, connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO buys
                (signature, wallet, token, amount_sol, token_amount, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                buy["signature"],
                buy["wallet"],
                buy["token"],
                buy["amount_sol"],
                buy["token_amount"],
                buy["timestamp"],
            ),
        )

        pending_buy = connection.execute(
            """
            SELECT * FROM buys
            WHERE signature = ? AND wallet = ? AND token = ? AND alert_sent = 0
            """,
            (buy["signature"], buy["wallet"], buy["token"]),
        ).fetchone()

        latest_timestamp = connection.execute(
            "SELECT MAX(timestamp) AS latest FROM buys WHERE token = ?",
            (buy["token"],),
        ).fetchone()["latest"]
        wallets = [
            row["wallet"]
            for row in connection.execute(
                """
                SELECT DISTINCT wallet FROM buys
                WHERE token = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY wallet
                """,
                (buy["token"], latest_timestamp - 600, latest_timestamp),
            )
        ]
        if len(wallets) >= 3:
            connection.execute(
                """
                INSERT OR IGNORE INTO cluster_alerts
                    (token, detected_at, wallets_json)
                VALUES (?, ?, ?)
                """,
                (buy["token"], latest_timestamp, json.dumps(wallets)),
            )

        pending_cluster = connection.execute(
            """
            SELECT * FROM cluster_alerts
            WHERE token = ? AND alert_sent = 0
            """,
            (buy["token"],),
        ).fetchone()
    return pending_buy, pending_cluster


def _mark_alert_sent(
    database_path: str | Path, table: str, row_id: int | str
) -> None:
    if table == "buys":
        query = "UPDATE buys SET alert_sent = 1 WHERE id = ?"
    elif table == "cluster_alerts":
        query = "UPDATE cluster_alerts SET alert_sent = 1 WHERE token = ?"
    else:
        raise ValueError("Unsupported alert table")
    with closing(_db_connect(database_path)) as connection, connection:
        connection.execute(query, (row_id,))


def _deliver_pending_alerts(
    database_path: str | Path,
    buy: dict[str, Any],
    telegram_token: str,
    chat_id: str,
) -> None:
    pending_buy, pending_cluster = _save_buy_and_detect_cluster(database_path, buy)
    if pending_buy is not None:
        sent = _send_claimed_alert(
            database_path,
            buy["signature"],
            "buy",
            telegram_token,
            chat_id,
            _buy_message(dict(pending_buy)),
        )
        if sent:
            _mark_alert_sent(database_path, "buys", int(pending_buy["id"]))
    if pending_cluster is not None:
        wallets = json.loads(pending_cluster["wallets_json"])
        sent = _send_claimed_alert(
            database_path,
            pending_cluster["token"],
            "cluster",
            telegram_token,
            chat_id,
            _cluster_message(pending_cluster["token"], wallets),
        )
        if sent:
            _mark_alert_sent(
                database_path, "cluster_alerts", pending_cluster["token"]
            )


def create_app(
    database_path: str | Path | None = None,
    wallets_path: str | Path | None = None,
) -> Flask:
    app = Flask(__name__)
    if database_path is None:
        database_path = os.getenv("DB_PATH", "./tracker.db")
    if wallets_path is None:
        wallets_path = _default_wallets_path()
    app.config["DATABASE_PATH"] = str(database_path)
    app.config["WALLETS_PATH"] = str(wallets_path)
    initialize_database(database_path)

    @app.post("/webhook")
    def webhook():
        webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
        authorization = request.headers.get("Authorization", "")
        if not webhook_secret or not secrets.compare_digest(
            authorization, webhook_secret
        ):
            return jsonify(error="Unauthorized"), 401

        if os.getenv("DEBUG_LOG_PAYLOADS", "false").strip().lower() == "true":
            logger.info("Incoming webhook payload: %s", request.get_data(as_text=True))

        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify(error="Request body must be valid JSON"), 400
        if not isinstance(payload, list) or not all(
            isinstance(transaction, dict) for transaction in payload
        ):
            return jsonify(error="Webhook payload must be a list of transaction objects"), 400
        transactions = payload

        try:
            wallets = load_wallets(app.config["WALLETS_PATH"])
            processed = 0
            for transaction in transactions:
                buys = extract_wallet_buys(
                    transaction,
                    wallets,
                    _minimum_buy_sol(),
                )
                if not buys:
                    continue
                telegram_token, chat_id = _required_telegram_settings()
                for buy in buys:
                    _deliver_pending_alerts(
                        app.config["DATABASE_PATH"], buy, telegram_token, chat_id
                    )
                    processed += 1
            return jsonify(status="ok", buys_processed=processed), 200
        except (OSError, RuntimeError, sqlite3.Error, requests.RequestException, ValueError) as exc:
            logger.error(
                "Webhook processing failed (%s; HTTP status: %s)",
                type(exc).__name__,
                _http_status(exc),
            )
            return jsonify(error="Webhook processing failed"), 500

    @app.get("/health")
    def health():
        try:
            with closing(_db_connect(app.config["DATABASE_PATH"])) as connection:
                connection.execute("SELECT 1")
            return jsonify(status="ok"), 200
        except sqlite3.Error as exc:
            logger.error(
                "Health check failed (%s; HTTP status: %s)",
                type(exc).__name__,
                _http_status(exc),
            )
            return jsonify(status="error"), 503

    return app


def _minimum_buy_sol() -> float:
    raw_value = os.getenv("MIN_BUY_SOL", "1").strip()
    try:
        minimum = float(raw_value)
    except ValueError as exc:
        raise RuntimeError("MIN_BUY_SOL must be a non-negative number") from exc
    if not math.isfinite(minimum) or minimum < 0:
        raise RuntimeError("MIN_BUY_SOL must be a non-negative number")
    return minimum


def run_server() -> None:
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
    )


app = create_app()


if __name__ == "__main__":
    run_server()
