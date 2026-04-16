"""SQL safety validation.

Two validators:

1. `validate_read_only(sql)` — allows a single SELECT statement. Rejects anything
   that could mutate state: writes, DDL, DCL, system procs.
2. `validate_write(sql)` — allows a single INSERT/UPDATE/DELETE. UPDATE and DELETE
   must have a WHERE clause (the most common "oops I just trashed production"
   footgun). Multi-statement inputs and DDL are rejected.

Both validators use sqlparse to tokenize — cheaper and more correct than regex,
which mis-handles things like `SELECT 'UPDATE me'` in string literals.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DML, Keyword


class SQLValidationError(ValueError):
    """Raised when a SQL statement fails the safety validator."""


# ------------------------------------------------------------
# Constants
# ------------------------------------------------------------

_READ_ONLY_ALLOWED_DML = {"SELECT"}
_WRITE_ALLOWED_DML = {"INSERT", "UPDATE", "DELETE"}

# Statements that must never appear as the *leading* token of whatever we run.
# We single-statement-enforce above; this list catches anything whose first
# DML/DDL keyword is one of these. Listing them as leading-token matches only
# avoids false positives on legitimate sub-clauses (e.g. the SET in an UPDATE,
# the ANALYZE inside EXPLAIN ANALYZE — neither of which we allow anyway, but
# which would break a naive "appears anywhere" check on intermediate tokens).
_FORBIDDEN_LEADING = {
    "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE",
    "COMMIT", "ROLLBACK", "SAVEPOINT", "BEGIN", "END",
    "COPY",           # can read/write the server filesystem
    "VACUUM", "REINDEX", "CLUSTER",
    "SET", "RESET", "SHOW",
    "EXECUTE", "PREPARE", "DEALLOCATE",
    "LISTEN", "NOTIFY", "UNLISTEN",
    "DO",             # anonymous PL/pgSQL blocks
    "CALL",           # stored-procedure call
}


@dataclass(frozen=True)
class ValidatedStatement:
    """Result of a successful validation."""

    kind: str  # "SELECT" | "INSERT" | "UPDATE" | "DELETE"
    normalized_sql: str


# ------------------------------------------------------------
# Core parsing helpers
# ------------------------------------------------------------


def _parse_single(sql: str) -> Statement:
    """Parse and return exactly one non-empty statement."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SQLValidationError("empty SQL")

    parsed = sqlparse.parse(sql)
    # sqlparse splits on `;`. Multiple statements = reject.
    non_empty = [s for s in parsed if s.tokens and str(s).strip()]
    if len(non_empty) != 1:
        raise SQLValidationError(
            f"exactly one statement required, got {len(non_empty)}"
        )
    return non_empty[0]


def _leading_keyword(stmt: Statement) -> str:
    """Return the first meaningful keyword of the statement, uppercased.

    Walks the flattened tokens, skipping whitespace and comments. For a
    well-formed statement this is either a DML (SELECT/INSERT/...) or a
    DDL/DCL/utility keyword (DROP/SET/COPY/...).
    """
    for tok in stmt.flatten():
        if tok.is_whitespace:
            continue
        if tok.ttype is not None and "Comment" in str(tok.ttype):
            continue
        if tok.value.strip():
            return tok.value.upper().strip()
    raise SQLValidationError("statement has no leading keyword")


def _first_dml_keyword(stmt: Statement) -> str:
    """Return the first DML keyword (SELECT, INSERT, …) in uppercase, or raise."""
    for tok in stmt.flatten():
        if tok.ttype in (DML,) and tok.value.strip():
            return tok.value.upper()
    raise SQLValidationError("statement has no DML keyword — not a valid query")


def _check_not_forbidden(stmt: Statement) -> None:
    """Raise if the statement's leading keyword is in the forbidden set."""
    leading = _leading_keyword(stmt)
    if leading in _FORBIDDEN_LEADING:
        raise SQLValidationError(f"forbidden statement type: {leading}")


def _has_where_clause(stmt: Statement) -> bool:
    """Check whether an UPDATE/DELETE contains a WHERE clause."""
    for tok in stmt.flatten():
        if tok.ttype is Keyword and tok.value.upper() == "WHERE":
            return True
    return False


# ------------------------------------------------------------
# Public API
# ------------------------------------------------------------


def validate_read_only(sql: str) -> ValidatedStatement:
    """Validate that `sql` is a single read-only SELECT.

    Rejects multi-statement input, any write or DDL, and statements whose
    leading token is on the forbidden list (COPY, SET, VACUUM, etc.).
    """
    stmt = _parse_single(sql)
    _check_not_forbidden(stmt)

    kind = _first_dml_keyword(stmt)
    if kind not in _READ_ONLY_ALLOWED_DML:
        raise SQLValidationError(
            f"read-only tool rejects '{kind}' — only SELECT is permitted"
        )

    return ValidatedStatement(kind=kind, normalized_sql=str(stmt).strip())


def validate_write(sql: str) -> ValidatedStatement:
    """Validate that `sql` is a single INSERT/UPDATE/DELETE.

    UPDATE and DELETE must contain a WHERE clause.
    """
    stmt = _parse_single(sql)
    _check_not_forbidden(stmt)

    kind = _first_dml_keyword(stmt)
    if kind not in _WRITE_ALLOWED_DML:
        raise SQLValidationError(
            f"write tool rejects '{kind}' — only INSERT/UPDATE/DELETE are permitted"
        )

    if kind in {"UPDATE", "DELETE"} and not _has_where_clause(stmt):
        raise SQLValidationError(
            f"{kind} without a WHERE clause is rejected — "
            "this is almost always a mistake"
        )

    return ValidatedStatement(kind=kind, normalized_sql=str(stmt).strip())
