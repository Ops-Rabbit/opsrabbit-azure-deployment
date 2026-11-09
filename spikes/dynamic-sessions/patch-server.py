"""Apply narrow, fail-closed additions to the pinned upstream release."""

from pathlib import Path

root = Path("/app/opensandbox_server")


def replace(path, old, new):
    data = path.read_text()
    if data.count(old) != 1:
        raise RuntimeError(f"Pinned upstream interface changed: {path.name}")
    path.write_text(data.replace(old, new))


replace(
    root / "config.py",
    'type: Literal["docker", "kubernetes"]',
    'type: Literal["docker", "kubernetes", "azure_dynamic_sessions"]',
)
replace(
    root / "config.py",
    '        elif self.runtime.type == "kubernetes":',
    """        elif self.runtime.type == "azure_dynamic_sessions":
            if self.kubernetes is not None or self.agent_sandbox is not None or self.secure_runtime is not None:
                raise ValueError("Azure proof does not accept Kubernetes or secure-runtime configuration")
        elif self.runtime.type == "kubernetes":""",
)
replace(
    root / "services/factory.py",
    "    # Service implementation registry",
    """    if selected_type == "azure_dynamic_sessions":
        from opensandbox_server.services.azure_service import AzureSandboxService
        return AzureSandboxService(active_config)

    # Service implementation registry""",
)
# Azure supports server-proxied access only. Avoid returning an unusable local endpoint.
replace(
    root / "api/lifecycle.py",
    "    endpoint = sandbox_service.get_endpoint(sandbox_id, port, expires=expires)",
    """    if get_config().runtime.type == "azure_dynamic_sessions" and not use_server_proxy:
        raise HTTPException(status_code=400, detail="Azure requires use_server_proxy=true")
    endpoint = sandbox_service.get_endpoint(sandbox_id, port, expires=expires)""",
)
replace(
    root / "services/snapshot_runtime_factory.py",
    '    if runtime_type == "docker":',
    """    if runtime_type == "azure_dynamic_sessions":
        from opensandbox_server.services.azure_service import AzureSnapshotsUnsupported
        return AzureSnapshotsUnsupported()

    if runtime_type == "docker":""",
)

# The adapter synchronously verifies Azure allocation absence before returning.
replace(
    root / "api/lifecycle.py",
    "def delete_sandbox(\n    sandbox_id: str,",
    "def delete_sandbox(\n    sandbox_id: str,\n    wait_for_termination: bool = Query(False),",
)
replace(
    root / "api/lifecycle.py",
    "    sandbox_service.delete_sandbox(sandbox_id)\n    return Response(status_code=status.HTTP_204_NO_CONTENT)",
    """    if wait_for_termination and get_config().runtime.type != "azure_dynamic_sessions":
        raise HTTPException(status_code=501, detail="Confirmed retirement is unavailable")
    sandbox_service.delete_sandbox(sandbox_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT,
                    headers={"X-OpsRabbit-Workload-Deleted": "true"} if wait_for_termination else None)""",
)
