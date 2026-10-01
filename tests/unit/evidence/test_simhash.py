from africasignal.evidence.simhash import hamming_distance, simhash

ARTICLE = (
    "The pump price of petrol rose to N870 per litre in Lagos on Monday according to dealers "
    "as depot prices climbed again this week. Marketers said the increase followed a rise in the "
    "exchange rate and supply constraints at several depots across the country, and motorists "
    "queued at filling stations in Ikeja and Surulere."
)


def test_identical_text_has_identical_hash() -> None:
    assert simhash(ARTICLE) == simhash(ARTICLE)


def test_hash_is_a_signed_64_bit_integer() -> None:
    for text in (ARTICLE, "cooking gas price", "a b c d e f g"):
        value = simhash(text)
        assert value is not None and -(2**63) <= value < 2**63


def test_lightly_edited_copy_is_close_and_unrelated_text_is_far() -> None:
    copy = ARTICLE.replace("Monday", "Tuesday") + " Reuters contributed to this report."
    other = (
        "Electricity distribution companies in Nigeria announced a new tariff order for Band A "
        "customers effective next month, the regulator said in a statement issued in Abuja."
    )
    a, b, c = simhash(ARTICLE), simhash(copy), simhash(other)
    assert a is not None and b is not None and c is not None
    assert hamming_distance(a, b) < hamming_distance(a, c)
    assert hamming_distance(a, b) <= 12
    assert hamming_distance(a, c) >= 20


def test_case_and_punctuation_do_not_matter() -> None:
    assert simhash("Petrol price, up!") == simhash("petrol PRICE up")


def test_empty_text_has_no_hash() -> None:
    assert simhash("") is None and simhash("   ...  ") is None


def test_short_text_still_hashes() -> None:
    assert simhash("petrol") is not None


def test_hamming_distance_handles_signed_values() -> None:
    assert hamming_distance(-1, 0) == 64
    assert hamming_distance(5, 5) == 0
    assert hamming_distance(-(2**63), 0) == 1
