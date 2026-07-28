"""Gate #3 — the reflected XSS at connectors/vercel.py:179,183.

docs §9 lists this as a gate needing a live server. It does not: `/callback` is
an unauthenticated GET with no dependencies, so `httpx.ASGITransport` reaches it
in a millisecond. Reclassified from manual to automated.

This is the P0 in the changeset and the cheapest test in the suite. Write it
first.

STATUS: these tests FAIL, deliberately and loudly. The bug is live — an
unauthenticated reflected XSS reachable at `GET /callback` right now.

They are NOT marked xfail. That was tried and reverted on purpose: xfail is the
right tool for "known-broken, scheduled, tracked", and it is the wrong tool for
a live vulnerability, because it converts a standing alarm into a quiet line of
expected-failure output that nobody reads. A green suite that is green by
agreement is worth less than a red one that is telling the truth.

The cost is real and accepted: `venv/bin/pytest` exits non-zero on this branch
until vercel.py is fixed, so it cannot be used as a bare pre-commit gate. Gate
on the rest with `-k "not security_callback"` if you need to, and let the
failure keep showing.

The fix is two calls — `html.escape()` for the body, `json.dumps()` for the JS
literal — and then this file goes green on its own with no edit here. Do not
"fix" it by weakening the assertions or re-adding a marker.
"""
from __future__ import annotations

import html
import json

import pytest

PAYLOADS = [
    "<img src=x onerror=alert(1)>",
    "</script><script>alert(1)</script>",
    "'; alert(1); //",
    '" onmouseover="alert(1)',
    " alert(1)",          # JS line separator — breaks a JS string literal
]


@pytest.mark.parametrize("payload", PAYLOADS)
async def test_callback_escapes_error_description(client, payload):
    r = await client.get(
        "/callback", params={"error": "access_denied", "error_description": payload}
    )
    assert r.status_code == 400   # the error branch renders 400 + HTML
    body = r.text

    # The raw payload must never appear verbatim in the HTML body.
    assert payload not in body, (
        f"unescaped reflection of {payload!r} — see connectors/vercel.py:179"
    )
    # ...but the *escaped* form should, i.e. we escaped rather than dropped it.
    assert html.escape(payload) in body or "access_denied" in body


@pytest.mark.parametrize("payload", ["</script><script>alert(1)</script>"])
async def test_callback_js_literal_is_json_encoded(client, payload):
    """`repr()` is not JS escaping: `repr("</script>")` is `'</script>'`, which
    closes the enclosing <script> tag. Only `json.dumps` is safe here
    (connectors/vercel.py:183)."""
    r = await client.get(
        "/callback", params={"error": "x", "error_description": payload}
    )
    script = r.text.split("<script", 1)[-1]
    assert "</script><script>" not in script.replace("<\\/script>", ""), (
        "payload broke out of the JS string literal — use json.dumps(), not repr()"
    )


async def test_callback_has_no_auth_dependency_by_design_is_documented(app):
    """Not a security assertion — a tripwire. If someone adds an auth dependency
    to /callback the OAuth flow silently breaks, and if someone adds a NEW
    unauthenticated reflecting route this is where they'll be reminded to
    escape it. Keep the list short."""
    unauth_html_routes = {"/callback"}
    paths = set(app.openapi()["paths"])
    assert unauth_html_routes <= paths
