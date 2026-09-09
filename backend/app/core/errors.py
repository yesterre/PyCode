class BackendError(Exception):
    """An application error with a public message, independent of HTTP."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
