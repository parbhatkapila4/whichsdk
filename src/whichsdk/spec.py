from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class Competitor(BaseModel):
    """An SDK the study counts as a choice, identified by its npm packages."""

    model_config = ConfigDict(extra="forbid")

    name: str
    npm_packages: list[str] = Field(min_length=1)


class AgentSpec(BaseModel):
    """An agent a study runs and the model it is asked to use."""

    model_config = ConfigDict(extra="forbid")

    name: Literal["claude_code"]
    model: str = Field(min_length=1)


class StudySpec(BaseModel):
    """A study definition as written in studies/<name>.yaml."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    category: str
    competitors: list[Competitor] = Field(min_length=1)
    starter_template: Path
    tasks: list[str] = Field(min_length=1)
    agents: list[AgentSpec] = Field(min_length=1)
    repeats: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)
    max_turns: int = Field(gt=0)
    run_budget_usd: float = Field(gt=0)
    study_budget_usd: float = Field(gt=0)

    def package_owners(self) -> dict[str, str]:
        """Map each competitor npm package to the name of the competitor that ships it."""
        return {
            package: competitor.name
            for competitor in self.competitors
            for package in competitor.npm_packages
        }


def load_study(path: Path) -> StudySpec:
    """Read a study YAML file and validate it."""
    return StudySpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
