"""Метаданные без живой БД: DDL компилируется под PostgreSQL, ключевые ограничения на месте."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from svetofor.core.config import get_settings
from svetofor.db.models import Base

EXPECTED_TABLES = {
    "users",
    "packages",
    "documents",
    "pages",
    "sections",
    "chunks",
    "rules",
    "rule_versions",
    "rulesets",
    "ruleset_versions",
    "checks",
    "check_stages",
    "facts",
    "findings",
    "rule_results",
    "feedback",
    "llm_cache",
    "llm_calls",
    "eval_runs",
    "audit_log",
}


def compile_ddl() -> str:
    dialect = postgresql.dialect()
    parts = []
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)))
        parts.extend(str(CreateIndex(ix).compile(dialect=dialect)) for ix in table.indexes)
    return "\n".join(parts)


def test_all_tables_present() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_ddl_compiles_for_postgres() -> None:
    ddl = compile_ddl()
    assert f"VECTOR({get_settings().embed_dim})" in ddl  # размерность — из EMBED_DIM
    assert "USING hnsw (embedding vector_cosine_ops)" in ddl
    assert "USING gin (tsv)" in ddl
    assert "to_tsvector('russian'" in ddl


def test_package_children_cascade_on_delete() -> None:
    for table_name in ("documents", "checks"):
        fks = Base.metadata.tables[table_name].foreign_keys
        to_packages = [f for f in fks if f.column.table.name == "packages"]
        assert to_packages and all(f.ondelete == "CASCADE" for f in to_packages)


def test_enums_are_checked_strings_not_native() -> None:
    ddl = compile_ddl()
    assert "CREATE TYPE" not in ddl
    assert "CONSTRAINT ck_checks_checkstatus CHECK (status IN (" in ddl
    assert "'done_with_warnings'" in ddl


def test_constraint_names_follow_convention() -> None:
    ddl = compile_ddl()
    assert "CONSTRAINT pk_users PRIMARY KEY" in ddl
    assert "CONSTRAINT fk_documents_package_id_packages FOREIGN KEY" in ddl
    assert "CONSTRAINT uq_pages_document_number UNIQUE" in ddl
