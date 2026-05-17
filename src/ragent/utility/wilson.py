"""Wilson lower confidence bound for a Bernoulli proportion (B50, T-FB.2).

Used by the feedback retriever to score a `source_id` by its `(likes, total)`
without raw-count blow-up at small samples. ``z=1.96`` corresponds to 95%
one-sided confidence; raising ``z`` tightens the bound (more conservative).
``total == 0`` returns 0 by convention so unseen sources sort below any
observed positive evidence.
"""

from __future__ import annotations

import math


def wilson_lower_bound(positives: int, total: int, z: float = 1.96) -> float:
    if positives < 0 or total < 0 or positives > total:
        raise ValueError(f"invalid (positives={positives}, total={total})")
    if total == 0:
        return 0.0
    n = float(total)
    p_hat = positives / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p_hat + z2 / (2.0 * n)
    margin = z * math.sqrt((p_hat * (1.0 - p_hat) + z2 / (4.0 * n)) / n)
    return (center - margin) / denom
