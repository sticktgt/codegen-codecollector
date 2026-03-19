"""Пример шаблона функции, которая формирует пользовательское текстовое уведомление."""

from __future__ import annotations


def build_localized_message(entity_id: str, subject: str, assignee: str, action_label: str) -> str:
    """Возвращает короткое русскоязычное сообщение по заданному шаблону.

    Функция показывает минимальный паттерн:
    - короткая сигнатура;
    - понятный docstring;
    - явная формулировка результата для пользователя.
    """
    return f"{subject} {entity_id} {action_label} на {assignee}."
