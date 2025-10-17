class FzException(Exception):
    extraData = {}
    def __init__(self, message, extraData=None):
        super().__init__(message)
        if extraData is not None:
            self.extraData = extraData