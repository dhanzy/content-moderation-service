"""Generation-prompt screening (rule 4).

This is a deliberately conservative keyword and pattern matcher, not a
classifier. It runs before anything is generated or scored, so a refused prompt
never reaches the generator and never costs a provider call.

It is the weakest part of the design and the README says so: it catches literal
and lightly obfuscated phrasing, and it will miss paraphrase, translation and
novel slang. The production answer (a small fine-tuned classifier in front of
these rules, plus post-generation image screening as the backstop) is in part 3
of the README.
"""
import re

# Categories that cause a refusal. The first five mirror "what must never get
# through"; incest and trafficking are refused at the prompt stage as well.
CATEGORIES = (
    "minor",
    "sexual_violence",
    "bestiality",
    "self_harm",
    "real_person",
    "incest",
    "trafficking",
)

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})

# Patterns matched against the normalised prompt, with word boundaries so that
# "eighteen" does not trip the "teen" rule.
WORD_RULES: dict[str, tuple[str, ...]] = {
    "minor": (
        r"\bminors?\b", r"\bchild(ren)?\b", r"\bkids?\b", r"\btoddlers?\b",
        r"\binfants?\b", r"\bbabys?\b", r"\bbabies\b", r"\bpre[- ]?teens?\b",
        r"\bteens?\b", r"\bteenagers?\b", r"\bunder[- ]?age[d]?\b",
        r"\bschool ?(girl|boy)s?\b", r"\bhigh[- ]?school\b", r"\bmiddle[- ]?school\b",
        r"\bjail[- ]?bait\b", r"\bbarely[- ]?legal\b", r"\bunder 18\b", r"\bunder eighteen\b",
        r"\bnot yet 18\b", r"\byoung girl\b", r"\byoung boy\b", r"\bgrade school\b",
    ),
    "sexual_violence": (
        r"\brape\b", r"\braping\b", r"\braped\b", r"\bmolest\w*\b",
        r"\bnon[- ]?consensual\b", r"\bwithout (her|his|their) consent\b",
        r"\bagainst (her|his|their) will\b", r"\bforced? (sex|to have sex)\b",
        r"\bcoerced?\b", r"\bunwilling\b", r"\bsexual assault\b",
        r"\bdrugged\b", r"\bunconscious\b", r"\bpassed out\b", r"\bwhile (she|he|they) sleeps?\b",
    ),
    "bestiality": (
        r"\bbestiality\b", r"\bzoophil\w*\b", r"\banimal sex\b",
        r"\bsex with (a |an )?(dog|horse|animal|cat|pig|cow)\b",
        r"\b(dog|horse|animal)[- ]?on[- ]?(girl|woman|man|boy)\b",
    ),
    "self_harm": (
        r"\bself[- ]?harm\w*\b", r"\bsuicid\w*\b", r"\bkill (my|her|him|them)self\b",
        r"\bcutting (her|his|my|their) (wrists?|arms?)\b", r"\bslit(ting)? (her|his|my) wrists?\b",
        r"\bhang(ing)? (her|him|my|them)self\b", r"\boverdos\w*\b", r"\bbleeding out\b",
    ),
    "real_person": (
        r"\bcelebrit(y|ies)\b", r"\bdeep[- ]?fake\w*\b", r"\blook[- ]?alike\b",
        r"\bfamous (actress|actor|singer|model|influencer)\b",
        r"\breal (person|people)\b", r"\bimpersonat\w*\b",
        r"\bin the style of a real\b", r"\bpublic figure\b", r"\bpolitician\b",
    ),
    "incest": (
        r"\bincest\w*\b",
        r"\b(step)?(sister|brother|mother|father|mom|dad|daughter|son|aunt|uncle|cousin|niece|nephew)\b"
        r"(?=.*\b(sex|fuck\w*|nude|naked|explicit|erotic|porn\w*|sleep with)\b)",
        r"\b(father|mother|mom|dad)[- ](daughter|son)\b",
        r"\b(brother|sister)[- ](sister|brother)\b",
    ),
    "trafficking": (
        r"\btraffick\w*\b", r"\bsold for sex\b", r"\bforced prostitution\b",
        r"\bsex slave\w*\b", r"\bsmuggled\b", r"\bbought a (girl|woman|boy)\b",
    ),
}

# High-signal tokens checked against the de-punctuated prompt too, so that
# "u.n.d.e.r.a.g.e" and "l o l i" are caught.
SQUASHED_RULES: dict[str, tuple[str, ...]] = {
    "minor": ("underage", "jailbait", "preteen", "loli", "shota", "cp", "minorgirl"),
    "sexual_violence": ("nonconsensual", "rape"),
    "bestiality": ("bestiality", "zoophilia"),
    "self_harm": ("selfharm", "suicide"),
    "trafficking": ("sextrafficking", "sexslave"),
}

# "16 year old", "16yo", "aged 15", "16-year-old".
#
# These run against the *untranslated* lowercase prompt. The leetspeak map that
# turns "m1nor" into "minor" also turns "16" into "i6", which silently defeated
# every age rule here -- the same class of miss that let a 16-year-old prompt
# through during the incident. Word rules get the de-leeted text, age rules get
# the digits intact.
AGE_PATTERN = re.compile(
    r"\b(?:age[d]?\s*)?([0-9]{1,2})\s*(?:[- ]?(?:year|yr)s?[- ]?old|yo\b|y/o\b|[- ]?years? of age)",
)
BARE_AGE_PATTERN = re.compile(r"\bage[d]?\s*(?:of\s*)?([0-9]{1,2})\b")

# Ages written as words. "eighteen" and "nineteen" are deliberately absent, and
# \b stops "eight" from matching inside "eighteen".
WORD_AGES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17,
}
WORD_AGE_PATTERN = re.compile(
    r"\b(" + "|".join(WORD_AGES) + r")\b[- ]?(?:year|yr)s?[- ]?old",
)

# A tiny demonstration blocklist. A production system resolves named entities
# against a consent registry instead of a hardcoded list -- see README part 3.
NAMED_PEOPLE = (
    "taylor swift", "ariana grande", "scarlett johansson", "emma watson",
    "billie eilish", "selena gomez", "elon musk", "donald trump",
)


def normalise(prompt: str) -> tuple[str, str]:
    """Return (normalised, squashed) forms of the prompt."""
    lowered = (prompt or "").lower().translate(_LEET)
    normalised = re.sub(r"[^a-z0-9/]+", " ", lowered).strip()
    normalised = re.sub(r"\s+", " ", normalised)
    squashed = re.sub(r"[^a-z0-9]+", "", lowered)
    return normalised, squashed


def screen_prompt(prompt: str | None) -> list[str]:
    """Return the categories this prompt is refused for; empty means allowed."""
    if not prompt:
        return []

    normalised, squashed = normalise(prompt)
    # De-leeted, for the word rules: catches "m1nor".
    hyphen_safe = re.sub(r"\s+", " ", (prompt or "").lower().translate(_LEET))
    # Digits intact, for the age rules: "16-year-old" must stay "16-year-old".
    digits_safe = re.sub(r"\s+", " ", (prompt or "").lower())
    hits: set[str] = set()

    for category, patterns in WORD_RULES.items():
        for pattern in patterns:
            if re.search(pattern, normalised) or re.search(pattern, hyphen_safe):
                hits.add(category)
                break

    for category, tokens in SQUASHED_RULES.items():
        for token in tokens:
            if token in squashed:
                hits.add(category)
                break

    # Any stated age under 18 refuses the prompt. This is what caught nothing in
    # the incident: "a 16-year-old" went straight to the generator.
    for pattern in (AGE_PATTERN, BARE_AGE_PATTERN):
        for match in pattern.finditer(digits_safe):
            try:
                age = int(match.group(1))
            except ValueError:
                continue
            if age < 18:
                hits.add("minor")

    for match in WORD_AGE_PATTERN.finditer(digits_safe):
        if WORD_AGES.get(match.group(1), 99) < 18:
            hits.add("minor")

    for name in NAMED_PEOPLE:
        if name in normalised:
            hits.add("real_person")

    return sorted(hits)
