from functools import lru_cache
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    def __init__(self) -> None:
        self.database_path = Path(os.getenv("DATABASE_PATH", "data/retail.sqlite3"))
        self.gemini_api_key1 = os.getenv("GEMINI_API_KEY1", "").strip()
        self.gemini_api_key2 = os.getenv("GEMINI_API_KEY2", "").strip()
        self.gemini_legacy_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        candidates = [self.gemini_api_key1, self.gemini_api_key2, self.gemini_legacy_api_key]
        self.gemini_api_keys = list(dict.fromkeys(key for key in candidates if key))
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.nvidia_api_key = os.getenv("NVIDIA_API_KEY", "").strip()
        self.nvidia_model = os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b").strip()
        self.max_rows = int(os.getenv("MAX_RESULT_ROWS", "200"))
        self.query_timeout_ms = int(os.getenv("QUERY_TIMEOUT_MS", "2000"))
        self.max_upload_bytes = max(1, min(int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024))), 100 * 1024 * 1024))
        self.max_databases_per_session = max(1, min(int(os.getenv("MAX_DATABASES_PER_SESSION", "5")), 20))
        self.max_import_rows = max(1, min(int(os.getenv("MAX_IMPORT_ROWS", "100000")), 1_000_000))
        self.max_import_sheets = max(1, min(int(os.getenv("MAX_IMPORT_SHEETS", "20")), 100))
        self.max_import_cells = max(1, min(int(os.getenv("MAX_IMPORT_CELLS", "2000000")), 10_000_000))


@lru_cache
def get_settings() -> Settings:
    return Settings()

