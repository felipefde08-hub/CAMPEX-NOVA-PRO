"""
Vercel FastAPI entry point.

Vercel's FastAPI framework adapter discovers an ``app`` object from this file
and forwards incoming paths to the ASGI router.
"""

from api.index import app

__all__ = ["app"]
