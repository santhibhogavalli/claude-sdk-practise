import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient, PermissionResultAllow, PermissionResultDeny, ToolPermissionContext
from dotenv import load_dotenv
from helpers import parse_message, base_options

BASE_IMAGE = os.getenv("BASE_IMAGE", "ubuntu:24.04")
CONTAINER = os.getenv("TARGET_CONTAINER", "target-ubuntu")
APPROVAL_TIMEOUT = 120  # seconds to wait for a y/N answer before denying

PROMPT = """Software list (CSV: sno,name,version,url; version and url may be empty):
{rows}

Target: Docker container "{container}" (ubuntu base image, runs as root).

For each row:
1. Find the latest stable release. If url is set, WebFetch it; otherwise WebSearch "<name> latest stable release".
2. Check the container: docker exec {container} <tool> --version. If it already has the latest version, skip the row.
3. Otherwise install inside the container:
   docker exec {container} bash -c "apt-get update && <install commands> && <tool> --version"

Finish with a short table: name, previous version, installed version, status.
"""


def validate_tools():
    # Check versions directly, not via the agent, so the result doesn't depend on the model
    cmd = "node --version; npm --version; curl --version | head -n 1; python3 --version"
    subprocess.run(["docker", "exec", CONTAINER, "bash", "-c", cmd])


def ensure_container():
    # Pull the base image only if it isn't available locally
    if subprocess.run(["docker", "image", "inspect", BASE_IMAGE], capture_output=True).returncode != 0:
        subprocess.run(["docker", "pull", BASE_IMAGE], check=True)
    else:
        print(f"Base image {BASE_IMAGE} already exists locally.")
    # Reuse the container if it exists, otherwise create it from the base image
    if subprocess.run(["docker", "inspect", CONTAINER], capture_output=True).returncode == 0:
        subprocess.run(["docker", "start", CONTAINER], check=True)
    else:
        subprocess.run(
            ["docker", "run", "-d", "--name", CONTAINER, BASE_IMAGE, "sleep", "infinity"],
            check=True,
        )

async def ask_approval(tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext):
    # Wait up to APPROVAL_TIMEOUT for a y/N answer; silence or anything but y denies
    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(input, f"\nAllow {tool_name} {tool_input}? [y/N] "),
            APPROVAL_TIMEOUT,
        )
    except TimeoutError:
        return PermissionResultDeny(message=f"No approval within {APPROVAL_TIMEOUT}s")
    if reply.strip().lower() in ("y", "yes"):
        return PermissionResultAllow()
    return PermissionResultDeny(message="Denied by user")


async def run(prompt: str):
    options = base_options(
        # WebSearch/WebFetch auto-allowed; every Bash call (docker exec) waits for approval
        allowed_tools=["WebSearch", "WebFetch"],
        can_use_tool=ask_approval,
        max_turns=60,
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            parse_message(message)


def main():
    load_dotenv()
    ensure_container()
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "software.csv")

    prompt = PROMPT.format(rows=src.read_text(encoding="utf-8"), container=CONTAINER)
    asyncio.run(run(prompt))
    validate_tools()


if __name__ == "__main__":
    main()
