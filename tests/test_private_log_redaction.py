import importlib.util
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "send_private_log.py"
spec = importlib.util.spec_from_file_location("send_private_log", SCRIPT_PATH)
send_private_log = importlib.util.module_from_spec(spec)
spec.loader.exec_module(send_private_log)


class PrivateLogRedactionTests(unittest.TestCase):
    def test_exact_short_and_long_configured_secrets_are_redacted(self):
        env = {
            "CSFLOAT_API_KEY": "short7",
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123456789012345678/webhook-token-value",
            "DISCORD_LOG_WEBHOOK_URL": "https://canary.discord.com/api/webhooks/223456789012345678/log-token-value",
        }
        text = send_private_log.redact_secrets(
            "key short7 webhook https://discord.com/api/webhooks/123456789012345678/webhook-token-value "
            "log https://canary.discord.com/api/webhooks/223456789012345678/log-token-value",
            env,
        )
        self.assertNotIn("short7", text)
        self.assertNotIn("webhook-token-value", text)
        self.assertNotIn("log-token-value", text)
        self.assertIn("[REDACTED]", text)

    def test_webhook_paths_without_environment_or_scheme_are_redacted(self):
        text = send_private_log.redact_secrets(
            "a https://discordapp.com/api/webhooks/123456789012345678/Token.Value_-\n"
            "b /api/webhooks/123456789012345678/Token.Value_-",
            {},
        )
        self.assertNotIn("Token.Value_-", text)
        self.assertIn("[REDACTED_DISCORD_WEBHOOK]", text)
        self.assertIn("/api/webhooks/[REDACTED]", text)

    def test_missing_empty_unicode_and_multiline_values_are_safe(self):
        self.assertEqual(send_private_log.redact_secrets("", {}), "")
        self.assertEqual(
            send_private_log.redact_secrets("plain é scan\nnext", {}),
            "plain é scan\nnext",
        )
        text = send_private_log.redact_secrets(
            "before secret-é-value after",
            {"CSFLOAT_API_KEY": "secret-é-value"},
        )
        self.assertEqual(text, "before [REDACTED] after")

    def test_exact_configured_secret_is_redacted_without_trimming(self):
        for secret in ("   ", "  abc  ", "\tsecret-é\n"):
            text = send_private_log.redact_secrets(
                f"before|{secret}|after",
                {"CSFLOAT_API_KEY": secret},
            )
            self.assertNotIn(secret, text)
            self.assertEqual(text, "before|[REDACTED]|after")

    def test_malformed_webhook_secret_does_not_break_redaction(self):
        text = send_private_log.redact_secrets(
            "malformed http://[ remains redacted",
            {"DISCORD_WEBHOOK_URL": "http://["},
        )
        self.assertEqual(text, "malformed [REDACTED] remains redacted")


if __name__ == "__main__":
    unittest.main()
