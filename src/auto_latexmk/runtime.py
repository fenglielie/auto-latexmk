import logging

logger = logging.getLogger("auto_latexmk")
logger.addHandler(logging.NullHandler())
logger.propagate = False


class AutoLatexmkError(Exception):
    pass
