"""
Запуск всего проекта одной командой (без Airflow):
    python run_all.py
"""
import subprocess
import sys

STEPS = [
    ("Генерация сырых логов", ["generate_logs.py"]),
    ("ETL: очистка и загрузка в БД", ["etl.py"]),
    ("SQL-аналитика и оптимизация", ["run_sql.py"]),
    ("Прогноз ошибок (scikit-learn)", ["forecast.py"]),
    ("Excel-отчёт", ["report_excel.py"]),
]

for title, args in STEPS:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}", flush=True)
    subprocess.run([sys.executable, *args], check=True)

print("\nГотово. Результаты в папке output/")
