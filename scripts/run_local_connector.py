"""Run the camera backend on this computer; the frontend may stay on Vercel.

Usage: python scripts/run_local_connector.py
The private API token is read from CAMPEXTOKEN, CAMPEX_API_TOKEN or the
project's .env.
"""
from pathlib import Path
import os
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["CAMPEX_RUNTIME"] = "local"
os.environ.setdefault("CAMPEX_ENV", "development")

from backend.config import get_settings


def main():
    if not get_settings().api_token:
        token = secrets.token_urlsafe(32)
        with (ROOT / ".env").open("a", encoding="utf-8") as env_file:
            env_file.write(f"\nCAMPEX_API_TOKEN={token}\n")
        os.environ["CAMPEX_API_TOKEN"] = token
    print("CAMPEX local: http://127.0.0.1:8000")
    print("Na interface da Vercel: Configurações → Conexão com as câmeras.")
    print("Selecione Neste computador e use CAMPEXTOKEN ou CAMPEX_API_TOKEN do .env local.")
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
