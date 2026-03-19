from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from codecollector.domain.models import RelationRecord, SymbolRecord


class SQLiteIndexStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    project_root TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    PRIMARY KEY (project_root, file_path)
                );

                CREATE TABLE IF NOT EXISTS symbols (
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
                );

                CREATE TABLE IF NOT EXISTS relations (
                    project_root TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    source_qualname TEXT NOT NULL,
                    relation_kind TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    target_qualname TEXT,
                    relation_source TEXT NOT NULL DEFAULT 'index',
                    relation_confidence TEXT NOT NULL DEFAULT 'medium'
                );
                CREATE TABLE IF NOT EXISTS search_documents (
                    project_root TEXT NOT NULL,
                    doc_id TEXT NOT NULL PRIMARY KEY,
                    qualname TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL
                );
                """
            )
            self._ensure_relation_column(conn, 'target_qualname', 'TEXT')
            self._ensure_relation_column(conn, 'relation_source', "TEXT NOT NULL DEFAULT 'index'")
            self._ensure_relation_column(conn, 'relation_confidence', "TEXT NOT NULL DEFAULT 'medium'")
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(project_root, name);
                CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(project_root, file_path);
                CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(project_root, source_qualname);
                CREATE INDEX IF NOT EXISTS idx_relations_target_ref ON relations(project_root, target_ref);
                CREATE INDEX IF NOT EXISTS idx_relations_target_qualname ON relations(project_root, target_qualname);
                CREATE INDEX IF NOT EXISTS idx_relations_kind ON relations(project_root, relation_kind);
                CREATE INDEX IF NOT EXISTS idx_search_documents_qualname ON search_documents(project_root, qualname);
                """
            )

    def _ensure_relation_column(self, conn: sqlite3.Connection, column_name: str, column_type: str) -> None:
        columns = {row['name'] for row in conn.execute("PRAGMA table_info(relations)").fetchall()}
        if column_name not in columns:
            conn.execute(f"ALTER TABLE relations ADD COLUMN {column_name} {column_type}")

    def get_known_files(self, project_root: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT file_path, file_hash FROM files WHERE project_root = ?",
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
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO files(project_root, file_path, file_hash, module_name) VALUES (?, ?, ?, ?)",
                (project_root, file_path, file_hash, module_name),
            )
            conn.execute(
                "DELETE FROM symbols WHERE project_root = ? AND file_path = ?",
                (project_root, file_path),
            )
            conn.execute(
                "DELETE FROM relations WHERE project_root = ? AND file_path = ? AND relation_source = 'index'",
                (project_root, file_path),
            )
            conn.executemany(
                """
                INSERT OR REPLACE INTO symbols(
                    project_root, file_path, module_name, name, qualname, kind,
                    parent_qualname, start_line, end_line, docstring, source_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_root,
                        item.file_path,
                        item.module_name,
                        item.name,
                        item.qualname,
                        item.kind,
                        item.parent_qualname,
                        item.start_line,
                        item.end_line,
                        item.docstring,
                        item.source_code,
                    )
                    for item in symbols
                ],
            )
            conn.executemany(
                """
                INSERT INTO relations(
                    project_root, file_path, source_qualname, relation_kind,
                    target_ref, target_qualname, relation_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_root,
                        item.file_path,
                        item.source_qualname,
                        item.relation_kind,
                        item.target_ref,
                        item.target_qualname,
                        item.relation_source,
                    )
                    for item in relations
                ],
            )

    def replace_knowledge_relations(self, project_root: str, relations: Iterable[RelationRecord]) -> None:
        rows = list(relations)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM relations WHERE project_root = ? AND relation_source = 'knowledge'",
                (project_root,),
            )
            conn.executemany(
                """
                INSERT INTO relations(
                    project_root, file_path, source_qualname, relation_kind,
                    target_ref, target_qualname, relation_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_root,
                        item.file_path,
                        item.source_qualname,
                        item.relation_kind,
                        item.target_ref,
                        item.target_qualname,
                        item.relation_source,
                    )
                    for item in rows
                ],
            )


    def replace_search_documents(self, project_root: str, documents: Iterable[dict[str, str]]) -> None:
        rows = list(documents)
        with self._connect() as conn:
            conn.execute("DELETE FROM search_documents WHERE project_root = ?", (project_root,))
            conn.executemany(
                """
                INSERT INTO search_documents(project_root, doc_id, qualname, file_path, source_kind, title, text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_root,
                        item['doc_id'],
                        item['qualname'],
                        item['file_path'],
                        item['source_kind'],
                        item['title'],
                        item['text'],
                    )
                    for item in rows
                ],
            )

    def list_search_documents(self, project_root: str) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT doc_id, qualname, file_path, source_kind, title, text FROM search_documents WHERE project_root = ? ORDER BY qualname",
                (project_root,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_file(self, project_root: str, file_path: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM files WHERE project_root = ? AND file_path = ?", (project_root, file_path))
            conn.execute("DELETE FROM symbols WHERE project_root = ? AND file_path = ?", (project_root, file_path))
            conn.execute(
                "DELETE FROM relations WHERE project_root = ? AND file_path = ? AND relation_source = 'index'",
                (project_root, file_path),
            )

    def get_symbol(self, project_root: str, qualname: str) -> SymbolRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM symbols WHERE project_root = ? AND qualname = ?",
                (project_root, qualname),
            ).fetchone()
        return _row_to_symbol(row) if row else None

    def list_symbols(self, project_root: str) -> list[SymbolRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM symbols WHERE project_root = ? ORDER BY file_path, start_line",
                (project_root,),
            ).fetchall()
        return [_row_to_symbol(row) for row in rows]

    def list_symbols_in_file(self, project_root: str, file_path: str) -> list[SymbolRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM symbols WHERE project_root = ? AND file_path = ? ORDER BY start_line",
                (project_root, file_path),
            ).fetchall()
        return [_row_to_symbol(row) for row in rows]

    def list_symbols_by_short_name(self, project_root: str, short_name: str) -> list[SymbolRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM symbols WHERE project_root = ? AND name = ? ORDER BY file_path, start_line",
                (project_root, short_name),
            ).fetchall()
        return [_row_to_symbol(row) for row in rows]

    def list_outbound_relations(self, project_root: str, source_qualname: str) -> list[RelationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM relations WHERE project_root = ? AND source_qualname = ? ORDER BY relation_kind, COALESCE(target_qualname, target_ref)",
                (project_root, source_qualname),
            ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def list_inbound_relations_for_qualname(self, project_root: str, target_qualname: str) -> list[RelationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM relations WHERE project_root = ? AND target_qualname = ? ORDER BY source_qualname",
                (project_root, target_qualname),
            ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def list_inbound_relations_for_name(self, project_root: str, target_name: str) -> list[RelationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM relations WHERE project_root = ? AND target_ref = ? ORDER BY source_qualname",
                (project_root, target_name),
            ).fetchall()
        return [_row_to_relation(row) for row in rows]

    def list_relations_by_kind(self, project_root: str, relation_kind: str) -> list[RelationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM relations WHERE project_root = ? AND relation_kind = ? ORDER BY source_qualname",
                (project_root, relation_kind),
            ).fetchall()
        return [_row_to_relation(row) for row in rows]


def _row_to_symbol(row: sqlite3.Row) -> SymbolRecord:
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


def _row_to_relation(row: sqlite3.Row) -> RelationRecord:
    return RelationRecord(
        source_qualname=row['source_qualname'],
        relation_kind=row['relation_kind'],
        target_ref=row['target_ref'],
        target_qualname=row['target_qualname'],
        file_path=row['file_path'],
        relation_source=row['relation_source'],
        relation_confidence=row['relation_confidence'],
    )
