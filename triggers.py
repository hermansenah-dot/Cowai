from __future__ import annotations

import re


def analyze_input(text: str) -> int:
    """
    Return an integer mood delta based on message content.

    Convention:
      +2 strong positive
      +1 mild positive
       0 neutral / no mood change
      -1 mild negative
      -2 strong negative

    Designed for real-time chat bots.
    Deterministic, fast, and predictable.
    """

    t = text.lower().strip()
    t = re.sub(r"\s+", " ", t)

    def has_any(phrases: list[str]) -> bool:
        return any(re.search(rf"\b{re.escape(p)}\b", t) for p in phrases)

    # ==================================================
    # STRONG NEGATIVE — insults, hostility, aggression
    # ==================================================
    if has_any([
        "stupid", "idiot", "moron", "dumb", "brain dead",
        "useless", "worthless", "pathetic", "trash",
        "shut up", "fuck you", "go to hell",
        "kill yourself", "kys",
        "nobody cares", "you suck",
        "cringe", "embarrassing",
    ]):
        return -2

    # ==================================================
    # MILD NEGATIVE — dismissive, passive aggressive
    # ==================================================
    if has_any([
        "wrong", "that's wrong", "that makes no sense",
        "nonsense", "what are you talking about",
        "nah", "nope", "meh",
        "bro what", "seriously?",
        "this is dumb", "this sucks",
        "waste of time", "lame",
        "unimpressive", "boring",
    ]):
        # Avoid overreaction to short replies
        if t in {"no", "nah", "nope", "meh"}:
            return -1
        return -1

    # ==================================================
    # STRONG POSITIVE — praise, affection, excitement
    # ==================================================
    if has_any([
        "you're amazing", "you’re amazing",
        "you're awesome", "you’re awesome",
        "you are incredible", "legend",
        "i love this", "i love you",
        "this is perfect", "this is awesome",
        "so good", "fantastic", "brilliant",
        "you nailed it", "nailed it",
        "10/10",
    ]):
        return +2

    # ==================================================
    # MILD POSITIVE — politeness, encouragement
    # ==================================================
    if has_any([
        "thanks", "thank you", "thank u", "ty",
        "nice", "cool", "neat",
        "well done", "good job",
        "appreciate it", "much appreciated",
        "sounds good", "looks good",
        "okay nice", "not bad",
    ]):
        return +1

    # ==================================================
    # FRUSTRATION / CONFUSION — neutral (important!)
    # ==================================================
    if has_any([
        "i don't get it", "i dont get it",
        "i'm stuck", "im stuck",
        "confusing", "confused",
        "doesn't work", "it doesn't work",
        "why isn't this working",
        "this is frustrating",
        "i tried everything",
    ]):
        return 0

    return 0
