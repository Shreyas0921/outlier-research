import os

import pytest

# Tests never read your real .env (its notes folder or vault would change the paths they check).
os.environ["OUTLIERS_ENV_FILE"] = os.devnull
for _k in ("OUTLIERS_VAULT", "OUTLIERS_NOTES_DIR", "OUTLIERS_CREATORS_DIR"):
    os.environ.pop(_k, None)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_SLOW") == "1":
        return
    skip = pytest.mark.skip(reason="slow test; run with RUN_SLOW=1")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
