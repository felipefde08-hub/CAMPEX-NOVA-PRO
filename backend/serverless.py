"""
Serverless-safe filesystem utilities.

Detects runtime mode and provides safe operations that fail gracefully
in serverless environments where filesystem is ephemeral.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from backend.config import Settings


logger = logging.getLogger("campex.serverless")


def is_serverless_runtime(settings: Settings) -> bool:
    """Check if running in serverless mode."""
    return settings.runtime == "serverless"


def ensure_directory_exists(
    directory: Path,
    settings: Settings,
    allow_creation: bool = True,
) -> bool:
    """
    Ensure a directory exists, handling serverless gracefully.

    Args:
        directory: Path to ensure
        settings: Settings instance
        allow_creation: Whether to allow creating the directory

    Returns:
        True if directory exists or was created, False if operation failed gracefully

    Raises:
        RuntimeError: If not in serverless mode and directory creation fails
    """
    try:
        if directory.exists():
            return True

        if not allow_creation:
            if is_serverless_runtime(settings):
                logger.warning(
                    f"[serverless] Directory does not exist: {directory}",
                    extra={"runtime": settings.runtime},
                )
                return False
            raise FileNotFoundError(f"Directory does not exist: {directory}")

        directory.mkdir(parents=True, exist_ok=True)
        return True
    except Exception as exc:
        if is_serverless_runtime(settings):
            logger.warning(
                f"[serverless] Failed to create directory (non-fatal): {directory}",
                extra={"error": str(exc), "runtime": settings.runtime},
            )
            return False
        logger.error(f"Failed to create directory: {directory}", exc_info=True)
        raise


def write_file_safe(
    path: Path,
    content: bytes | str,
    settings: Settings,
) -> bool:
    """
    Write file safely, handling serverless gracefully.

    Args:
        path: File path to write
        content: Content to write
        settings: Settings instance

    Returns:
        True if write succeeded, False if operation failed gracefully in serverless

    Raises:
        Exception: If not in serverless mode and write fails
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content)
        else:
            path.write_bytes(content)
        return True
    except Exception as exc:
        if is_serverless_runtime(settings):
            logger.warning(
                f"[serverless] Failed to write file (non-fatal): {path}",
                extra={"error": str(exc), "runtime": settings.runtime},
            )
            return False
        logger.error(f"Failed to write file: {path}", exc_info=True)
        raise


def read_file_safe(
    path: Path,
    settings: Settings,
    binary: bool = False,
) -> Optional[bytes | str]:
    """
    Read file safely, handling serverless gracefully.

    Args:
        path: File path to read
        settings: Settings instance
        binary: Whether to read as binary

    Returns:
        File content or None if operation failed gracefully in serverless

    Raises:
        FileNotFoundError: If file does not exist and not in serverless mode
    """
    try:
        if not path.exists():
            if is_serverless_runtime(settings):
                logger.debug(f"[serverless] File not found: {path}")
                return None
            raise FileNotFoundError(f"File not found: {path}")
        if binary:
            return path.read_bytes()
        return path.read_text()
    except Exception as exc:
        if is_serverless_runtime(settings):
            logger.warning(
                f"[serverless] Failed to read file (non-fatal): {path}",
                extra={"error": str(exc), "runtime": settings.runtime},
            )
            return None
        logger.error(f"Failed to read file: {path}", exc_info=True)
        raise
