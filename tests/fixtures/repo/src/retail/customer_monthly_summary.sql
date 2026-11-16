{# Rebuild one month of the summary table. #}
DELETE FROM cos.retail_dm.customer_monthly_summary
WHERE month = '{{ execution_date.strftime("%Y-%m") }}';

INSERT INTO cos.retail_dm.customer_monthly_summary
SELECT
    c.customer_id,
    '{{ execution_date.strftime("%Y-%m") }}' AS month,
    COUNT(t.txn_id) AS txn_count,
    SUM(t.amount) AS txn_amount,
    c.segment_code
FROM cos.retail_ods.transaction t
JOIN cos.retail_ods.account a ON t.account_id = a.account_id
JOIN cos.retail_ods.customer c ON a.customer_id = c.customer_id
WHERE t.dt BETWEEN DATE '{{ data_interval_start | ds }}' AND DATE '{{ data_interval_end | ds }}'
{% if params.debit_only %}
  AND t.txn_type = 'DEBIT'
{% endif %}
GROUP BY c.customer_id, c.segment_code;
