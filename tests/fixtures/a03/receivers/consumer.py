from service import Service


class Consumer:
    def __init__(self):
        self.service = Service()

    def run(self):
        self.service.work()
        self.helper()

    def helper(self):
        return 2

    @classmethod
    def kind(cls):
        return cls.code()

    @staticmethod
    def code():
        return "consumer"
