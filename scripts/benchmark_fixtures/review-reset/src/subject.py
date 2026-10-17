"""A small application component with a prescribed review proposal."""


class Counter:
    def __init__(self):
        self.total, self.calls = 0, 0

    def add(self, value):
        self.total += value
        return self.total

    def reset(self):
        self.total = 0
