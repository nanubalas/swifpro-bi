"""Light-touch UK business-identifier validators used by company setup forms."""
import re
from django.core.exceptions import ValidationError


def validate_vat_number(value):
    if not value:
        return
    s = value.replace(" ", "").upper()
    if s.startswith("GB"):
        s = s[2:]
    if not re.fullmatch(r"\d{9}(\d{3})?", s):
        raise ValidationError("Enter a valid UK VAT number - 9 or 12 digits, with an optional 'GB' prefix.")


def validate_company_number(value):
    if not value:
        return
    s = value.replace(" ", "").upper()
    if not re.fullmatch(r"(?:[A-Z]{2}\d{6}|\d{8})", s):
        raise ValidationError("Enter a valid UK company number - 8 digits, or 2 letters followed by 6 digits.")


def validate_utr(value):
    if not value:
        return
    s = value.replace(" ", "")
    if not re.fullmatch(r"\d{10}", s):
        raise ValidationError("Enter a valid UTR - 10 digits.")


def validate_phone(value):
    if not value:
        return
    if not re.fullmatch(r"[0-9+()\-\s]{7,20}", value.strip()):
        raise ValidationError("Enter a valid phone number.")
