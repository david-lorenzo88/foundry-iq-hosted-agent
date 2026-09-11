"""Deploy agent/ to Foundry Agent Service as a hosted agent.

Source-code deployment: the agent folder is zipped and uploaded, and Foundry resolves
the dependencies and builds the image. No Docker and no container registry involved.

Run from the repo root:  python deploy/deploy_agent.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentEndpointConfig,
    CodeConfiguration,
    CodeDependencyResolution,
    FixedRatioVersionSelectionRule,
    HostedAgentDefinition,
    ProtocolConfiguration,
    ProtocolVersionRecord,
    ResponsesProtocolConfiguration,
    VersionSelector,
)
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AGENT_DIR = ROOT / "agent"
# Never ship local-only files. .env in particular would bake developer settings into
# the deployed image; the hosted agent gets its configuration from environment_variables.
EXCLUDE_NAMES = {".env", ".DS_Store"}
EXCLUDE_DIRS = {"__pycache__", ".venv", ".pytest_cache"}

RUNTIME = os.getenv("AGENT_RUNTIME", "python_3_13")
CPU = os.getenv("AGENT_CPU", "0.5")
MEMORY = os.getenv("AGENT_MEMORY", "1Gi")
# The Responses protocol contract version. Newer projects expect 2.0.0; older ones
# only accept 1.0.0, so the deploy retries once on a rejection.
PROTOCOL_VERSIONS = [v for v in os.getenv("RESPONSES_PROTOCOL_VERSIONS", "2.0.0,1.0.0").split(",") if v]

SMOKE_TEST = "What is the maximum load value to be applied on the NLG jacking point for the A320neo?"


def log(message: str) -> None:
    print(message, flush=True)


def build_zip(destination: Path) -> str:
    """Zip agent/ flat at the root and return the archive's SHA-256.

    A wrapper folder inside the zip is the documented cause of ModuleNotFoundError at
    startup, so every path is stored relative to agent/. The archive is written to a
    real file because the upload is multipart and the service requires the part to
    carry a filename ending in .zip.
    """
    buffer = io.BytesIO()
    included: list[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(AGENT_DIR.rglob("*")):
            if not path.is_file():
                continue
            if path.name in EXCLUDE_NAMES or any(part in EXCLUDE_DIRS for part in path.parts):
                continue
            relative = path.relative_to(AGENT_DIR)
            archive.write(path, relative)
            included.append(str(relative))

    data = buffer.getvalue()
    log(f"  packaged {len(included)} files ({len(data) / 1024:.1f} KB): {', '.join(included)}")
    if "main.py" not in included:
        raise SystemExit("agent/main.py is missing -- the entry point must be at the zip root.")
    destination.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def definition(protocol_version: str, environment: dict[str, str]) -> HostedAgentDefinition:
    return HostedAgentDefinition(
        cpu=CPU,
        memory=MEMORY,
        code_configuration=CodeConfiguration(
            runtime=RUNTIME,
            entry_point=["python", "main.py"],
            dependency_resolution=CodeDependencyResolution.REMOTE_BUILD,
        ),
        protocol_versions=[ProtocolVersionRecord(protocol="responses", version=protocol_version)],
        environment_variables=environment,
    )


def wait_for_active(client: AIProjectClient, agent_name: str, version: str) -> None:
    log(f"  waiting for version {version} to provision (remote build, usually 2-5 min)…")
    deadline = time.monotonic() + 20 * 60
    last = ""
    while time.monotonic() < deadline:
        details = client.agents.get_version(agent_name=agent_name, agent_version=version)
        status = details["status"]
        if status != last:
            log(f"    status: {status}")
            last = status
        if status == "active":
            return
        if status == "failed":
            error = details.get("error") or {}
            raise SystemExit(
                f"\nProvisioning failed: {error.get('code')}\n{error.get('message')}\n"
                "For a remote build the message usually ends with the failing pip line."
            )
        time.sleep(10)
    raise SystemExit("Timed out waiting for the agent version to become active.")


def agent_principal_id(client: AIProjectClient, agent_name: str, version: str) -> str | None:
    """The Entra identity Foundry created for this agent, which retrieval runs as."""
    details = client.agents.get_version(agent_name=agent_name, agent_version=version)
    identity = details.get("instance_identity") or {}
    return identity.get("principal_id")


def grant_search_access(principal_id: str, scope: str) -> None:
    """Give the agent identity read access to the knowledge base.

    The identity only exists once the agent is deployed, so this can't be done up
    front -- on a first deploy the smoke test may run before the assignment has
    propagated.
    """
    role = "Search Index Data Reader"
    existing = subprocess.run(
        ["az", "role", "assignment", "list", "--assignee", principal_id, "--scope", scope,
         "--query", f"[?roleDefinitionName=='{role}'] | length(@)", "-o", "tsv"],
        capture_output=True, text=True,
    )
    if existing.returncode == 0 and existing.stdout.strip() not in ("", "0"):
        log(f"  '{role}' already assigned to {principal_id}")
        return

    log(f"  assigning '{role}' to the agent identity {principal_id}…")
    result = subprocess.run(
        ["az", "role", "assignment", "create", "--assignee-object-id", principal_id,
         "--assignee-principal-type", "ServicePrincipal", "--role", role, "--scope", scope],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(
            "\n  Could not create the role assignment (this needs "
            "Microsoft.Authorization/roleAssignments/write on the search service).\n"
            f"  Azure said: {result.stderr.strip()[:300]}\n\n"
            "  Ask someone with Owner or User Access Administrator to run:\n\n"
            f"    az role assignment create --assignee-object-id {principal_id} \\\n"
            f"      --assignee-principal-type ServicePrincipal \\\n"
            f'      --role "{role}" \\\n'
            f"      --scope {scope}\n\n"
            "  Until then the agent will answer, but every retrieval will return 403."
        )
        return
    log("  role assigned. Allow up to a minute for it to propagate.")


def route_endpoint(client: AIProjectClient, agent_name: str, version: str) -> None:
    log(f"  routing 100% of the agent endpoint to version {version}…")
    client.agents.update_details(
        agent_name=agent_name,
        agent_endpoint=AgentEndpointConfig(
            version_selector=VersionSelector(
                version_selection_rules=[
                    FixedRatioVersionSelectionRule(agent_version=version, traffic_percentage=100)
                ]
            ),
            protocol_configuration=ProtocolConfiguration(responses=ResponsesProtocolConfiguration()),
        ),
    )


def main() -> None:
    try:
        project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
        search_endpoint = os.environ["SEARCH_ENDPOINT"]
        search_scope = os.environ["SEARCH_RESOURCE_ID"]
        kb_name = os.environ["KB_NAME"]
    except KeyError as missing:
        raise SystemExit(f"Missing {missing} -- copy .env.example to .env and fill it in.")

    agent_name = os.getenv("FOUNDRY_HOSTED_AGENT_NAME", "airbus-maintenance-agent")
    model = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4")

    environment = {
        "FOUNDRY_PROJECT_ENDPOINT": project_endpoint,
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": model,
        "SEARCH_ENDPOINT": search_endpoint,
        "KB_NAME": kb_name,
        "KB_API_VERSION": os.getenv("KB_API_VERSION", "2026-08-01-preview"),
        "KB_MAX_PASSAGES": os.getenv("KB_MAX_PASSAGES", "12"),
        "KB_MAX_PASSAGE_CHARS": os.getenv("KB_MAX_PASSAGE_CHARS", "1800"),
        "KB_TIMEOUT_SECONDS": os.getenv("KB_TIMEOUT_SECONDS", "180"),
        # Agent Framework emits OpenTelemetry spans that Foundry forwards to the
        # linked Application Insights resource, if the project has one.
        "ENABLE_INSTRUMENTATION": "true",
    }

    log(f"Deploying '{agent_name}' to {project_endpoint}")
    log(f"  model={model}  runtime={RUNTIME}  cpu={CPU}  memory={MEMORY}")
    log(f"  knowledge base={kb_name} on {search_endpoint}\n")

    log("Packaging agent/…")
    zip_path = Path(tempfile.gettempdir()) / f"{agent_name}.zip"
    sha256 = build_zip(zip_path)
    log(f"  sha256={sha256[:16]}…  ->  {zip_path}\n")

    with DefaultAzureCredential() as credential, AIProjectClient(
        endpoint=project_endpoint, credential=credential
    ) as client:
        created = None
        errors: list[str] = []
        for protocol_version in PROTOCOL_VERSIONS:
            log(f"Creating version (responses protocol {protocol_version})…")
            try:
                with zip_path.open("rb") as code:
                    created = client.agents.create_version_from_code(
                        agent_name=agent_name,
                        definition=definition(protocol_version, environment),
                        code=code,
                        code_zip_sha256=sha256,
                        description="Airbus maintenance agent grounded in the Foundry IQ knowledge base.",
                    )
                break
            except Exception as exc:  # noqa: BLE001 - report every attempt, then give up
                errors.append(f"{protocol_version}: {exc}")
                log(f"  rejected: {str(exc)[:200]}")
        if created is None:
            raise SystemExit("Could not create an agent version.\n  " + "\n  ".join(errors))

        version = created.version
        log(f"  created version {version}\n")

        wait_for_active(client, agent_name, version)
        log("  version is active\n")

        log("Granting the agent identity access to the knowledge base…")
        principal_id = agent_principal_id(client, agent_name, version)
        if principal_id:
            grant_search_access(principal_id, search_scope)
        else:
            log("  no instance identity reported yet; assign 'Search Index Data Reader' manually.")
        print()

        route_endpoint(client, agent_name, version)
        log("  endpoint routed\n")

        log("Smoke test…")
        log(f"  > {SMOKE_TEST}")
        try:
            with client.get_openai_client(agent_name=agent_name) as openai_client:
                response = openai_client.responses.create(input=SMOKE_TEST)
            log(f"\n  {response.output_text[:800]}\n")
        except Exception as exc:  # noqa: BLE001
            log(
                f"\n  Smoke test failed: {exc}\n"
                "  A 403 from Azure AI Search here usually means the role assignment above\n"
                "  hasn't propagated yet. Wait a minute and re-run the smoke test.\n"
            )

        log("Done.")
        log(f"  Agent:    {agent_name} v{version}")
        log(f"  Endpoint: {project_endpoint}/agents/{agent_name}/endpoint/protocols/openai/responses")
        log("  Point the UI at it by removing LOCAL_AGENT_URL from your environment.")


if __name__ == "__main__":
    sys.exit(main())
