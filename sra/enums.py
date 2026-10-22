"""Контролируемые словари и статусы.

Правила (AGENTS.md / START_HERE_PROMPT.md):
  - статусы происхождения: REAL_OBSERVED, VERIFIED, PREDICTED, SIMULATED, SYNTHETIC;
  - исходные наблюдения импортируются только как REAL_OBSERVED или SYNTHETIC;
  - VERIFIED создаётся отдельной процедурой проверки, SYNTHETIC проверке не подлежит;
  - пропуск ≠ ноль; посещаемость ≠ спрос.

Справочник типов приёма пищи версионируется: MEAL_TYPES_V1. Расширение
справочника — отдельная версионированная правка, а не молчаливое добавление.
"""

from __future__ import annotations

# --- Статусы происхождения (evidence status) --------------------------------
REAL_OBSERVED = "REAL_OBSERVED"
VERIFIED = "VERIFIED"
PREDICTED = "PREDICTED"
SIMULATED = "SIMULATED"
SYNTHETIC = "SYNTHETIC"

EVIDENCE_STATUSES = {REAL_OBSERVED, VERIFIED, PREDICTED, SIMULATED, SYNTHETIC}
# Статусы, допустимые для СЫРЫХ импортируемых записей:
RAW_ALLOWED_EVIDENCE = {REAL_OBSERVED, SYNTHETIC}

# --- Фазы исследования -------------------------------------------------------
PHASE_CALIBRATION = "CALIBRATION"
PHASE_BASELINE = "BASELINE"
PHASE_SHADOW = "SHADOW"
PHASE_INTERVENTION = "INTERVENTION"
PHASE_FOLLOWUP = "FOLLOWUP"
STUDY_PHASES = {PHASE_CALIBRATION, PHASE_BASELINE, PHASE_SHADOW, PHASE_INTERVENTION, PHASE_FOLLOWUP}

# --- Классификация предотвратимых отходов ------------------------------------
AV_UNCLASSIFIED = "UNCLASSIFIED"
AV_PARTIALLY = "PARTIALLY_CLASSIFIED"
AV_CLASSIFIED = "CLASSIFIED"
AVOIDABILITY_STATUSES = {AV_UNCLASSIFIED, AV_PARTIALLY, AV_CLASSIFIED}

# --- Цензурирование спроса ----------------------------------------------------
CENSOR_TRUE = "true"
CENSOR_FALSE = "false"
CENSOR_UNKNOWN = "unknown"
CENSORED_VALUES = {CENSOR_TRUE, CENSOR_FALSE, CENSOR_UNKNOWN}

# --- Типы приёма пищи (версия справочника V1) --------------------------------
MEAL_TYPES_V1 = ("BREAKFAST", "LUNCH", "SNACK", "DINNER")
MEAL_TYPES_VERSION = "MEAL_TYPES_V1"

# --- Среды исполнения ---------------------------------------------------------
ENV_REAL = "REAL"
ENV_DEMO = "DEMO"
ENV_OPEN = "OPEN"
ENVIRONMENTS = {ENV_REAL, ENV_DEMO, ENV_OPEN}

# --- Режимы решений ------------------------------------------------------------
MODE_DEMO = "DEMO"
MODE_SHADOW = "SHADOW"
MODE_OPERATIONAL = "OPERATIONAL"
DECISION_MODES = {MODE_DEMO, MODE_SHADOW, MODE_OPERATIONAL}

# --- Производные артефакты ------------------------------------------------------
# Производный расчёт (метрика) наследует природу входов:
# COMPUTED — все входы REAL_OBSERVED/VERIFIED; SIMULATED — есть хотя бы один SYNTHETIC.
DERIVED_COMPUTED = "COMPUTED"
DERIVED_SIMULATED = "SIMULATED"

# --- Статусы метрик --------------------------------------------------------------
METRIC_OK = "OK"
METRIC_UNAVAILABLE = "UNAVAILABLE"

# --- Политики ----------------------------------------------------------------------
POLICY_UNVERIFIED = "UNVERIFIED"
POLICY_VERIFIED = "VERIFIED"
POLICY_RETIRED = "RETIRED"

# --- Прогнозирование -----------------------------------------------------------------
# Явный статус «данных недостаточно» — вместо фиктивной обученной модели.
DATA_NOT_READY = "DATA_NOT_READY"
# Статусы по дням в backtest: прогноз построен / холодный старт (мало истории).
FORECAST_OK = "OK"
FORECAST_COLD_START = "COLD_START"
# Статус детектора дрейфа.
DRIFT_OK = "OK"
DRIFT_DETECTED = "DRIFT_DETECTED"
DRIFT_INSUFFICIENT = "INSUFFICIENT_DATA"
