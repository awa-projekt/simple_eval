from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic_ai import Agent

load_dotenv()

MODEL_NAME = os.getenv("SIMPLE_EVAL_EXAMPLE_MODEL", "openai:gpt-5-mini")
SYSTEM_PROMPT = (
    "You are a concise and accurate general-purpose assistant. "
    "Answer the user's prompt directly and do not add extra commentary."
)
agent = Agent(model=MODEL_NAME, system_prompt=SYSTEM_PROMPT)


async def general_llm_agent(case, env) -> str:
    response = await agent.run(case.prompt)
    return str(response.output)
