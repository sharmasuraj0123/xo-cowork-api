"""
Bundled-skill installer.

Copies skills shipped with this repo into the on-disk skill directory of every
installed agent. Runs at server startup. Non-fatal on missing parents or write
errors — matches the rclone/usage-sync bootstrap pattern in server.py.

Install targets are resolved from each discovered agent manifest's ``home_dir``
(no backend name is hardcoded here): a skill lands in ``<home_dir>/skills/<name>``
for every agent whose home dir exists on the host.
"""

import shutil
from pathlib import Path

from services.cowork_agent.registry.agent_registry import all_agents

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_DIR = _REPO_ROOT / ".agents" / "skills"

# Skills to install globally so any agent in any project can invoke them.
# Each entry must match a directory under `.agents/skills/` containing a
# `SKILL.md`. Add a name here to bundle a new skill with every installed agent.
BUNDLED_SKILLS = (
    "xo-projects",
)


def install_xo_skills() -> None:
    """
    Install each bundled skill into every installed agent's skill dir.

    Always overwrites: the repo is the source of truth. Copies the full
    skill directory (SKILL.md plus any `references/` or other supporting
    files) so progressive-disclosure layouts survive the install. The
    target `skills/<name>/` is wiped first so files removed from the
    source don't linger after a re-run. Skips an agent (with a warning)
    if its `home_dir` is absent — that means the corresponding CLI isn't
    installed on this host.
    """
    runtimes = [(manifest.name, manifest.home_dir) for manifest in all_agents()]

    for skill_name in BUNDLED_SKILLS:
        source = _SOURCE_DIR / skill_name
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            print(f"⚠️ bundled skill source missing or invalid: {source}")
            continue

        for label, home in runtimes:
            if not home.is_dir():
                print(f"⚠️ {label} skill install skipped for {skill_name!r}: {home} does not exist")
                continue
            target = home / "skills" / skill_name
            try:
                if target.exists():
                    shutil.rmtree(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, target)
                print(f"✅ {label} skill installed: {target}")
            except Exception as exc:
                print(f"⚠️ {label} skill install failed ({target}): {exc}")
