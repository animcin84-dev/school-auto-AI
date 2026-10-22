"""Типы ошибок SRA. Все ошибки несут машиночитаемый код."""

from __future__ import annotations


class SraError(Exception):
    """Базовая ошибка системы. `code` — стабильный машиночитаемый код."""

    code = "SRA_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


class ValidationError(SraError):
    """Нарушение контракта данных. Такие данные не попадают в хранилище."""

    code = "VALIDATION_ERROR"


class GuardError(SraError):
    """Срабатывание защитного ограждения безопасности (guard).

    Примеры: попытка записать SYNTHETIC в REAL-среду, выдать синтетику
    за VERIFIED, повторное исполнение, переход в заблокированный режим.
    """

    code = "GUARD_ERROR"


class NotFoundError(SraError):
    """Сущность не найдена."""

    code = "NOT_FOUND"
