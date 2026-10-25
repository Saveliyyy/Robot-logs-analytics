"""
Выполняет аналитические запросы из sql/analysis.sql и показывает оптимизацию на индексе.

Запуск:  python run_sql.py
Результат: output/sql/<имя_запроса>.csv
"""
import re
import sqlite3
import time
from pathlib import Path

import pandas as pd

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "warehouse.db"
SQL_FILE = BASE / "sql" / "analysis.sql"
OUT_DIR = BASE / "output" / "sql"

pd.set_option("display.width", 140)
pd.set_option("display.max_columns", 12)


def load_queries(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"^-- name:\s*(\S+)\s*$", text, flags=re.MULTILINE)
    # parts = [шапка, имя1, sql1, имя2, sql2, ...]
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def run_queries(conn: sqlite3.Connection) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, sql in load_queries(SQL_FILE).items():
        t0 = time.perf_counter()
        df = pd.read_sql(sql, conn)
        ms = (time.perf_counter() - t0) * 1000
        df.to_csv(OUT_DIR / f"{name}.csv", index=False)
        print(f"\n=== {name}  ({len(df)} строк, {ms:.0f} мс)")
        print(df.head(8).to_string(index=False))


def timed(conn: sqlite3.Connection, sql: str, params=(), repeat: int = 5) -> float:
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        conn.execute(sql, params).fetchall()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


def optimization_demo(conn: sqlite3.Connection) -> None:
    """Ищем все ошибки перегрева одного робота: без индекса и с составным индексом."""
    sql = ("SELECT ts, temp FROM events "
           "WHERE robot_id = ? AND error_code = 'E310_OVERHEAT' ORDER BY ts")
    params = ("R-03",)
    conn.execute("DROP INDEX IF EXISTS ix_events_robot_code_ts")

    plan_before = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    t_before = timed(conn, sql, params)

    conn.execute("CREATE INDEX ix_events_robot_code_ts ON events(robot_id, error_code, ts)")
    conn.execute("ANALYZE")
    plan_after = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    t_after = timed(conn, sql, params)

    print("\n=== Оптимизация: поиск перегревов робота R-03")
    print("  план без индекса: ", "; ".join(r[-1] for r in plan_before))
    print(f"  время без индекса: {t_before:.1f} мс")
    print("  план с индексом:  ", "; ".join(r[-1] for r in plan_after))
    print(f"  время с индексом:  {t_after:.1f} мс  (быстрее в {t_before / t_after:.0f} раз)")


def main():
    with sqlite3.connect(DB_PATH) as conn:
        run_queries(conn)
        optimization_demo(conn)
    print(f"\nCSV с результатами: {OUT_DIR}")


if __name__ == "__main__":
    main()
