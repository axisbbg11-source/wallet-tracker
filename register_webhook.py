import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

HELIUS_API_URL = "https://api.helius.xyz/v0/webhooks"
WALLETS_PATH = Path(__file__).resolve().parent / "wallets.txt"


def load_wallets(wallets_path: str | Path = WALLETS_PATH) -> list[str]:
    with Path(wallets_path).open("r", encoding="utf-8") as wallets_file:
        wallets = list(
            dict.fromkeys(
                line.strip()
                for line in wallets_file
                if line.strip() and not line.lstrip().startswith("#")
            )
        )
    if not wallets:
        raise ValueError(f"No wallet addresses found in {wallets_path}")
    return wallets


def _webhook_body(
    webhook_url: str, wallets: list[str], webhook_secret: str
) -> dict[str, Any]:
    return {
        "webhookURL": webhook_url,
        "authHeader": webhook_secret,
        "transactionTypes": ["SWAP"],
        "accountAddresses": wallets,
        "webhookType": "enhanced",
        "txnStatus": "success",
    }


def register_webhook(
    webhook_url: str,
    api_key: str,
    wallets: list[str],
    webhook_secret: str,
    session: requests.Session | Any = requests,
) -> str:
    params = {"api-key": api_key}
    response = session.get(HELIUS_API_URL, params=params, timeout=30)
    response.raise_for_status()
    existing_webhooks = response.json()
    if not isinstance(existing_webhooks, list):
        raise ValueError("Helius returned an unexpected webhook list")

    body = _webhook_body(webhook_url, wallets, webhook_secret)
    existing = next(
        (
            webhook
            for webhook in existing_webhooks
            if isinstance(webhook, dict) and webhook.get("webhookURL") == webhook_url
        ),
        None,
    )
    if existing is not None:
        webhook_id = existing.get("webhookID")
        if not isinstance(webhook_id, str) or not webhook_id:
            raise ValueError("Matching Helius webhook has no webhookID")
        updated = session.patch(
            f"{HELIUS_API_URL}/{webhook_id}",
            params=params,
            json=body,
            timeout=30,
        )
        updated.raise_for_status()
        return f"Updated Helius webhook {webhook_id} for {len(wallets)} wallets"

    created = session.post(
        HELIUS_API_URL,
        params=params,
        json=body,
        timeout=30,
    )
    created.raise_for_status()
    response_body = created.json()
    webhook_id = response_body.get("webhookID") if isinstance(response_body, dict) else None
    return f"Created Helius webhook {webhook_id or '(ID not returned)'} for {len(wallets)} wallets"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or update a Helius enhanced transaction webhook."
    )
    parser.add_argument(
        "--webhook-url",
        required=True,
        help="Publicly reachable URL ending in /webhook",
    )
    parser.add_argument(
        "--wallets",
        default=str(WALLETS_PATH),
        help="Path to wallets.txt (defaults to the project directory)",
    )
    arguments = parser.parse_args()

    api_key = os.getenv("HELIUS_API_KEY", "").strip()
    if not api_key:
        logger.error("HELIUS_API_KEY must be configured in the environment or .env")
        return 1
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        logger.error("WEBHOOK_SECRET must be configured in the environment or .env")
        return 1
    try:
        wallets = load_wallets(arguments.wallets)
        result = register_webhook(
            arguments.webhook_url, api_key, wallets, webhook_secret
        )
    except (OSError, ValueError, requests.RequestException):
        logger.exception("Unable to register Helius webhook")
        return 1
    logger.info(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
