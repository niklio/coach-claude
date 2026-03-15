"""
CdA (coefficient of drag area) estimation from cycling power data.

Physics:
    P = CdA * 0.5 * rho * v^3  +  Crr * m * g * v  +  m * g * grade * v

Rearranged:
    CdA = (P/v - Crr*m*g - m*g*grade) / (0.5 * rho * v^2)

where grade is the decimal slope (e.g. 0.03 for 3% incline).
"""

import logging
from typing import Tuple

import numpy as np

log = logging.getLogger(__name__)

G = 9.81  # m/s^2

MIN_VELOCITY = 3.0    # m/s (~11 km/h) — below this aero signal is swamped by noise
MIN_POWER = 30        # watts — freewheeling / power dropout
MAX_POWER = 1500      # watts — implausible spikes
MAX_GRADE_ABS = 0.08  # 8% — steep grades dominate gravity term, bury aero signal
MIN_SAMPLES = 30      # require at least this many valid samples


class InsufficientDataError(Exception):
    pass


class NoPowerDataError(Exception):
    pass


def _extract_stream(streams: dict, key: str) -> np.ndarray | None:
    entry = streams.get(key)
    if entry is None:
        return None
    return np.array(entry["data"], dtype=float)


def calculate_cda(
    streams: dict,
    mass_kg: float,
    crr: float = 0.004,
    rho: float = 1.225,
) -> Tuple[float, int]:
    """
    Calculate median CdA from Strava activity streams.

    Args:
        streams: dict returned by strava_client.get_activity_streams()
        mass_kg: combined rider + bike mass in kg
        crr: coefficient of rolling resistance (default 0.004)
        rho: air density in kg/m^3 (default 1.225 — sea level, 15°C)

    Returns:
        (cda_m2, n_samples) — median CdA in m² and the number of valid samples used
    """
    velocity = _extract_stream(streams, "velocity_smooth")
    power = _extract_stream(streams, "watts")
    grade_pct = _extract_stream(streams, "grade_smooth")

    if power is None or np.all(power == 0):
        raise NoPowerDataError("Activity has no power meter data — cannot calculate CdA.")

    if velocity is None:
        raise InsufficientDataError("Activity is missing velocity stream.")

    # grade_smooth is in percent (e.g. 3.0 = 3%), convert to decimal
    if grade_pct is None:
        log.warning("No grade stream; assuming flat (grade=0)")
        grade_pct = np.zeros_like(velocity)
    grade = grade_pct / 100.0

    # Align lengths (truncate to shortest)
    n = min(len(velocity), len(power), len(grade))
    velocity = velocity[:n]
    power = power[:n]
    grade = grade[:n]

    # Build validity mask
    valid = (
        (velocity >= MIN_VELOCITY) &
        (power >= MIN_POWER) &
        (power <= MAX_POWER) &
        (np.abs(grade) <= MAX_GRADE_ABS)
    )

    v = velocity[valid]
    p = power[valid]
    g_slope = grade[valid]

    if len(v) < MIN_SAMPLES:
        raise InsufficientDataError(
            f"Only {len(v)} valid samples after filtering (need {MIN_SAMPLES}). "
            "Activity may be too short, flat data, or missing power."
        )

    # Instantaneous CdA at each sample
    P_rolling = crr * mass_kg * G * v
    P_gravity = mass_kg * G * g_slope * v
    P_aero = p - P_rolling - P_gravity

    # Exclude samples where computed aero power is negative
    aero_valid = P_aero > 0
    P_aero = P_aero[aero_valid]
    v_aero = v[aero_valid]

    if len(v_aero) < MIN_SAMPLES:
        raise InsufficientDataError(
            f"Only {len(v_aero)} samples with positive aero power (need {MIN_SAMPLES})."
        )

    cda_instant = P_aero / (0.5 * rho * v_aero ** 3)

    # IQR outlier rejection
    q1, q3 = np.percentile(cda_instant, [25, 75])
    iqr = q3 - q1
    inliers = (cda_instant >= q1 - 1.5 * iqr) & (cda_instant <= q3 + 1.5 * iqr)
    cda_clean = cda_instant[inliers]

    if len(cda_clean) < MIN_SAMPLES:
        raise InsufficientDataError(
            f"Only {len(cda_clean)} inlier samples after IQR filtering (need {MIN_SAMPLES})."
        )

    cda = float(np.median(cda_clean))
    log.info("CdA calculated: %.4f m² from %d samples", cda, len(cda_clean))
    return cda, len(cda_clean)
