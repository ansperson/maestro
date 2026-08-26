"""Audit- and log-safe model identifiers shared across trust boundaries."""

from __future__ import annotations

import re
from typing import Self

from pydantic import ConfigDict, RootModel, model_validator

MAX_MODEL_IDENTIFIER_CHARS = 128
_MODEL_IDENTIFIER_PATTERN = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_MODEL_IDENTIFIER_CHARS - 1}}}",
    flags=re.ASCII,
)


class ModelIdentifier(RootModel[str]):
    """A conservative ASCII identifier safe for Audit payloads and structured logs.

    Supported identifiers contain only ASCII letters, digits, dots, underscores, and hyphens,
    begin with an alphanumeric character, and are at most 128 characters long. Delimiters used
    by URIs, filesystem paths, credential assignments, and free-form text are intentionally not
    accepted.
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    @model_validator(mode="after")
    def validate_identifier(self) -> Self:
        if _MODEL_IDENTIFIER_PATTERN.fullmatch(self.root) is None:
            raise ValueError("model identifier is not Audit-safe")
        return self

    @property
    def value(self) -> str:
        """Return the already validated identifier for string-only adapters."""

        return self.root
