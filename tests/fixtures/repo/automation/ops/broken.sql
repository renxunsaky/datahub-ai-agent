-- This file is intentionally unparseable to exercise failure handling.
SELEC * FORM nowhere WHEER 1 = ;
{% if weird %}
SELECT 1 FROM cos.ref.branch
