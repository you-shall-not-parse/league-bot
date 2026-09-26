import socket
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession
from league_web.server import running_site


class WebLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_serves_while_running_and_releases_port_on_failure(self):
        port = None
        with self.assertRaisesRegex(RuntimeError, "bot failed"):
            async with running_site(port=0) as runner:
                port = runner.addresses[0][1]
                async with ClientSession() as client:
                    async with client.get(f"http://127.0.0.1:{port}/") as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn("The Allied Front", await response.text())
                raise RuntimeError("bot failed")
        async with running_site(port=port):
            pass  # The service can immediately restart on the same port.

    async def test_bind_failure_cleans_runner_and_does_not_enter_bot_scope(self):
        cleanup = AsyncMock()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with patch("league_web.server.web.AppRunner.cleanup", cleanup):
                with self.assertRaises(OSError):
                    async with running_site(port=occupied.getsockname()[1]):
                        self.fail("Bot scope must not start if HTTP binding failed")
        cleanup.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
