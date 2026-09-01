from pathlib import Path
import os
import sys


BACKEND_DIR = Path(__file__).resolve().parent
os.chdir(BACKEND_DIR)
sys.path.insert(0, str(BACKEND_DIR))
if os.environ.get("DEBUG", "").strip().lower() not in {"", "0", "1", "true", "false", "yes", "no", "on", "off"}:
    os.environ["DEBUG"] = "false"

import database as db


def main():
    db.init_db()
    recovered = db.recover_orphaned_assignments()
    print(f"Recovered {recovered} orphaned assignment(s)")


if __name__ == "__main__":
    main()
