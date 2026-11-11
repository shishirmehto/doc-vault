from __future__ import annotations

import re

from docvault import db as dbmod
from docvault.domain import DocKind, DocStatus, PageBasis, Purpose, Step, StepOutcome, TurnOutcome

from .conftest import scalar

VALUE_CHECKS = {
    "documents_kind_values": DocKind,
    "documents_status_values": DocStatus,
    "documents_page_basis_values": PageBasis,
    "step_runs_step_values": Step,
    "step_runs_outcome_values": StepOutcome,
    "chat_turns_outcome_values": TurnOutcome,
    "llm_calls_purpose_values": Purpose,
}


def test_check_lists_match_the_enums(db):
    with db.connection() as conn:
        rows = conn.execute(r"SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
                            r"WHERE conname LIKE '%\_values'").fetchall()
    in_schema = {r["conname"]: set(re.findall(r"'([^']+)'", r["def"])) for r in rows}
    assert in_schema == {name: {member.value for member in enum} for name, enum in VALUE_CHECKS.items()}


def test_migrate_again_applies_nothing_and_takes_no_table_locks(db, database_url):
    with db.connection() as writer, writer.transaction():
        writer.execute("LOCK TABLE documents IN ACCESS EXCLUSIVE MODE")
        dbmod.migrate(database_url)   # would block forever if it re-ran the DDL
    assert scalar(db, "SELECT count(*) FROM schema_version") == 1
    assert scalar(db, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'") == 10
