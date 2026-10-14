from app.config import get_settings
from app.database import initialize_database

if __name__ == "__main__":
    path = get_settings().database_path
    initialize_database(path)
    print(f"Seeded deterministic retail data at {path}")

