"""Turn stored menu rows into something a person would say.

"For breakfast you have shahi paneer, roti and rice, and for dessert to go
with it, gulab jamun." Every dish still comes from the row, word for word;
this only drops the everyday fillers (bread/butter/jam, plain tea and coffee,
pickle, papad), joins "with ..." to the dish before it, and pulls desserts and
special drinks into their own clause. If nothing would be left after dropping
the fillers, everything is read.
"""

from __future__ import annotations

import re

# A piece made only of these words is a filler nobody needs read out.
_FILLER = set("""
bread butter jam peanut nutella cereal cereals cornflakes milk fresh tea coffee cofee teacofee
pickle pickles achar papad papadam pappadam papadum poppadom fruit fruits
salad green yoghurt yogurt curd and max
""".split())
_DESSERT = ("jamun", "kheer", "payasam", "halwa", "halwah", "pudding", "crumble", "ice cream", "icecream",
            "custard", "jalebi", "rasgulla", "rasmalai", "ras malai", "ladoo", "laddu", "barfi", "burfi",
            "cake", "brownie", "mousse", "sheera", "kesari", "phirni", "firni", "rabri", "shrikhand",
            "gulab", "sweet dish", "dessert", "pastry", "basundi", "shahi tukda", "malpua")
_FRUIT = ("watermelon", "muskmelon", "melon", "banana", "papaya", "apple", "pineapple", "orange",
          "grapes", "guava", "mango", "pomegranate", "chikoo", "fruit salad")
_DRINK = ("lassi", "lemonade", "juice", "shake", "buttermilk", "butter milk", "chaas", "rose milk",
          "badam milk", "jaljeera", "sharbat", "sherbet", "smoothie", "cold coffee", "hot chocolate")
THERE = "there's "
_STAPLE = ("rice", "roti", "chapati", "chapatti", "chapathi", "phulka", "naan", "kulcha")
_NOT_STAPLE = ("biryani", "biriyani", "pulao", "pulav", "fried rice", "khichdi", "pudding")
_PROPER = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "diwali",
           "holi", "eid", "christmas", "onam", "pongal", "navratri", "dussehra", "ramadan", "nutella")


def _clean(piece: str) -> str:
    piece = re.sub(r"\([^)]*\)", " ", piece)          # "(max 2)", "(Wheat)"
    piece = piece.replace("&", " and ")
    piece = re.sub(r"\s+", " ", piece).strip(" .-:")
    words = piece.lower().split()
    return " ".join(w.capitalize() if w in _PROPER else w for w in words)


def pieces(items: str) -> list[str]:
    """'Penne Pasta, With Cheese & Vegetable Sauce, Dal / Rice' -> dishes, in order."""
    out: list[str] = []
    for raw in re.split(r"[,/;|\n]+", items):
        piece = _clean(raw)
        if not piece:
            continue
        if out and piece.startswith("with "):
            out[-1] = f"{out[-1]} {piece}"
        elif piece not in out:
            out.append(piece)
    return out


def _has(piece: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(w)}", piece) for w in words)


def _filler(piece: str) -> bool:
    words = re.findall(r"[a-z0-9]+", piece.lower())
    return bool(words) and all(w in _FILLER or w.isdigit() for w in words)


def sort_out(meal: str, items: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """(dishes, rice and breads, sweets, drinks) worth saying for one meal."""
    dishes, staples, sweets, drinks = [], [], [], []
    all_pieces = pieces(items)
    for p in all_pieces:
        if _has(p, _DRINK):
            drinks.append(p)
        elif _has(p, _DESSERT):
            sweets.append(p)
        elif _has(p, _FRUIT) and meal in ("lunch", "dinner"):
            sweets.append(p)
        elif _has(p, _STAPLE) and not _has(p, _NOT_STAPLE):
            staples.append(p)
        elif not _filler(p):
            dishes.append(p)
    if not dishes:
        dishes, staples = staples, []
    if not (dishes or sweets or drinks):
        dishes = all_pieces  # only fillers: read them rather than say nothing
    return dishes, staples, sweets, drinks


def and_list(things: list[str]) -> str:
    if len(things) <= 1:
        return "".join(things)
    return ", ".join(things[:-1]) + " and " + things[-1]


def _special(items: str) -> str | None:
    """'Diwali Special Menu' -> 'the Diwali special menu'."""
    p = pieces(items)
    if len(p) == 1 and "special" in p[0]:
        return f"the {p[0]}"
    return None


def what_there_is(meal: str, items: str, lead: str = "you have ") -> str:
    """The part after 'for dinner': 'you have X and Y, and for dessert to go with it, Z'."""
    special = _special(items)
    if special:
        return f"it's {special}"
    dishes, staples, sweets, drinks = sort_out(meal, items)
    sweet_word = "for dessert to go with it" if meal in ("lunch", "dinner") else "for something sweet"
    parts = []
    if dishes:
        parts.append(lead + and_list(dishes)
                     + (f", with {and_list(staples)}" if staples else ""))
    if drinks:
        parts.append(f"to drink, {and_list(drinks)}" if dishes else
                     lead + and_list(drinks) + " to drink")
    if sweets:
        parts.append(f"{sweet_word}, {and_list(sweets)}" if parts else
                     lead + and_list(sweets))
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def one_meal(meal: str, items: str, on_day: str) -> str:
    """'Tomorrow for breakfast you have poha and boiled egg.'"""
    head = f"{on_day[:1].upper()}{on_day[1:]}, for {meal}" if on_day.startswith("on ") else \
        f"{on_day.capitalize()} for {meal}"
    return f"{head} {what_there_is(meal, items)}."


def whole_day(rows: list[tuple[str, str]], on_day: str) -> str:
    """Every meal of a day, one short sentence each."""
    sentences = []
    for i, (meal, items) in enumerate(rows):
        if i == 0:
            head = f"{on_day[:1].upper()}{on_day[1:]}, for {meal}" if on_day.startswith("on ") else \
                f"{on_day.capitalize()} for {meal}"
            sentences.append(f"{head} {what_there_is(meal, items)}.")
        else:
            lead = "And for" if i == len(rows) - 1 else "For"
            sentences.append(f"{lead} {meal} {what_there_is(meal, items, THERE)}.")
    return " ".join(sentences)
