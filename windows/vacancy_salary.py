"""Salary boundaries shared by preview and manual sending."""

from decimal import Decimal
import math


MINIMUM_NET_RUB = 80_000
MAXIMUM_RUB = 300_000


def currency_rates(api_client):
    # Upstream ApiClient is an unhashable dataclass: lru_cache cannot key on it.
    # Keep successful loads on the client itself, without retaining other clients.
    cache = vars(api_client)
    key = "_hh_applicant_tool_currency_rates"
    if key in cache:
        return cache[key]
    # HH quotes currency units per RUB, not RUB per currency unit.
    try:
        rates = {item["code"]: item["rate"]
                 for item in api_client.get("/dictionaries")["currency"]}
    except Exception:
        raise SystemExit("Cannot load HH currency rates; refusing salary checks") from None
    cache[key] = rates
    return rates


def is_amount(value):
    return (type(value) in (int, float) and math.isfinite(value) and value >= 0)


def salary_rejection_reason(vacancy, api_client=None, *, minimum=MINIMUM_NET_RUB, ceiling=MAXIMUM_RUB):
    """The limits are the saved vacancy filters (#15); ceiling None means no upper limit."""
    salary = vacancy.get("salary") or {}
    amounts = [salary.get(key) for key in ("from", "to")
               if is_amount(salary.get(key))]
    currency = salary.get("currency")
    if amounts and ceiling is not None:
        rate = 1 if currency in ("RUR", "RUB") else (
            currency_rates(api_client).get(currency) if api_client is not None else None
        )
        if not is_amount(rate) or rate == 0:
            return "не удалось проверить лимит зарплаты: неизвестен курс валюты"
        # Compare without division or binary floating-point boundary errors.
        if Decimal(str(max(amounts))) > ceiling * Decimal(str(rate)):
            return f"указанная зарплата выше {ceiling:,} ₽ (по курсу HH)".replace(",", " ")
    top = salary.get("to")
    if salary.get("currency") != "RUR" or not isinstance(top, (int, float)):
        return None
    # For a gross salary, 13% is the lowest applicable income-tax rate here.
    # Higher rates only lower the take-home amount further. An unspecified tax
    # basis is treated as net, so borderline offers are left for human review.
    if salary.get("gross") is True:
        below_minimum = top * 87 < minimum * 100
    else:
        below_minimum = top < minimum
    if below_minimum:
        return f"потолок зарплаты не достигает {minimum:,} ₽ на руки".replace(",", " ")
    return None
