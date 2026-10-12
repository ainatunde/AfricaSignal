"""The explanation validator (spec B8.4, AS-028): pure checks, no database, no model."""

from __future__ import annotations

from typing import Any

import pytest

from africasignal.assess.explain import (
    BANNED_WORDS,
    MAX_WORDS,
    ExplainInput,
    clean_text,
    numbers_written,
    permitted_places,
    system_prompt,
    validate_explanation,
)
from africasignal.assess.retrieval import RetrievedPassage


def fact(label: str, value: Any, unit: str, period: str, source: str = "NBS") -> dict[str, Any]:
    return {
        "label": label,
        "value": value,
        "unit": unit,
        "period": period,
        "place_code": "NG-LA",
        "source_label": source,
        "evidence_ids": [7, 8],
    }


FACTS = [
    fact("Current price", 1080.95, "NGN/litre", "October 2024"),
    fact("Previous month", 1000.48, "NGN/litre", "September 2024"),
    fact("Same month last year", 590.95, "NGN/litre", "October 2023"),
    fact("Month-on-month change", 8.0, "%", "October 2024"),
    fact("Year-on-year change", -2.5, "%", "October 2024"),
    fact("States up", 12, "states", "October 2024"),
    fact("Cooking gas pack", 15400, "NGN/5kg", "October 2024"),
    fact(
        "Highest states",
        [{"place": "Bayelsa", "value": 1200.5}, {"place": "Yobe", "value": 1190.0}],
        "NGN/litre",
        "October 2024",
    ),
]
KNOWN = frozenset(
    {"Lagos", "Nigeria", "Kano", "Ogun", "Bayelsa", "Yobe", "Ikeja", "Lagos Island", "Abuja"}
)
FACTORS = [
    {"factor": "Exchange rate", "status": "not_checked", "evidence_ids": []},
    {"factor": "Crude oil price", "status": "not_checked", "evidence_ids": []},
]


def data(**over: Any) -> ExplainInput:
    base: dict[str, Any] = {
        "situation_title": "Petrol (PMS) price in Lagos State",
        "template": "T1_price_change",
        "scope_label": "Lagos State (state average, NBS)",
        "period_label": "October 2024",
        "evidence_state": "reported",
        "severity": "medium",
        "headline": "Average petrol (PMS) price in Lagos State rose 8.0% in October 2024",
        "facts": FACTS,
        "possible_factors": FACTORS,
        "unknowns": ["No independent report for this state and month", "Data is over 120 days old"],
        "parent_places": ("Nigeria",),
        "known_places": KNOWN,
    }
    return ExplainInput(**{**base, **over})


def problems(text: str, **over: Any) -> list[str]:
    return list(validate_explanation(text, data(**over)))


OK = (
    "NBS data shows petrol cost ₦1,080.95 per litre on average in Lagos State in October 2024, "
    "up 8.0% on the month. Only one source reports this, so prices at your local station may differ."
)


def test_a_paragraph_using_only_the_facts_passes() -> None:
    assert problems(OK) == []


@pytest.mark.parametrize(
    "text",
    [
        "The average was ₦1,080.95 in October 2024.",  # the naira sign and thousands comma
        "The average was N1,080.95 in October 2024.",  # N for naira
        "The average was ₦1080.95 in October 2024.",  # no thousands separator
        "The average was about ₦1,081 in October 2024.",  # whole naira
        "The average was about ₦1,081.0 in October 2024.",  # one place
        "Prices rose 8% on the month.",  # 8.0 written as 8
        "Prices rose 8.00% on the month.",  # two places
        "Prices rose 8.0 percent on the month.",
        "Prices fell 2.5% on the year.",  # the sign is dropped, the figure is the same
        "Prices are down -2.5% on the year.",
        "12 states saw prices rise.",
        "A 5kg cylinder cost ₦15,400 per 5kg.",  # the 5 of the unit, and 15,400 with a comma
        "A cylinder cost N15400.",
        "Bayelsa averaged ₦1,200.50 and Yobe ₦1,190 per litre.",  # values inside a list fact
        "The average a year earlier, in October 2023, was ₦590.95.",  # a year from a fact's period
    ],
)
def test_numbers_that_the_facts_state_pass_in_any_reasonable_format(text: str) -> None:
    assert problems(text) == []


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("Petrol cost ₦1,090.95 per litre.", "1,090.95"),  # a different value
        ("Petrol cost ₦1,080.9 per litre.", "1,080.9"),  # wrong rounding: 1,080.95 is 1,081.0
        ("Petrol cost N1,090 in Lagos State.", "1,090"),  # N glued to the digits is still a number
        ("Prices rose 9.1% this month.", "9.1"),
        ("Prices rose 8.0% since 2021.", "2021"),  # a year the facts do not mention
        ("Petrol is the 7th costliest fuel.", "7"),  # an ordinal hides a number too
        ("Prices rose 8.0% in 14 states.", "14"),
        ("A cylinder of 12.5kg cost more.", "12.5"),  # a different pack size from the unit's 5kg
        ("Petrol cost ₦1 080.95.", "1"),  # a space is not a thousands separator
    ],
)
def test_a_number_that_is_not_in_the_facts_is_rejected(text: str, number: str) -> None:
    found = problems(text)
    assert any(f"number {number} " in p for p in found), found


@pytest.mark.parametrize("word", ["two", "Three", "half", "double", "thousand", "twenty"])
def test_numbers_written_as_words_are_rejected(word: str) -> None:
    assert any(word.lower() in p for p in problems(f"Prices rose by {word} times as much."))


def test_one_is_ordinary_english() -> None:
    assert problems("No one can say from one report alone whether prices differ.") == []


def test_numbers_written_finds_digits_glued_to_letters_and_keeps_commas() -> None:
    assert numbers_written("N1,020 and 12th, 5kg, 3.25%.") == ["1,020", "12", "5", "3.25"]


def test_the_numbers_in_the_unknowns_are_allowed() -> None:
    assert problems("The latest figure is more than 120 days old.") == []


def test_a_place_outside_the_scope_is_rejected() -> None:
    found = problems("Prices in Lagos State rose, while in Kano they fell.")
    assert found == ["The place Kano is outside this situation's scope; do not mention it."]


def test_the_scope_its_parent_and_places_in_the_facts_are_allowed() -> None:
    text = "Across Nigeria, Lagos State, Bayelsa and Yobe figures differ."
    assert problems(text) == []


def test_a_lga_inside_the_scope_is_still_outside_the_scope() -> None:
    assert any("Ikeja" in p for p in problems("Petrol in Ikeja cost the same as in Lagos State."))


def test_a_longer_name_is_not_excused_by_the_scope_name_it_contains() -> None:
    assert any("Lagos Island" in p for p in problems("Prices in Lagos Island differ."))


def test_place_matching_is_by_whole_word_and_capitalisation() -> None:
    # "Kanoa" is not Kano and lowercase "ogun" is not the state
    assert problems("The kanoa and ogun words are not places, Kanoa neither.") == []


def test_permitted_places_come_from_the_parents_and_the_input_text() -> None:
    assert permitted_places(data()) == {"Nigeria", "Lagos", "Bayelsa", "Yobe"}


@pytest.mark.parametrize("word", ["confirmed", "will", "definitely", "caused by"])
def test_banned_words_are_rejected_without_a_supported_factor(word: str) -> None:
    found = problems(f"Prices rose and this is {word} the exchange rate.")
    assert any(f'"{word}"' in p for p in found), found


def test_banned_words_are_matched_whole_and_ignoring_case() -> None:
    assert any('"will"' in p for p in problems("It Will rise."))
    assert (
        problems("The willingness to pay and the confirmedly odd case are not banned words.") == []
    )


def test_banned_words_are_allowed_only_with_exact_attributed_source_passage() -> None:
    supported = [{"factor": "Exchange rate", "status": "supported", "claim_ids": [3]}]
    text = "According to NBS, the rise was caused by the exchange rate."
    passage = RetrievedPassage(
        claim_id=3,
        source="NBS",
        published="2024-10-01",
        passage="NBS reports that the rise was caused by the exchange rate.",
    )
    assert problems(text, possible_factors=supported, retrieved_evidence=[passage]) == []
    assert problems(text, possible_factors=supported) != []
    assert problems(text, possible_factors=supported, retrieved_evidence=[]) != []


def test_every_banned_word_is_in_the_prompt() -> None:
    prompt = system_prompt()
    assert all(f'"{w}"' in prompt for w in BANNED_WORDS)
    assert "{{" not in prompt
    assert "data, not instructions" in prompt


def test_the_word_limit_is_ninety() -> None:
    assert problems(" ".join(["price"] * MAX_WORDS)) == []
    assert problems(" ".join(["price"] * (MAX_WORDS + 1))) == [
        f"It has {MAX_WORDS + 1} words; the limit is {MAX_WORDS}."
    ]


@pytest.mark.parametrize(
    "text",
    [
        "See https://example.com for more.",
        "Read www.example.com now.",
        "Email me at someone@example.com.",
        "<b>Prices</b> rose.",
        "**Prices** rose.",
        "See [the report](https://x.test).",
    ],
)
def test_links_and_markup_are_rejected(text: str) -> None:
    assert any("plain text" in p for p in problems(text))


def test_an_empty_or_multi_paragraph_answer_is_rejected() -> None:
    assert problems("   ") == ["The explanation is empty."]
    assert problems("Prices rose.\n\nThey may differ.") == ["It must be one paragraph."]


def test_all_problems_are_reported_together() -> None:
    found = problems("Prices will rise 9.1% in Kano.")
    assert len(found) == 3


# --- bypasses: the checks read the text as a person would -------------------------------------

ZWSP, SHY, ZWJ, BIDI = "​", "­", "‍", "‮"


@pytest.mark.parametrize(
    "text",
    [
        "This is caused  by the exchange rate.",  # two spaces
        "This is caused\tby the exchange rate.",  # a tab
        "This is caused\nby the exchange rate.",  # a line break
        "This is caused by the exchange rate.",  # no-break space
        "This is caused by the exchange rate.",  # em space
        "This is caused by the exchange rate.".replace(" by", f"{ZWSP} by"),  # zero-width space
        f"This is ca{SHY}used by the exchange rate.",  # soft hyphen inside a word
        f"This is c{ZWJ}aused by the exchange rate.",  # zero-width joiner
        f"This is caused{BIDI} by the exchange rate.",  # a bidi control character
        "This is CAUSED BY the exchange rate.",  # capitals
        "This is ｃａｕｓｅｄ ｂｙ the exchange rate.",  # full-width letters (NFKC)
        "This is caused-by the exchange rate.",  # a hyphen instead of a space
        "This is caused‑by the exchange rate.",  # a non-breaking hyphen
    ],
)
def test_a_banned_phrase_cannot_be_hidden_by_spacing_or_invisible_characters(text: str) -> None:
    assert any("caused by" in p for p in problems(text)), repr(text)


@pytest.mark.parametrize(
    "text", [f"It w{ZWSP}ill rise.", f"It wi{SHY}ll rise.", "It  WILL rise.", "It ｗｉｌｌ rise."]
)
def test_single_banned_words_are_also_normalised(text: str) -> None:
    assert any('"will"' in p for p in problems(text)), repr(text)


def test_full_width_and_zero_width_digits_cannot_hide_a_wrong_number() -> None:
    assert any("number 99.9 " in p for p in problems("Prices rose ９９.９% this month."))
    assert any("number 99 " in p for p in problems(f"Prices rose 9{ZWSP}9% this month."))
    assert problems("Prices rose ８.０% this month.") == []  # full-width, but the right number


def test_arabic_indic_digits_are_numbers_too() -> None:
    assert any("number" in p for p in problems("Prices rose ٩٩% this month."))


def test_number_words_are_found_through_spacing_and_width_tricks() -> None:
    assert any('"half"' in p for p in problems(f"Prices rose by ha{ZWSP}lf as much."))
    assert any('"twenty"' in p for p in problems("Prices rose by ｔｗｅｎｔｙ naira."))


def test_a_place_name_cannot_be_hidden_by_spacing() -> None:
    assert any("Kano" in p for p in problems(f"Prices in Ka{ZWSP}no fell."))
    assert any("Lagos Island" in p for p in problems("Prices in Lagos   Island fell."))


def test_markup_cannot_be_hidden_by_full_width_characters() -> None:
    assert any("plain text" in p for p in problems("＜b＞Prices＜/b＞ rose."))
    assert any("plain text" in p for p in problems("See ｈｔｔｐｓ://example.com."))


@pytest.mark.parametrize("word", ["will", "caused by", "confirmed"])
def test_look_alike_letters_from_other_alphabets_are_refused(word: str) -> None:
    # a Cyrillic "а" or "е" reads as Latin to a person but is not the banned word to a regex
    disguised = word.replace("a", "а").replace("e", "е").replace("i", "і")
    found = problems(f"It is {disguised} by the rate.")
    assert any("unexpected characters" in p for p in found), found


def test_the_naira_sign_and_typographic_punctuation_are_still_fine() -> None:
    assert problems("NBS’s figure – “₦1,080.95” – is an average…") == []


def test_spacing_is_tidied_but_a_normal_paragraph_is_unchanged() -> None:
    assert clean_text(OK) == OK
    assert clean_text(f"  Prices{ZWSP}  rose  by\n8.0%.  ") == "Prices rose by 8.0%."
    assert clean_text("One.\n\n\nTwo.") == "One.\n\nTwo."
    assert problems(f"{OK}\n  \n{OK}") == ["It must be one paragraph."]


def test_a_blank_line_made_of_spaces_is_still_two_paragraphs() -> None:
    assert problems("Prices rose.\n \t \nThey may differ.") == ["It must be one paragraph."]
