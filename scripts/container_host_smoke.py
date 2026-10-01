"""Qualify the real container host: non-root auth, load, durable jobs and restart."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx

from energy_agent_tools.hosting import token_digest

ARGS = {
    "indoor_temp_c": 21,
    "outdoor_temp_c": 2,
    "components": [{"name": "wall", "area_m2": 100, "u_value_w_m2k": 0.2}],
    "air_changes_per_hour": 0.4,
}


def docker(*args: str) -> str:
    completed = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if completed.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {completed.stderr[-1000:]}")
    return completed.stdout.strip()


async def wait_ready(client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            response = await client.post("/sessions", json={"site_id": "home"})
            if response.status_code == 401:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("Container did not reach authenticated API readiness")


async def qualify(image: str, config: Path, name: str, volume: str) -> dict:
    token = secrets.token_urlsafe(32)
    config.write_text(
        json.dumps(
            {
                "sites": [{"id": "home", "user_id": "operator", "name": "Home", "timezone": "UTC"}],
                "hosting": {
                    "max_requests_per_minute": 200,
                    "principals": [
                        {
                            "user_id": "operator",
                            "allowed_site_ids": ["home"],
                            "token_digest": token_digest(token),
                        }
                    ],
                },
            }
        )
    )
    config.chmod(0o644)  # Contains only a token digest; container UID must read it.
    docker("volume", "create", volume)
    docker(
        "run",
        "--detach",
        "--name",
        name,
        "--read-only",
        "--memory",
        "768m",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--publish",
        "127.0.0.1::8765",
        "--mount",
        f"type=volume,source={volume},target=/var/lib/energy-agent",
        "--mount",
        f"type=bind,source={config},target=/etc/energy-agent/config.json,readonly",
        image,
    )
    if docker("exec", name, "id", "-u") != "10001":
        raise RuntimeError("Container does not run as the intended unprivileged user")
    port = docker("port", name, "8765/tcp").split(":")[-1]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
        await wait_ready(client)
        headers = {"Authorization": f"Bearer {token}"}
        session_response = await client.post("/sessions", json={"site_id": "home"}, headers=headers)
        session_response.raise_for_status()
        session = session_response.json()["session_id"]
        endpoint = f"/sessions/{session}"
        artifact_response = await client.post(
            endpoint + "/execute",
            headers=headers,
            json={
                "tool": "engineering.calculate_heat_loss",
                "arguments": ARGS,
                "persist": True,
            },
        )
        artifact_response.raise_for_status()
        artifact = artifact_response.json()
        assert artifact["ok"], artifact
        artifact_id = artifact["result"]["data"]["artifact_id"]
        submitted = (
            await client.post(
                endpoint + "/jobs",
                headers=headers,
                json={
                    "operation": "submit",
                    "simulation": "heat_loss",
                    "arguments": ARGS,
                },
            )
        ).json()
        assert submitted["ok"], submitted
        job_id = submitted["job"]["job_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = (
                await client.post(
                    endpoint + "/jobs",
                    headers=headers,
                    json={"operation": "status", "job_id": job_id},
                )
            ).json()
            if job["job"]["status"] == "completed":
                break
            if job["job"]["status"] in {"failed", "interrupted", "cancelled"}:
                raise RuntimeError("Container numerical worker failed")
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Container numerical job exceeded its completion deadline")

        async def read_once() -> float:
            started = time.monotonic()
            response = await client.post(
                endpoint + "/execute",
                headers=headers,
                json={
                    "tool": "engineering.calculate_heat_loss",
                    "arguments": ARGS,
                },
            )
            response.raise_for_status()
            result = response.json()
            assert result["ok"] and result["result"]["kind"] == "calculated", result
            assert result["result"]["data"]["gross_heat_loss_kw"] == 0.38
            return time.monotonic() - started

        timings = await asyncio.gather(*(read_once() for _ in range(30)))
        docker("restart", "--timeout", "10", name)
        restarted_port = docker("port", name, "8765/tcp").split(":")[-1]
        client.base_url = f"http://127.0.0.1:{restarted_port}"
        await wait_ready(client)
        response = await client.post(
            "/sessions", headers=headers, json={"site_id": "home", "resume_job_id": job_id}
        )
        response.raise_for_status()
        assert response.json()["session_id"] == session
        result = (
            await client.post(
                endpoint + "/jobs", headers=headers, json={"operation": "result", "job_id": job_id}
            )
        ).json()
        assert result["ok"] and result["result"]["data"]["gross_heat_loss_kw"] == 0.38, result
        artifacts = (await client.get(endpoint + "/artifacts", headers=headers)).json()["artifacts"]
        assert any(item["artifact_id"] == artifact_id for item in artifacts)
        return {
            "ok": True,
            "container_uid": 10001,
            "unauthenticated_denied": True,
            "load_requests": 30,
            "median_seconds": statistics.median(timings),
            "p95_seconds": sorted(timings)[28],
            "port_reassigned": restarted_port != port,
            "job_recovered": True,
            "artifact_recovered": True,
            "qualification": "Project-controlled numerical fixtures; no live meter or sustained soak.",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="energy-agent-tools:ci")
    args = parser.parse_args()
    identity = "eat-qualification-" + uuid4().hex[:10]
    volume = identity + "-state"
    try:
        with tempfile.TemporaryDirectory(prefix="eat-container-") as root:
            directory = Path(root)
            directory.chmod(0o755)
            result = asyncio.run(qualify(args.image, directory / "config.json", identity, volume))
            print(json.dumps(result, sort_keys=True))
    except Exception:
        diagnostic = subprocess.run(
            ["docker", "logs", "--tail", "40", identity], capture_output=True, text=True, timeout=15
        )
        print(diagnostic.stdout[-4000:] + diagnostic.stderr[-4000:])
        raise
    finally:
        for command in [("rm", "--force", identity), ("volume", "rm", volume)]:
            subprocess.run(["docker", *command], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
