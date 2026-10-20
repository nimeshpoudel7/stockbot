"""Test package setup: hermetic environment.

config.py calls load_dotenv() at import, which never overrides variables
that are already set. Pinning dummy values here (before any test imports
config) means the suite behaves the same on every machine — with or
without a real .env — and can never reach a live service with real keys
or push to the real ntfy topic.
"""
import os

for _k, _v in {
    "ALPACA_KEY": "test-alpaca-key",
    "ALPACA_SECRET": "test-alpaca-secret",
    "FINNHUB_KEY": "test-finnhub-key",
    "MARKETAUX_API_TOKEN": "",
    "NTFY_TOPIC": "",
    "DASHBOARD_TOKEN": "",
}.items():
    os.environ[_k] = _v

# No real network from the test suite: any request a test didn't mock fails
# fast as a connection error (the code under test fails open on those).
import requests  # noqa: E402
import requests.adapters  # noqa: E402


def _no_network(self, request, *args, **kwargs):
    raise requests.exceptions.ConnectionError(
        f"network disabled in tests: {request.url.split('?')[0]}")


requests.adapters.HTTPAdapter.send = _no_network
