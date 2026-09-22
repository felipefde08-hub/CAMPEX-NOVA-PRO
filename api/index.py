"""
Vercel entry point for CAMPEX backend.

Vercel looks for serverless functions in /api directory.
This file exports the FastAPI app for Vercel to discover and run.
"""

from backend.main import app

__all__ = ["app"]
