-- Airflow task: retail.customer_daily_balance (runs daily)
INSERT INTO cos.retail_dm.customer_daily_balance
SELECT
    c.customer_id,
    DATE '{{ ds }}' AS as_of_date,
    SUM(a.balance) AS total_balance,
    COUNT(*) AS account_count
FROM cos.retail_ods.account a
JOIN cos.retail_ods.customer c ON a.customer_id = c.customer_id
WHERE a.dt = DATE '{{ ds }}'
  AND a.status = 'OPEN'
GROUP BY c.customer_id;
