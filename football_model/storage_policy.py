"""Explicit transport budgets; these are storage limits, never model settings."""

MIB = 1024 * 1024
MAX_PHYSICAL_BYTES = 1024 * MIB
MAX_LOGICAL_BYTES = 4096 * MIB
SHADOW_WRITE_LIMIT_BYTES = 800 * MIB
DIAGNOSTIC_RESERVE_BYTES = 256 * 1024

# Existing schema-1 manifests froze their declared limits. Recognize only
# published pairs; accepting arbitrary values would weaken manifest validation.
SUPPORTED_MANIFEST_LIMITS = frozenset({
    (2048 * MIB, 500 * MIB),
    (MAX_LOGICAL_BYTES, MAX_PHYSICAL_BYTES),
})
