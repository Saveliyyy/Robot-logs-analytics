"""
Прогноз числа ошибок робота на следующий день (scikit-learn).

Идея для бизнеса: если завтра у робота ожидается много ошибок,
его стоит заранее отправить на обслуживание, а не ждать поломки в смену.

Подход:
  * признаки: лаги ошибок (вчера, неделю назад), скользящее среднее,
    температура вчера, день недели, робот;
  * честная валидация на временном ряду: TimeSeriesSplit + отложенные последние 14 дней;
  * сравнение с наивными прогнозами «как вчера» и «как неделю назад».

Запуск:  python forecast.py
Результат: output/forecast_test.csv, output/forecast_next_day.csv, output/forecast.png
"""
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "warehouse.db"
OUT = BASE / "output"
TEST_DAYS = 14


def load_daily() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql("SELECT event_date, robot_id, events, errors, avg_temp, max_temp "
                         "FROM daily_robot_metrics ORDER BY robot_id, event_date", conn,
                         parse_dates=["event_date"])
    return df


def make_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["robot_id", "event_date"]).copy()
    g = df.groupby("robot_id")
    # Все признаки — только из прошлого (shift), чтобы не «заглядывать в будущее»
    df["err_lag1"] = g["errors"].shift(1)
    df["err_lag7"] = g["errors"].shift(7)
    df["err_roll7"] = g["errors"].transform(lambda s: s.shift(1).rolling(7).mean())
    df["err_trend"] = df["err_roll7"] - g["errors"].transform(lambda s: s.shift(8).rolling(7).mean())
    df["temp_lag1"] = g["avg_temp"].shift(1)
    df["max_temp_lag1"] = g["max_temp"].shift(1)
    df["dow"] = df["event_date"].dt.dayofweek
    df["is_weekend"] = (df["dow"] >= 5).astype(int)
    df = pd.concat([df, pd.get_dummies(df["robot_id"], prefix="robot", dtype=int)], axis=1)
    return df


def feature_cols(df: pd.DataFrame) -> list[str]:
    return (["err_lag1", "err_lag7", "err_roll7", "err_trend", "temp_lag1",
             "max_temp_lag1", "dow", "is_weekend"]
            + [c for c in df.columns if c.startswith("robot_R-")])


MODELS = {
    "Ridge": lambda: make_pipeline(StandardScaler(), Ridge(alpha=1.0)),
    "GradientBoosting": lambda: GradientBoostingRegressor(
        n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42),
}


def cv_score(train: pd.DataFrame, cols: list[str], make_model) -> float:
    """TimeSeriesSplit по датам: модель всегда учится на прошлом и проверяется на будущем."""
    dates = np.sort(train["event_date"].unique())
    maes = []
    for tr_idx, va_idx in TimeSeriesSplit(n_splits=4).split(dates):
        tr = train[train["event_date"].isin(dates[tr_idx])]
        va = train[train["event_date"].isin(dates[va_idx])]
        model = make_model().fit(tr[cols], tr["errors"])
        maes.append(mean_absolute_error(va["errors"], model.predict(va[cols])))
    return float(np.mean(maes))


def main():
    OUT.mkdir(exist_ok=True)
    df = make_features(load_daily())
    cols = feature_cols(df)
    data = df.dropna(subset=cols).copy()

    split_date = data["event_date"].max() - pd.Timedelta(days=TEST_DAYS - 1)
    train = data[data["event_date"] < split_date]
    test = data[data["event_date"] >= split_date].copy()
    print(f"Обучение: {train['event_date'].min():%d.%m}–{train['event_date'].max():%d.%m}, "
          f"тест: {test['event_date'].min():%d.%m}–{test['event_date'].max():%d.%m}")

    # 1. Выбор модели по кросс-валидации на обучающем периоде
    cv = {name: cv_score(train, cols, mk) for name, mk in MODELS.items()}
    best_name = min(cv, key=cv.get)
    print("\nКросс-валидация (MAE, ошибок в день):")
    for name, mae in cv.items():
        print(f"  {name:<17} {mae:.2f}" + ("   <- лучшая" if name == best_name else ""))

    # 2. Честная проверка на отложенных 14 днях
    model = MODELS[best_name]().fit(train[cols], train["errors"])
    test["pred_model"] = model.predict(test[cols]).clip(min=0).round(1)
    test["pred_naive_yesterday"] = test["err_lag1"]
    test["pred_naive_week_ago"] = test["err_lag7"]

    print(f"\nТест на последних {TEST_DAYS} днях (MAE, ошибок в день):")
    results = {}
    for col, label in [("pred_model", f"Модель ({best_name})"),
                       ("pred_naive_yesterday", "Наивный: как вчера"),
                       ("pred_naive_week_ago", "Наивный: как неделю назад")]:
        results[label] = mean_absolute_error(test["errors"], test[col])
        print(f"  {label:<28} {results[label]:.2f}")
    best_naive = min(v for k, v in results.items() if k.startswith("Наивный"))
    gain = (1 - results[f"Модель ({best_name})"] / best_naive) * 100
    print(f"  Модель точнее лучшего наивного прогноза на {gain:.0f}%")

    print("\nMAE по роботам:")
    by_robot = (test.groupby("robot_id")
                    .apply(lambda g: pd.Series({
                        "факт_ср": g["errors"].mean(),
                        "MAE_модель": mean_absolute_error(g["errors"], g["pred_model"]),
                        "MAE_как_вчера": mean_absolute_error(g["errors"], g["err_lag1"])}),
                           include_groups=False)
                    .round(2))
    print(by_robot.to_string())

    test[["event_date", "robot_id", "errors", "pred_model",
          "pred_naive_yesterday", "pred_naive_week_ago"]].to_csv(
        OUT / "forecast_test.csv", index=False)

    # 3. Прогноз на завтра: обучаем на всех данных и строим признаки для следующей даты
    final = MODELS[best_name]().fit(data[cols], data["errors"])
    next_date = df["event_date"].max() + pd.Timedelta(days=1)
    future = df[["robot_id", "event_date", "events", "errors", "avg_temp", "max_temp"]].copy()
    future = pd.concat([future, pd.DataFrame({"robot_id": df["robot_id"].unique(),
                                              "event_date": next_date})], ignore_index=True)
    future = make_features(future)
    nxt = future[future["event_date"] == next_date].copy()
    for c in cols:                          # на случай отсутствующих dummy-колонок
        if c not in nxt:
            nxt[c] = 0
    nxt["forecast_errors"] = final.predict(nxt[cols]).clip(min=0).round(1)
    fleet_avg = data.groupby("robot_id")["errors"].mean().median()
    nxt["recommendation"] = np.where(nxt["forecast_errors"] > 2 * fleet_avg,
                                     "отправить на обслуживание", "штатная работа")
    nxt = nxt[["event_date", "robot_id", "forecast_errors", "recommendation"]]
    nxt.to_csv(OUT / "forecast_next_day.csv", index=False)
    print(f"\nПрогноз на {next_date:%d.%m.%Y}:")
    print(nxt.to_string(index=False))

    # 4. График: факт и прогноз на тестовом периоде
    robots = sorted(test["robot_id"].unique())
    hist = data[data["event_date"] >= split_date - pd.Timedelta(days=21)]
    fig, axes = plt.subplots(len(robots), 1, figsize=(10, 2.4 * len(robots)), sharex=True)
    for ax, r in zip(np.atleast_1d(axes), robots):
        h = hist[hist["robot_id"] == r]
        t = test[test["robot_id"] == r]
        ax.plot(h["event_date"], h["errors"], color="#4a5568", lw=1.6, label="Факт")
        ax.plot(t["event_date"], t["pred_model"], color="#d9480f", lw=1.8, ls="--",
                label=f"Прогноз ({best_name})")
        ax.axvline(split_date, color="#a0aec0", lw=1, ls=":")
        ax.set_title(r, loc="left", fontsize=10)
        ax.set_ylabel("ошибок/день", fontsize=8)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.25)
    np.atleast_1d(axes)[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    np.atleast_1d(axes)[0].legend(loc="upper left", fontsize=8, frameon=False)
    fig.suptitle("Ошибки роботов: факт и прогноз на отложенных 14 днях", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(OUT / "forecast.png", dpi=130)
    print(f"\nГрафик: {OUT / 'forecast.png'}")


if __name__ == "__main__":
    main()
