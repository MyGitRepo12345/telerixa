class SendResult:
    def __init__(self, ok, error="", terminal=False):
        self.ok = bool(ok)
        self.error = str(error or "")
        self.terminal = bool(terminal)

    def __bool__(self):
        return self.ok

    @classmethod
    def success(cls):
        return cls(True)

    @classmethod
    def retry(cls, error):
        return cls(False, error, terminal=False)

    @classmethod
    def terminal_failure(cls, error):
        return cls(False, error, terminal=True)

