from requests import Session


class SingleRequestSession(Session):
    """Reject SDK retries/redirects before a second HTTP send."""
    def __init__(self):
        super().__init__()
        self.attempts = 0

    def send(self, request, **kwargs):
        if self.attempts:
            raise RuntimeError("本次 Embedding 的一次 HTTP 尝试已用完，禁止自动重发")
        self.attempts += 1
        return super().send(request, **kwargs)
