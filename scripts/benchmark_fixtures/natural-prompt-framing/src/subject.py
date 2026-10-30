from __future__ import annotations


def submit_prompt(task, process):
    process.stdin.write(task.prompt + "\n")
    process.stdin.close()
