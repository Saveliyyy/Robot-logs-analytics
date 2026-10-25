-- Аналитика логов роботов: подзапросы, CTE, оконные функции, оптимизация.
-- Диалект SQLite. Окна, CTE и подзапросы переносятся в PostgreSQL без изменений;
-- функции дат различаются: strftime('%Y-W%W', d) -> to_char(d, 'IYYY-"W"IW'),
-- julianday(d) -> d::date - DATE '2000-01-01'.
-- Каждый запрос отделён строкой "-- name: <имя>", run_sql.py выполняет их по очереди.


-- name: 01_errors_day_over_day
-- Ошибки по дням для каждого робота и изменение к предыдущему дню (LAG).
SELECT
    event_date,
    robot_id,
    errors,
    LAG(errors) OVER w                    AS errors_prev_day,
    errors - LAG(errors) OVER w           AS diff_vs_prev_day
FROM daily_robot_metrics
WINDOW w AS (PARTITION BY robot_id ORDER BY event_date)
ORDER BY robot_id, event_date;


-- name: 02_rolling_7d_errors
-- Скользящее среднее ошибок за 7 дней и накопительная сумма (рамка окна ROWS BETWEEN).
SELECT
    event_date,
    robot_id,
    errors,
    ROUND(AVG(errors) OVER (PARTITION BY robot_id ORDER BY event_date
                            ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 1) AS errors_avg_7d,
    SUM(errors) OVER (PARTITION BY robot_id ORDER BY event_date
                      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS errors_cumulative
FROM daily_robot_metrics
ORDER BY robot_id, event_date;


-- name: 03_top3_worst_days_per_robot
-- Топ-3 худших дня по доле ошибок у каждого робота (ROW_NUMBER + CTE).
WITH ranked AS (
    SELECT
        robot_id,
        event_date,
        error_rate_pct,
        max_temp,
        ROW_NUMBER() OVER (PARTITION BY robot_id ORDER BY error_rate_pct DESC) AS rn
    FROM daily_robot_metrics
)
SELECT robot_id, rn AS place, event_date, error_rate_pct, max_temp
FROM ranked
WHERE rn <= 3
ORDER BY robot_id, rn;


-- name: 04_robots_above_fleet_avg
-- Роботы, у которых доля ошибок выше средней по парку (подзапрос в WHERE).
SELECT
    robot_id,
    ROUND(SUM(errors) * 100.0 / SUM(events), 3) AS error_rate_pct,
    ROUND(AVG(avg_temp), 1)                     AS avg_temp
FROM daily_robot_metrics
GROUP BY robot_id
HAVING SUM(errors) * 100.0 / SUM(events) > (
    SELECT SUM(errors) * 100.0 / SUM(events) FROM daily_robot_metrics
)
ORDER BY error_rate_pct DESC;


-- name: 05_error_codes_share
-- Структура ошибок: доля каждого кода внутри робота (оконная сумма поверх GROUP BY).
SELECT
    robot_id,
    error_code,
    COUNT(*) AS cnt,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY robot_id), 1) AS share_pct,
    RANK() OVER (PARTITION BY robot_id ORDER BY COUNT(*) DESC)               AS rnk
FROM events
WHERE level = 'ERROR'
GROUP BY robot_id, error_code
ORDER BY robot_id, rnk;


-- name: 06_error_rate_by_temp_band
-- Как частота ошибок зависит от температуры (CASE WHEN для бакетов).
SELECT
    CASE
        WHEN temp < 40 THEN '1. < 40°C'
        WHEN temp < 44 THEN '2. 40–44°C'
        WHEN temp < 48 THEN '3. 44–48°C'
        ELSE                '4. >= 48°C'
    END AS temp_band,
    COUNT(*)                                                   AS events,
    SUM(CASE WHEN level = 'ERROR' THEN 1 ELSE 0 END)           AS errors,
    ROUND(AVG(CASE WHEN level = 'ERROR' THEN 1.0 ELSE 0 END) * 100, 2) AS error_rate_pct
FROM events
WHERE temp IS NOT NULL
GROUP BY temp_band
ORDER BY temp_band;


-- name: 07_week_over_week_trend
-- Неделя к неделе: рост ошибок у каждого робота (CTE + LAG по неделям).
WITH weekly AS (
    SELECT
        robot_id,
        strftime('%Y-W%W', event_date) AS week,
        SUM(errors)  AS errors,
        SUM(events)  AS events
    FROM daily_robot_metrics
    GROUP BY robot_id, week
    HAVING COUNT(*) = 7          -- только полные недели, иначе сравнение некорректно
)
SELECT
    robot_id,
    week,
    errors,
    ROUND(errors * 100.0 / events, 2) AS error_rate_pct,
    ROUND((errors - LAG(errors) OVER (PARTITION BY robot_id ORDER BY week)) * 100.0
          / LAG(errors) OVER (PARTITION BY robot_id ORDER BY week), 1) AS wow_change_pct
FROM weekly
ORDER BY robot_id, week;


-- name: 08_first_overheat_streak
-- Первый день, с которого у робота пошли 3+ дня подряд с max_temp >= 50°C
-- (приём «gaps and islands»: разность номеров строк даёт id серии).
WITH hot AS (
    SELECT
        robot_id,
        event_date,
        ROW_NUMBER() OVER (PARTITION BY robot_id ORDER BY event_date) AS rn_all,
        julianday(event_date) AS jd
    FROM daily_robot_metrics
    WHERE max_temp >= 50
),
islands AS (
    SELECT robot_id, event_date, jd - rn_all AS island_id
    FROM hot
)
SELECT
    robot_id,
    MIN(event_date) AS streak_start,
    MAX(event_date) AS streak_end,
    COUNT(*)        AS days_in_row
FROM islands
GROUP BY robot_id, island_id
HAVING COUNT(*) >= 3
ORDER BY robot_id, streak_start
LIMIT 10;
