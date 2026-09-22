"""
Vercel entry point for CAMPEX backend.

Vercel looks for serverless functions in /api directory.
This file exports the FastAPI app for Vercel to discover and run.
"""

import os


os.environ.setdefault("CAMPEX_RUNTIME", "serverless")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/campex_serverless.sqlite3")
os.environ.setdefault("VIDEO_UPLOAD_DIR", "/tmp/campex_video_uploads")

from backend.main import app

__all__ = ["app"]
