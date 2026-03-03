"""Async-safe logger setup using QueueHandler/QueueListener."""

import logging
import sys
import queue
from logging.handlers import QueueHandler, QueueListener


def setup_logger(name):
    """Configure and return a logger that won't block the asyncio event loop.

    Uses a QueueHandler to enqueue log records and a QueueListener to process
    them on a separate thread, preventing slow stderr writes from blocking.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        log_queue = queue.Queue(-1)
        queue_handler = QueueHandler(log_queue)
        logger.addHandler(queue_handler)
        logger.propagate = False
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(logging.Formatter("%(message)s"))
        listener = QueueListener(log_queue, stream_handler)
        listener.start()
        logger.setLevel(logging.WARNING)
    return logger


def set_log_verbose(logger, verbose_level, quiet_level):
    """Adjust log level based on verbose/quiet settings."""
    logger.setLevel(logging.WARNING)
    if quiet_level:
        logger.setLevel(logging.ERROR if quiet_level == 1 else logging.CRITICAL)
    if verbose_level:
        logger.setLevel(logging.INFO if verbose_level == 1 else logging.DEBUG)


LOG = setup_logger("linak_mqtt_ctrl")
