"""Общие фабрики тестовых данных (валидные строки по контракту)."""

BASE_SESSION = {
    "session_id": "S1", "school_id": "SCH-1", "service_date": "2026-10-01",
    "timezone": "Asia/Almaty", "meal_type": "LUNCH",
    "attendance_count": "100", "meals_requested": "95", "meals_served": "95",
    "demand_censored": "false", "evidence_status": "REAL_OBSERVED",
    "study_phase": "BASELINE", "source_ref": "SRC-1",
    "observed_at": "2026-10-01T13:30:00+05:00",
    "recorded_at": "2026-10-01T15:00:00+05:00",
}

BASE_OBSERVATION = {
    "observation_id": "O1", "session_id": "S1", "dish_id": "D1",
    "prepared_mass_g": "10000", "served_mass_g": "9000",
    "production_waste_g": "400", "serving_waste_g": "600",
    "plate_waste_g": "900", "avoidable_waste_g": "500",
    "avoidability_status": "CLASSIFIED", "evidence_status": "REAL_OBSERVED",
    "source_ref": "SRC-1",
    "observed_at": "2026-10-01T13:30:00+05:00",
    "recorded_at": "2026-10-01T15:00:00+05:00",
}


def make_session(**overrides) -> dict:
    row = dict(BASE_SESSION)
    row.update(overrides)
    return row


def make_observation(**overrides) -> dict:
    row = dict(BASE_OBSERVATION)
    row.update(overrides)
    return row
