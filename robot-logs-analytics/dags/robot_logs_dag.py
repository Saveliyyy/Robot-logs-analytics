"""
Airflow DAG: ежедневная обработка логов роботов.

    etl_load -> quality_check -> build_mart -> forecast -> excel_report

Каждый запуск обрабатывает день {{ ds }} (logical date). Загрузка идемпотентна:
повторный запуск или backfill за прошлые даты не создаёт дублей.

Backfill за весь демо-период:
    airflow dags backfill robot_logs_daily -s 2026-08-01 -e 2026-09-29      # Airflow 2.x
    airflow backfill create --dag-id robot_logs_daily \
        --from-date 2026-08-01 --to-date 2026-09-29                         # Airflow 3.x
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:                                    # Airflow 3.x
    from airflow.sdk import dag, task
except ImportError:                     # Airflow 2.x
    from airflow.decorators import dag, task

# Папка проекта: в docker-compose она смонтирована в /opt/airflow/project
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

default_args = {
    "owner": "analytics",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


@dag(
    dag_id="robot_logs_daily",
    description="Логи роботов: ETL, витрина, прогноз ошибок, Excel-отчёт",
    schedule="0 6 * * *",               # каждый день в 06:00 UTC
    start_date=datetime(2026, 8, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["robots", "etl"],
)
def robot_logs_daily():

    @task
    def etl_load(ds=None) -> dict:
        import etl
        lines = etl.extract(ds)
        df, stats = etl.transform(lines)
        etl.load(df, stats, ds)
        return stats                    # уходит в XCom, видно в интерфейсе

    @task
    def quality_check(stats: dict, ds=None) -> None:
        import etl
        etl.check_quality(ds)           # падает с ошибкой, если данных мало или много мусора

    @task
    def build_mart(ds=None) -> None:
        import etl
        etl.build_mart(ds)

    @task
    def forecast() -> None:
        import forecast as fc
        fc.main()

    @task
    def excel_report() -> None:
        import report_excel
        report_excel.main()

    stats = etl_load()
    quality_check(stats) >> build_mart() >> forecast() >> excel_report()


robot_logs_daily()
