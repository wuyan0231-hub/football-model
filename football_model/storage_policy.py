"""Explicit transport budgets; these are storage limits, never model settings."""

MIB = 1024 * 1024
# A bounded one-round expansion, derived from the measured r11 league writes.
# Keep the whole current transport tree below GitHub's 2 GiB push bound; cloud
# save also measures the actual new reachable pack before attempting a push.
MAX_PHYSICAL_BYTES = 1792 * MIB
MAX_LOGICAL_BYTES = 8192 * MIB
SHADOW_WRITE_LIMIT_BYTES = 1536 * MIB
DIAGNOSTIC_RESERVE_BYTES = 256 * 1024

# Existing schema-1 manifests froze their declared limits. Recognize only
# published pairs; accepting arbitrary values would weaken manifest validation.
SUPPORTED_MANIFEST_LIMITS = frozenset({
    (2048 * MIB, 500 * MIB),
    (4096 * MIB, 1024 * MIB),
    (MAX_LOGICAL_BYTES, MAX_PHYSICAL_BYTES),
})
