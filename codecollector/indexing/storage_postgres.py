from __future__ import annotations

from pathlib import Path
from typing import Iterable

from codecollector.domain.models import RelationRecord, SymbolRecord


class PostgresIndexStore:
    def __init__(self, connection_string: str, schema: str = 'public') -> None:
        self.connection_string = connection_string
        self.schema = schema
        self._initialize()

    def _connect(self, set_search_path: bool = True):
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(self.connection_string, row_factory=dict_row)
        if set_search_path:
            conn.execute(f"SET search_path TO {self.schema}")
        return conn

    def _initialize(self) -> None:
        with self._connect(set_search_path=False) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS {self.schema}')
            conn.execute(f"SET search_path TO {self.schema}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cc_files (
                    project_root TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    PRIMARY KEY (project_root, file_path)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cc_symbols (
                    project_root TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    name TEXT NOT NULL,
                    qualname TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    parent_qualname TEXT,
                    start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL,
                    docstring TEXT NOT NULL,
                    source_code TEXT NOT NULL,
                    PRIMARY KEY (project_root, qualname)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cc_relations (
                    project_root TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    source_qualname TEXT NOT NULL,
                    relation_kind TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    target_qualname TEXT,
                    relation_source TEXT NOT NULL DEFAULT 'index',
                    relation_confidence TEXT NOT NULL DEFAULT 'medium'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cc_search_documents (
                    project_root TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    qualname TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (project_root, doc_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_symbols_name ON cc_symbols(project_root, name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_symbols_file ON cc_symbols(project_root, file_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_relations_source ON cc_relations(project_root, source_qualname)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_relations_target_ref ON cc_relations(project_root, target_ref)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_relations_target_qualname ON cc_relations(project_root, target_qualname)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_relations_kind ON cc_relations(project_root, relation_kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cc_search_documents_qualname ON cc_search_documents(project_root, qualname)")
            conn.commit()

    def get_known_files(self, project_root: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT file_path, file_hash FROM cc_files WHERE project_root = %s",
                (project_root,),
            ).fetchall()
        return {row['file_path']: row['file_hash'] for row in rows}

    def replace_file_contents(
        self,
        project_root: str,
        file_path: str,
        file_hash: str,
        module_name: str,
        symbols: Iterable[SymbolRecord],
        relations: Iterable[RelationRecord],
    ) -> None:
        symbol_rows = list(symbols)
        relation_rows = list(relations)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cc_files(project_root, file_path, file_hash, module_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (project_root, file_path)
                DO UPDATE SET file_hash = EXCLUDED.file_hash, module_name = EXCLUDED.module_name
                """,
                (project_root, file_path, file_hash, module_name),
            )
            conn.execute("DELETE FROM cc_symbols WHERE project_root = %s AND file_path = %s", (project_root, file_path))
            conn.execute(
                "DELETE FROM cc_relations WHERE project_root = %s AND file_path = %s AND relation_source = 'index'",
                (project_root, file_path),
            )
            for item in symbol_rows:
                conn.execute(
                    """
                    INSERT INTO cc_symbols(
                        project_root, file_path, module_name, name, qualname, kind,
                        parent_qualname, start_line, end_line, docstring, source_code
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        project_root, item.file_path, item.module_name, item.name, item.qualname, item.kind,
                        item.parent_qualname, item.start_line, item.end_line, item.docstring, item.source_code,
                    ),
                )
            for item in relation_rows:
                conn.execute(
                    """
                    INSERT INTO cc_relations(
                        project_root, file_path, source_qualname, relation_kind,
                        target_ref, target_qualname, relation_source, relation_confidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        project_root, item.file_path, item.source_qualname, item.relation_kind,
                        item.target_ref, item.target_qualname, item.relation_source, item.relation_confidence,
                    ),
                )
            conn.commit()

    def replace_knowledge_relations(self, project_root: str, relations: Iterable[RelationRecord]) -> None:
        rows = list(relations)
        with self._connect() as conn:
            conn.execute("DELETE FROM cc_relations WHERE project_root = %s AND relation_source = 'knowledge'", (project_root,))
            for item in rows:
                conn.execute(
                    """
                    INSERT INTO cc_relations(
                        project_root, file_path, source_qualname, relation_kind,
                        target_ref, target_qualname, relation_source, relation_confidence
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        project_root, item.file_path, item.source_qualname, item.relation_kind,
                        item.target_ref, item.target_qualname, item.relation_source, item.relation_confidence,
                    ),
                )
            conn.commit()

    def replace_search_documents(self, project_root: str, documents: Iterable[dict[str, str]]) -> None:
        rows = list(documents)
        with self._connect() as conn:
            conn.execute("DELETE FROM cc_search_documents WHERE project_root = %s", (project_root,))
            for item in rows:
                conn.execute(
                    """
                    INSERT INTO cc_search_documents(project_root, doc_id, qualname, file_path, source_kind, title, text)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        project_root, item['doc_id'], item['qualname'], item['file_path'],
                        item['source_kind'], item['title'], item['text'],
                    ),
                )
            conn.commit()

    def list_search_documents(self, project_root: str) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT doc_id, qualname, file_path, source_kind, title, text FROM cc_search_documents WHERE project_root = %s ORDER BY qualname",
                (project_root,),
            ).fetchall()
        return rows

    def delete_file(self, project_root: str, file_path: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM cc_files WHERE project_root = %s AND file_path = %s", (project_root, file_path))
            conn.execute("DELETE FROM cc_symbols WHERE project_root = %s AND file_path = %s", (project_root, file_path))
            conn.execute(
                "DELETE FROM cc_relations WHERE project_root = %s AND file_path = %s AND relation_source = 'index'",
                (project_root, file_path),
            )
            conn.commit()

    def get_symbol(self, project_root: str, qualname: str) -> SymbolRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cc_symbols WHERE project_root = %s AND qualname = %s",
                (project_root, qualname),
            ).fetchone()
        return _row_to_symbol(row) if row else None

    def list_symbols(self, project_root: str) -> list[SymbolRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cc_symbols WHERE project_root = %s ORDER BY file_path, start_line",
                (project_root,),
            ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def list_symbols_in_file(self, project_root: str, file_path: str) -> list[SymbolRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cc_symbols WHERE project_root = %s AND file_path = %s ORDER BY start_line",
                (project_root, file_path),
            ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def list_symbols_by_short_name(self, project_root: str, short_name: str) -> list[SymbolRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cc_symbols WHERE project_root = %s AND name = %s ORDER BY file_path, start_line",
                (project_root, short_name),
            ).fetchall()
        return [_row_to_symbol(r) for r in rows]

    def list_outbound_relations(self, project_root: str, source_qualname: str) -> list[RelationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cc_relations WHERE project_root = %s AND source_qualname = %s ORDER BY relation_kind, COALESCE(target_qualname, target_ref)",
                (project_root, source_qualname),
            ).fetchall()
        return [_row_to_relation(r) for r in rows]

    def list_inbound_relations_for_qualname(self, project_root: str, target_qualname: str) -> list[RelationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cc_relations WHERE project_root = %s AND target_qualname = %s ORDER BY source_qualname",
                (project_root, target_qualname),
            ).fetchall()
        return [_row_to_relation(r) for r in rows]

    def list_inbound_relations_for_name(self, project_root: str, target_name: str) -> list[RelationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cc_relations WHERE project_root = %s AND target_ref = %s ORDER BY source_qualname",
                (project_root, target_name),
            ).fetchall()
        return [_row_to_relation(r) for r in rows]

    def list_relations_by_kind(self, project_root: str, relation_kind: str) -> list[RelationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cc_relations WHERE project_root = %s AND relation_kind = %s ORDER BY source_qualname",
                (project_root, relation_kind),
            ).fetchall()
        return [_row_to_relation(r) for r in rows]


def _row_to_symbol(row) -> SymbolRecord:
    return SymbolRecord(
        file_path=row['file_path'],
        module_name=row['module_name'],
        name=row['name'],
        qualname=row['qualname'],
        kind=row['kind'],
        parent_qualname=row['parent_qualname'],
        start_line=row['start_line'],
        end_line=row['end_line'],
        docstring=row['docstring'],
        source_code=row['source_code'],
    )


def _row_to_relation(row) -> RelationRecord:
    return RelationRecord(
        source_qualname=row['source_qualname'],
        relation_kind=row['relation_kind'],
        target_ref=row['target_ref'],
        target_qualname=row['target_qualname'],
        file_path=row['file_path'],
        relation_source=row['relation_source'],
        relation_confidence=row['relation_confidence'],
    )
