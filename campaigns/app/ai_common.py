class ProviderError(Exception):
    """An AI provider call failed; the message is safe to show to the operator.

    retry=True marks temporary problems (rate limits, outages) worth trying again
    later; anything else (bad key, unknown model, no credit) needs the operator.
    """

    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry
