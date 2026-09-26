import os
from typing import Final

# Resolve a shared override before any cog or fixture-store path is cached.
# Process environment takes precedence over the repository's .env file.
_env_file = os.path.join(os.path.dirname(__file__), ".env")
if os.path.isfile(_env_file):
    from dotenv import load_dotenv
    load_dotenv(_env_file)


def data_path(filename: str) -> str:
    """Return an absolute path under the repo's ./data folder.

    Ensures the folder exists so callers can read/write safely.
    """

    base_dir: Final[str] = os.path.dirname(__file__)
    data_dir: Final[str] = os.path.abspath(os.path.expanduser(os.environ.get("LEAGUE_DATA_DIR") or os.path.join(base_dir, "data")))
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, filename)
