from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import get_settings
from backend.database.db import initialize_database
from backend.maintenance.local_archive import archive_month


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive one CAMPEX local month.")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--purge", action="store_true", help="Delete archived operational rows after backup.")
    parser.add_argument("--desktop", action="store_true", help="Use the Windows desktop data directory.")
    args = parser.parse_args(argv)

    if args.desktop:
        os.environ["CAMPEX_DESKTOP_MODE"] = "1"

    today = datetime.now()
    year = args.year or (today.year if today.month > 1 else today.year - 1)
    month = args.month or (today.month - 1 if today.month > 1 else 12)

    settings = get_settings()
    initialize_database(settings)
    result = archive_month(settings, year=year, month=month, purge=args.purge)
    print(f"Arquivo mensal gerado: {result.archive_dir}")
    print(f"Relatorio: {result.report_md}")
    print(f"Backup SQLite: {result.database_backup}")
    if result.evidence_zip:
        print(f"Evidencias: {result.evidence_zip}")
    if result.dry_run:
        print("Modo simulacao: nada foi apagado. Use --purge para limpar dados arquivados.")
    else:
        print(f"Linhas apagadas: {result.deleted_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
