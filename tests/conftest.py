import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: tests requiring external API keys")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    has_paper_keys = bool(
        os.environ.get("ALPACA_PAPER_API_KEY") and os.environ.get("ALPACA_PAPER_SECRET_KEY")
    )
    if has_paper_keys:
        return
    skip_integration = pytest.mark.skip(reason="Alpaca paper API keys not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
