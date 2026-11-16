MERGE INTO cos.risk_dm.customer_risk_score t
USING (
    SELECT
        b.customer_id,
        DATE '{{ ds }}' AS score_date,
        CASE WHEN b.total_balance < 0 THEN 0.2 ELSE 0.01 END AS pd_score,
        CASE WHEN b.total_balance < 0 THEN 'C' ELSE 'A' END AS rating,
        'v{{ var.value.risk_model_version }}' AS model_version
    FROM cos.retail_dm.customer_daily_balance b
    JOIN cos.retail_ods.customer c ON b.customer_id = c.customer_id
    WHERE b.as_of_date = DATE '{{ ds }}'
) s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET score_date = s.score_date, pd_score = s.pd_score, rating = s.rating, model_version = s.model_version
WHEN NOT MATCHED THEN INSERT (customer_id, score_date, pd_score, rating, model_version)
    VALUES (s.customer_id, s.score_date, s.pd_score, s.rating, s.model_version);
