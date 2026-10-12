"""The repos a maintainer runs Linus on: setup (`linus init`), where each repo's
rules come from, and a sandbox image with each repo's dependencies.

Dependencies are read from the default branch only (requirements files and
pyproject.toml), never from a PR, and installed when the image is built, with
network. Tests still run with no network. A PR that changes dependencies goes
to a human (see diffcheck), because its new packages aren't in the image.
"""

import base64
import json
import re
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from linus import sandbox
from linus.github import GitHub, GitHubError, split_repo
from linus.policy import REPO_RULES, Policy, PolicyError, load, parse

REQUIREMENTS = re.compile(r"^(requirements[^/]*\.txt|requirements/[^/]+\.txt)$")
EXTRAS = ("test", "tests", "testing", "dev")  # optional-dependency groups that hold test tools
BUILD_TIMEOUT_S = 900


def slug(repo: str) -> str:
    owner, name = split_repo(repo)
    return f"{owner}__{name}"


def home(state_dir: Path, repo: str) -> Path:
    """Linus's local folder for one repo: its starter rules and setup record."""
    return state_dir / "repos" / slug(repo)


def registered(state_dir: Path) -> list[dict]:
    found = []
    for path in sorted((state_dir / "repos").glob("*/repo.json")):
        try:
            found.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return found


def register(state_dir: Path, repo: str, info: dict) -> Path:
    path = home(state_dir, repo) / "repo.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"repo": repo, "set_up_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **info}
    path.write_text(json.dumps(record, indent=2, sort_keys=True))
    return path


# -- rules ---------------------------------------------------------------------

def read_file(gh: GitHub, repo: str, path: str, ref: str | None = None) -> bytes | None:
    """A file from the repo (default branch unless `ref`), or None if it isn't there."""
    try:
        data = gh.get(f"/repos/{repo}/contents/{path}", **({"ref": ref} if ref else {}))
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(data, dict) or data.get("type") != "file":
        return None
    return base64.b64decode(data.get("content") or "")


def rules(repo: str | None, state_dir: Path, gh: GitHub | None = None, override: Path | None = None,
          default: Path = Path("bounty.yml")) -> tuple[Policy, list[str]]:
    """The rules for a close, and notes for the maintainer. In order: `--policy`,
    the repo's .github/bounty.yml, the copy `linus init` wrote, Linus's bounty.yml."""
    notes: list[str] = []
    if override:
        return load(override), notes
    if repo and gh:
        raw = read_file(gh, repo, REPO_RULES)
        if raw is not None:
            try:
                return parse(raw, f"{repo}:{REPO_RULES}"), notes
            except PolicyError as exc:
                raise PolicyError(f"{repo}:{REPO_RULES}: {exc}") from exc
        notes.append(f"{repo} has no {REPO_RULES}, so contributors can't see the rules yet")
    if repo:
        local = home(state_dir, repo) / "bounty.yml"
        if local.exists():
            notes.append(f"using {local} (written by `linus init`)")
            return load(local), notes
    if repo:
        notes.append(f"using the default rules in {default}")
    return load(default), notes


# -- dependencies --------------------------------------------------------------

def _requirement_lines(text: str) -> tuple[list[str], list[str]]:
    keep, skipped = [], []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") or line.startswith((".", "/", "file:")):
            skipped.append(line)  # -r/-e/-c/--index-url and local paths need the checkout
            continue
        keep.append(line)
    return keep, skipped


def _own_name(requirement: str, project: str) -> bool:
    name = re.split(r"[\s\[<>=!~;@]", requirement, maxsplit=1)[0]
    return bool(project) and re.sub(r"[-_.]+", "-", name).lower() == re.sub(r"[-_.]+", "-", project).lower()


def dependencies(files: dict[str, str]) -> tuple[list[str], list[str]]:
    """Requirements to install from a repo's dependency files ({path: text}),
    and notes on anything left out."""
    reqs: list[str] = []
    notes: list[str] = []
    for path in sorted(p for p in files if REQUIREMENTS.match(p)):
        keep, skipped = _requirement_lines(files[path])
        reqs += keep
        if skipped:
            notes.append(f"{path}: skipped {', '.join(skipped[:3])}{' ...' if len(skipped) > 3 else ''}")
    if "pyproject.toml" in files:
        try:
            data = tomllib.loads(files["pyproject.toml"])
        except tomllib.TOMLDecodeError as exc:
            notes.append(f"pyproject.toml is not valid TOML ({exc}); its dependencies were skipped")
            data = {}
        project = data.get("project") or {}
        name = str(project.get("name") or "")
        listed = list(project.get("dependencies") or [])
        extras = project.get("optional-dependencies") or {}
        groups = data.get("dependency-groups") or {}
        for key in EXTRAS:
            listed += list(extras.get(key) or []) + list(groups.get(key) or [])
        for req in listed:
            if not isinstance(req, str):
                continue  # {include-group = ...} entries
            if _own_name(req, name):
                continue  # `widgets[test]` inside widgets' own extras
            reqs.append(req.strip())
        if (data.get("tool") or {}).get("poetry"):
            notes.append("Poetry dependencies aren't read; list test dependencies in a requirements file")
    if "setup.py" in files and not reqs:
        notes.append("setup.py isn't run to find dependencies; list them in requirements.txt or pyproject.toml")
    return list(dict.fromkeys(r for r in reqs if r)), notes


def dependency_files(gh: GitHub, repo: str, paths: list[str]) -> dict[str, str]:
    wanted = [p for p in paths if REQUIREMENTS.match(p) or p in ("pyproject.toml", "setup.py")]
    found = {}
    for path in wanted:
        raw = read_file(gh, repo, path)
        if raw is not None:
            found[path] = raw.decode(errors="replace")
    return found


def tree(gh: GitHub, repo: str, branch: str) -> list[str]:
    data = gh.get(f"/repos/{repo}/git/trees/{branch}", recursive="1")
    return [entry["path"] for entry in data.get("tree", []) if entry.get("type") == "blob"]


# -- sandbox image -------------------------------------------------------------

DOCKERFILE = f"""FROM {sandbox.BASE_IMAGE}
USER root
COPY requirements.txt /tmp/linus-requirements.txt
RUN pip install --no-cache-dir -r /tmp/linus-requirements.txt
USER runner
"""


def build_image(repo: str, requirements: list[str], log: Callable[[str], None] = print) -> str:
    """Build the repo's sandbox image: Linus's base image plus `requirements`.
    Returns the tag. The label ties the image to the repo it was built for."""
    tag = sandbox.repo_image(repo)
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Dockerfile").write_text(DOCKERFILE)
        Path(tmp, "requirements.txt").write_text("".join(f"{r}\n" for r in requirements))
        cmd = ["docker", "build", "-q", "-t", tag, "--label", f"{sandbox.REPO_LABEL}={repo}", tmp]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
        except FileNotFoundError as exc:
            raise sandbox.InfrastructureError("docker is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise sandbox.InfrastructureError(f"building {tag} took over {BUILD_TIMEOUT_S}s") from exc
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).strip().splitlines()
        raise sandbox.InfrastructureError(f"building {tag} failed: {' | '.join(detail[-4:])[:400]}")
    log(f"built {tag} with {len(requirements)} dependencies")
    return tag
