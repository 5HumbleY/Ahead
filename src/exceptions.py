class ProgramFailure(Exception):
    def __init__(self, node, reason):
        self.node = node
        self.reason = str(reason)
        super().__init__(f"{node}: {reason}")


class ProgramRevisionRequired(ProgramFailure):
    pass
