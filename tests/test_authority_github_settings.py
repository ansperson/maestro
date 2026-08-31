"""Coverage of the work-management credential and address configuration.

The token receives the same owner-only controls an Audit role password does, because a
tracker token and a database password are the same kind of thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from maestro.config import (
    AuditWriterSettings,
    GitHubWorkItemSettings,
    Settings,
    load_work_item_settings,
    reject_secret_inside_allowed_roots,
)

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


def test_a_token_inside_an_allowed_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential a read-only capability may read can reach an append-only Trail.

    `resolve_codebase_fact` returns evidence from anywhere inside an allowed root, and Audit
    persists what it returns, so a token stored there would be recorded permanently.
    """

    repository = tmp_path / "repository"
    repository.mkdir()
    inside = repository / "github-token"
    inside.write_text(TOKEN, encoding="utf-8")
    inside.chmod(0o600)
    monkeypatch.setenv("MAESTRO_WORKITEM_GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("MAESTRO_WORKITEM_GITHUB_TOKEN_FILE", str(inside))
    settings = Settings.model_validate(
        {"allowed_roots": (repository,), "audit_writer": AuditWriterSettings()}  # pyright: ignore[reportCallIssue]
    )

    with pytest.raises(ValueError, match="outside every allowed root"):
        load_work_item_settings(settings)


def test_a_token_beside_an_allowed_root_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token_file: Path
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("MAESTRO_WORKITEM_GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("MAESTRO_WORKITEM_GITHUB_TOKEN_FILE", str(token_file))
    settings = Settings.model_validate(
        {"allowed_roots": (repository,), "audit_writer": AuditWriterSettings()}  # pyright: ignore[reportCallIssue]
    )

    assert load_work_item_settings(settings).token_file == token_file


def test_the_allowed_root_itself_cannot_be_the_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(ValueError, match="outside every allowed root"):
        reject_secret_inside_allowed_roots(repository, (repository,), "Work-management token file")
