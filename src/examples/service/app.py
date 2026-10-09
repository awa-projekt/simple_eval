from __future__ import annotations

import os

from fastapi import FastAPI
from pydantic import BaseModel
from pydantic_ai import Agent

agent = Agent(
    model=os.getenv("SIMPLE_EVAL_EXAMPLE_MODEL", "openai:gpt-5-mini"),
    system_prompt=(
        "You are a concise and accurate general-purpose assistant. "
        "Answer the user's prompt directly and do not add extra commentary."
    ),
)
app = FastAPI()


class ChatRequest(BaseModel):
    prompt: str
    category: str | None = None


class ChatResponse(BaseModel):
    answer: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
async def chat(request: ChatRequest) -> ChatResponse:
    response = await agent.run(request.prompt)
    return ChatResponse(answer=str(response.output))
