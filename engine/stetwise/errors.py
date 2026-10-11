class StetwiseError(Exception):
    def __init__(self, reason: str, code: str = "refused"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class StetwiseRefusal(StetwiseError):
    """The edit cannot be done without risking untouched content."""
