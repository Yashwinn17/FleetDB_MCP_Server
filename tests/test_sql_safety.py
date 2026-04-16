"""Unit tests for the SQL safety validator.

These are the most important tests in the repo — the validator is what
stands between an LLM and a `DROP TABLE`.
"""

from __future__ import annotations

import pytest

from fleetdb_mcp.sql_safety import (
    SQLValidationError,
    validate_read_only,
    validate_write,
)


# ============================================================
# READ-ONLY VALIDATOR
# ============================================================


class TestValidateReadOnly:
    # --- happy path ---

    def test_simple_select(self):
        v = validate_read_only("SELECT * FROM vehicles")
        assert v.kind == "SELECT"

    def test_select_with_trailing_semicolon(self):
        v = validate_read_only("SELECT 1;")
        assert v.kind == "SELECT"

    def test_select_with_join_group_order(self):
        sql = """
            SELECT v.make, COUNT(*) AS n
            FROM vehicles v
            JOIN trips t ON t.vehicle_id = v.vehicle_id
            WHERE t.started_at > NOW() - INTERVAL '30 days'
            GROUP BY v.make
            ORDER BY n DESC
        """
        v = validate_read_only(sql)
        assert v.kind == "SELECT"

    def test_cte_is_allowed(self):
        sql = """
            WITH recent AS (SELECT * FROM trips WHERE started_at > NOW() - INTERVAL '7 days')
            SELECT COUNT(*) FROM recent
        """
        # WITH-prefixed SELECT — sqlparse reports the first DML token as SELECT.
        v = validate_read_only(sql)
        assert v.kind == "SELECT"

    # --- rejections ---

    def test_rejects_empty(self):
        with pytest.raises(SQLValidationError, match="empty SQL"):
            validate_read_only("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(SQLValidationError, match="empty SQL"):
            validate_read_only("   \n  ")

    def test_rejects_multiple_statements(self):
        with pytest.raises(SQLValidationError, match="exactly one statement"):
            validate_read_only("SELECT 1; SELECT 2")

    def test_rejects_update(self):
        with pytest.raises(SQLValidationError, match="only SELECT is permitted"):
            validate_read_only("UPDATE vehicles SET status='retired' WHERE vehicle_id=1")

    def test_rejects_insert(self):
        with pytest.raises(SQLValidationError, match="only SELECT is permitted"):
            validate_read_only("INSERT INTO drivers (first_name) VALUES ('x')")

    def test_rejects_delete(self):
        with pytest.raises(SQLValidationError, match="only SELECT is permitted"):
            validate_read_only("DELETE FROM trips WHERE trip_id=1")

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE vehicles",
            "TRUNCATE vehicles",
            "ALTER TABLE vehicles ADD COLUMN x int",
            "CREATE TABLE foo (id int)",
            "GRANT ALL ON vehicles TO public",
        ],
    )
    def test_rejects_ddl_dcl(self, sql):
        with pytest.raises(SQLValidationError):
            validate_read_only(sql)

    def test_rejects_copy(self):
        # COPY can read/write server filesystem — always blocked.
        with pytest.raises(SQLValidationError):
            validate_read_only("COPY vehicles TO '/tmp/x.csv'")

    def test_rejects_set(self):
        with pytest.raises(SQLValidationError):
            validate_read_only("SET search_path TO public")

    def test_rejects_vacuum(self):
        with pytest.raises(SQLValidationError):
            validate_read_only("VACUUM vehicles")

    # --- defense against tricky inputs ---

    def test_string_literal_with_update_word_is_fine(self):
        """A SELECT that happens to include the word UPDATE in a string literal
        should still validate — this is where naive regex validators fail."""
        v = validate_read_only("SELECT 'please UPDATE me' AS note")
        assert v.kind == "SELECT"

    def test_comment_hiding_write_is_rejected(self):
        # Single statement with a comment then an UPDATE after — sqlparse
        # treats the first DML as UPDATE and we reject.
        with pytest.raises(SQLValidationError):
            validate_read_only("/* looks safe */ UPDATE vehicles SET status='x' WHERE vehicle_id=1")


# ============================================================
# WRITE VALIDATOR
# ============================================================


class TestValidateWrite:
    # --- happy path ---

    def test_insert(self):
        v = validate_write(
            "INSERT INTO drivers (first_name, last_name, license_number, "
            "license_expiry, hired_at) VALUES ('A','B','X',  '2030-01-01','2020-01-01')"
        )
        assert v.kind == "INSERT"

    def test_update_with_where(self):
        v = validate_write("UPDATE vehicles SET status='retired' WHERE vehicle_id=1")
        assert v.kind == "UPDATE"

    def test_delete_with_where(self):
        v = validate_write("DELETE FROM trips WHERE trip_id=1")
        assert v.kind == "DELETE"

    # --- rejections ---

    def test_rejects_select(self):
        with pytest.raises(SQLValidationError, match="only INSERT/UPDATE/DELETE"):
            validate_write("SELECT * FROM vehicles")

    def test_rejects_update_without_where(self):
        with pytest.raises(SQLValidationError, match="WHERE clause"):
            validate_write("UPDATE vehicles SET status='retired'")

    def test_rejects_delete_without_where(self):
        with pytest.raises(SQLValidationError, match="WHERE clause"):
            validate_write("DELETE FROM trips")

    def test_rejects_multiple_statements(self):
        with pytest.raises(SQLValidationError, match="exactly one statement"):
            validate_write(
                "UPDATE vehicles SET status='x' WHERE vehicle_id=1; "
                "DELETE FROM trips WHERE trip_id=1"
            )

    def test_rejects_drop_dressed_as_write(self):
        with pytest.raises(SQLValidationError):
            validate_write("DROP TABLE vehicles")

    def test_insert_does_not_require_where(self):
        # Sanity: INSERT never has a WHERE, shouldn't be rejected for its absence.
        v = validate_write("INSERT INTO drivers (first_name, last_name, license_number, "
                           "license_expiry, hired_at) VALUES ('A','B','X','2030-01-01','2020-01-01')")
        assert v.kind == "INSERT"

    # --- regression ---

    def test_update_set_clause_is_not_confused_with_set_statement(self):
        """Regression: an earlier version blocked `SET` as a forbidden keyword
        wherever it appeared, which broke every UPDATE because `UPDATE ... SET col=val`
        contains `SET` as a clause. The fix is to only check the *leading* keyword."""
        v = validate_write("UPDATE vehicles SET status='retired' WHERE vehicle_id=1")
        assert v.kind == "UPDATE"

    def test_update_with_multiple_set_columns(self):
        v = validate_write(
            "UPDATE vehicles SET status='retired', odometer_km=100000 WHERE vehicle_id=1"
        )
        assert v.kind == "UPDATE"
