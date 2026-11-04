"""Hosted logs retain severity/component, never raw messages or traceback text."""
import logging


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return f"{record.levelname}:{record.name}"


def configure():
    handler = logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
