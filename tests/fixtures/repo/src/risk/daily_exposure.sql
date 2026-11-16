-- Spark SQL job (two-part table names, ${var} placeholders)
INSERT OVERWRITE TABLE risk_dm.daily_exposure PARTITION (as_of_date = '${ds}')
SELECT
    a.customer_id,
    SUM(CASE WHEN p.category = 'LOAN' THEN -a.balance ELSE 0 END) AS exposure_amount,
    a.product_code
FROM retail_ods.account a
LEFT JOIN ref.product p ON a.product_code = p.product_code
WHERE a.dt = '${ds}'
  AND a.status <> 'CLOSED'
GROUP BY a.customer_id, a.product_code;
