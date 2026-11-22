"""Neutralize spreadsheet formulas in externally supplied CSV text."""


def csv_text(value: str | None) -> str:
    text = value or ""
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text
