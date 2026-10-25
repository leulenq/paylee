import pytest

from app.money import format_dollars, parse_dollars


@pytest.mark.parametrize(
    "text, cents",
    [("212.50", 21_250), ("$1,234.5", 123_450), (" 89 ", 8_900), ("0.01", 1), ("25000", 2_500_000)],
)
def test_parse_dollars_accepts_common_formats(text, cents):
    assert parse_dollars(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "0", "-5", "1.234", "NaN", "Infinity", "25000.01"])
def test_parse_dollars_rejects_bad_amounts(text):
    with pytest.raises(ValueError):
        parse_dollars(text)


def test_format_dollars():
    assert format_dollars(123_456) == "$1,234.56"
    assert format_dollars(5) == "$0.05"
