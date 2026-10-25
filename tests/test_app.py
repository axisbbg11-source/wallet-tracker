import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import (
    USDC_MINT,
    USDT_MINT,
    WRAPPED_SOL_MINT,
    create_app,
    extract_wallet_buys,
)
from register_webhook import register_webhook


class FakeResponse:
    def __init__(self, body=None):
        self.body = {} if body is None else body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeHeliusSession:
    def __init__(self, existing_webhooks):
        self.existing_webhooks = existing_webhooks
        self.get_calls = []
        self.patch_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return FakeResponse(self.existing_webhooks)

    def patch(self, url, **kwargs):
        self.patch_calls.append((url, kwargs))
        return FakeResponse()

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse({"webhookID": "created-id"})


def make_swap(wallet, signature, token="TokenMint111111111111111111111111111111111"):
    return {
        "type": "SWAP",
        "signature": signature,
        "timestamp": int(time.time()),
        "feePayer": wallet,
        "fee": 5000,
        "nativeTransfers": [
            {"fromUserAccount": wallet, "toUserAccount": "Pool", "amount": 2_000_000_000}
        ],
        "tokenTransfers": [
            {
                "fromUserAccount": wallet,
                "toUserAccount": "Pool",
                "mint": WRAPPED_SOL_MINT,
                "tokenAmount": 2,
            },
            {
                "fromUserAccount": "Pool",
                "toUserAccount": wallet,
                "mint": token,
                "tokenAmount": 150,
            },
        ],
    }


class ExtractWalletBuysTests(unittest.TestCase):
    def test_detects_non_stable_token_bought_with_sufficient_sol(self):
        wallet = "WalletAddress1111111111111111111111111111111"
        buys = extract_wallet_buys(make_swap(wallet, "signature-1"), {wallet}, 1)

        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["wallet"], wallet)
        self.assertEqual(buys[0]["token"], "TokenMint111111111111111111111111111111111")
        self.assertEqual(buys[0]["amount_sol"], 2)
        self.assertEqual(buys[0]["token_amount"], 150)

    def test_ignores_small_buys_and_stablecoin_outputs(self):
        wallet = "WalletAddress1111111111111111111111111111111"
        small_swap = make_swap(wallet, "signature-small")
        small_swap["nativeTransfers"][0]["amount"] = 500_000_000
        small_swap["tokenTransfers"][0]["tokenAmount"] = 0.5
        self.assertEqual(extract_wallet_buys(small_swap, {wallet}, 1), [])

        for stable_mint in (USDC_MINT, USDT_MINT, WRAPPED_SOL_MINT):
            stable_swap = make_swap(wallet, f"signature-{stable_mint}", stable_mint)
            self.assertEqual(extract_wallet_buys(stable_swap, {wallet}, 1), [])


class WebhookEndpointTests(unittest.TestCase):
    def test_persists_buys_and_sends_only_one_cluster_alert(self):
        wallets = [
            "WalletAddress1111111111111111111111111111111",
            "WalletAddress2222222222222222222222222222222",
            "WalletAddress3333333333333333333333333333333",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wallets_path = root / "wallets.txt"
            wallets_path.write_text("\n".join(wallets), encoding="utf-8")
            app = create_app(root / "buys.sqlite", wallets_path)
            client = app.test_client()
            payload = [
                make_swap(wallet, f"signature-{index}")
                for index, wallet in enumerate(wallets)
            ]

            with patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "test-token",
                    "TELEGRAM_CHAT_ID": "123",
                    "WEBHOOK_SECRET": "test-secret",
                },
            ), patch("app.send_telegram_message") as send_message:
                response = client.post(
                    "/webhook",
                    json=payload,
                    headers={"Authorization": "test-secret"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["buys_processed"], 3)
                self.assertEqual(send_message.call_count, 4)
                self.assertIn("CLUSTER ALERT", send_message.call_args_list[-1].args[2])

                replay = client.post(
                    "/webhook",
                    json=payload[-1],
                    headers={"Authorization": "test-secret"},
                )
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(send_message.call_count, 4)

            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json, {"status": "ok"})

    def test_rejects_invalid_json_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wallets_path = root / "wallets.txt"
            wallets_path.write_text("WalletAddress1111111111111111111111111111111\n")
            app = create_app(root / "buys.sqlite", wallets_path)
            with patch.dict("os.environ", {"WEBHOOK_SECRET": "test-secret"}):
                response = app.test_client().post(
                    "/webhook",
                    data="{invalid",
                    content_type="application/json",
                    headers={"Authorization": "test-secret"},
                )
            self.assertEqual(response.status_code, 400)

    def test_requires_matching_authorization_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wallets_path = root / "wallets.txt"
            wallets_path.write_text(
                "WalletAddress1111111111111111111111111111111\n", encoding="utf-8"
            )
            app = create_app(root / "buys.sqlite", wallets_path)
            client = app.test_client()
            with patch.dict("os.environ", {"WEBHOOK_SECRET": "expected-secret"}):
                missing = client.post("/webhook", json={})
                incorrect = client.post(
                    "/webhook", json={}, headers={"Authorization": "wrong-secret"}
                )
                accepted = client.post(
                    "/webhook", json={}, headers={"Authorization": "expected-secret"}
                )
            self.assertEqual(missing.status_code, 401)
            self.assertEqual(incorrect.status_code, 401)
            self.assertEqual(accepted.status_code, 200)


class RegistrationTests(unittest.TestCase):
    def test_updates_matching_webhook_and_creates_when_missing(self):
        webhook_url = "https://example.test/webhook"
        wallets = ["Wallet111", "Wallet222"]
        existing_session = FakeHeliusSession(
            [{"webhookID": "existing-id", "webhookURL": webhook_url}]
        )
        result = register_webhook(
            webhook_url,
            "api-key",
            wallets,
            "shared-secret",
            session=existing_session,
        )
        self.assertIn("Updated", result)
        self.assertEqual(len(existing_session.patch_calls), 1)
        self.assertEqual(
            existing_session.patch_calls[0][1]["json"]["accountAddresses"], wallets
        )
        self.assertEqual(
            existing_session.patch_calls[0][1]["json"]["authHeader"], "shared-secret"
        )

        new_session = FakeHeliusSession([])
        result = register_webhook(
            webhook_url,
            "api-key",
            wallets,
            "shared-secret",
            session=new_session,
        )
        self.assertIn("Created", result)
        self.assertEqual(len(new_session.post_calls), 1)
        self.assertEqual(
            new_session.post_calls[0][1]["json"]["authHeader"], "shared-secret"
        )


if __name__ == "__main__":
    unittest.main()
