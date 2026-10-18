"""Fridge2Fork: type what's in the fridge, a local model (via Ollama) suggests recipes.

Usage: python3 app.py   then open http://localhost:8000
Saved recipes live in recipes.json next to this file.
"""
import html
import http.client
import json
import os
import random
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA = "http://localhost:11434/api/generate"
MODEL = os.environ.get("MODEL", "gemma3")
PORT = int(os.environ.get("PORT", 8000))
HERE = os.path.dirname(os.path.abspath(__file__))
FAVS = os.path.join(HERE, "recipes.json")
BACKUPS = os.path.join(HERE, "backups")
KEEP_BACKUPS = 30
favs_lock = threading.Lock()

PROMPT = """You are a practical {who} who hates wasting food.
Ingredients on hand: {ingredients}
Spices in the cupboard: {spices}
Other cupboard items: {pantry}
{favourites}
Suggest {task}. Use up as many of the fridge ingredients as possible.
Cupboard items are there to help, not required.
Season only with the spices listed above; do not call for any other spice.
A spice is only seasoning: "taco spice" does not mean tacos. Never name a dish after a spice or build it around one.
Strongly prefer recipes that need nothing beyond what is listed.
Soft or ageing veg is still good food: perfect for soups, sauces, mash, fritters and pies.
Oil, butter, flour, sugar and water are always available: never list them in "also_need".
Anything else a recipe needs that is not listed above (tortillas, rice, pasta, bread, cream...) MUST go in "also_need". Never write "assumed".
Name each dish honestly: a risotto has rice, a taco has a tortilla. If the base is missing, it is a different dish.
Meal: {meal}. Diet: {diet}. Max total time: {max_time} minutes. Servings: {servings}.
{freeze}
{styles}
{avoid}
{extra}
Reply with JSON only, in exactly this shape:
{{
  "recipes": [
    {{
      "name": "short dish name",
      "pitch": "why this works, max 12 words",
      "minutes": 25,
      "uses": ["ingredients from the list that this recipe uses"],
      "also_need": ["anything beyond the list and basic staples, or empty"],
      "steps": ["short steps, 3-6 of them, one sentence each"],
      "based_on": "name of the saved recipe this adapts, or empty",
      "changes": ["what was swapped or left out from that saved recipe, or empty"],
      "freezable": true,
      "freezer": "freezes how and how long, or why best eaten fresh, max 12 words"{side_field}
    }}
  ]
}}
"""

# Left alone, a small model answers almost everything with a hash, stew, bake or scramble.
# So the code picks a different style for each recipe, and the model fills it in.
COTTAGE = "cottage pie: a veg filling topped with mash made from any veg that mashes"

STYLES = {
    "breakfast": ["savoury pancakes", "omelette or frittata", "breakfast tacos", "loaded toast", "savoury porridge",
                  "breakfast wrap", "shakshuka-style eggs in sauce", "fritters", "muffins", "breakfast bowl",
                  "smoothie"],
    "snack": ["dip with dippers", "fritters", "toastie", "skewers", "quesadilla", "savoury muffins",
              "stuffed bites", "flatbread", "spring rolls", "loaded toast"],
    "meal": ["stir-fry", "soup", "warm salad", "curry", "fritters", "wraps or tacos", "pasta dish", "rice bowl",
             "fried rice", "noodle dish", "skewers", "stuffed vegetables", "flatbread pizza", "toastie",
             "risotto", "tart or quiche", "dumplings", "pie", "burger or patties", "grain salad",
             # rescue dishes (see RESCUE): made for soft, ageing or odd bits of veg
             COTTAGE, "veggie ragu: finely chopped veg simmered into a sauce",
             "one-tray roast", "cheesy gratin", "hand pies or pasties"],
}

# styles that only make sense with a certain base: a risotto without rice isn't a risotto.
# Any one of the words must be in the fridge or cupboard (flour, eggs etc. count via STAPLES/fridge).
STYLE_NEEDS = {
    "risotto": {"rice", "arborio"}, "fried rice": {"rice"}, "rice bowl": {"rice"},
    "pasta dish": {"pasta", "spaghetti", "penne", "macaroni", "fusilli", "lasagne", "lasagna", "linguine"},
    "noodle dish": {"noodle", "ramen", "udon", "soba"},
    "wraps or tacos": {"tortilla", "wrap", "taco", "pitta", "pita"}, "breakfast tacos": {"tortilla", "taco"},
    "breakfast wrap": {"tortilla", "wrap"}, "quesadilla": {"tortilla"},
    "tart or quiche": {"pastry"}, "pie": {"pastry", "potato"}, "hand pies or pasties": {"pastry"},
    # almost any root veg or cauliflower makes the topping; the rest goes in the filling
    COTTAGE: {"potato", "cauliflower", "parsnip", "carrot", "pumpkin", "squash",
                                                    "swede", "turnip", "celeriac", "bean"},
    "cheesy gratin": {"cheese", "cheddar", "parmesan", "cream", "milk"},
    "smoothie": {"banana", "berry", "berrie", "apple", "mango", "pear", "spinach", "kale", "yoghurt", "yogurt", "fruit"}, "spring rolls": {"pastry", "wrapper", "paper"},
    "toastie": {"bread", "sourdough", "loaf", "toast"}, "loaded toast": {"bread", "sourdough", "loaf", "toast"},
    "omelette or frittata": {"egg"}, "shakshuka-style eggs in sauce": {"egg"},
    "savoury porridge": {"oat", "porridge", "rice"},
    "grain salad": {"rice", "quinoa", "couscou", "bulgur", "barley", "farro", "grain", "lentil"},
}


# dishes made for using up odds and ends; picked twice as often as the others
RESCUE = {COTTAGE, "veggie ragu: finely chopped veg simmered into a sauce", "one-tray roast", "cheesy gratin",
          "hand pies or pasties", "fritters", "soup", "fried rice", "smoothie"}


def pick_styles(form, k):
    """k different styles that can be made from what's on hand, rescue dishes weighted double."""
    pool = styles_for(form)
    picked = []
    while len(picked) < k:
        left = [st for st in pool if st not in picked]
        picked += random.choices(left, weights=[2 if st in RESCUE else 1 for st in left])
    return picked


def styles_for(form):
    """The styles for this meal that can really be made from what's on hand."""
    on_hand = words_in(f"{form['ingredients']} {' '.join(form.get('pantry') or [])}") | STAPLES
    styles = STYLES.get(form.get("meal"), STYLES["meal"])
    ok = [st for st in styles if st not in STYLE_NEEDS or STYLE_NEEDS[st] & on_hand]
    return ok if len(ok) >= 2 else styles


STYLE_LINE = """Recipe 1 style: {a}. Recipe 2 style: {b}. Each must really be that kind of dish.
Only if a style really can't work with these ingredients, pick another style, but not a hash, stew, bake or scramble."""

AUTO_STYLE_LINE = """If recipe 1 adapts a saved recipe, keep it the same kind of dish. Recipe 2 style: {b}."""

# one recipe per request: on a laptop CPU, the first idea arrives in half the time
STYLE_LINE_1 = """Style: {a}. It must really be that kind of dish.
Only if that style really can't work with these ingredients, pick another, but not a hash, stew, bake or scramble."""

DISH_LINE_1 = """The cook asked for a {dish}: the recipe must be a {dish}."""

SIDE_LINE_1 = """The recipe is a main dish PLUS a side dish that goes with it, in "side".
The side must be {a}{leftover}. It should use ingredients the main does not, so together they use up more of the fridge."""

DISH_LINE = """The cook asked for a {dish}: BOTH recipes must be a {dish}, and two clearly different ones.
Not a hash, stew, bake or scramble unless that is what was asked for."""

CAFE_EXTRA = ('This is for a small cafe: make each one sellable as a "today\'s special", '
              'and put a menu-style name in "name".')

FAV_AUTO = """Saved recipes this cook already loves. Their ingredients are NOT on hand: only the
"Ingredients on hand" list above is in the fridge. If one suits what's on hand, adapt it
(swap or leave out what's missing) and put its name in "based_on":
{list}
"""

FAV_PICK = """Both recipes MUST be versions of this saved recipe the cook loves, adapted to what's on hand.
Its ingredients are NOT on hand: only the "Ingredients on hand" list above is in the fridge.
Keep it the same kind of dish: if it is a soup, both must be soups.
Swap or leave out what's missing, list those swaps in "changes", and set "based_on" to its name:
{list}
"""


EXTRACT = """Pull the recipe out of this text copied from a web page. Use ONLY what is in the text;
ignore adverts, life stories and comments. Do not invent anything.
Reply with JSON only, in exactly this shape:
{{"name": "dish name", "minutes": 30, "ingredients": ["one ingredient with its amount per item"], "steps": ["one step per item"]}}

TEXT:
{text}
"""

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Fridge2Fork/1.0"}


def load_favs():
    try:
        with open(FAVS) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_favs(favs):
    # copy the current file aside first, so a wrong save or delete can be undone
    if os.path.exists(FAVS):
        os.makedirs(BACKUPS, exist_ok=True)
        shutil.copy2(FAVS, os.path.join(BACKUPS, time.strftime("recipes-%Y-%m-%d-%H%M%S.json")))
        for old in sorted(os.listdir(BACKUPS))[:-KEEP_BACKUPS]:
            os.remove(os.path.join(BACKUPS, old))
    # write to a temp file and swap it in, so a crash mid-save can't leave half a file
    tmp = FAVS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(favs, f, indent=2, ensure_ascii=False)
    os.replace(tmp, FAVS)


# words that say nothing about which food it is, so they mustn't make a saved recipe "match"
NOISE = {"cup", "tbsp", "tsp", "tablespoon", "teaspoon", "pound", "ounce", "gram", "kilo", "litre",
         "and", "for", "the", "with", "fresh", "chopped", "sliced", "diced", "large", "small", "half",
         "salt", "pepper", "black", "oil", "olive", "butter", "flour", "sugar", "water", "taste",
         "vegetable", "little", "finely", "roughly", "thinly", "crushed", "minced", "grated", "peeled", "medium",
         "handful", "clove", "piece", "bunch", "optional", "serve", "serving", "plu", "about", "into", "cut"}

# different words for the same thing in a recipe: a shallot will do where an onion is asked for.
# Every word on the left is read as the word on the right.
SAME_AS = {"shallot": "onion", "scallion": "onion", "leek": "onion", "broth": "stock", "bouillon": "stock",
           "lime": "lemon", "courgette": "zucchini", "aubergine": "eggplant", "coriander": "cilantro",
           "capsicum": "bell", "prawn": "shrimp", "chilli": "chili", "chile": "chili", "mince": "ground",
           "rocket": "arugula", "spud": "potato", "potatoe": "potato", "tomatoe": "tomato"}

# always assumed to be in the kitchen (the prompt says so too)
STAPLES = {"oil", "olive", "butter", "flour", "sugar", "water", "salt", "pepper"}


def words_in(text):
    # crude singular so "eggs" matches "egg yolks", then swaps so "shallots" counts as "onion"
    words = {w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]{3,}", text.lower())}
    return {SAME_AS.get(w, w) for w in words}


def food_words(text):
    return words_in(text) - NOISE


def matching_favs(ingredients, cupboard=()):
    """Saved recipes that fit this fridge, best first, as (lines on hand, lines that count, recipe).
    One shared word ("chicken") isn't enough: the model would borrow the rest of that recipe's list."""
    fridge = food_words(ingredients)
    have_words = fridge | food_words(" ".join(cupboard))
    scored = []
    for f in load_favs():
        if len(food_words(" ".join(f["ingredients"])) & fridge) < 2:
            continue
        # optional extras don't count either way
        lines = [x for x in f["ingredients"] if "optional" not in x.lower()]
        # a line with no real food left in it ("25g butter", "salt and pepper") is a staple: you have it
        have = sum(1 for x in lines if not food_words(x) or food_words(x) & have_words)
        scored.append((have, len(lines) or 1, f))
    # best share of the recipe on hand first, so a long recipe doesn't win just by being long
    scored.sort(key=lambda s: -s[0] / s[1])
    return scored


AUTO_MIN_SHARE = 0.4
# above this share you can more or less make the saved recipe as it is, so it becomes recipe 1
STRONG_SHARE = 0.6

FAV_STRONG = """The cook can almost make their own saved recipe "{name}" right now.
They have everything except: {missing}.
Recipe 1 MUST be this recipe, kept as close to the original as possible: only leave out or swap what's missing.
Set "based_on" to "{name}" and list the swaps in "changes".
- {name}: {ingredients}
"""

# basics a dish is built on: if the steps use one that isn't on hand, it goes in "also need"
BASES = {"tortilla": "tortillas", "wrap": "wraps", "rice": "rice", "pasta": "pasta", "spaghetti": "spaghetti",
         "noodle": "noodles", "bread": "bread", "pastry": "pastry", "cream": "cream", "milk": "milk",
         "cheese": "cheese", "egg": "eggs", "coconut": "coconut milk", "stock": "stock"}


# a dish built on one of these needs it on hand: tacos need tortillas, a risotto needs rice.
# ("wrap" and "toast" are left out: "wrap in foil" and "toast the seeds" aren't bases)
BULK = {"tortilla": "tortilla", "taco": "tortilla", "burrito": "tortilla", "quesadilla": "tortilla",
        "enchilada": "tortilla", "rice": "rice", "risotto": "rice", "paella": "rice",
        "pilaf": "rice", "pasta": "pasta", "spaghetti": "pasta", "penne": "pasta", "lasagne": "pasta",
        "noodle": "noodle", "ramen": "noodle", "bread": "bread", "crouton": "bread",
        "sandwich": "bread", "pastry": "pastry", "quiche": "pastry", "tart": "pastry"}


def side_of(recipe):
    side = recipe.get("side")
    return side if isinstance(side, dict) else {}


def recipe_text(recipe):
    """Name and steps of the main and its side, for word checks."""
    side = side_of(recipe)
    return " ".join(map(str, [recipe.get("name", ""), *(recipe.get("steps") or []),
                              side.get("name", ""), *(side.get("steps") or [])]))


def missing_bases(recipe, on_hand):
    """Bases the recipe's name or steps lean on that aren't in the fridge or cupboard."""
    words = words_in(recipe_text(recipe))
    return {BULK[w] for w in words & BULK.keys() if BULK[w] not in on_hand}


def dish_words(dish):
    """'wraps or tacos' -> {'wrap', 'taco'}: words that should show up in a recipe of that kind."""
    return words_in(dish) - {"dish", "and"}


def strong_match(form):
    """A saved recipe the cook can nearly make as it is: (recipe, missing lines, fridge items it leaves over).
    Only on a first ask in Auto with no kind of dish chosen; "Different ideas" should be different."""
    if form.get("base") or form.get("dish") or form.get("avoid"):
        return None
    cupboard = (form.get("pantry") or []) + (form.get("spices") or [])
    for have, total, f in matching_favs(form["ingredients"], cupboard)[:1]:
        if have / total >= STRONG_SHARE:
            have_words = food_words(form["ingredients"] + " " + " ".join(cupboard))
            missing = [x for x in f["ingredients"] if "optional" not in x.lower()
                       and food_words(x) and not food_words(x) & have_words]
            used = food_words(" ".join(f["ingredients"]))
            leftover = [x.strip() for x in re.split(r"[,\n;]+", form["ingredients"])
                        if food_words(x) and not food_words(x) & used]
            return f, missing, leftover
    return None


def favourites_block(form, strong=None):
    """Pick which saved recipes go into the prompt; a small context only has room for a couple."""
    base = form.get("base") or ""
    favs = load_favs()
    if base == "none" or not favs:
        return ""
    if strong:
        f, missing, _ = strong
        return FAV_STRONG.format(name=f["name"], missing=", ".join(missing) or "nothing",
                                 ingredients=", ".join(f["ingredients"]))
    if base:
        picked = [f for f in favs if f["name"] == base][:1]
        lines = [f"- {f['name']}: {', '.join(f['ingredients'])}. Steps: {' '.join(f['steps'])}" for f in picked]
        return FAV_PICK.format(list="\n".join(lines)) if picked else ""
    # auto: only saved recipes you could mostly make; weaker matches are still offered as buttons to pick
    cupboard = (form.get("pantry") or []) + (form.get("spices") or [])
    good = [f for have, total, f in matching_favs(form["ingredients"], cupboard) if have / total >= AUTO_MIN_SHARE]
    lines = [f"- {f['name']}: {', '.join(f['ingredients'])}" for f in good[:2]]
    return FAV_AUTO.format(list="\n".join(lines)) if lines else ""


def ollama_json(prompt, valid, num_ctx=2560, num_predict=1000, temperature=0.7, good=None):
    """Ask the local model for JSON, retrying until valid(reply) is true.
    good(reply) is a softer wish: retry for it too, but settle for a valid reply on the last try."""
    body = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        # small context keeps gemma3 inside a ~7 GB laptop's RAM
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
    }).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    fallback = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                text = json.load(r)["response"]
        except (ConnectionError, http.client.HTTPException, urllib.error.URLError) as e:
            print(f"  Ollama dropped ({e.__class__.__name__}), waiting 10s", flush=True)
            time.sleep(10)
            continue
        try:
            reply = json.loads(text)
            if valid(reply):
                if good is None or good(reply):
                    return reply
                if fallback:
                    return reply  # two tries is enough for a soft wish
                fallback = reply
                print(f"  {MODEL} built a dish on something you don't have, asking again", flush=True)
                continue
        except (json.JSONDecodeError, AttributeError):
            pass
        print(f"  bad reply from {MODEL}, retrying ({attempt + 1}/3)", flush=True)
    if fallback:
        return fallback
    raise RuntimeError(f"{MODEL} gave no usable reply - is Ollama running?")


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s)))).strip()


def find_recipe(node):
    """Walk a page's JSON-LD for the first schema.org Recipe (it can hide in @graph or mainEntity)."""
    if isinstance(node, list):
        for n in node:
            if r := find_recipe(n):
                return r
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
            return node
        for v in node.values():
            if isinstance(v, (list, dict)) and (r := find_recipe(v)):
                return r
    return None


def steps_of(ins):
    # recipeInstructions can be a string, a list of strings, HowToSteps, or HowToSections of steps
    if isinstance(ins, str):
        return [s for s in (clean(x) for x in re.split(r"</p>|<br\s*/?>|\n", ins)) if s]
    if isinstance(ins, dict):
        return steps_of(ins.get("itemListElement") or ins.get("text") or "")
    if isinstance(ins, list):
        out = []
        for i in ins:
            if isinstance(i, dict) and "itemListElement" in i:
                out += steps_of(i["itemListElement"])
            elif s := clean((i.get("text") or i.get("name") or "") if isinstance(i, dict) else i):
                out.append(s)
        return out
    return []


def minutes_of(iso):
    m = re.match(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?", str(iso or ""))  # e.g. PT1H15M
    return int(m[1] or 0) * 60 + int(m[2] or 0) if m and (m[1] or m[2]) else ""


def import_text(text):
    r = ollama_json(EXTRACT.format(text=text[:3500]),
                    lambda o: o.get("name") and isinstance(o.get("ingredients"), list) and isinstance(o.get("steps"), list)
                    and o["ingredients"] and o["steps"],
                    num_predict=800, temperature=0.2)
    return {"name": r["name"], "minutes": r.get("minutes") or "", "ingredients": r["ingredients"],
            "steps": r["steps"], "how": MODEL}


def import_url(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        page = r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    # most recipe sites embed a machine-readable recipe card for search engines: exact and instant
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            rec = find_recipe(json.loads(block))
        except json.JSONDecodeError:
            continue
        if rec and rec.get("recipeIngredient") and rec.get("recipeInstructions"):
            return {"name": clean(rec.get("name", "")),
                    "minutes": minutes_of(rec.get("totalTime") or rec.get("cookTime")),
                    "ingredients": [clean(x) for x in rec["recipeIngredient"]],
                    "steps": steps_of(rec["recipeInstructions"]), "how": "recipe card"}
    # no recipe card: let the model read the page text, starting near the ingredients
    text = clean(re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", page))
    i = text.lower().find("ingredient")
    return import_text(text[max(0, i - 300):] if i > 0 else text)


SHOP_EXTRA = """The cook is standing in a shop looking at reduced-price specials that must be eaten today.
The "Ingredients on hand" are those specials, with prices; the cupboard items are already at home.
They are on a tight budget{budget}. Each recipe should buy only a few specials, keep the cost down,
and put the specials it buys in "uses"."""

PRICE = re.compile(r"([$£€])\s?(\d+(?:[.,]\d{1,2})?)|(\d+[.,]\d{2})\b")


def to_money(x):
    try:
        return max(float(str(x).replace(",", ".")), 0)
    except ValueError:
        return 0


def specials(text):
    """'chicken thighs $3.50, spinach 1.20' -> [('chicken thighs', 3.5, '$'), ('spinach', 1.2, '')]"""
    out = []
    for item in re.split(r"[,\n;]+", text):
        m = PRICE.search(item)
        if m:
            price = float((m.group(2) or m.group(3)).replace(",", "."))
            out.append((PRICE.sub("", item).strip(" -:@"), price, m.group(1) or ""))
    return out


def shopping(recipe, shop):
    """What to buy for this recipe and what it costs, worked out in code, not by the model."""
    used = food_words(" ".join(map(str, (recipe.get("uses") or []) + (side_of(recipe).get("uses") or []))))
    buy = [{"name": n, "price": p} for n, p, _ in shop if food_words(n) & used]
    return {"buy": buy, "cost": round(sum(b["price"] for b in buy), 2)}


FREEZE_LINE = """Cook {total} portions: {servings} to eat now and {extra} to freeze for lunches or another dinner.
So every recipe MUST freeze well (set "freezable": true) and the steps should say how to portion and freeze them."""


# a main plus a side uses up more of the fridge; the code picks the kind of side
SIDES = ["crispy oven chips or crisps", "crunchy slaw", "roasted vegetables", "fresh salad", "dip", "fritters",
         "quick pickles", "mash", "flatbread", "garlic bread", "buttery greens", "herby rice"]
# sides that turn odd leftover veg into something families eat: soup and chips
LEFTOVER_SIDES = ["crispy oven chips or crisps", "roasted vegetables", "crunchy slaw", "fritters"]
SIDE_NEEDS = {"garlic bread": {"bread", "sourdough", "loaf", "baguette"}, "herby rice": {"rice"}}

# dropped into PROMPT as a value, so single braces (it isn't run through format itself)
SIDE_FIELD = """,
      "side": {"name": "short side dish name", "uses": ["ingredients from the list the side uses"],
               "steps": ["short steps, 2-4 of them"]}"""

SIDE_LEFTOVER_LINE = """Each recipe is a main dish PLUS a side dish that goes with it, in "side".
Recipe 1's side must be {a}, made from what the main leaves over: {leftover}. Recipe 2's side must be {b}.
The side should use ingredients the main does not, so together they use up more of the fridge."""

SIDE_LINE = """Each recipe is a main dish PLUS a side dish that goes with it, in "side".
Recipe 1's side must be {a}. Recipe 2's side must be {b}.
The side should use ingredients the main does not, so together they use up more of the fridge."""


def sides_for(form):
    on_hand = words_in(f"{form['ingredients']} {' '.join(form.get('pantry') or [])}") | STAPLES
    return [x for x in SIDES if x not in SIDE_NEEDS or SIDE_NEEDS[x] & on_hand]


def portions(form):
    """(eat now, extra for the freezer), with sensible bounds."""
    try:
        servings = min(max(int(form.get("servings") or 2), 1), 50)
    except ValueError:
        servings = 2
    try:
        extra = min(max(int(form.get("extra") or 0), 0), 20)
    except ValueError:
        extra = 0
    return servings, extra


def ask_model(form):
    cafe = form.get("mode") == "cafe"
    shop = form.get("mode") == "shop"
    budget = to_money(form.get("budget"))
    servings, extra = portions(form)
    # dishes already shown for this fridge, so "Different ideas" really is different
    avoid = [str(x)[:60] for x in form.get("avoid") or []][-8:]
    # one idea at a time (faster), or two at once
    count = 2 if str(form.get("count")) == "2" else 1
    cuisine = form.get("cuisine") or "any-cuisine"
    # the cook's own choice of dish wins; a chosen saved recipe sets the dish itself;
    # otherwise assign random styles
    styles = ""
    strong = strong_match(form)
    favourites = favourites_block(form, strong)
    dish = str(form.get("dish") or "").strip()[:40]
    if dish:
        styles = (DISH_LINE_1 if count == 1 else DISH_LINE).format(dish=dish)
    elif strong:
        # recipe 1 is the cook's own recipe; a second one is something new
        if count == 2:
            styles = f"Recipe 2 must be something different, in this style: {pick_styles(form, 1)[0]}."
    elif not favourites:
        a, b = pick_styles(form, 2)
        styles = STYLE_LINE_1.format(a=a) if count == 1 else STYLE_LINE.format(a=a, b=b)
    elif form.get("base") in ("", None):
        # auto found a saved recipe: recipe 1 may adapt it as it is, recipe 2 is something new
        styles = ("If it adapts a saved recipe, keep it the same kind of dish." if count == 1 else
                  AUTO_STYLE_LINE.format(b=pick_styles(form, 1)[0]))
    # side: "1" always, "" never, "auto" when your own recipe leaves fridge food over (soup + carrot chips)
    side = form.get("side", "auto")
    leftover = strong[2] if strong else []
    want_side = side == "1" or (side == "auto" and bool(leftover))
    a, b = random.sample(sides_for(form), 2)
    if leftover:
        a = random.choice(LEFTOVER_SIDES)
    if not want_side:
        side_line = ""
    elif count == 1:
        side_line = SIDE_LINE_1.format(
            a=a, leftover=f", made from what the main leaves over: {', '.join(leftover)}" if leftover else "")
    elif leftover:
        side_line = SIDE_LEFTOVER_LINE.format(a=a, b=b, leftover=", ".join(leftover))
    else:
        side_line = SIDE_LINE.format(a=a, b=b)
    prompt = PROMPT.format(
        side_field=SIDE_FIELD if want_side else "",
        who="cafe chef" if cafe else "home cook",
        ingredients=form["ingredients"],
        spices=", ".join(form.get("spices") or []) or "none",
        pantry=", ".join(form.get("pantry") or []) or "none",
        favourites=favourites,
        task=f"one {cuisine} recipe" if count == 1 else f"2 different {cuisine} recipes",
        meal=form.get("meal") or "any meal",
        diet=form.get("diet") or "anything",
        max_time=form.get("max_time") or 45,
        servings=servings,
        freeze=FREEZE_LINE.format(total=servings + extra, servings=servings, extra=extra) if extra else "",
        styles="\n".join(x for x in (styles, side_line) if x),
        avoid=("Do not suggest any of these again, or close variations: " + ", ".join(avoid)) if avoid else "",
        extra=CAFE_EXTRA if cafe else SHOP_EXTRA.format(budget=f" of {budget:g} to spend" if budget else "") if shop else "",
    )
    want = dish_words(dish)

    def valid(o):
        if not (o.get("recipes") and all(r.get("name") and r.get("steps") for r in o["recipes"])):
            return False
        # asked for a side? each recipe needs one, and it should use something the main doesn't
        if want_side and not all(side_of(r).get("name") and side_of(r).get("steps") for r in o["recipes"]):
            return False
        # asked for soup? then both must actually be soups, or ask again
        return all(want & words_in(f"{r['name']} {r.get('pitch', '')} {' '.join(map(str, r['steps']))}")
                   for r in o["recipes"]) if want else True

    # unless the cook picked the kind of dish, don't build it on a base they don't have
    on_hand = words_in(f"{form['ingredients']} {' '.join(form.get('pantry') or [])}") | STAPLES


    def good(o):
        # nearly have one of your own recipes? then recipe 1 should be it
        if strong and str(o["recipes"][0].get("based_on") or "").strip().lower() != strong[0]["name"].lower():
            return False
        # cooking extra for the freezer? then both recipes must actually freeze
        if extra and not all(r.get("freezable") is True for r in o["recipes"]):
            return False
        # (over budget, or a side reusing the main's ingredients, aren't worth a 2-minute retry:
        # the card already shows the cost, and the side is still food used up)
        return dish or not any(missing_bases(r, on_hand) for r in o["recipes"])

    # a side makes the reply longer, so give the model room to finish it (but not to ramble)
    budget_tokens = (600 if want_side else 450) * count
    reply = ollama_json(prompt, valid, temperature=0.9, good=good, num_predict=budget_tokens)
    return reply["recipes"][:count]


class Handler(BaseHTTPRequestHandler):
    def send(self, code, data, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # lets index.html work when opened straight from disk too
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/favourites":
            self.send_json(200, load_favs())
        else:
            self.send(404, b"not found", "text/plain")

    def do_POST(self):
        form = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/recipes":
            return self.recipes(form)
        if self.path == "/api/favourites":
            return self.add_favourite(form)
        if self.path == "/api/matches":
            # instant, no model: which saved recipes fit what's typed so far
            return self.send_json(200, [{"name": f["name"], "have": have, "total": total}
                                        for have, total, f in matching_favs(
                                            form.get("ingredients", ""),
                                            (form.get("pantry") or []) + (form.get("spices") or []))[:5]])
        if self.path == "/api/import":
            return self.import_recipe(form)
        if self.path == "/api/favourites/delete":
            with favs_lock:
                favs = [f for f in load_favs() if f["name"] != form.get("name")]
                save_favs(favs)
            return self.send_json(200, favs)
        self.send(404, b"not found", "text/plain")

    def add_favourite(self, form):
        recipe = {
            "name": str(form.get("name", "")).strip(),
            "minutes": form.get("minutes") or "",
            "ingredients": [str(x).strip() for x in form.get("ingredients") or [] if str(x).strip()],
            "steps": [str(x).strip() for x in form.get("steps") or [] if str(x).strip()],
        }
        source = str(form.get("source", "")).strip()
        if re.match(r"https?://\S+$", source):
            recipe["source"] = source
        if not (recipe["name"] and recipe["ingredients"] and recipe["steps"]):
            return self.send_json(400, {"error": "A recipe needs a name, ingredients and steps"})
        with favs_lock:
            # same name replaces the old version
            favs = [f for f in load_favs() if f["name"].lower() != recipe["name"].lower()] + [recipe]
            save_favs(favs)
        print(f"saved recipe {recipe['name']!r}", flush=True)
        self.send_json(200, favs)

    def import_recipe(self, form):
        """A link or pasted text -> a draft recipe for the user to check before saving."""
        src = str(form.get("source", "")).strip()
        t0 = time.time()
        try:
            if re.match(r"https?://\S+$", src):
                rec = import_url(src)
                rec["source"] = src
            elif len(src) > 40:
                rec = import_text(src)
            else:
                return self.send_json(400, {"error": "Paste a link (starting with http) or the whole recipe text"})
        except urllib.error.HTTPError as e:
            why = "couldn't find that page" if e.code == 404 else "that website wouldn't let us in"
            return self.send_json(502, {"error": f"Sorry, {why} ({e.code}). "
                                                 "Copy the recipe from the page and paste the text in instead."})
        except (OSError, http.client.HTTPException, ValueError):
            return self.send_json(502, {"error": "Couldn't open that link. Check it, or paste the recipe text instead."})
        except RuntimeError as e:
            return self.send_json(502, {"error": str(e)})
        rec["seconds"] = round(time.time() - t0)
        print(f"imported {rec['name']!r} via {rec['how']} ({rec['seconds']}s)", flush=True)
        self.send_json(200, rec)

    def recipes(self, form):
        if not form.get("ingredients", "").strip():
            return self.send_json(400, {"error": "Add some ingredients first"})
        t0 = time.time()
        try:
            recipes = ask_model(form)
        except RuntimeError as e:
            return self.send_json(502, {"error": str(e)})
        # small models sometimes list things the user already ticked; drop those
        ticked = (form.get("spices") or []) + (form.get("pantry") or [])
        have = {x.lower() for x in ticked}
        on_hand = words_in(form["ingredients"] + " " + " ".join(ticked)) | STAPLES
        names = {f["name"].lower(): f["name"] for f in load_favs()}
        for r in recipes:
            # anything in "uses" that isn't in the fridge or cupboard really belongs in "also_need"
            uses = [str(x) for x in r.get("uses") or []]
            r["uses"] = [x for x in uses if words_in(x) & on_hand]
            missing = [x for x in uses if not words_in(x) & on_hand]
            side = side_of(r)
            if side:
                side_uses = [str(x) for x in side.get("uses") or []]
                side["uses"] = [x for x in side_uses if words_in(x) & on_hand]
                missing += [x for x in side_uses if not words_in(x) & on_hand]
                side["steps"] = [str(x) for x in side.get("steps") or []]
                r["side"] = side
            need = (r.get("also_need") or []) + missing
            # catch basics hidden in the steps, e.g. "serve in warmed tortillas (assumed)"
            listed = words_in(" ".join(map(str, need)))
            steps = words_in(recipe_text(r))
            need += [v for k, v in BASES.items() if k in steps and k not in on_hand and k not in listed]
            r["also_need"] = [x for x in need if x.lower() not in have]
            # only keep based_on when it really names a saved recipe
            r["based_on"] = names.get(str(r.get("based_on") or "").strip().lower(), "")
            r["changes"] = r.get("changes") or [] if r["based_on"] else []
            r["freezable"] = r.get("freezable") is True
            r["extra"] = portions(form)[1]
            if form.get("mode") == "shop":
                priced = specials(form["ingredients"])
                r["shop"] = shopping(r, priced) | {
                    "budget": to_money(form.get("budget")), "portions": sum(portions(form)),
                    "symbol": next((sym for _, _, sym in priced if sym), "$")}
        secs = round(time.time() - t0)
        print(f"{form['ingredients'][:60]!r} mode={form.get('mode')} dish={form.get('dish') or 'surprise'} base={form.get('base') or 'auto'}"
              f" -> {len(recipes)} recipes ({secs}s)", flush=True)
        self.send_json(200, {"recipes": recipes, "model": MODEL, "seconds": secs})


if __name__ == "__main__":
    print(f"Fridge2Fork on http://localhost:{PORT}  (model: {MODEL})")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
