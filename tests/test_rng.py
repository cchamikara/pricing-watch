from pricing_watch.rng import rng_for


def draws(seed: int, name: str) -> list[float]:
    return [float(x) for x in rng_for(seed, name).random(5)]


def test_same_seed_and_name_repeats() -> None:
    assert draws(42, "demand") == draws(42, "demand")


def test_streams_differ_by_name_and_seed() -> None:
    assert draws(42, "demand") != draws(42, "competitors")
    assert draws(42, "demand") != draws(43, "demand")


def test_stream_does_not_depend_on_other_subsystems() -> None:
    before = draws(42, "demand")
    rng_for(42, "new-subsystem").random(100)
    assert draws(42, "demand") == before
