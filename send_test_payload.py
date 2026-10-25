import os
import secrets
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"


def _base58_encode(value: bytes) -> str:
    leading_zeros = len(value) - len(value.lstrip(b"\0"))
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    return "1" * leading_zeros + encoded


def _first_wallet() -> str:
    with (PROJECT_DIR / "wallets.txt").open("r", encoding="utf-8") as wallets_file:
        for line in wallets_file:
            wallet = line.strip()
            if wallet and not line.lstrip().startswith("#"):
                return wallet
    raise ValueError(f"No wallet addresses found in {PROJECT_DIR / 'wallets.txt'}")


def _test_payload(wallet: str) -> list[dict[str, object]]:
    token_mint = _base58_encode(secrets.token_bytes(32))
    pool_account = _base58_encode(secrets.token_bytes(32))
    signature = _base58_encode(secrets.token_bytes(64))
    timestamp = int(time.time())

    return [
        {
            "description": "Swap 2 SOL for a made-up test token",
            "type": "SWAP",
            "source": "RAYDIUM",
            "feePayer": wallet,
            "signature": signature,
            "timestamp": timestamp,
            "slot": 1,
            "fee": 5000,
            "nativeTransfers": [
                {
                    "fromUserAccount": wallet,
                    "toUserAccount": pool_account,
                    "amount": 2_000_000_000,
                }
            ],
            "tokenTransfers": [
                {
                    "fromUserAccount": wallet,
                    "toUserAccount": pool_account,
                    "mint": WRAPPED_SOL_MINT,
                    "tokenAmount": 2,
                },
                {
                    "fromUserAccount": pool_account,
                    "toUserAccount": wallet,
                    "mint": token_mint,
                    "tokenAmount": 250_000,
                },
            ],
            "accountData": [
                {
                    "account": wallet,
                    "nativeBalanceChange": -2_000_005_000,
                    "tokenBalanceChanges": [],
                }
            ],
        }
    ]


def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if not webhook_url:
        raise SystemExit("WEBHOOK_URL must be configured in .env")
    if not webhook_secret:
        raise SystemExit("WEBHOOK_SECRET must be configured in .env")

    wallet = _first_wallet()
    response = requests.post(
        webhook_url,
        json=_test_payload(wallet),
        headers={"Authorization": webhook_secret},
        timeout=30,
    )
    print(response.status_code)


if __name__ == "__main__":
    main()
