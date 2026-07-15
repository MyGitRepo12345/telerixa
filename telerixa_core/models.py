class SendResult:
    def __init__(self, ok, error="", terminal=False, count_attempt=True):
        self.ok = bool(ok)
        self.error = str(error or "")
        self.terminal = bool(terminal)
        self.count_attempt = bool(count_attempt)

    def __bool__(self):
        return self.ok

    @classmethod
    def success(cls):
        return cls(True, count_attempt=False)

    @classmethod
    def retry(cls, error):
        return cls(False, error, terminal=False, count_attempt=True)

    @classmethod
    def transient_retry(cls, error):
        return cls(False, error, terminal=False, count_attempt=False)

    @classmethod
    def terminal_failure(cls, error):
        return cls(False, error, terminal=True, count_attempt=True)
