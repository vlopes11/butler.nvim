class ButlerError(Exception):
    """An actionable error that should be displayed without a traceback."""


class ConfigError(ButlerError):
    pass


class Interrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"interrupted by signal {signum}")
