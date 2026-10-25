"""
ETL-пайплайн: сырые логи -> очищенные события -> витрина метрик.

  Extract   читаем текстовый лог за день
  Transform разбираем строки регуляркой (pandas, без циклов), чистим данные
  Load      пишем в SQLite (warehouse.db) идемпотентно: перезапуск дня не плодит дубли

Запуск:
  python etl.py               # обработать все дни из data/raw
  python etl.py --date 2026-08-15
"""
import argparse
import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).parent
RAW_DIR = BASE / "data" / "raw"
DB_PATH = BASE / "data" / "warehouse.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("etl")

LINE_RE = (
    r"^(?P<ts>\S+Z) robot=(?P<robot_id>R-\d+) level=(?P<level>\w+) "
    r"event=(?P<event>\w+) battery=(?P<battery>[\d.]*) temp=(?P<temp>\S+) "
    r'latency_ms=(?P<latency_ms>\d+) msg="(?P<msg>[^"]*)"$'
)


# ---------- Extract ----------
def extract(day: str) -> pd.Series:
    path = RAW_DIR / f"robot_logs_{day}.log"
    lines = pd.Series(path.read_text(encoding="utf-8").splitlines(), name="line")
    log.info("extract %s: %d строк, %.1f МБ", day, len(lines), path.stat().st_size / 1e6)
    return lines


# ---------- Transform ----------
def transform(lines: pd.Series) -> tuple[pd.DataFrame, dict]:
    stats = {"raw_lines": len(lines)}

    # 1. Дубли: одинаковые строки, отправленные повторно
    lines = lines.drop_duplicates()
    stats["duplicates"] = stats["raw_lines"] - len(lines)

    # 2. Разбор строк регуляркой; битые строки не совпадут с шаблоном
    df = lines.str.extract(LINE_RE)
    bad = df["ts"].isna()
    stats["malformed"] = int(bad.sum())
    df = df[~bad].copy()

    # 3. Типы
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce").dt.tz_localize(None)
    df["battery"] = pd.to_numeric(df["battery"], errors="coerce")
    df["temp"] = pd.to_numeric(df["temp"], errors="coerce")
    df["latency_ms"] = pd.to_numeric(df["latency_ms"], errors="coerce").astype("Int64")

    # 4. Мусор датчика: физически невозможная температура -> NaN
    df.loc[~df["temp"].between(-20, 120), "temp"] = np.nan
    stats["bad_temp"] = int(df["temp"].isna().sum())

    # 5. Пропуски заряда: протягиваем последнее известное значение внутри робота
    df = df.sort_values(["robot_id", "ts"])
    stats["missing_battery"] = int(df["battery"].isna().sum())
    df["battery"] = df.groupby("robot_id")["battery"].ffill()
    df["temp"] = df.groupby("robot_id")["temp"].ffill()

    # 6. Код ошибки из сообщения
    df["error_code"] = df["msg"].str.extract(r"^(E\d{3}_\w+)", expand=False)

    df = df.drop(columns=["msg"]).reset_index(drop=True)
    stats["clean_rows"] = len(df)
    log.info("transform: %s", stats)
    return df, stats


# ---------- Load ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts          TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    robot_id    TEXT NOT NULL,
    level       TEXT NOT NULL,
    event       TEXT NOT NULL,
    battery     REAL,
    temp        REAL,
    latency_ms  INTEGER,
    error_code  TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_date_robot ON events(event_date, robot_id);

CREATE TABLE IF NOT EXISTS daily_robot_metrics (
    event_date      TEXT NOT NULL,
    robot_id        TEXT NOT NULL,
    events          INTEGER,
    errors          INTEGER,
    warnings        INTEGER,
    error_rate_pct  REAL,
    avg_temp        REAL,
    max_temp        REAL,
    min_battery     REAL,
    p95_latency_ms  REAL,
    PRIMARY KEY (event_date, robot_id)
);

CREATE TABLE IF NOT EXISTS etl_runs (
    event_date TEXT PRIMARY KEY,
    raw_lines INTEGER, duplicates INTEGER, malformed INTEGER,
    bad_temp INTEGER, missing_battery INTEGER, clean_rows INTEGER,
    loaded_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def load(df: pd.DataFrame, stats: dict, day: str) -> None:
    out = df.copy()
    out["event_date"] = day
    out["ts"] = out["ts"].dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3]
    cols = ["ts", "event_date", "robot_id", "level", "event",
            "battery", "temp", "latency_ms", "error_code"]
    with get_conn() as conn:
        conn.execute("DELETE FROM events WHERE event_date = ?", (day,))  # идемпотентность
        out[cols].to_sql("events", conn, if_exists="append", index=False)
        conn.execute("INSERT OR REPLACE INTO etl_runs (event_date, raw_lines, duplicates, "
                     "malformed, bad_temp, missing_battery, clean_rows) VALUES (?,?,?,?,?,?,?)",
                     (day, stats["raw_lines"], stats["duplicates"], stats["malformed"],
                      stats["bad_temp"], stats["missing_battery"], stats["clean_rows"]))
    log.info("load %s: %d строк в events", day, len(out))


def check_quality(day: str, min_rows: int = 1000, max_malformed_pct: float = 2.0) -> None:
    """Проверка качества: если данных подозрительно мало или много битых строк — падаем."""
    with get_conn() as conn:
        row = conn.execute("SELECT raw_lines, malformed, clean_rows FROM etl_runs "
                           "WHERE event_date = ?", (day,)).fetchone()
    if row is None:
        raise ValueError(f"{day}: нет записи о загрузке")
    raw, malformed, clean = row
    if clean < min_rows:
        raise ValueError(f"{day}: всего {clean} строк, ожидалось >= {min_rows}")
    if malformed / raw * 100 > max_malformed_pct:
        raise ValueError(f"{day}: битых строк {malformed / raw:.1%}")
    log.info("quality %s: OK (%d строк)", day, clean)


def build_mart(day: str) -> None:
    """Витрина: дневные метрики по каждому роботу. p95 считаем в pandas."""
    with get_conn() as conn:
        df = pd.read_sql("SELECT * FROM events WHERE event_date = ?", conn, params=(day,))
        mart = (df.groupby("robot_id")
                  .agg(events=("event", "size"),
                       errors=("level", lambda s: (s == "ERROR").sum()),
                       warnings=("level", lambda s: (s == "WARN").sum()),
                       avg_temp=("temp", "mean"),
                       max_temp=("temp", "max"),
                       min_battery=("battery", "min"),
                       p95_latency_ms=("latency_ms", lambda s: np.percentile(s, 95)))
                  .reset_index())
        mart["error_rate_pct"] = (mart["errors"] / mart["events"] * 100).round(3)
        mart["event_date"] = day
        mart = mart.round({"avg_temp": 2, "max_temp": 2, "min_battery": 1})
        conn.execute("DELETE FROM daily_robot_metrics WHERE event_date = ?", (day,))
        mart.to_sql("daily_robot_metrics", conn, if_exists="append", index=False)
    log.info("mart %s: %d роботов", day, len(mart))


def run_day(day: str) -> dict:
    lines = extract(day)
    df, stats = transform(lines)
    load(df, stats, day)
    check_quality(day)
    build_mart(day)
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD; по умолчанию все дни")
    args = parser.parse_args()

    days = [args.date] if args.date else sorted(
        p.stem.replace("robot_logs_", "") for p in RAW_DIR.glob("robot_logs_*.log"))
    if not days:
        raise SystemExit("Нет сырых логов. Сначала запустите: python generate_logs.py")

    totals = {}
    for day in days:
        for k, v in run_day(day).items():
            totals[k] = totals.get(k, 0) + v

    raw_mb = sum(p.stat().st_size for p in RAW_DIR.glob("*.log")) / 1e6
    print("\nИтог ETL")
    print(f"  дней обработано:     {len(days)}")
    print(f"  строк на входе:      {totals['raw_lines']:,}")
    print(f"  удалено дублей:      {totals['duplicates']:,}")
    print(f"  битых строк:         {totals['malformed']:,}")
    print(f"  мусор в temp:        {totals['bad_temp']:,}")
    print(f"  пропуски battery:    {totals['missing_battery']:,} (заполнены ffill)")
    print(f"  чистых строк:        {totals['clean_rows']:,}")
    print(f"  сырые логи:          {raw_mb:.1f} МБ")
    print(f"  база warehouse.db:   {DB_PATH.stat().st_size / 1e6:.1f} МБ")


if __name__ == "__main__":
    main()
