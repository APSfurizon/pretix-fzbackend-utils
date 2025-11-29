class FzException(Exception):
    extraData = {}
    code = None

    def __init__(self, message, extraData=None, code=None):
        super().__init__(message)
        if extraData is not None:
            self.extraData = extraData
        self.code = code