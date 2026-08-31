"""Coverage of when a document counts as authority.

The gap being closed: ADR-0006 forbids inferring authority from source code while accepting a
requirements artifact as authoritative. Without an explicit marker, an observation about the
code can be written into an artifact and acquire authority the rule denies it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers.authority_fixtures import TODAY, action, decision_block, decision_entry

from maestro.authority.contracts import DecisionScopeKind, MalformedDecisionBlockError
from maestro.authority.documents import (
    AuthorityDocumentError,
    authoritative_sources,
    read_authority_document,
    read_authority_documents,
)
from maestro.authority.engine import AuthorityOutcomeKind, evaluate_authority

MARKED = decision_block(decision_entry(scope="project maestro"))


def write(tmp_path: Path, body: str, name: str = "adr-0005.md") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_an_accepted_document_with_marked_decisions_confers_authority(tmp_path: Path) -> None:
    path = write(tmp_path, f"* **Status:** Accepted\n\n{MARKED}")

    document = read_authority_document(path)

    assert document.is_authoritative
    assert len(document.sources) == 1
    assert document.sources[0].scope.kind is DecisionScopeKind.PROJECT
    assert document.sources[0].origin == "adr-0005.md"


@pytest.mark.parametrize("status", ["Proposed", "Superseded", "Rejected", "Draft", "Deprecated"])
def test_a_document_that_is_not_current_confers_nothing(tmp_path: Path, status: str) -> None:
    path = write(tmp_path, f"* **Status:** {status}\n\n{MARKED}")

    document = read_authority_document(path)

    assert not document.is_authoritative
    assert document.sources == ()


def test_a_document_with_no_declared_status_confers_nothing(tmp_path: Path) -> None:
    document = read_authority_document(write(tmp_path, MARKED))

    assert document.status is None
    assert document.sources == ()


def test_an_accepted_document_with_no_marked_decisions_confers_nothing_and_is_not_an_error(
    tmp_path: Path,
) -> None:
    body = "\n".join(
        [
            "* **Status:** Accepted",
            "",
            "The current implementation archives invoices, and administrators search them.",
            "",
            "### Decision: invoices.searchable",
            "- Decided: yes",
        ]
    )

    document = read_authority_document(write(tmp_path, body))

    assert document.is_authoritative
    assert document.sources == ()


def test_an_unmarked_observation_cannot_acquire_authority_by_being_written_down(
    tmp_path: Path,
) -> None:
    """The anti-laundering rule: prose in an accepted document is still only context."""

    body = "* **Status:** Accepted\n\nMaestro Audit will use SQLite for storage.\n"
    document = read_authority_document(write(tmp_path, body))

    outcome = evaluate_authority(
        action(choice="sqlite"),
        authoritative_sources((document,)),
        evaluated_on=TODAY,
    )

    assert outcome.kind is AuthorityOutcomeKind.APPROVAL_REQUIRED


def test_a_marked_decision_from_a_document_is_matched_like_any_other(tmp_path: Path) -> None:
    document = read_authority_document(write(tmp_path, f"* **Status:** Accepted\n\n{MARKED}"))
    sources = authoritative_sources((document,))

    assert evaluate_authority(action(), sources, evaluated_on=TODAY).kind is (
        AuthorityOutcomeKind.CLEARED
    )
    assert (
        evaluate_authority(action(project="another-product"), sources, evaluated_on=TODAY).kind
        is AuthorityOutcomeKind.APPROVAL_REQUIRED
    )


def test_a_documents_marked_decision_that_does_not_cover_the_action_does_not_clear_it(
    tmp_path: Path,
) -> None:
    body = "* **Status:** Accepted\n\n" + decision_block(
        decision_entry(subject="audit.retention", scope="project maestro")
    )
    document = read_authority_document(write(tmp_path, body))

    outcome = evaluate_authority(action(), authoritative_sources((document,)), evaluated_on=TODAY)

    assert outcome.kind is AuthorityOutcomeKind.APPROVAL_REQUIRED


def test_a_maestro_status_marker_is_accepted_for_documents_without_an_adr_header(
    tmp_path: Path,
) -> None:
    body = "# Project rules\n\n<!-- maestro:status: accepted -->\n\n" + "\n".join(
        [
            "<!-- maestro:rules:begin -->",
            "### Rule: naming.local_helpers",
            "- Decided: agent-chosen",
            "- Scope: project maestro",
            "- Validity: until superseded",
            "<!-- maestro:rules:end -->",
        ]
    )

    document = read_authority_document(write(tmp_path, body, name="rules.md"))

    assert document.is_authoritative
    assert len(document.sources) == 1


def test_the_marker_wins_over_a_later_prose_status(tmp_path: Path) -> None:
    body = f"<!-- maestro:status: proposed -->\n\n* **Status:** Accepted\n\n{MARKED}"

    document = read_authority_document(write(tmp_path, body))

    assert document.status == "proposed"
    assert document.sources == ()


def test_only_the_first_declared_status_counts(tmp_path: Path) -> None:
    body = f"* **Status:** Proposed\n\n* **Status:** Accepted\n\n{MARKED}"

    document = read_authority_document(write(tmp_path, body))

    assert document.status == "proposed"


def test_a_malformed_block_in_an_accepted_document_is_rejected(tmp_path: Path) -> None:
    body = "* **Status:** Accepted\n\n" + decision_block(decision_entry(validity="soon"))

    with pytest.raises(MalformedDecisionBlockError):
        read_authority_document(write(tmp_path, body))


def test_a_malformed_block_in_a_document_that_is_not_current_is_inert(tmp_path: Path) -> None:
    body = "* **Status:** Proposed\n\n" + decision_block(decision_entry(validity="soon"))

    assert read_authority_document(write(tmp_path, body)).sources == ()


def test_a_missing_document_is_reported_rather_than_ignored(tmp_path: Path) -> None:
    with pytest.raises(AuthorityDocumentError):
        read_authority_document(tmp_path / "absent.md")


def test_a_directory_is_not_an_authority_document(tmp_path: Path) -> None:
    with pytest.raises(AuthorityDocumentError):
        read_authority_document(tmp_path)


def test_a_symlinked_document_is_refused(tmp_path: Path) -> None:
    real = write(tmp_path, f"* **Status:** Accepted\n\n{MARKED}")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    with pytest.raises(AuthorityDocumentError):
        read_authority_document(link)


def test_a_non_utf8_document_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "binary.md"
    path.write_bytes(b"\xff\xfe* **Status:** Accepted")

    with pytest.raises(AuthorityDocumentError):
        read_authority_document(path)


def test_an_oversized_document_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, "* **Status:** Accepted\n" + "x" * 200_000)

    with pytest.raises(AuthorityDocumentError):
        read_authority_document(path)


def test_documents_are_read_in_the_configured_order(tmp_path: Path) -> None:
    first = write(tmp_path, f"* **Status:** Accepted\n\n{MARKED}", name="first.md")
    second = write(tmp_path, "* **Status:** Proposed\n", name="second.md")

    documents = read_authority_documents((first, second))

    assert [document.origin for document in documents] == ["first.md", "second.md"]
    assert len(authoritative_sources(documents)) == 1


def test_authority_documents_are_never_discovered_by_scanning(tmp_path: Path) -> None:
    """Only explicitly configured paths are read; repository content is untrusted."""

    write(tmp_path, f"* **Status:** Accepted\n\n{MARKED}", name="planted.md")
    configured = write(tmp_path, "* **Status:** Accepted\n", name="configured.md")

    assert authoritative_sources(read_authority_documents((configured,))) == ()
