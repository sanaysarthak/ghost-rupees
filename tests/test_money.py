import pytest

from core.money import MoneyError, Paisa, apply_bps, gst_inclusive, paisa_to_rupees_str, rupees_to_paisa


def test_rupees_to_paisa_basic():
    assert rupees_to_paisa("236.00") == 23600
    assert rupees_to_paisa("20000") == 2000000
    assert rupees_to_paisa("100.5") == 10050
    assert rupees_to_paisa("0.01") == 1


def test_rupees_to_paisa_rejects_float():
    with pytest.raises(MoneyError):
        rupees_to_paisa(236.00)


def test_rupees_to_paisa_int_is_whole_rupees():
    assert rupees_to_paisa(500) == 50000


def test_paisa_to_rupees_str_roundtrip():
    assert paisa_to_rupees_str(Paisa(23600)) == "236.00"
    assert paisa_to_rupees_str(Paisa(1)) == "0.01"
    assert paisa_to_rupees_str(Paisa(-500)) == "-5.00"


def test_apply_bps():
    # 10% of 20,000.00 = 2,000.00
    assert apply_bps(Paisa(2_000_000), 1000) == 200_000
    # 0.1% of 5,00,000.00 = 500.00
    assert apply_bps(Paisa(50_000_000), 10) == 50_000
    # 18% of 20,000.00 = 3,600.00
    assert apply_bps(Paisa(2_000_000), 1800) == 360_000


def test_gst_inclusive():
    # 20,000.00 base + 18% GST = 23,600.00
    assert gst_inclusive(Paisa(2_000_000), 1800) == 2_360_000


def test_no_float_ever_returned():
    # every function above must return an int subtype, never float
    assert isinstance(rupees_to_paisa("1.23"), int)
    assert isinstance(apply_bps(Paisa(100), 1000), int)
    assert isinstance(gst_inclusive(Paisa(100), 1800), int)
