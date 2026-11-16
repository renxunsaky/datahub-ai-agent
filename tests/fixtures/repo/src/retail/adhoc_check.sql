-- Data quality check used by a sensor task; not a write.
SELECT COUNT(*) AS n
FROM cos.retail_dm.customer_daily_balance
WHERE as_of_date = DATE '{{ ds }}'
  AND total_balance < 0
  AND customer_id IN ({{ params.watchlist | join(', ') }});
