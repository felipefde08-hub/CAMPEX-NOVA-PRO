from __future__ import annotations

import logging

from backend.cameras.security import sanitize_error_message, sanitize_source_uri

from campex_node.core.config import NodeSettings


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _sanitize(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: _sanitize(value) for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(_sanitize(value) for value in record.args)
        return True


def configure_logging(settings: NodeSettings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    root_logger = logging.getLogger()
    if not any(isinstance(item, SecretRedactionFilter) for item in root_logger.filters):
        root_logger.addFilter(SecretRedactionFilter())
    for handler in root_logger.handlers:
        if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            handler.addFilter(SecretRedactionFilter())


def _sanitize(value):
    if not isinstance(value, str):
        return value
    return sanitize_error_message(sanitize_source_uri(value))
