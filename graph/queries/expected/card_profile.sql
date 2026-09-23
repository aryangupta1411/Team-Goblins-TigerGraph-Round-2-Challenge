-- card_profile(c, as_of) — DuckDB equivalent (params: $c, $as_of). g_card is the ETL card_feat table, so card{}
-- carries every Card attribute incl. the JSON strings (known_device_ids capped at 200, recurring_amounts at 50).
WITH s AS (SELECT * FROM g_card WHERE id = $c),
     t AS (SELECT * FROM g_txn WHERE card_id = $c AND ts <= $as_of::TIMESTAMP),
     l30 AS (SELECT * FROM t WHERE ts > $as_of::TIMESTAMP - INTERVAL 30 DAY)
SELECT
  (SELECT struct_pack(id, customer_id, card_type, card_network, n_txns, first_ts := strftime(first_ts, '%Y-%m-%d %H:%M:%S'),
                      last_ts := strftime(last_ts, '%Y-%m-%d %H:%M:%S'), median_amt, p90_amt, max_amt, max_in_person_amt,
                      n_online, n_in_person, modal_region, n_regions, home_regions, usual_products, usual_p_email, n_devices_seen,
                      known_device_ids, recurring_amounts, n_prior_fraud_cases, n_prior_cleared_cases, ring_id, burst_lookalike_ids,
                      region_cluster_30d, wcc_id, device_degree, fraud_ppr := fraud_ppr::DOUBLE) FROM s) AS card,
  (SELECT struct_pack(n := count(*), sum_amt := round(coalesce(sum(amt), 0), 2), n_online := count(*) FILTER (WHERE channel = 'online'),
                      n_regions := count(DISTINCT addr1) FILTER (WHERE addr1 <> ''), n_devices := count(DISTINCT device_id) FILTER (WHERE device_id <> ''),
                      n_products := count(DISTINCT product_cd)) FROM l30) AS last30,
  (SELECT list(struct_pack(addr1, n, "days", first_ts, last_ts) ORDER BY n DESC, addr1) FROM (
     SELECT addr1, count(*) n, count(DISTINCT ts::DATE) AS "days", strftime(min(ts), '%Y-%m-%d %H:%M:%S') first_ts, strftime(max(ts), '%Y-%m-%d %H:%M:%S') last_ts
     FROM t WHERE addr1 <> '' GROUP BY 1 ORDER BY n DESC, addr1 LIMIT 5)) AS regions,
  (SELECT list(struct_pack(device_id, n, first_ts, is_strong) ORDER BY n DESC, device_id) FROM (
     SELECT t.device_id, count(*) n, strftime(min(t.ts), '%Y-%m-%d %H:%M:%S') first_ts, any_value(d.is_strong) is_strong
     FROM t JOIN g_dev d ON d.id = t.device_id GROUP BY 1 ORDER BY n DESC, device_id LIMIT 5)) AS devices,
  (SELECT list(struct_pack("domain", n) ORDER BY n DESC, "domain") FROM (
     SELECT p_email AS "domain", count(*) n FROM t WHERE p_email <> '' GROUP BY 1 ORDER BY n DESC, "domain" LIMIT 10)) AS emails,
  (SELECT coalesce(list(struct_pack(id, card_type, n_txns)), []) FROM g_card WHERE customer_id = (SELECT customer_id FROM s) AND id <> $c) AS other_cards,
  (SELECT list(struct_pack(id, opened_at := strftime(opened_at, '%Y-%m-%d %H:%M:%S'), outcome, pattern, exposure_usd, report_filed, template_id) ORDER BY opened_at DESC, id) FROM (
     SELECT * FROM g_cc WHERE card_id = $c AND opened_at <= $as_of::TIMESTAMP ORDER BY opened_at DESC, id LIMIT 30)) AS prior_closed_cases,
  [] AS prior_agent_cases;
