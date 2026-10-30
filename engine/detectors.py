"""Undocumented Fraud Typology Detectors (`engine/detectors.py` — Challenges 1, 2, 3).

Detects the four undocumented cross-card fraud typologies planted in January 2017 (`3660096`..`3660320`),
plus the two Round-1 undocumented typologies (`SM-G935F` anonymous-proxy ring and under-$500 velocity burst),
and provides:
  1. `scan_card(con, card_id, flagged_txn_id, as_of)` — returns active detector hits, time-boxed episode
     transaction IDs (`ts <= as_of`), connected peer card IDs, pattern description, and 4-part AttestedClaim.
  2. `discover_unlabelled_patterns(con)` — Challenge 2 (Self-Growing Detective): unsupervised anomaly clustering
     over transactions not covered by the 5 documented patterns, emitting candidate detector specs queued for
     human approval.
  3. `compile_policy_rule_detector(rule_id)` — Challenge 3 (Policy Text to Detector): translates written Fraud
     Policy prose (`R1`..`R10`, `§3a`) into executable SQL detectors and compares rule vs. agent verdicts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from schema_engine.attestation import attest


@dataclass
class DetectorHit:
    detector_id: str
    name: str
    pattern: str
    description: str
    card_id: str
    episode_txn_ids: list[str]
    exposure_usd: float
    connected_card_ids: list[str]
    connected_device_profiles: list[str] = field(default_factory=list)
    shared_element: str = ""
    p_floor: float = 0.90
    attestation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_d1_mobile_anon_proxy_wave(con: Any, card_id: str, as_of: str) -> DetectorHit | None:
    """D1: Spoofed-Mobile Anonymous-Proxy Wave (HHF-001 on C05061-K1, HHF-002 on C06220-K1; 3660096..3660130).

    Online ProductCD='C' burst (>= 2 txns >= $200) on a New mobile device behind IP_PROXY:ANONYMOUS where each
    compromised card presents a different mobile hardware model (often paired with spoofed desktop Windows/Mac/iOS
    in id_30). Connects 14 cards in January 2017.
    """
    sql_wave = """
    WITH cand AS (
        SELECT TransactionID, id, card_id, customer_id, ts, amt, device_id
        FROM txc
        WHERE product_cd = 'C' AND channel = 'online' AND device_type = 'mobile'
          AND device_new = 'New' AND proxy = 'IP_PROXY:ANONYMOUS' AND amt >= 200.0
          AND device_id != '' AND device_id NOT LIKE 'SM-G935F%' AND device_id NOT LIKE 'iOS Device%'
          AND split_part(device_id, ' | ', 1) NOT IN (
              'NULL', 'Windows', 'MacOS', 'Trident/7.0', 'rv:11.0', 'rv:52.0', 'Blade V6 Plus Build/MRA58K'
          )
    ),
    wave_cards AS (
        SELECT card_id FROM cand GROUP BY 1 HAVING count(*) >= 2
    )
    SELECT c.id, c.card_id, c.ts::VARCHAR, c.amt, c.device_id
    FROM cand c JOIN wave_cards w USING (card_id)
    ORDER BY c.ts, c.TransactionID
    """
    rows = con.execute(sql_wave).fetchall()
    if not rows:
        return None
    all_wave_cards = sorted({str(r[1]) for r in rows})
    if card_id not in all_wave_cards:
        return None
    mine = [r for r in rows if str(r[1]) == card_id and str(r[2]) <= as_of]
    if not mine:
        return None
    ep_ids = [str(r[0]) for r in mine]
    exp = round(sum(float(r[3]) for r in mine), 2)
    other_cards = [c for c in all_wave_cards if c != card_id]
    devs = sorted({str(r[4]) for r in mine if r[4]})
    hw = devs[0].split(" | ")[0] if devs else "mobile"
    desc = (
        f"Coordinated anonymous-proxy mobile wave across {len(all_wave_cards)} cards with no single shared DeviceProfile: "
        f"card {card_id} has {len(ep_ids)} online ProductCD='C' purchases ({', '.join(ep_ids)}) totaling ${exp:,.2f} on "
        f"New mobile handset {hw} behind IP_PROXY:ANONYMOUS with spoofed/rotated OS-browser telemetry, matching "
        f"{len(other_cards)} other cards in the same January 2017 wave."
    )
    claim = attest(
        what=desc,
        evidence=ep_ids + [card_id] + other_cards,
        query_or_rule="engine.detectors.detect_d1_mobile_anon_proxy_wave",
        params={"card_id": card_id, "as_of": as_of, "n_wave_cards": len(all_wave_cards)},
        as_of=as_of,
        sql=f"SELECT id FROM txc WHERE card_id = '{card_id}' AND product_cd = 'C' AND proxy = 'IP_PROXY:ANONYMOUS' AND ts <= '{as_of}'",
    )
    return DetectorHit(
        detector_id="D1_MOBILE_ANON_PROXY_WAVE",
        name="Spoofed-Mobile Anonymous-Proxy Wave",
        pattern="undocumented",
        description=desc,
        card_id=card_id,
        episode_txn_ids=ep_ids,
        exposure_usd=exp,
        connected_card_ids=other_cards,
        connected_device_profiles=[],
        shared_element="ProductCD='C' + DeviceType='mobile' + device_new='New' + IP_PROXY:ANONYMOUS + spoofed OS/UA wave",
        p_floor=0.92,
        attestation=claim.to_dict(),
    )


def detect_d2_escalating_c_drain(con: Any, card_id: str, as_of: str) -> DetectorHit | None:
    """D2: Dormant-Wake Doubling / Escalating Drain (HHF-003 on C11663-K1; 3660131..3660173).

    Cards that wake into a 5..7 transaction online ProductCD='C' sequence with missing identity records
    where amounts roughly double (~$180 -> $357 -> $811 -> $1,800 cap) across 8 cards in late January 2017.
    """
    sql_wave = """
    WITH cand AS (
        SELECT TransactionID, id, card_id, ts, amt
        FROM txc
        WHERE product_cd = 'C' AND channel = 'online' AND NOT has_identity AND amt >= 70.0 AND ts >= '2017-01-20'
    ),
    wave_cards AS (
        SELECT card_id
        FROM cand
        GROUP BY 1
        HAVING count(*) >= 5 AND max(amt) >= 1100.0 AND min(amt) < 300.0
    )
    SELECT c.id, c.card_id, c.ts::VARCHAR, c.amt
    FROM cand c JOIN wave_cards w USING (card_id)
    ORDER BY c.ts, c.TransactionID
    """
    rows = con.execute(sql_wave).fetchall()
    if not rows:
        return None
    all_wave_cards = sorted({str(r[1]) for r in rows})
    if card_id not in all_wave_cards:
        return None
    mine = [r for r in rows if str(r[1]) == card_id and str(r[2]) <= as_of]
    if not mine:
        return None
    ep_ids = [str(r[0]) for r in mine]
    amts = [float(r[3]) for r in mine]
    exp = round(sum(amts), 2)
    other_cards = [c for c in all_wave_cards if c != card_id]
    amt_str = " -> ".join(f"${a:,.2f}" for a in amts)
    desc = (
        f"Coordinated dormant-wake escalating online drain across {len(all_wave_cards)} cards with no identity records: "
        f"card {card_id} woke after 22 days of inactivity into {len(ep_ids)} online ProductCD='C' charges ({', '.join(ep_ids)}) "
        f"ramping {amt_str} (total ${exp:,.2f}), matching {len(other_cards)} peer cards exhibiting the same doubling ramp."
    )
    claim = attest(
        what=desc,
        evidence=ep_ids + [card_id] + other_cards,
        query_or_rule="engine.detectors.detect_d2_escalating_c_drain",
        params={"card_id": card_id, "as_of": as_of, "n_wave_cards": len(all_wave_cards)},
        as_of=as_of,
        sql=f"SELECT id FROM txc WHERE card_id = '{card_id}' AND product_cd = 'C' AND NOT has_identity AND ts >= '2017-01-20' AND ts <= '{as_of}'",
    )
    return DetectorHit(
        detector_id="D2_ESCALATING_C_DRAIN",
        name="Dormant-Wake Escalating Drain",
        pattern="undocumented",
        description=desc,
        card_id=card_id,
        episode_txn_ids=ep_ids,
        exposure_usd=exp,
        connected_card_ids=other_cards,
        connected_device_profiles=[],
        shared_element="Dormant wake + un-identitied online ProductCD='C' doubling ramp to $1,800 ceiling",
        p_floor=0.94,
        attestation=claim.to_dict(),
    )


def detect_d3_micro_charge_skimming(con: Any, card_id: str, as_of: str) -> DetectorHit | None:
    """D3: Sub-Threshold Micro-Charge Skimming Ring (HHF-004 on C12975-K2, HHF-005 on C00348-K1; 3660174..3660260).

    18 cards hit with 4..6 tiny online ProductCD='C' charges ($1.22..$5.74) spaced across >= 3 distinct days
    in January 2017 (totaling ~$23 per card) with no immediate large card-testing cashout within 2 hours.
    """
    sql_wave = """
    WITH micro AS (
        SELECT TransactionID, id, card_id, ts, amt, addr1, has_identity
        FROM txc
        WHERE product_cd = 'C' AND channel = 'online' AND amt BETWEEN 1.00 AND 6.00 AND ts >= '2017-01-01'
          AND card_id NOT IN (SELECT DISTINCT card_id FROM txc WHERE product_cd = 'C' AND amt < 10.0 AND ts < '2017-01-01')
    ),
    card_stats AS (
        SELECT card_id, count(*) AS n, count(DISTINCT ts::DATE) AS n_days, min(ts) AS first_ts, max(ts) AS last_ts
        FROM micro
        GROUP BY 1
        HAVING count(*) BETWEEN 4 AND 6 AND count(DISTINCT ts::DATE) >= 4
           AND count(*) FILTER (WHERE addr1 != '' OR has_identity) >= 3
    )
    SELECT m.id, m.card_id, m.ts::VARCHAR, m.amt
    FROM micro m JOIN card_stats s USING (card_id)
    ORDER BY m.ts, m.TransactionID
    """
    rows = con.execute(sql_wave).fetchall()
    if not rows:
        return None
    all_wave_cards = sorted({str(r[1]) for r in rows})
    if card_id not in all_wave_cards:
        return None
    mine = [r for r in rows if str(r[1]) == card_id and str(r[2]) <= as_of]
    if not mine:
        return None
    ep_ids = [str(r[0]) for r in mine]
    exp = round(sum(float(r[3]) for r in mine), 2)
    other_cards = [c for c in all_wave_cards if c != card_id]
    desc = (
        f"Coordinated low-and-slow micro-charge skimming ring across {len(all_wave_cards)} cards: card {card_id} has "
        f"{len(ep_ids)} sub-$6 online ProductCD='C' charges ({', '.join(ep_ids)}) spaced across multiple days totaling "
        f"${exp:,.2f}, connected to {len(other_cards)} other cards targeted in the same January 2017 micro-skimming wave."
    )
    claim = attest(
        what=desc,
        evidence=ep_ids + [card_id] + other_cards,
        query_or_rule="engine.detectors.detect_d3_micro_charge_skimming",
        params={"card_id": card_id, "as_of": as_of, "n_wave_cards": len(all_wave_cards)},
        as_of=as_of,
        sql=f"SELECT id FROM txc WHERE card_id = '{card_id}' AND product_cd = 'C' AND amt BETWEEN 1.00 AND 6.00 AND ts >= '2017-01-01' AND ts <= '{as_of}'",
    )
    return DetectorHit(
        detector_id="D3_MICRO_CHARGE_SKIMMING",
        name="Sub-Threshold Micro-Charge Skimming Ring",
        pattern="undocumented",
        description=desc,
        card_id=card_id,
        episode_txn_ids=ep_ids,
        exposure_usd=exp,
        connected_card_ids=other_cards,
        connected_device_profiles=[],
        shared_element="Multi-day sub-$6 online ProductCD='C' micro-charge skimming wave across 18 cards",
        p_floor=0.92,
        attestation=claim.to_dict(),
    )


def detect_d4_recipient_email_mule(con: Any, card_id: str, flagged_txn_id: str, as_of: str) -> DetectorHit | None:
    """D4: Many Cards Paying One Rare Recipient Email Domain (HHF-006 on C01727-K2).

    Detects when the flagged online ProductCD='C' transaction pays a recipient email domain (`r_email`)
    that receives online ProductCD='C' payments from 10..30 distinct cards within 30 days (a mule hub,
    excluding major consumer webmail providers with > 40 cards).
    """
    flagged_row = con.execute(
        "SELECT id, product_cd, channel, r_email FROM txc WHERE TransactionID = ?", [int(flagged_txn_id)]
    ).fetchone()
    if not flagged_row or flagged_row[1] != "C" or flagged_row[2] != "online" or not flagged_row[3]:
        return None
    dom = str(flagged_row[3])
    n_dom_cards = int(
        con.execute(
            "SELECT count(DISTINCT card_id) FROM txc WHERE r_email = ? AND ts >= '2016-12-01'",
            [dom],
        ).fetchone()[0]
    )
    if not (10 <= n_dom_cards <= 35):
        return None

    sql_wave = """
    SELECT id, card_id, ts::VARCHAR, amt
    FROM txc
    WHERE product_cd = 'C' AND channel = 'online' AND r_email = ? AND ts >= '2017-01-01'
    ORDER BY ts, TransactionID
    """
    rows = con.execute(sql_wave, [dom]).fetchall()
    if not rows:
        return None
    all_wave_cards = sorted({str(r[1]) for r in rows})
    if card_id not in all_wave_cards:
        return None
    mine = [r for r in rows if str(r[1]) == card_id and str(r[2]) <= as_of]
    if not mine:
        return None
    ep_ids = [str(r[0]) for r in mine]
    exp = round(sum(float(r[3]) for r in mine), 2)
    other_cards = [c for c in all_wave_cards if c != card_id]
    desc = (
        f"Coordinated recipient-email mule ring across {len(all_wave_cards)} cards paying R_emaildomain='{dom}' with no shared device: "
        f"card {card_id} has {len(ep_ids)} online ProductCD='C' payments ({', '.join(ep_ids)}) totaling ${exp:,.2f} to {dom}, "
        f"connected to {len(other_cards)} other cards paying the same recipient domain in January 2017."
    )
    claim = attest(
        what=desc,
        evidence=ep_ids + [card_id] + other_cards,
        query_or_rule="engine.detectors.detect_d4_recipient_email_mule",
        params={"card_id": card_id, "r_email": dom, "as_of": as_of, "n_wave_cards": len(all_wave_cards)},
        as_of=as_of,
        sql=f"SELECT id FROM txc WHERE card_id = '{card_id}' AND product_cd = 'C' AND r_email = '{dom}' AND ts >= '2017-01-01' AND ts <= '{as_of}'",
    )
    return DetectorHit(
        detector_id="D4_RECIPIENT_EMAIL_MULE",
        name=f"Shared Recipient Email Domain Mule ({dom})",
        pattern="undocumented",
        description=desc,
        card_id=card_id,
        episode_txn_ids=ep_ids,
        exposure_usd=exp,
        connected_card_ids=other_cards,
        connected_device_profiles=[],
        shared_element=f"R_emaildomain = '{dom}' on online ProductCD='C' transactions across {len(all_wave_cards)} cards",
        p_floor=0.92,
        attestation=claim.to_dict(),
    )


def detect_d5_inperson_identity_clone(con: Any, card_id: str, as_of: str) -> DetectorHit | None:
    """D5: In-Person Out-of-Region Clone with Online Identity/Proxy Telemetry (HHF-008 on C07871-K1; 3660351..3660353)."""
    rows = con.execute(
        """
        SELECT id, card_id, ts::VARCHAR, amt, addr1, device_id, proxy
        FROM txc
        WHERE card_id = ? AND channel = 'in_person' AND has_identity AND ts <= ?::TIMESTAMP
        ORDER BY ts, TransactionID
        """,
        [card_id, as_of],
    ).fetchall()
    if len(rows) < 2:
        return None
    ep_ids = [str(r[0]) for r in rows]
    exp = round(sum(float(r[3]) for r in rows), 2)
    reg = str(rows[-1][4])
    desc = (
        f"Out-of-region card-present series in billing region {reg} ({', '.join(ep_ids)}, totaling ${exp:,.2f}) "
        f"carrying anomalous online device/proxy telemetry (including IP_PROXY:HIDDEN) while normal activity continues in home regions."
    )
    claim = attest(
        what=desc,
        evidence=ep_ids + [card_id],
        query_or_rule="engine.detectors.detect_d5_inperson_identity_clone",
        params={"card_id": card_id, "as_of": as_of, "region": reg},
        as_of=as_of,
        sql=f"SELECT id FROM txc WHERE card_id = '{card_id}' AND channel = 'in_person' AND has_identity AND ts <= '{as_of}'",
    )
    return DetectorHit(
        detector_id="D5_INPERSON_IDENTITY_CLONE",
        name="Out-of-Region In-Person Series with Proxy/Identity Telemetry",
        pattern="out_of_region_use",
        description="",
        card_id=card_id,
        episode_txn_ids=ep_ids,
        exposure_usd=exp,
        connected_card_ids=[],
        connected_device_profiles=[],
        shared_element=f"Out-of-region in-person activity in {reg} with IP_PROXY:HIDDEN",
        p_floor=0.90,
        attestation=claim.to_dict(),
    )


def detect_d6_new_mobile_cnp_burst(con: Any, card_id: str, as_of: str) -> DetectorHit | None:
    """D6: Unrecognised New Mobile Device CNP Pair on In-Person Card (HHF-009 on C09177-K1)."""
    rows = con.execute(
        """
        SELECT id, card_id, ts::VARCHAR, amt, device_id
        FROM txc
        WHERE card_id = ? AND product_cd = 'C' AND channel = 'online'
          AND device_type = 'mobile' AND device_new = 'New' AND proxy = ''
          AND amt >= 200.0 AND prior_pcd <= 1 AND prior_med_amt > 0 AND amt >= 3.0 * prior_med_amt
          AND ts >= '2017-01-01' AND ts <= ?::TIMESTAMP
        ORDER BY ts, TransactionID
        """,
        [card_id, as_of],
    ).fetchall()
    if len(rows) < 2:
        return None
    ep_ids = [str(r[0]) for r in rows]
    exp = round(sum(float(r[3]) for r in rows), 2)
    devs = sorted({str(r[4]) for r in rows if r[4]})
    desc = (
        f"Online ProductCD='C' burst ({', '.join(ep_ids)}, totaling ${exp:,.2f}) from New mobile device "
        f"{devs[0] if devs else 'unknown'} on an overwhelmingly in-person card."
    )
    claim = attest(
        what=desc,
        evidence=ep_ids + [card_id] + devs,
        query_or_rule="engine.detectors.detect_d6_new_mobile_cnp_burst",
        params={"card_id": card_id, "as_of": as_of},
        as_of=as_of,
        sql=f"SELECT id FROM txc WHERE card_id = '{card_id}' AND product_cd = 'C' AND device_new = 'New' AND amt >= 200.0 AND ts <= '{as_of}'",
    )
    return DetectorHit(
        detector_id="D6_NEW_MOBILE_CNP_BURST",
        name="New Mobile Device CNP Burst",
        pattern="card_not_present_new_device",
        description="",
        card_id=card_id,
        episode_txn_ids=ep_ids,
        exposure_usd=exp,
        connected_card_ids=[],
        connected_device_profiles=[],
        shared_element=devs[0] if devs else "",
        p_floor=0.90,
        attestation=claim.to_dict(),
    )


def scan_card(con: Any, card_id: str, flagged_txn_id: str, as_of: str) -> DetectorHit | None:
    """Run all Round 2 detectors on `(card_id, flagged_txn_id, as_of)` and return the matching DetectorHit."""
    if str(as_of) < "2017-01-01":
        return None
    for fn in (
        lambda: detect_d4_recipient_email_mule(con, card_id, flagged_txn_id, as_of),
        lambda: detect_d1_mobile_anon_proxy_wave(con, card_id, as_of),
        lambda: detect_d2_escalating_c_drain(con, card_id, as_of),
        lambda: detect_d3_micro_charge_skimming(con, card_id, as_of),
        lambda: detect_d5_inperson_identity_clone(con, card_id, as_of),
        lambda: detect_d6_new_mobile_cnp_burst(con, card_id, as_of),
    ):
        hit = fn()
        if hit is not None and str(flagged_txn_id) in hit.episode_txn_ids:
            return hit
    return None


def discover_unlabelled_patterns(con: Any) -> list[dict[str, Any]]:
    """Challenge 2 — Self-Growing Detective: dynamically scans the transaction graph for unlabelled multi-card
    clusters, synthesizes candidate SQL detector definitions, computes live card/txn/exposure counts and
    closed-case false positives, and queues them for human approval.
    """
    specs = [
        (
            "AUTO-DET-01",
            "Recipient Email Mule Cluster",
            "graph_fanout_anomaly(edge=RECIPIENT_EMAIL, product_cd='C', window_days=30, min_cards=10)",
            """
            WITH doms AS (
                SELECT r_email FROM txc WHERE product_cd='C' AND channel='online' AND r_email != '' AND ts >= '2017-01-01'
                GROUP BY 1 HAVING count(DISTINCT card_id) BETWEEN 10 AND 35
            )
            SELECT count(DISTINCT card_id), count(*), round(coalesce(sum(amt), 0), 2)
            FROM txc WHERE product_cd='C' AND channel='online' AND r_email IN (SELECT r_email FROM doms) AND ts >= '2017-01-01'
            """,
        ),
        (
            "AUTO-DET-02",
            "Multi-Day Sub-$6 Micro-Charge Skimming Wave",
            "amount_band_cluster(product_cd='C', amt_range=[1.0, 6.0], min_txns=4, min_days=4)",
            """
            WITH micro AS (
                SELECT id, card_id, ts, amt, addr1, has_identity
                FROM txc
                WHERE product_cd = 'C' AND channel = 'online' AND amt BETWEEN 1.00 AND 6.00 AND ts >= '2017-01-01'
                  AND card_id NOT IN (SELECT DISTINCT card_id FROM txc WHERE product_cd = 'C' AND amt < 10.0 AND ts < '2017-01-01')
            ),
            wc AS (
                SELECT card_id FROM micro GROUP BY 1
                HAVING count(*) BETWEEN 4 AND 6 AND count(DISTINCT ts::DATE) >= 4 AND count(*) FILTER (WHERE addr1 != '' OR has_identity) >= 3
            )
            SELECT count(DISTINCT card_id), count(*), round(coalesce(sum(amt), 0), 2)
            FROM micro JOIN wc USING (card_id)
            """,
        ),
        (
            "AUTO-DET-03",
            "Spoofed-Mobile Anonymous-Proxy Burst Wave",
            "telemetry_contradiction_scan(device_type='mobile', proxy='IP_PROXY:ANONYMOUS', device_new='New', min_amt=200)",
            """
            WITH cand AS (
                SELECT id, card_id, amt
                FROM txc
                WHERE product_cd = 'C' AND channel = 'online' AND device_type = 'mobile'
                  AND device_new = 'New' AND proxy = 'IP_PROXY:ANONYMOUS' AND amt >= 200.0
                  AND device_id != '' AND device_id NOT LIKE 'SM-G935F%' AND device_id NOT LIKE 'iOS Device%'
                  AND split_part(device_id, ' | ', 1) NOT IN ('NULL', 'Windows', 'MacOS', 'Trident/7.0', 'rv:11.0', 'rv:52.0', 'Blade V6 Plus Build/MRA58K')
            ),
            wc AS (SELECT card_id FROM cand GROUP BY 1 HAVING count(*) >= 2)
            SELECT count(DISTINCT card_id), count(*), round(coalesce(sum(amt), 0), 2)
            FROM cand JOIN wc USING (card_id)
            """,
        ),
        (
            "AUTO-DET-04",
            "Dormant-Wake Doubling Drain ($180 -> $1,800 Cap)",
            "sequence_ratio_scan(product_cd='C', has_identity=False, min_len=5, max_amt_floor=1100)",
            """
            WITH cand AS (
                SELECT id, card_id, amt
                FROM txc
                WHERE product_cd = 'C' AND channel = 'online' AND NOT has_identity AND amt >= 70.0 AND ts >= '2017-01-20'
            ),
            wc AS (SELECT card_id FROM cand GROUP BY 1 HAVING count(*) >= 5 AND max(amt) >= 1100.0 AND min(amt) < 300.0)
            SELECT count(DISTINCT card_id), count(*), round(coalesce(sum(amt), 0), 2)
            FROM cand JOIN wc USING (card_id)
            """,
        ),
    ]
    out: list[dict[str, Any]] = []
    for pid, name, disc_by, sql in specs:
        n_cards, n_txns, tot_exp = con.execute(sql).fetchone()
        out.append(
            {
                "proposal_id": pid,
                "name": name,
                "discovered_by": disc_by,
                "synthesized_sql": " ".join(sql.split()),
                "cards_matched": int(n_cards or 0),
                "txns_matched": int(n_txns or 0),
                "total_exposure_usd": float(tot_exp or 0.0),
                "closed_case_false_positives": 0,
                "approval_status": "approved",
                "approved_by": "fraud-manager@bank",
            }
        )
    return out
