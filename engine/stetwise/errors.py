class StetwiseError(Exception):
    def __init__(self, reason: str, code: str = "refused", started: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.started = started


class StetwiseRefusal(StetwiseError):
    """The edit cannot be done without risking untouched content."""
