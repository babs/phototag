import logging
import sys

import structlog


def setup_logging(*, log_level: str = "INFO", json_logs: bool | None = None) -> None:
    """Configure structlog with TTY detection.

    Why TTY detection: human-readable on a terminal, JSON when piped or run in a job.
    """
    if json_logs is None:
        json_logs = not sys.stderr.isatty()

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
