import logging
import sys

class Logger:
    """
    Simple logger class with output on console only
    """
    _log = None  # Class-level logger instance

    @classmethod
    def get_logger(cls, logger_name=None):
        """
        Return the configured logging.Logger object
        """
        if cls._log is None:
            cls._log = logging.getLogger()  # Get root logger if no name
            cls._setup_logger()
            cls._log.set_verbose = cls.set_verbose
            if logger_name:
                return logging.getLogger(logger_name)

        return logging.getLogger(logger_name) if logger_name else cls._log

    @classmethod
    def set_verbose(cls, verbose_level, quiet_level):
        """
        Change verbosity level. Default level is warning.
        """
        cls._log.setLevel(logging.WARNING)

        if quiet_level:
            cls._log.setLevel(logging.ERROR)
            if quiet_level > 1:
                cls._log.setLevel(logging.CRITICAL)

        if verbose_level:
            cls._log.setLevel(logging.INFO)
            if verbose_level > 1:
                cls._log.setLevel(logging.DEBUG)

    @classmethod
    def _setup_logger(cls):
        """
        Create setup instance and make output meaningful :)
        """
        if cls._log.handlers:
            # need only one handler
            return

        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.set_name("console")
        console_formatter = logging.Formatter("%(levelname)s: %(message)s")
        console_handler.setFormatter(console_formatter)
        cls._log.addHandler(console_handler)
        cls._log.setLevel(logging.WARNING)
