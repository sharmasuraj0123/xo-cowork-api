"""Print host directories the Docker installer should expose to the runtime.

The installer asks the image for this list instead of hardcoding backend
names or paths. Full agents contribute required state homes from manifests;
read-only telemetry providers may contribute optional native-data homes
without pretending to be chat backends.
"""

from __future__ import annotations

from pathlib import Path

from services.cowork_agent.adapters.loader import (
    list_capability_providers,
    try_load_capability,
)
from services.cowork_agent.registry.agent_registry import all_agents


def runtime_mounts() -> list[tuple[str, str, bool]]:
    container_home = Path.home().resolve()
    paths: dict[Path, bool] = {}

    def add(path: Path, *, required: bool) -> None:
        resolved = path.expanduser().resolve()
        paths[resolved] = paths.get(resolved, False) or required

    for manifest in all_agents():
        add(manifest.home_dir, required=True)
        setup = manifest.raw.get("runtime_setup") or {}
        for raw_path in setup.get("extra_mounts") or []:
            if isinstance(raw_path, str) and raw_path.strip():
                add(Path(raw_path), required=True)

    for provider in list_capability_providers("session_telemetry"):
        module = try_load_capability("session_telemetry", agent=provider)
        discover = getattr(module, "runtime_mounts", None) if module else None
        if not callable(discover):
            continue
        for path in discover():
            if isinstance(path, (str, Path)):
                add(Path(path), required=False)

    mounts: list[tuple[str, str, bool]] = []
    for destination, required in sorted(paths.items(), key=lambda item: str(item[0])):
        try:
            relative = destination.relative_to(container_home)
        except ValueError:
            continue
        if not relative.parts:
            continue
        mounts.append((relative.as_posix(), str(destination), required))
    return mounts


if __name__ == "__main__":
    for relative_path, container_path, required in runtime_mounts():
        print(f"{relative_path}\t{container_path}\t{int(required)}")
