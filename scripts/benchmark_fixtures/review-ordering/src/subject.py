"""A small application component with a prescribed review proposal."""


def recent(rows, limit):
    ordered = sorted(rows, key=lambda row: row["at"], reverse=True)
    return ordered[:limit]
