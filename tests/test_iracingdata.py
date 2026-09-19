from app.services.iracingdata import IRacingDataClient


def test_iracingdata_selects_only_race_row_for_customer() -> None:
    payload = {
        "subsession_id": 88756192,
        "season_id": 6444,
        "season_name": "Global Mazda MX-5 Cup by Fanatec - 2026 Season 4",
        "start_time": "2026-09-19T17:30:00Z",
        "track_name": "Okayama International Circuit",
        "event_strength_of_field": 3196,
        "results": [
            {"simsession_name": "PRACTICE", "results": [{"cust_id": 123, "oldi_rating": 1}]},
            {"simsession_name": "QUALIFY", "results": [{"cust_id": 1294360, "oldi_rating": 7707}]},
            {
                "simsession_name": "RACE",
                "results": [
                    {
                        "cust_id": 1294360,
                        "car_id": 67,
                        "car_name": "Global Mazda MX-5 Cup",
                        "oldi_rating": 7707,
                        "newi_rating": 7728,
                        "starting_position": 0,
                        "finish_position": 0,
                        "incidents": 0,
                    }
                ],
            },
        ],
    }

    race = IRacingDataClient().extract_race(payload, 1294360)

    assert race is not None
    assert race.old_irating == 7707
    assert race.new_irating == 7728
    assert race.starting_position_api == 0
    assert race.finish_position_api == 0
    assert race.raw_row["car_id"] == 67

