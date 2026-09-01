from navidrome_music_adder.domain import normalize_isrc


def test_normalize_isrc() -> None:
    assert normalize_isrc("us-rc1-76-07839") == "USRC17607839"
    assert normalize_isrc(" USRC17607839 ") == "USRC17607839"


def test_reject_invalid_isrc() -> None:
    assert normalize_isrc(None) is None
    assert normalize_isrc("not-an-isrc") is None
