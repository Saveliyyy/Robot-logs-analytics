"""
Генератор «сырых» логов роботов.

Имитирует то, что приходит из системы логирования (например, Loki):
текстовые строки с телеметрией и событиями. В данные специально добавлены
типичные проблемы: дубли, битые строки, пропуски значений, мусор в полях.

Запуск:  python generate_logs.py --days 60 --robots 4
Результат: data/raw/robot_logs_YYYY-MM-DD.log (один файл на день)
"""
import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

RAW_DIR = Path(__file__).parent / "data" / "raw"

EVENTS = ["MOVE", "PICK", "PLACE", "CHARGE", "IDLE", "SCAN"]
EVENT_P = [0.35, 0.15, 0.15, 0.05, 0.2, 0.1]
ERROR_CODES = ["E101_MOTOR_STALL", "E205_LIDAR_TIMEOUT", "E310_OVERHEAT",
               "E404_PATH_BLOCKED", "E512_LOW_BATTERY"]


def robot_profile(robot_id: str) -> dict:
    """У каждого робота своя «личность»: R-03 со временем начинает перегреваться."""
    base = {"R-01": 38.0, "R-02": 40.0, "R-03": 41.0, "R-04": 39.0}
    return {"base_temp": base.get(robot_id, 40.0),
            "degrading": robot_id == "R-03"}


def make_line(ts: datetime, robot: str, level: str, event: str,
              battery, temp, latency, msg: str) -> str:
    bat = "" if battery is None else f"{battery:.1f}"
    return (f"{ts.isoformat(timespec='milliseconds')}Z robot={robot} level={level} "
            f"event={event} battery={bat} temp={temp:.1f} latency_ms={latency} "
            f'msg="{msg}"')


def generate_day(day: datetime, robots: list, day_idx: int, total_days: int,
                 rng: np.random.Generator) -> list:
    lines = []
    weekend = day.weekday() >= 5
    for robot in robots:
        prof = robot_profile(robot)
        # Нагрузка: в будни смена длиннее, на выходных роботы работают меньше
        n_events = int(rng.normal(1400 if weekend else 2600, 150))
        start = day.replace(hour=7)
        seconds = np.sort(rng.uniform(0, 14 * 3600, n_events))
        battery = 100.0
        drift = (day_idx / total_days) * 9.0 if prof["degrading"] else 0.0

        for s in seconds:
            ts = start + timedelta(seconds=float(s))
            event = rng.choice(EVENTS, p=EVENT_P)
            if event == "CHARGE":
                battery = min(100.0, battery + rng.uniform(10, 25))
            else:
                battery = max(3.0, battery - rng.uniform(0.0, 0.12))
            hour_heat = 3.0 * np.sin((ts.hour - 7) / 14 * np.pi)
            temp = prof["base_temp"] + drift + hour_heat + rng.normal(0, 1.2)
            latency = int(max(5, rng.lognormal(3.4, 0.45)))

            # Вероятность ошибки растёт с температурой и низким зарядом
            p_err = 0.004 + max(0, temp - 44) * 0.006 + (0.02 if battery < 15 else 0)
            if rng.random() < p_err:
                level = "ERROR"
                if temp > 46:
                    code = "E310_OVERHEAT"
                elif battery < 15:
                    code = "E512_LOW_BATTERY"
                else:
                    code = rng.choice(ERROR_CODES[:4])
                msg = f"{code} during {event.lower()}"
                event = "FAULT"
            elif latency > 120:
                level, msg = "WARN", "high network latency"
            else:
                level, msg = "INFO", f"{event.lower()} ok"

            bat_val = None if rng.random() < 0.01 else battery   # 1% пропусков
            line = make_line(ts, robot, level, event, bat_val, temp, latency, msg)

            r = rng.random()
            if r < 0.005:                       # 0.5% битых строк
                line = line[: rng.integers(10, len(line) // 2)]
            elif r < 0.008:                     # мусор в температуре датчика
                line = line.replace(f"temp={temp:.1f}", "temp=NaN")
            lines.append(line)
            if rng.random() < 0.02:             # 2% дублей (повторная отправка)
                lines.append(line)
    random.shuffle(lines)   # строки приходят не по порядку
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--robots", type=int, default=4)
    parser.add_argument("--start", default="2026-08-01")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    random.seed(args.seed)
    robots = [f"R-{i:02d}" for i in range(1, args.robots + 1)]
    start = datetime.fromisoformat(args.start)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    total = 0
    for d in range(args.days):
        day = start + timedelta(days=d)
        lines = generate_day(day, robots, d, args.days, rng)
        path = RAW_DIR / f"robot_logs_{day:%Y-%m-%d}.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        total += len(lines)
    print(f"Сгенерировано {total:,} строк логов за {args.days} дн. в {RAW_DIR}")


if __name__ == "__main__":
    main()
