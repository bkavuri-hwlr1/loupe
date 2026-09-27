from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from rich.table import Table
from rich.text import Text

from llm_cli.cli.terminal import TerminalUI, safe_text


def emit(
    value: Any, *, as_json: bool, stream: TextIO | None = None, plain: bool = False
) -> None:
    stream = stream if stream is not None else sys.stdout
    if as_json:
        json.dump(
            value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        stream.write("\n")
        return
    ui = TerminalUI(stream, plain=plain)
    if ui.plain:
        _human(value, stream)
    else:
        _styled(value, ui)
    stream.flush()


def _styled(value: Any, ui: TerminalUI) -> None:
    if isinstance(value, list):
        if not value:
            ui.notice("No records yet.")
        elif all(isinstance(item, dict) for item in value):
            preferred = [
                "task_id",
                "session_id",
                "repository_id",
                "title",
                "state",
                "provider",
                "model",
                "workspace_mode",
                "path",
            ]
            columns = [key for key in preferred if any(key in row for row in value)]
            if not columns:
                columns = list(dict.fromkeys(key for row in value for key in row))
            table = Table(box=None, padding=(0, 2), expand=False)
            for column in columns:
                table.add_column(
                    column.replace("_", " ").title(),
                    style="brand" if column.endswith("_id") else None,
                )
            for row in value:
                table.add_row(
                    *(Text(_display(row.get(column, ""))) for column in columns)
                )
            ui.console.print(table)
        else:
            for item in value:
                _styled(item, ui)
    elif isinstance(value, dict):
        table = Table.grid(padding=(0, 2))
        table.add_column(style="muted")
        table.add_column(overflow="fold")
        for key, item in value.items():
            table.add_row(safe_text(key).replace("_", " "), Text(_display(item)))
        ui.console.print(table)
    else:
        ui.notice(_display(value))


def _display(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return safe_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        )
    return safe_text(value)


def _human(value: Any, stream: TextIO) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                body = safe_text(json.dumps(item, ensure_ascii=False, sort_keys=True))
                stream.write(f"{safe_text(key)}: {body}\n")
            else:
                stream.write(f"{safe_text(key)}: {safe_text(item)}\n")
    elif isinstance(value, list):
        if not value:
            stream.write("No records yet.\n")
        for item in value:
            if isinstance(item, dict):
                stream.write(
                    safe_text(json.dumps(item, ensure_ascii=False, sort_keys=True))
                )
                stream.write("\n")
            else:
                stream.write(f"{safe_text(item)}\n")
    else:
        stream.write(f"{safe_text(value)}\n")


__all__ = ["emit"]
