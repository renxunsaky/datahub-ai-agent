/* Monthly card spend report, materialised as a new table each run. */
CREATE TABLE cos.cards_dm.card_spend_monthly
WITH (format = 'ORC')
AS
WITH spend AS (
    SELECT
        cd.account_id,
        date_format(ct.txn_ts, '%Y-%m') AS month,
        SUM(ct.amount) AS spend_amount,
        COUNT(*) AS txn_count
    FROM cos.cards_ods.card_transaction ct
    JOIN cos.cards_ods.card cd ON ct.card_id = cd.card_id
    WHERE ct.dt BETWEEN DATE '{{ macros.ds_add(ds, -30) }}' AND DATE '{{ ds }}'
      AND cd.status = 'ACTIVE'
    GROUP BY cd.account_id, date_format(ct.txn_ts, '%Y-%m')
)
SELECT s.account_id, s.month, s.spend_amount, s.txn_count
FROM spend s
JOIN cos.retail_ods.account a ON s.account_id = a.account_id
WHERE a.status = 'OPEN';
