from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import template

register = template.Library()

CURRENCY_SYMBOLS = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
}

TWO_PLACES = Decimal("0.01")

@register.filter
def money(value, currency_code="GBP"):
    try:
        # Keep the value as Decimal end-to-end to avoid binary float rounding.
        amount = Decimal(value if value is not None else "0").quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )
        symbol = CURRENCY_SYMBOLS.get(currency_code, currency_code + " ")
        return f"{symbol}{amount:,.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return value