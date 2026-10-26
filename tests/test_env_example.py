import re
import unittest
from pathlib import Path

SECRETS = ("SECRET_KEY", "ADMIN_PASSWORD_HASH", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_HOST")


class EnvExampleTests(unittest.TestCase):
    def test_env_example_contains_no_real_settings(self):
        """.env.example попадает в Git: настоящие значения должны быть только в .env."""
        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
        for name in SECRETS:
            with self.subTest(name=name):
                match = re.search(rf"^{name}=(.*)$", text, re.MULTILINE)
                self.assertIsNotNone(match, f"{name} пропал из .env.example")
                self.assertEqual(match.group(1).strip(), "",
                                 f"{name} заполнен в .env.example — перенесите значение в .env")


if __name__ == "__main__":
    unittest.main()
