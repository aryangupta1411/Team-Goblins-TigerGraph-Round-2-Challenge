"""Schema Engine (Challenges 11, 12, 13):
- Challenge 11: Data Quality Detective & Tainted Investigations (`schema_engine.data_quality`)
- Challenge 12: Schema Drift Detection, Impact Analysis & Approval Gate (`schema_engine.drift`)
- Challenge 13: Universal Column & Reshape Mapper for Variant Schemas (`schema_engine.mapper`)
- Canonical Normalizer & Round 2 Delta Merger (`schema_engine.normalizer`)
- Four-part Claim Attestation (`schema_engine.attestation`)
"""
from __future__ import annotations

from schema_engine.attestation import AttestedClaim, attest, verify_attestation

__all__ = ["AttestedClaim", "attest", "verify_attestation"]
