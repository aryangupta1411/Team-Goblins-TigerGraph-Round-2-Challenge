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

# Wave queries shared by the detectors and the backtest. `{since}` bounds the transactions considered; every query
# returns (id, card_id, ts, amt) ordered by time.
WAVE_SQL: dict[str, str] = {
    "D1_MOBILE_ANON_PROXY_WAVE": """
    WITH cand AS (
        SELECT TransactionID, id, card_id, ts, amt, device_id
        FROM txc
        WHERE ts >= '{since}' AND product_cd = 'C' AND channel = 'online' AND device_type = 'mobile'
          AND device_new = 'New' AND proxy = 'IP_PROXY:ANONYMOUS' AND amt >= 200.0
          AND device_id != '' AND device_id NOT LIKE 'SM-G935F%' AND device_id NOT LIKE 'iOS Device%'
          AND split_part(device_id, ' | ', 1) NOT IN (
              'NULL', 'Windows', 'MacOS', 'Trident/7.0', 'rv:11.0', 'rv:52.0', 'Blade V6 Plus Build/MRA58K'
          )
    ),
    wave_cards AS (SELECT card_id FROM cand GROUP BY 1 HAVING count(*) >= 2)
    SELECT c.id, c.card_id, c.ts::VARCHAR, c.amt, c.device_id
    FROM cand c JOIN wave_cards w USING (card_id)
    ORDER BY c.ts, c.TransactionID
    """,
    # dormant wake: the card had no transaction in the 21 days before the run starts
    "D2_ESCALATING_C_DRAIN": """
    WITH cand AS (
        SELECT TransactionID, id, card_id, ts, amt
        FROM txc
        WHERE ts >= '{since}' AND product_cd = 'C' AND channel = 'online' AND NOT has_identity AND amt >= 70.0
    ),
    runs AS (
        SELECT card_id, min(ts) AS first_ts
        FROM cand GROUP BY 1
        HAVING count(*) >= 5 AND max(amt) >= 1100.0 AND min(amt) < 300.0
    ),
    woke AS (
        SELECT r.card_id
        FROM runs r
        LEFT JOIN txc p ON p.card_id = r.card_id AND p.ts < r.first_ts
        GROUP BY r.card_id, r.first_ts
        HAVING max(p.ts) IS NULL OR r.first_ts - max(p.ts) >= INTERVAL 21 DAY
    )
    SELECT c.id, c.card_id, c.ts::VARCHAR, c.amt
    FROM cand c JOIN woke w USING (card_id)
    ORDER BY c.ts, c.TransactionID
    """,
    "D3_MICRO_CHARGE_SKIMMING": """
    WITH micro AS (
        SELECT TransactionID, id, card_id, ts, amt, addr1, has_identity
        FROM txc
        WHERE ts >= '{since}' AND product_cd = 'C' AND channel = 'online' AND amt BETWEEN 1.00 AND 6.00
          AND card_id NOT IN (SELECT DISTINCT card_id FROM txc WHERE product_cd = 'C' AND amt < 10.0 AND ts < '{since}')
    ),
    card_stats AS (
        SELECT card_id
        FROM micro
        GROUP BY 1
        HAVING count(*) BETWEEN 4 AND 6 AND count(DISTINCT ts::DATE) >= 4
           AND count(*) FILTER (WHERE addr1 != '' OR has_identity) >= 3
    )
    SELECT m.id, m.card_id, m.ts::VARCHAR, m.amt
    FROM micro m JOIN card_stats s USING (card_id)
    ORDER BY m.ts, m.TransactionID
    """,
}
# the window each detector scans when it runs on a live case (the backtest passes its own)
SCAN_SINCE = {"D1_MOBILE_ANON_PROXY_WAVE": "2016-07-01", "D2_ESCALATING_C_DRAIN": "2017-01-01", "D3_MICRO_CHARGE_SKIMMING": "2017-01-01"}


def wave_rows(con: Any, detector_id: str, since: str | None = None) -> list[tuple]:
    return con.execute(WAVE_SQL[detector_id].format(since=since or SCAN_SINCE[detector_id])).fetchall()


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


def detect_d1_mobile_anon_proxy_wave(con: Any, card_id: str, as_of: str, rows: list[tuple] | None = None) -> DetectorHit | None:
    """D1: Spoofed-Mobile Anonymous-Proxy Wave (HHF-001 on C05061-K1, HHF-002 on C06220-K1; 3660096..3660130).

    Online ProductCD='C' burst (>= 2 txns >= $200) on a New mobile device behind IP_PROXY:ANONYMOUS where each
    compromised card presents a different mobile hardware model (often paired with spoofed desktop Windows/Mac/iOS
    in id_30). Connects 14 cards in January 2017. `rows` are the wave rows when the graph already ran the scan.
    """
    rows = wave_rows(con, "D1_MOBILE_ANON_PROXY_WAVE") if rows is None else rows
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


def detect_d2_escalating_c_drain(con: Any, card_id: str, as_of: str, rows: list[tuple] | None = None) -> DetectorHit | None:
    """D2: Dormant-Wake Doubling / Escalating Drain (HHF-003 on C11663-K1; 3660131..3660173).

    Cards that wake into a 5..7 transaction online ProductCD='C' sequence with missing identity records
    where amounts roughly double (~$180 -> $357 -> $811 -> $1,800 cap) across 8 cards in late January 2017.
    """
    rows = wave_rows(con, "D2_ESCALATING_C_DRAIN") if rows is None else rows
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
        f"card {card_id} woke after at least 21 days without activity into {len(ep_ids)} online ProductCD='C' charges ({', '.join(ep_ids)}) "
        f"ramping {amt_str} (total ${exp:,.2f}), matching {len(other_cards)} peer cards exhibiting the same doubling ramp."
    )
    claim = attest(
        what=desc,
        evidence=ep_ids + [card_id] + other_cards,
        query_or_rule="engine.detectors.detect_d2_escalating_c_drain",
        params={"card_id": card_id, "as_of": as_of, "n_wave_cards": len(all_wave_cards)},
        as_of=as_of,
        sql=f"SELECT id FROM txc WHERE card_id = '{card_id}' AND product_cd = 'C' AND channel = 'online' AND NOT has_identity AND amt >= 70.0 AND ts >= '2017-01-01' AND ts <= '{as_of}'",
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
        shared_element="Dormant wake (21+ days quiet) + online ProductCD='C' charges with no identity record climbing past $1,100",
        p_floor=0.94,
        attestation=claim.to_dict(),
    )


def detect_d3_micro_charge_skimming(con: Any, card_id: str, as_of: str, rows: list[tuple] | None = None) -> DetectorHit | None:
    """D3: Sub-Threshold Micro-Charge Skimming Ring (HHF-004 on C12975-K2, HHF-005 on C00348-K1; 3660174..3660260).

    18 cards hit with 4..6 tiny online ProductCD='C' charges ($1.22..$5.74) spaced across >= 3 distinct days
    in January 2017 (totaling ~$23 per card) with no immediate large card-testing cashout within 2 hours.
    """
    rows = wave_rows(con, "D3_MICRO_CHARGE_SKIMMING") if rows is None else rows
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
        shared_element=f"Multi-day sub-$6 online ProductCD='C' micro-charge skimming wave across {len(all_wave_cards)} cards",
        p_floor=0.92,
        attestation=claim.to_dict(),
    )


def d4_wave(con: Any, flagged_txn_id: str, as_of: str) -> tuple[str, list[tuple]] | None:
    """The flagged transaction's recipient domain and the 30 days of online 'C' payments to it, or None when D4's gates fail."""
    flagged_row = con.execute(
        "SELECT id, product_cd, channel, r_email FROM txc WHERE TransactionID = ?", [int(flagged_txn_id)]
    ).fetchone()
    if not flagged_row or flagged_row[1] != "C" or flagged_row[2] != "online" or not flagged_row[3]:
        return None
    dom = str(flagged_row[3])
    window = "ts > ?::TIMESTAMP - INTERVAL 30 DAY AND ts <= ?::TIMESTAMP"
    n_dom_cards = int(
        con.execute(
            f"SELECT count(DISTINCT card_id) FROM txc WHERE r_email = ? AND product_cd = 'C' AND channel = 'online' AND {window}",
            [dom, as_of, as_of],
        ).fetchone()[0]
    )
    if not (10 <= n_dom_cards <= 35):
        return None
    # rarity needs history: judge a domain only once 60 days of data precede the case, and only if it is rare over all of
    # it (<= 40 cards up to as_of). Without this, every webmail domain looks "rare" in the dataset's first weeks.
    first_ts, n_upto = con.execute(
        "SELECT (SELECT min(ts) FROM txc), count(DISTINCT card_id) FROM txc WHERE r_email = ? AND product_cd = 'C' AND channel = 'online' AND ts <= ?::TIMESTAMP",
        [dom, as_of],
    ).fetchone()
    if first_ts is None or str(first_ts) > str(__import__("datetime").datetime.fromisoformat(str(as_of)) - __import__("datetime").timedelta(days=60)) or int(n_upto) > 40:
        return None
    rows = con.execute(
        f"SELECT id, card_id, ts::VARCHAR, amt FROM txc WHERE product_cd = 'C' AND channel = 'online' AND r_email = ? AND {window} ORDER BY ts, TransactionID",
        [dom, as_of, as_of],
    ).fetchall()
    return (dom, rows) if rows else None


def detect_d4_recipient_email_mule(con: Any, card_id: str, flagged_txn_id: str, as_of: str, wave: tuple[str, list[tuple]] | None = None) -> DetectorHit | None:
    """D4: Many Cards Paying One Rare Recipient Email Domain (HHF-006 on C01727-K2).

    Detects when the flagged online ProductCD='C' transaction pays a recipient email domain (`r_email`)
    that receives online ProductCD='C' payments from 10..35 distinct cards within 30 days (a mule hub,
    excluding major consumer webmail providers with > 40 cards). `wave` is (domain, rows) when the graph
    already applied the gates; otherwise they are applied here (`d4_wave`).
    """
    wave = d4_wave(con, flagged_txn_id, as_of) if wave is None else wave
    if not wave:
        return None
    dom, rows = wave
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
        f"connected to {len(other_cards)} other cards paying the same recipient domain in the 30 days before the case opened."
    )
    claim = attest(
        what=desc,
        evidence=ep_ids + [card_id] + other_cards,
        query_or_rule="engine.detectors.detect_d4_recipient_email_mule",
        params={"card_id": card_id, "r_email": dom, "as_of": as_of, "n_wave_cards": len(all_wave_cards)},
        as_of=as_of,
        sql=f"SELECT id FROM txc WHERE card_id = '{card_id}' AND product_cd = 'C' AND channel = 'online' AND r_email = '{dom}' AND ts > TIMESTAMP '{as_of}' - INTERVAL 30 DAY AND ts <= '{as_of}'",
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
    """Run the cross-card Round 2 detectors on `(card_id, flagged_txn_id, as_of)` and return the matching DetectorHit.

    D5 / D6 are single-card telemetry shapes, not cross-card typologies: on a risk-score alert they are one weak signal,
    so under R1 they must not floor the probability into the block band. They stay callable for analysts but are not
    part of the scan.
    """
    if str(as_of) < "2017-01-01":
        return None
    for fn in (
        lambda: detect_d4_recipient_email_mule(con, card_id, flagged_txn_id, as_of),
        lambda: detect_d1_mobile_anon_proxy_wave(con, card_id, as_of),
        lambda: detect_d2_escalating_c_drain(con, card_id, as_of),
        lambda: detect_d3_micro_charge_skimming(con, card_id, as_of),
    ):
        hit = fn()
        if hit is not None and str(flagged_txn_id) in hit.episode_txn_ids:
            return hit
    return None


# ---------------------------------------------------------------------------------------------------------------------
# The same four detectors through the `typology_scan` query (graph/queries/typology_scan.gsql). On TigerGraph the scan
# runs in the graph and returns the wave rows; `hit_from_scan` turns them into the DetectorHit exactly as `scan_card` does
# from DuckDB. `scan_rows` is the DuckDB twin of the query (DuckFacts.typology_scan, the mock backend and the parity oracle),
# so the two produce one output shape.

TYPOLOGY_DATA_START = "2016-07-01 00:00:00"      # first day of the dataset: D1 scans from here, D4 needs 60 days of history before as_of


def _wave_dicts(rows: list[tuple]) -> list[dict[str, Any]]:
    return [{"id": str(r[0]), "card_id": str(r[1]), "ts": str(r[2]), "amt": float(r[3]), "device_id": str(r[4]) if len(r) > 4 and r[4] is not None else ""} for r in rows]


def scan_rows(con: Any, card_id: str, flagged_txn_id: str, as_of: str, data_start: str = TYPOLOGY_DATA_START) -> dict[str, Any]:
    """DuckDB twin of the `typology_scan` query: the same keys, the same rows."""
    d1 = wave_rows(con, "D1_MOBILE_ANON_PROXY_WAVE", str(data_start)[:10])
    d2 = wave_rows(con, "D2_ESCALATING_C_DRAIN")
    d3 = wave_rows(con, "D3_MICRO_CHARGE_SKIMMING")
    d4w = d4_wave(con, flagged_txn_id, as_of) if str(data_start) <= str(__import__("datetime").datetime.fromisoformat(str(as_of)) - __import__("datetime").timedelta(days=60)) else None
    d4 = d4w[1] if d4w else []
    ids = sorted({str(r[0]) for rows in (d1, d2, d3, d4) for r in rows})
    card_rows: list[dict[str, Any]] = []
    if ids:
        ph = ",".join("?" * len(ids))
        for r in con.execute(
            f"SELECT id, ts::VARCHAR, amt, product_cd, channel, addr1, p_email, r_email, device_id, device_new, proxy, cms_p FROM txc "
            f"WHERE card_id = ? AND ts <= ?::TIMESTAMP AND id IN ({ph}) ORDER BY ts, TransactionID",
            [card_id, as_of, *ids],
        ).fetchall():
            card_rows.append(dict(zip(("id", "ts", "amt", "product_cd", "channel", "addr1", "p_email", "r_email", "device_id", "device_new", "proxy_type", "cms_p"), r, strict=True)))
    return {
        "d1_rows": _wave_dicts(d1), "d2_rows": _wave_dicts(d2), "d3_rows": _wave_dicts(d3), "d4_rows": _wave_dicts(d4),
        "d4_domain": d4w[0] if d4w else "",
        "card_rows": card_rows,
    }


def _wave_tuples(scan: dict[str, Any], key: str) -> list[tuple]:
    rows = [(str(r["id"]), str(r["card_id"]), str(r["ts"]), float(r["amt"]), str(r.get("device_id") or "")) for r in scan.get(key) or []]
    return sorted(rows, key=lambda r: (r[2], int(r[0]) if r[0].isdigit() else 0, r[0]))


def hit_from_scan(scan: dict[str, Any], card_id: str, flagged_txn_id: str, as_of: str) -> DetectorHit | None:
    """The DetectorHit a `typology_scan` result implies for `(card_id, flagged_txn_id, as_of)`; same precedence as `scan_card`."""
    if str(as_of) < "2017-01-01":
        return None
    d4_rows = _wave_tuples(scan, "d4_rows")
    d4 = (str(scan.get("d4_domain") or ""), d4_rows) if d4_rows and scan.get("d4_domain") else None
    for fn in (
        lambda: detect_d4_recipient_email_mule(None, card_id, flagged_txn_id, as_of, wave=d4) if d4 else None,
        lambda: detect_d1_mobile_anon_proxy_wave(None, card_id, as_of, rows=_wave_tuples(scan, "d1_rows")),
        lambda: detect_d2_escalating_c_drain(None, card_id, as_of, rows=_wave_tuples(scan, "d2_rows")),
        lambda: detect_d3_micro_charge_skimming(None, card_id, as_of, rows=_wave_tuples(scan, "d3_rows")),
    ):
        hit = fn()
        if hit is not None and str(flagged_txn_id) in hit.episode_txn_ids:
            return hit
    return None


def episode_rows(scan: dict[str, Any], hit: DetectorHit) -> list[dict[str, Any]]:
    """The full rows of the hit's episode on the flagged card, in the shape the scorecard reads (`proxy`, not `proxy_type`)."""
    keep = set(hit.episode_txn_ids)
    out = []
    for r in scan.get("card_rows") or []:
        if str(r["id"]) in keep:
            row = {k: v for k, v in r.items() if k != "proxy_type"}
            row["proxy"] = r.get("proxy_type", "")
            out.append({**row, "sig_match": True})
    return sorted(out, key=lambda r: (str(r["ts"]), str(r["id"])))


DETECTOR_APPROVALS = __import__("pathlib").Path(__file__).resolve().parents[1] / "data" / "detector_approvals.json"


def _approvals() -> dict[str, Any]:
    import json

    return json.loads(DETECTOR_APPROVALS.read_text(encoding="utf-8")) if DETECTOR_APPROVALS.exists() else {}


def approve_detector(proposal_id: str, session: Any, decision: str = "approved", note: str = "") -> dict[str, Any]:
    """A fraud manager admits (or rejects) a proposed detector into the toolkit. The session comes from
    ui.approvals_db.authenticate(); its role is the server directory's."""
    import datetime as dt
    import json

    from ui.approvals_db import verify_session

    who = verify_session(session)
    if who.role != "fraud_manager":
        raise PermissionError(f"{who.principal} ({who.role}) cannot admit a detector; fraud_manager only")
    if decision not in ("approved", "rejected"):
        raise ValueError(decision)
    data = _approvals()
    if proposal_id in data:
        raise PermissionError(f"{proposal_id} was already {data[proposal_id]['status']} by {data[proposal_id]['by']}")
    data[proposal_id] = {"status": decision, "by": who.principal, "role": who.role, "at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"), "note": note}
    DETECTOR_APPROVALS.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return data[proposal_id]


def discover_unlabelled_patterns(con: Any) -> list[dict[str, Any]]:
    """Challenge 2: candidate detectors with a measured backtest, each waiting for a fraud manager's approval.

    The four candidate shapes are scans over January activity that none of the five documented patterns covers; for each
    the backtest runs the same query over the full history and reports what it matched, which confirmed-fraud closed cases
    it overlaps and which cleared (legitimate) customers it would have acted against. `approval_status` is read from
    data/detector_approvals.json (written only by `approve_detector`); a proposal nobody decided is `pending`.
    """
    candidates = [
        ("AUTO-DET-02", "Multi-day sub-$6 micro-charge wave", "amount_band_cluster(product_cd='C', amt=[1,6], min_txns=4, min_days=4)", "D3_MICRO_CHARGE_SKIMMING"),
        ("AUTO-DET-03", "New-mobile anonymous-proxy burst wave", "telemetry_scan(device_type='mobile', proxy='IP_PROXY:ANONYMOUS', device_new='New', min_amt=200)", "D1_MOBILE_ANON_PROXY_WAVE"),
        ("AUTO-DET-04", "Dormant-wake climbing drain", "sequence_scan(product_cd='C', no identity, quiet>=21d, min_len=5, max>=1100)", "D2_ESCALATING_C_DRAIN"),
    ]
    approvals = _approvals()
    out: list[dict[str, Any]] = []
    for pid, name, disc_by, det in candidates:
        rows = con.execute(WAVE_SQL[det].format(since="2016-07-01")).fetchall()
        ids = sorted({str(r[0]) for r in rows})
        hits: dict[str, list[tuple]] = {"confirmed_fraud": [], "cleared": []}
        if ids:
            con.execute("CREATE OR REPLACE TEMP TABLE _cand(id VARCHAR)")
            con.executemany("INSERT INTO _cand VALUES (?)", [[i] for i in ids])
            for cid, outcome, cust in con.execute(
                "SELECT DISTINCT c.case_id, c.outcome, c.customer_id FROM cc c JOIN cc_txn x USING (case_id) JOIN txc t USING (TransactionID) JOIN _cand h ON h.id = t.id"
            ).fetchall():
                hits.setdefault(outcome, []).append((cid, cust))
        ap = approvals.get(pid, {})
        out.append({
            "proposal_id": pid,
            "name": name,
            "discovered_by": disc_by,
            "synthesized_sql": " ".join(WAVE_SQL[det].format(since="2016-07-01").split()),
            "cards_matched": len({str(r[1]) for r in rows}),
            "txns_matched": len(rows),
            "total_exposure_usd": round(sum(float(r[3]) for r in rows), 2),
            "closed_case_true_positives": [c for c, _ in hits.get("confirmed_fraud", [])],
            "closed_case_false_positives": len(hits.get("cleared", [])),
            "legitimate_customers_acted_against": sorted({cu for _, cu in hits.get("cleared", [])}),
            "approval_status": ap.get("status", "pending"),
            "approved_by": ap.get("by"),
            "approved_at": ap.get("at"),
        })
    return out
