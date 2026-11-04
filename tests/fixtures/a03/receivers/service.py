class Service:
    def work(self):
        return 1

    @staticmethod
    def label():
        return "service"

    @classmethod
    def make(cls):
        return cls()


def helper():
    return "unrelated global helper"
