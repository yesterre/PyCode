from consumer import Consumer as ImportedConsumer
from service import Service

Alias = ImportedConsumer


def main():
    obj = Alias()
    saved = obj
    saved.run()
    Service.label()
    ImportedConsumer.kind()
