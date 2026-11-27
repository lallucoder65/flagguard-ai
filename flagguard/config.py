import os

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

# Python names below are package settings. os.getenv() keys stay as deployed.
FLAGSMITH_ENV_KEY = os.getenv("FLAGSMITH_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PROJECT_ID = os.getenv("FLAGSMITH_PROJECT_ID", "54836")
ENVS = {
    "Dev": os.getenv("FLAGSMITH_DEV_ENV_KEY", "BrTukFPrbc6d5jGFEy2K8w"),
    "Prod": os.getenv("FLAGSMITH_PROD_ENV_KEY", "AFikpqKycTH6CA6n5ZVYce"),
}
FS_HEADERS = {
    "Authorization": f"Api-Key {FLAGSMITH_ENV_KEY}",
    "Content-Type": "application/json",
}
console = Console()

FLAG_METHODS = {"is_feature_enabled", "has_feature", "get_feature_value"}
