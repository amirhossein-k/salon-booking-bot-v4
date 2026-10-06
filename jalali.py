"""Solar Hijri calendar using the Jalaali break-year algorithm.

Calendar calculations are restricted to Gregorian years 1900..2100.
No network or external calendar package is required.
"""
from datetime import date

MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
          "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")
WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
BREAKS = (-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
          1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178)


def _div(a, b):
    return abs(a) // abs(b) * (-1 if (a < 0) != (b < 0) else 1)


def _mod(a, b):
    return a - _div(a, b) * b


def new_year(jy):
    """Return Gregorian date of Farvardin 1, with astronomical break corrections."""
    gy = jy + 621
    if not 1899 <= gy <= 2101:
        raise ValueError("Supported Gregorian years: 1900..2100")
    leap_j = -14
    jp = BREAKS[0]
    for jm in BREAKS[1:]:
        jump = jm - jp
        if jy < jm:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    return date(gy, 3, 20 + leap_j - leap_g)


def from_gregorian(day):
    if not 1900 <= day.year <= 2100:
        raise ValueError("Supported Gregorian years: 1900..2100")
    jy = day.year - 621
    start = new_year(jy)
    if day < start:
        jy -= 1
        start = new_year(jy)
    offset = (day - start).days
    if offset < 186:
        return jy, offset // 31 + 1, offset % 31 + 1
    offset -= 186
    return jy, offset // 30 + 7, offset % 30 + 1


def persian_digits(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def numeric(day):
    y, m, d = from_gregorian(day)
    return persian_digits(f"{y:04d}/{m:02d}/{d:02d}")


def label(day):
    _, m, d = from_gregorian(day)
    return f"{WEEKDAYS[day.weekday()]} {persian_digits(d)} {MONTHS[m - 1]}"
