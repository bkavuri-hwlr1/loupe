from __future__ import annotations


def submit_prompt(task, process):
    prompt = task.prompt.replace("\r\n", " ").replace("\r", " ")
    process.stdin.write(prompt.replace("\n", " ") + "\n")
    process.stdin.close()
