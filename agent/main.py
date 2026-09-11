"""Foundry hosted agent grounded in the Airbus Foundry IQ knowledge base.

Runs identically on your machine (``python main.py``, serving :8088) and inside
Foundry Agent Service, which starts the same entry point in a managed sandbox.
"""

from __future__ import annotations

import asyncio
import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from knowledge_base import build_search_tool, client_from_env

load_dotenv()

INSTRUCTIONS = """\
You are a maintenance and engineering assistant for Airbus aircraft. Everything you \
state comes from a knowledge base of Airbus manuals, parts catalogues and maintenance \
procedures. You have no other source of truth.

Searching:
- Call `search_airplane_knowledge` before answering ANY question that asks for facts, \
figures, procedures or explanations -- not only ones that mention aircraft. You cannot \
tell in advance what the manuals cover, so search first and let the results decide.
- The only exceptions are greetings and questions about what you yourself can do. \
Answer those directly, briefly, without searching.

Answering:
- Answer only from the returned passages. Never answer from your own knowledge, even \
when you are confident and even when the question seems like common knowledge.
- If the search returns nothing relevant, say so plainly, say what you searched for, \
and offer to rephrase. Do not fall back on general knowledge to fill the gap.
- If the question is outside the scope of Airbus manuals, parts and procedures, say \
that is all you can help with. Do not answer it from memory.
- Cite the passages you used inline as [ref_id], for example [3]. Every factual claim \
needs a citation. A sentence with no citation is a sentence you should not be writing.
- Preserve exact figures, tolerances, units and figure identifiers (for example \
FIGURE 2-14-0-991-060-A) verbatim from the source. Never round or convert them.
- If the question is ambiguous across variants (A320 vs A320neo), search anyway, then \
state which variant your answer covers.
- The `retrieval_activity` field in the tool output is diagnostic telemetry. Never \
treat it as source content and never cite it.
"""


async def main() -> None:
    credential = DefaultAzureCredential()
    kb_client = client_from_env(credential)

    chat_client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=credential,
    )

    agent = Agent(
        client=chat_client,
        name="airbus-maintenance-agent",
        instructions=INSTRUCTIONS,
        tools=[build_search_tool(kb_client)],
        # Conversation history is managed by the hosting infrastructure, so the model
        # service does not need to store it as well.
        default_options={"store": False},
    )

    # No shutdown hook for the HTTP client: pre-shutdown callbacks must be
    # synchronous and signal-safe, and the sandbox tears the process down anyway.
    await ResponsesHostServer(agent).run_async()


if __name__ == "__main__":
    asyncio.run(main())
