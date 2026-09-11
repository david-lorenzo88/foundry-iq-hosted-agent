"""Backend configuration, read once at import."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Settings:
    project_endpoint: str
    agent_name: str
    model: str
    # When set, the backend talks to a locally running `python agent/main.py` instead
    # of the deployed hosted agent. Same Responses protocol either way, so the trace
    # mapping and the UI are identical -- only the transport and auth differ.
    local_agent_url: str | None

    @property
    def is_local(self) -> bool:
        return bool(self.local_agent_url)


settings = Settings(
    project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    agent_name=os.getenv("FOUNDRY_HOSTED_AGENT_NAME", "airbus-maintenance-agent"),
    model=os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4"),
    local_agent_url=os.getenv("LOCAL_AGENT_URL") or None,
)
