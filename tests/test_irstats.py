import json
from pathlib import Path

from app.services.irstats import parse_irstats_page


def test_irstats_parser_extracts_structured_race_fields() -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "irstats_page.json").read_text(
            encoding="utf-8"
        )
    )

    page = parse_irstats_page(payload, cust_id=1294360)

    assert page.has_more is False
    assert len(page.races) == 1
    race = page.races[0]
    assert race.subsession_id == 88756192
    assert race.series_name == "Global Mazda MX-5 Cup by Fanatec"
    assert race.car_name == "Global Mazda MX-5 Cup"
    assert race.track_name == "Okayama International Circuit"
    assert race.finish_position == 1
    assert race.incidents == 0
    assert race.sof == 3196

