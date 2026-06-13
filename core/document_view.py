"""Reusable Header + Lines document view.

A small, declarative config that any header/lines document (purchase order, sales
order, invoice, journal, transfer, ...) builds and hands to the shared template
`documents/header_lines.html`. The template renders the two-panel UX (left nav:
Header + collapsible Lines; right: header detail / line summary table / full line
detail) identically for every module, so no module reinvents the layout.

This module is pure presentation config - no DB writes, no business logic.
"""
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Field:
    label: str
    value: object = ""


@dataclass
class Section:
    title: str
    fields: list = field(default_factory=list)   # list[Field]


@dataclass
class Column:
    label: str
    align: str = "start"          # start | center | end


@dataclass
class Cell:
    value: object = ""
    align: str = "start"


@dataclass
class Line:
    number: int
    summary: list = field(default_factory=list)   # list[Cell], parallel to columns
    detail: list = field(default_factory=list)     # list[Section]
    status: str = ""


@dataclass
class Action:
    label: str
    url: str
    style: str = "outline-secondary"   # bootstrap button suffix
    icon: str = ""
    method: str = "get"                # "get" -> link, "post" -> csrf form button


@dataclass
class DocumentView:
    title: str
    subtitle: str = ""
    status: str = ""
    status_tone: str = "secondary"     # bootstrap badge tone
    back_url: str = "/"
    sections: list = field(default_factory=list)   # header sections: list[Section]
    columns: list = field(default_factory=list)     # line summary columns: list[Column]
    lines: list = field(default_factory=list)       # list[Line]
    actions: list = field(default_factory=list)     # list[Action]
    line_noun: str = "Line"
    empty_lines_msg: str = "This document has no lines yet."


def money(amount, code="GBP"):
    """Display-ready money string (builders pre-format so the template stays dumb)."""
    if amount is None:
        return "-"
    return f"{code} {Decimal(amount):,.2f}"
