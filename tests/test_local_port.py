from __future__ import annotations

import unittest

from utils.local_port import (
    LocalPortsUnavailableError,
    resolve_server_port,
)


class LocalPortSelectionTests(unittest.TestCase):
    def test_local_default_uses_5002_when_available(self) -> None:
        selected = resolve_server_port(
            host="127.0.0.1",
            requested_port=5002,
            stage="local",
            port_available=lambda _host, port: port == 5002,
        )

        self.assertEqual(selected, 5002)

    def test_local_default_falls_back_to_5003(self) -> None:
        selected = resolve_server_port(
            host="127.0.0.1",
            requested_port=5002,
            stage="local",
            port_available=lambda _host, port: port == 5003,
        )

        self.assertEqual(selected, 5003)

    def test_local_start_fails_when_both_supported_ports_are_busy(self) -> None:
        with self.assertRaisesRegex(
            LocalPortsUnavailableError,
            "5002 and 5003 are both in use",
        ):
            resolve_server_port(
                host="127.0.0.1",
                requested_port=5002,
                stage="local",
                port_available=lambda _host, _port: False,
            )

    def test_explicit_and_non_local_ports_are_unchanged(self) -> None:
        unavailable = lambda _host, _port: False

        self.assertEqual(
            resolve_server_port(
                host="127.0.0.1",
                requested_port=5010,
                stage="local",
                port_available=unavailable,
            ),
            5010,
        )
        self.assertEqual(
            resolve_server_port(
                host="0.0.0.0",
                requested_port=5002,
                stage="beta",
                port_available=unavailable,
            ),
            5002,
        )


if __name__ == "__main__":
    unittest.main()
