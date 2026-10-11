"""Settings loaded from .env / the environment."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    jev_api_key: str
    jev_model: str
    jev_base_url: str
    openai_api_key: str
    openai_model: str
    openai_reasoning: str
    neatlogs_api_key: str
    neatlogs_workflow_name: str
    neatlogs_endpoint: str
    github_token: str
    target_repo: str
    state_dir: Path

    def problems(self) -> list[str]:
        """Human-readable list of missing or invalid settings."""
        required = {
            "JEV_API_KEY": self.jev_api_key,
            "JEV_MODEL": self.jev_model,
            "OPENAI_API_KEY": self.openai_api_key,
            "LINUS_OPENAI_MODEL": self.openai_model,
            "NEATLOGS_API_KEY": self.neatlogs_api_key,
        }
        found = [f"{name} is empty" for name, value in required.items() if not value]
        if self.target_repo and self.target_repo.count("/") != 1:
            found.append(f"LINUS_TARGET_REPO={self.target_repo!r}, expected owner/name")
        return found


def _api_root(url: str) -> str:
    """Accept either the API root or the full /v1/systemone endpoint."""
    return url.strip().rstrip("/").removesuffix("/v1/systemone")


def load_settings(env_file: str | Path = ".env") -> Settings:
    load_dotenv(env_file, override=False)

    def get(key: str, default: str = "") -> str:
        return os.getenv(key, default).strip()

    return Settings(
        jev_api_key=get("JEV_API_KEY"),
        jev_model=get("JEV_MODEL", "jev-1.13.0"),
        jev_base_url=_api_root(get("JEV_BASE_URL")),
        openai_api_key=get("OPENAI_API_KEY"),
        openai_model=get("LINUS_OPENAI_MODEL", "gpt-5.4-mini"),
        openai_reasoning=get("LINUS_OPENAI_REASONING", "low"),
        neatlogs_api_key=get("NEATLOGS_API_KEY"),
        neatlogs_workflow_name=get("NEATLOGS_WORKFLOW_NAME", "linus"),
        neatlogs_endpoint=get("NEATLOGS_ENDPOINT"),
        github_token=get("GITHUB_TOKEN"),
        target_repo=get("LINUS_TARGET_REPO"),
        state_dir=Path(get("LINUS_STATE_DIR", ".linus")),
    )
