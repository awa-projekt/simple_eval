from __future__ import annotations

ANSWERS = {
    "What is the capital of France?": "Paris",
    "What is 17 multiplied by 6?": "102",
    "Tom is taller than Anna. Anna is taller than Mark. Who is the tallest?": "Tom",
}


def canned_agent(case, env) -> str:
    return ANSWERS.get(case.prompt, "I do not know.")
