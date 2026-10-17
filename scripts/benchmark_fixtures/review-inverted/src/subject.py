"""A small application component with a prescribed review proposal."""


def eligible(age, blocked=False):
    return age >= 18 and not blocked
