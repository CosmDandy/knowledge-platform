import asyncio
import tempfile
import unittest
from pathlib import Path

from knowledge import auth

TOKEN = "t" * 64


async def inner(scope, receive, send):
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    else:
        await send({"type": "lifespan.reached"})


def call(headers, scope_type="http"):
    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": scope_type, "headers": headers}
    asyncio.run(auth.BearerAuth(inner, TOKEN)(scope, None, send))
    return sent


def bearer(value):
    return [(b"authorization", value.encode())]


class Gate(unittest.TestCase):
    def test_right_token_passes(self):
        self.assertEqual(call(bearer(f"Bearer {TOKEN}"))[0]["status"], 200)
        self.assertEqual(call(bearer(f"bearer {TOKEN}"))[0]["status"], 200)  # scheme is case-insensitive

    def test_everything_else_is_401(self):
        for headers in ([], bearer("Bearer wrong"), bearer(TOKEN), bearer(f"Basic {TOKEN}"),
                        bearer(f"Bearer {TOKEN}x"), bearer("Bearer "), bearer("Bearer")):
            sent = call(headers)
            self.assertEqual(sent[0]["status"], 401, headers)
            self.assertIn((b"www-authenticate", b"Bearer"), sent[0]["headers"])
            self.assertNotIn(TOKEN.encode(), sent[1]["body"])

    def test_lifespan_is_not_gated(self):
        self.assertEqual(call([], scope_type="lifespan")[0]["type"], "lifespan.reached")


class TokenFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "token"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_and_strips(self):
        self.path.write_text(TOKEN + "\n")
        self.assertEqual(auth.load_token(self.path), TOKEN)

    def test_missing_or_short_token_stops_the_server(self):
        with self.assertRaises(SystemExit):
            auth.load_token(self.path)
        self.path.write_text("short\n")
        with self.assertRaises(SystemExit):
            auth.load_token(self.path)


if __name__ == "__main__":
    unittest.main()
