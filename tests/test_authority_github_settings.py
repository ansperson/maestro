"""Coverage of the work-management credential and address configuration.

The token receives the same owner-only controls an Audit role password does, because a
tracker token and a database password are the same kind of thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from maestro.config import GitHubWorkItemSettings

TOKEN = "synthetic-work-item-token"  # noqa: S105 - a fixture value, not a credential
REPOSITORY = "ansperson/maestro"


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "github-token"
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_a_repository_that_is_not_owner_slash_name_is_rejected(token_file: Path) -> None:
    with pytest.raises(ValidationError):
        GitHubWorkItemSettings(repository="maestro", token_file=token_file)


def test_a_plaintext_api_url_is_rejected(token_file: Path) -> None:
    with pytest.raises(ValidationError, match="https"):
        GitHubWorkItemSettings(
            repository=REPOSITORY,
            token_file=token_file,
            api_url="http://api.github.com",
        )


def test_a_world_readable_token_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "github-token"
    path.write_text(TOKEN, encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(ValidationError, match="owner-only"):
        GitHubWorkItemSettings(repository=REPOSITORY, token_file=path)


def test_a_symlinked_token_file_is_rejected(tmp_path: Path, token_file: Path) -> None:
    link = tmp_path / "linked-token"
    link.symlink_to(token_file)

    with pytest.raises(ValidationError, match="non-symlink"):
        GitHubWorkItemSettings(repository=REPOSITORY, token_file=link)


def test_the_token_is_not_rendered_by_the_configuration(token_file: Path) -> None:
    settings = GitHubWorkItemSettings(repository=REPOSITORY, token_file=token_file)

    configuration = settings.work_item_configuration()

    assert TOKEN not in repr(settings)
    assert TOKEN not in repr(configuration)
    assert TOKEN not in configuration.model_dump_json()


def test_a_rotated_token_is_picked_up_without_a_restart(token_file: Path) -> None:
    settings = GitHubWorkItemSettings(repository=REPOSITORY, token_file=token_file)
    token_file.write_text("rotated-token", encoding="utf-8")

    assert settings.work_item_configuration().token.get_secret_value() == "rotated-token"
