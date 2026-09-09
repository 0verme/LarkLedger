"""Benchmark the PostgreSQL access paths used by the web Entries page.

The benchmark creates a disposable schema and never touches the application's
``ledger_entries`` table.  By default it only accepts a loopback PostgreSQL
URL; pass ``--allow-remote`` only when the explicitly supplied database is a
separate benchmark database owned by the operator.

Example::

    $env:ENTRIES_BENCHMARK_DATABASE_URL = \
        "postgresql://benchmark:benchmark-only@127.0.0.1:55439/lark_ledger_audit"
    python scripts/benchmark_entries_query.py --include-million \
        --output docs/performance/entries-query-benchmark.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DEFAULT_SIZES = (10_000, 100_000)
MILLION = 1_000_000
PAGE_SIZE = 25
LEDGER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_OPEN_ID = "benchmark-user"
BASE_TIME = datetime(2030, 1, 1, tzinfo=UTC)
CATEGORIES = ("餐饮", "交通", "住房", "购物", "医疗", "教育", "娱乐", "其他")


@dataclass(frozen=True, slots=True)
class QueryCase:
    name: str
    where: str
    args: tuple[Any, ...]
    offset: int = 0
    include_list: bool = True
    use_scope_union: bool = True


@dataclass(frozen=True, slots=True)
class ExplainSummary:
    planning_ms: float
    execution_ms: float
    plan: str
    rows_scanned: int
    buffers: int
    sort: str


@dataclass(frozen=True, slots=True)
class ResultRow:
    dataset_size: int
    case: str
    operation: str
    summary: ExplainSummary


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _normalize_database_url(value: str) -> str:
    if value.startswith("postgresql+asyncpg://"):
        return "postgresql://" + value.removeprefix("postgresql+asyncpg://")
    return value


def _validate_database_url(value: str, allow_remote: bool) -> str:
    normalized = _normalize_database_url(value)
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise ValueError("database URL must be a PostgreSQL URL")
    if not allow_remote and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "refusing non-loopback database URL; use a disposable local database "
            "or pass --allow-remote explicitly"
        )
    return normalized


def _short_id(value: int) -> str:
    """Encode a stable integer as a five-character Crockford short id."""
    chars = [ALPHABET[0]] * 5
    for index in range(4, -1, -1):
        value, remainder = divmod(value, len(ALPHABET))
        chars[index] = ALPHABET[remainder]
    return "".join(chars)


def _benchmark_rows(size: int) -> Iterable[tuple[Any, ...]]:
    for index in range(size):
        occurred_at = BASE_TIME - timedelta(minutes=index)
        yield (
            index + 1,
            USER_OPEN_ID,
            None if index == size - 1 else LEDGER_ID,
            _short_id(index + 1),
            f"{(index % 997) + 1}.00",
            "EXPENSE" if index % 2 else "INCOME",
            CATEGORIES[index % len(CATEGORIES)],
            "benchmark-note-needle" if index == 42 else f"entry note {index}",
            occurred_at,
            "text" if index % 4 else "image",
            BASE_TIME + timedelta(seconds=index),
            BASE_TIME + timedelta(seconds=index),
            occurred_at if index % 23 == 0 else None,
        )


def _table_name(schema: str) -> str:
    return f'"{schema}"."ledger_entries"'


async def _create_schema(
    connection: asyncpg.Connection,
    schema: str,
    *,
    include_search_indexes: bool,
) -> None:
    table = _table_name(schema)
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(
        f"""
        CREATE TABLE {table} (
            id bigint PRIMARY KEY,
            user_open_id varchar(128) NOT NULL,
            ledger_id uuid NULL,
            short_id varchar(5) NOT NULL,
            amount numeric(14, 2) NOT NULL,
            direction varchar(8) NOT NULL,
            category varchar(64) NOT NULL,
            note text NOT NULL,
            occurred_at timestamptz NOT NULL,
            source_type varchar(16) NOT NULL,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            deleted_at timestamptz NULL
        )
        """
    )
    await connection.execute(
        f'CREATE INDEX "ix_entries_ledger_occurred" ON {table} (ledger_id, occurred_at)'
    )
    await connection.execute(
        f'CREATE INDEX "ix_entries_ledger_category" ON {table} (ledger_id, category)'
    )
    await connection.execute(
        f'CREATE UNIQUE INDEX "uq_entries_ledger_short_id" ON {table} (ledger_id, short_id)'
    )
    await connection.execute(
        f'CREATE INDEX "ix_entries_user_occurred" ON {table} (user_open_id, occurred_at)'
    )
    await connection.execute(
        f'CREATE INDEX "ix_entries_user_category" ON {table} (user_open_id, category)'
    )
    if include_search_indexes:
        await connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for name, column in (
            ("ix_entries_note_trgm", "note"),
            ("ix_entries_category_trgm", "category"),
            ("ix_entries_short_id_trgm", "short_id"),
        ):
            await connection.execute(
                f'CREATE INDEX "{name}" ON {table} USING gin ({column} gin_trgm_ops)'
            )


async def _load_dataset(
    connection: asyncpg.Connection, schema: str, size: int
) -> None:
    table = _table_name(schema)
    await connection.execute(f"TRUNCATE TABLE {table}")
    await connection.copy_records_to_table(
        "ledger_entries",
        schema_name=schema,
        records=_benchmark_rows(size),
        columns=[
            "id",
            "user_open_id",
            "ledger_id",
            "short_id",
            "amount",
            "direction",
            "category",
            "note",
            "occurred_at",
            "source_type",
            "created_at",
            "updated_at",
            "deleted_at",
        ],
    )
    await connection.execute(f"ANALYZE {table}")


def _cases() -> tuple[QueryCase, ...]:
    target_short_id = _short_id(43)
    active_where = "deleted_at IS NULL"
    cases: list[QueryCase] = [
        QueryCase("default_page", active_where, ()),
        QueryCase(
            "page_2",
            active_where,
            (),
            offset=(2 - 1) * PAGE_SIZE,
        ),
        QueryCase(
            "page_10",
            active_where,
            (),
            offset=(10 - 1) * PAGE_SIZE,
        ),
        QueryCase(
            "page_100",
            active_where,
            (),
            offset=(100 - 1) * PAGE_SIZE,
        ),
        QueryCase(
            "page_1000",
            active_where,
            (),
            offset=(1000 - 1) * PAGE_SIZE,
        ),
        QueryCase(
            "page_10000",
            active_where,
            (),
            offset=(10000 - 1) * PAGE_SIZE,
        ),
        QueryCase(
            "date_range",
            active_where + " AND occurred_at >= $3 AND occurred_at < $4",
            (BASE_TIME - timedelta(days=30), BASE_TIME),
        ),
        QueryCase(
            "direction_filter",
            active_where + " AND direction = $3",
            ("EXPENSE",),
        ),
        QueryCase(
            "category_filter",
            active_where + " AND category = $3",
            ("餐饮",),
        ),
        QueryCase(
            "short_id_current_ilike",
            active_where + " AND ("
            "note ILIKE '%' || $3 || '%' OR "
            "category ILIKE '%' || $3 || '%' OR "
            "short_id ILIKE '%' || $3 || '%'"
            ")",
            (target_short_id,),
            use_scope_union=False,
        ),
        QueryCase(
            "short_id_normalized_exact",
            active_where + " AND short_id = $3",
            (target_short_id,),
        ),
        QueryCase(
            "note_fuzzy",
            active_where + " AND ("
            "note ILIKE '%' || $3 || '%' OR "
            "category ILIKE '%' || $3 || '%' OR "
            "short_id ILIKE '%' || $3 || '%'"
            ")",
            ("needle",),
            use_scope_union=False,
        ),
        QueryCase("count_total", active_where, (), include_list=False),
    ]
    return tuple(cases)


async def _explain(
    connection: asyncpg.Connection,
    sql: str,
    args: Sequence[Any],
) -> ExplainSummary:
    raw = await connection.fetchval(
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", *args
    )
    document = json.loads(raw) if isinstance(raw, str) else raw
    root = document[0]
    plan_root = root["Plan"]
    nodes: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        nodes.append(node)
        for child in node.get("Plans", []):
            visit(child)

    visit(plan_root)
    scan_nodes = {
        "Seq Scan",
        "Index Scan",
        "Index Only Scan",
        "Bitmap Heap Scan",
        "Bitmap Index Scan",
    }
    plan_parts: list[str] = []
    rows_scanned = 0
    buffers = 0
    sort_methods: list[str] = []
    for node in nodes:
        node_type = str(node.get("Node Type", "unknown"))
        index_name = node.get("Index Name")
        plan_parts.append(
            f"{node_type} using {index_name}" if index_name else node_type
        )
        if node_type in scan_nodes:
            loops = int(node.get("Actual Loops", 1))
            rows_scanned += (
                int(node.get("Actual Rows", 0))
                + int(node.get("Rows Removed by Filter", 0))
            ) * loops
        buffers += sum(
            int(node.get(key, 0))
            for key in (
                "Shared Hit Blocks",
                "Shared Read Blocks",
                "Shared Dirtied Blocks",
                "Shared Written Blocks",
            )
        )
        sort_method = node.get("Sort Method")
        if sort_method:
            detail = str(sort_method)
            if node.get("Sort Space Used") is not None:
                detail += f" ({node['Sort Space Used']}kB {node.get('Sort Space Type', '')})"
            sort_methods.append(detail.strip())
    return ExplainSummary(
        planning_ms=float(root.get("Planning Time", 0.0)),
        execution_ms=float(root.get("Execution Time", 0.0)),
        plan=" → ".join(plan_parts),
        rows_scanned=rows_scanned,
        buffers=buffers,
        sort="; ".join(sort_methods) or "—",
    )


async def _run_case(
    connection: asyncpg.Connection,
    schema: str,
    size: int,
    case: QueryCase,
) -> list[ResultRow]:
    table = _table_name(schema)
    scope_where = "(ledger_id = $1 OR (ledger_id IS NULL AND user_open_id = $2))"
    common_args = (LEDGER_ID, USER_OPEN_ID, *case.args)
    count_sql = f"SELECT count(*) FROM {table} WHERE {scope_where} AND {case.where}"
    results = [
        ResultRow(
            size,
            case.name,
            "count",
            await _explain(connection, count_sql, common_args),
        )
    ]
    if not case.include_list:
        return results

    list_args = (*common_args, case.offset)
    offset_parameter = len(list_args)
    current_sql = (
        f"SELECT id, short_id, amount, direction, category, note, occurred_at "
        f"FROM {table} WHERE {scope_where} AND {case.where} "
        f"ORDER BY occurred_at DESC, id DESC LIMIT {PAGE_SIZE} OFFSET ${offset_parameter}"
    )
    results.append(
        ResultRow(
            size,
            f"{case.name}_or",
            "page",
            await _explain(connection, current_sql, list_args),
        )
    )
    if not case.use_scope_union:
        return results

    branch_limit = case.offset + PAGE_SIZE
    union_sql = (
        f"(SELECT id, short_id, amount, direction, category, note, occurred_at "
        f"FROM {table} WHERE ledger_id = $1 AND {case.where} "
        f"ORDER BY occurred_at DESC, id DESC LIMIT {branch_limit}) "
        "UNION ALL "
        f"(SELECT id, short_id, amount, direction, category, note, occurred_at "
        f"FROM {table} WHERE ledger_id IS NULL AND user_open_id = $2 "
        f"AND {case.where} ORDER BY occurred_at DESC, id DESC LIMIT {branch_limit})"
    )
    optimized_sql = (
        "SELECT id, short_id, amount, direction, category, note, occurred_at "
        f"FROM ({union_sql}) AS scoped "
        f"ORDER BY occurred_at DESC, id DESC LIMIT {PAGE_SIZE} OFFSET ${offset_parameter}"
    )
    results.append(
        ResultRow(
            size,
            f"{case.name}_union",
            "page",
            await _explain(connection, optimized_sql, list_args),
        )
    )
    return results


def _markdown(
    results: Sequence[ResultRow],
    sizes: Sequence[int],
    schema: str,
    *,
    include_search_indexes: bool,
) -> str:
    lines = [
        "# Entries 查询 benchmark",
        "",
        "本报告由 `scripts/benchmark_entries_query.py` 生成。数据写入随机 disposable schema，"
        "默认运行结束后删除；它不使用应用的 `ledger_entries` 表。",
        "",
        f"- PostgreSQL schema: `{schema}`（报告生成时的 disposable schema）",
        f"- 数据集：{', '.join(f'{size:,}' for size in sizes)} 条流水；"
        "每个数据集含 1 条 legacy scope fallback 行",
        f"- page_size：`{PAGE_SIZE}`；默认 active 条件：`deleted_at IS NULL`",
        f"- search GIN indexes：`{'enabled' if include_search_indexes else 'disabled'}`",
        "- 计时：`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 的 `Planning Time` / "
        "`Execution Time`；本地 Docker 结果不构成 CI SLA。",
        "",
        "## 结果摘要",
        "",
        "| 数据量 | 场景 | 操作 | Planning ms | Execution ms | Plan | "
        "rows scanned（估算） | buffers | sort |",
        "|---:|---|---|---:|---:|---|---:|---:|---|",
    ]
    for result in results:
        summary = result.summary
        lines.append(
            f"| {result.dataset_size:,} | `{result.case}` | `{result.operation}` | "
            f"{summary.planning_ms:.3f} | {summary.execution_ms:.3f} | "
            f"`{summary.plan}` | {summary.rows_scanned:,} | {summary.buffers:,} | "
            f"`{summary.sort}` |"
        )
    lines.extend(
        [
            "",
            "## 解读",
            "",
            "- `*_or` 是原有的 `(ledger_id = ... OR legacy fallback)` 查询形态；"
            "`*_union` 是默认 occurred_at 排序的优化形态：分别对现代/legacy 分支取 "
            "bounded top-N 后再合并。模糊搜索保留 `*_or`，避免无选择性时重复扫描。",
            "- `count` 仍保留完整 scope 条件并遍历满足账本/删除状态的行；"
            "它的成本与页面读取分开评估。",
            "- `short_id_current_ilike` 与 `note_fuzzy` 故意保留 `%keyword%` 形态；"
            "启用 GIN trigram indexes 后，三字段 OR 可走 BitmapOr，"
            "`--without-search-indexes` 可复现无搜索索引的 Seq Scan。",
            "- `rows scanned` 是 EXPLAIN 节点 `Actual Rows + Rows Removed by Filter` 的汇总，"
            "用于定位风险，不是 PostgreSQL 的独立计费指标。",
            "",
        ]
    )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> str:
    database_url = _validate_database_url(args.database_url, args.allow_remote)
    sizes = list(dict.fromkeys(args.sizes + ([MILLION] if args.include_million else [])))
    schema = f"entries_bench_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    results: list[ResultRow] = []
    connection = await asyncpg.connect(database_url)
    try:
        await _create_schema(
            connection,
            schema,
            include_search_indexes=not args.without_search_indexes,
        )
        for size in sizes:
            print(f"loading {size:,} rows into disposable schema {schema}...", file=sys.stderr)
            await _load_dataset(connection, schema, size)
            for case in _cases():
                results.extend(await _run_case(connection, schema, size, case))
    finally:
        if not args.keep_schema:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
    report = _markdown(
        results,
        sizes,
        schema,
        include_search_indexes=not args.without_search_indexes,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("ENTRIES_BENCHMARK_DATABASE_URL"),
        help="disposable PostgreSQL URL (or ENTRIES_BENCHMARK_DATABASE_URL)",
    )
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=_positive_int,
        default=list(DEFAULT_SIZES),
        help="rows per dataset (default: 10000 100000)",
    )
    parser.add_argument(
        "--include-million",
        action="store_true",
        help="also benchmark a 1,000,000-row dataset",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback database URL; use only for an isolated benchmark database",
    )
    parser.add_argument(
        "--keep-schema",
        action="store_true",
        help="keep the disposable schema for manual inspection",
    )
    parser.add_argument(
        "--without-search-indexes",
        action="store_true",
        help="skip pg_trgm GIN indexes; useful for a before/after comparison",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the Markdown report to this path",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.database_url:
        print(
            "error: pass --database-url or set ENTRIES_BENCHMARK_DATABASE_URL; "
            "the benchmark will not guess an application database",
            file=sys.stderr,
        )
        return 2
    try:
        report = asyncio.run(_run(args))
    except (OSError, ValueError, asyncpg.PostgresError) as exc:
        print(f"error: benchmark failed: {exc}", file=sys.stderr)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
