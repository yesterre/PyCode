"""Static ingestion fixture; decorators are deliberately not executed."""
from typing import TypeAlias

Record: TypeAlias = dict[str, int]
value = 1


@decorate(
    label="outer",
)
def outer():
    before = 1

    def nested():
        return before

    def nested():
        return 2

    return nested()


def outer():
    def nested():
        return 3
    return nested()


class Container:
    label = "container"

    @classmethod
    async def create(cls):
        return cls()

    class Nested:
        def method(self):
            return "nested"


if value:
    def conditional():
        return value

value += 1
