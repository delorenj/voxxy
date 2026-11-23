"""Edition flag and the hosted edition's "no real people" rule for described voices.

One build serves every Voxxy deployment. ``VOX_EDITION`` picks the rules:

- ``hosted`` (default, strict): a voice comes from your own recording (made live,
  with a consent challenge) or from a description. A description that names a
  real person is refused by ``refuse_real_person``.
- ``self``: the operator runs it on their own hardware and is responsible for the
  voices they make. The web app offers clip upload and the description check is off.

An unknown ``VOX_EDITION`` value falls back to ``hosted``: when in doubt, strict.

The name check is a heuristic, deliberately cheap and deliberately over-eager. A
false positive costs the user one rephrase and the refusal says how; a miss is
the thing the hosted rule exists to prevent. It is not an identity check.
"""

from __future__ import annotations

import os
import re

EDITION_HOSTED = "hosted"
EDITION_SELF = "self"


def current_edition() -> str:
    raw = os.environ.get("VOX_EDITION", "").strip().lower()
    return EDITION_SELF if raw == EDITION_SELF else EDITION_HOSTED


def is_hosted() -> bool:
    return current_edition() == EDITION_HOSTED


# ---------------------------------------------------------------- name check

# "sounds like X", "voice of X", "impression of X", ... Each is followed by the thing the voice
# resembles. It is fine when that is a plain description ("sounds like a tired old man") and a
# refusal when it is anything else ("sounds like morgan freeman", "voice of Oprah").
_COMPARISON = re.compile(
    r"\b(?:"
    r"sounds?\s+like|sounded\s+like|sounding\s+like|"
    r"voice\s+of|in\s+the\s+voice\s+of|impression\s+of|"
    r"impersonat(?:e|es|ing|ion\s+of)|imitat(?:e|es|ing|ion\s+of)|mimic(?:s|king)?|"
    r"in\s+the\s+style\s+of|reminds?\s+(?:me\s+)?of|reminiscent\s+of|similar\s+to|"
    r"clon(?:e|ed|ing)\s+(?:of|from)|as\s+spoken\s+by|as\s+played\s+by|"
    r"modell?ed\s+(?:on|after)|based\s+on|like"
    r")\s+([A-Za-z][A-Za-z'.-]*)",
    re.IGNORECASE,
)

# What may follow a comparison phrase without naming anybody.
_PLAIN_AFTER_COMPARISON = frozenset("""
a an the some someone somebody anyone everyone people one my your his her their our its this that these
those he she it they you i we very really quite more less slightly almost nearly kind sort as if though
no not there what how when
""".split())

# "<Name>-style": "an Obama-style speaker" names somebody, "news-style" and "radio-style" do not.
_STYLE_SUFFIX = re.compile(r"\b([A-Za-z]+)-style\b", re.IGNORECASE)
_PLAIN_STYLE = frozenset("""
news radio tv television documentary podcast cartoon anime storybook classic retro vintage old modern
formal casual conversational audiobook announcer narrator dramatic theater theatre stage movie film
trailer commercial ad advert broadcast corporate customer service teacher professor coach
""".split())

# Things a voice is commonly likened to that are not people ("smooth like butter").
_PLAIN_AFTER_LIKE = frozenset("""
honey butter velvet silk satin gravel sandpaper caramel chocolate whiskey smoke syrup glass steel
water thunder wind bells bell music a song cream milk coffee
""".split())

# Capitalised words that describe a voice rather than name a person: languages, nationalities,
# regions, sentence openers and the like. A run of capitalised words containing any of these is
# not read as a name ("American English", "Young Woman", "Southern Californian").
_DESCRIPTIVE = frozenset("""
a an the and or but with in on of for to from at by as is are was were it its this that these those
i he she they we you my your his her their our
young old older elderly middle aged teen teenage child childlike boy girl man woman male female adult
voice voices speaker narrator tone pace accent register pitch
warm cool deep soft low high bright dark smooth rough raspy husky gravelly breathy calm kind gentle
steady slow fast quick measured clear crisp friendly formal casual professional neutral energetic
cheerful serious playful sad happy tired confident shy bold quiet loud cold dry rich thin
american british english australian canadian irish scottish welsh indian african asian european
southern northern eastern western midwestern coastal californian texan new york london
spanish french german italian portuguese russian chinese japanese korean arabic hindi dutch swedish
latin latino latina mexican brazilian cuban jamaican nigerian kenyan
north south east west central
monday tuesday wednesday thursday friday saturday sunday
""".split())

# Two or more capitalised words in a row ("Morgan Freeman", "Samuel L. Jackson" via the L.).
_CAP_WORD = r"[A-Z][a-z]+(?:['-][A-Za-z]+)*\.?"
_CAP_RUN = re.compile(rf"\b{_CAP_WORD}(?:\s+(?:{_CAP_WORD}|[A-Z]\.))+")

REFUSAL_REASON = (
    "The hosted edition only makes voices that do not belong to a real person. "
    "Describe the voice itself instead: age, pace, tone and accent "
    "(for example: a calm woman in her forties, slow and warm, with a light Irish accent). "
    "Leave out names and who it should sound like."
)


def refuse_real_person(description: str) -> str | None:
    """The reason to refuse ``description`` (it seems to name a real person), or ``None``.

    The reason is plain text meant for the person who typed it. ``None`` means the
    description passes the heuristic; that is not a guarantee it names nobody.
    """
    text = description.strip()
    for match in _COMPARISON.finditer(text):
        follower = match.group(1).lower().strip(".'-")
        if follower in _PLAIN_AFTER_COMPARISON:
            continue
        if match.group(0).lower().startswith("like"):
            if follower in _DESCRIPTIVE or follower in _PLAIN_AFTER_LIKE:
                continue
        return REFUSAL_REASON
    for match in _STYLE_SUFFIX.finditer(text):
        if match.group(1).lower() not in _PLAIN_STYLE and match.group(1).lower() not in _DESCRIPTIVE:
            return REFUSAL_REASON
    for match in _CAP_RUN.finditer(text):
        words = [w.strip(".'-").lower() for w in re.split(r"\s+", match.group(0))]
        # A name is two neighbours that are both not voice vocabulary ("Young Morgan Freeman"
        # still has "Morgan Freeman"; "Young Woman" and "American English" have none).
        if any(a not in _DESCRIPTIVE and b not in _DESCRIPTIVE for a, b in zip(words, words[1:])):
            return REFUSAL_REASON
    return None
