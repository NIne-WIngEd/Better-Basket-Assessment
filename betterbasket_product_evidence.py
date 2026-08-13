#!/usr/bin/env python3
"""
Compact shared evidence extraction/comparison for BetterBasket runtime.

Design:
- 140 canonical attributes come from product_match_criteria_v2_audited.json.
- Missing is UNKNOWN, never disagreement.
- Product facts are extracted once and reused across candidate pairs.
- Deterministic extraction is primary; optional GPT-5 nano may fill explicit facts.
- Match and Non-match engines consume the same facts but score independently.
"""

from __future__ import annotations
import ast, html, json, math, re, unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple


# ---------- normalization ----------

TOK_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", re.I)
ALNUM_RE = re.compile(r"\b(?=[a-z0-9-]{2,24}\b)(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]+\b", re.I)
DIM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)(?:\s*[x×]\s*(\d+(?:\.\d+)?))?\s*(in(?:ch(?:es)?)?|ft|cm|mm)?\b", re.I)
PCT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*%")
ABV_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*%\s*(?:abv|alc(?:ohol)?(?:\s+by\s+volume)?)\b", re.I)
SPF_RE = re.compile(r"\bspf\s*[-:]?\s*(\d{1,3})\b", re.I)
VOLT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:v|volt|volts)\b", re.I)
WATT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:w|watt|watts)\b", re.I)
LUMEN_RE = re.compile(r"\b(\d{2,5})\s*(?:lm|lumen|lumens)\b", re.I)
KELVIN_RE = re.compile(r"\b(\d{3,5})\s*k\b", re.I)
PLY_RE = re.compile(r"\b(\d+)\s*[- ]?ply\b", re.I)
LOAD_RE = re.compile(r"\b(\d+)\s*(?:loads?|washes|uses|applications)\b", re.I)
COUNT_NOUN_RE = re.compile(r"\b(\d+)\s*(?:coated\s+)?(?:caplets?|tablets?|capsules?|gummies|gummy|refills?|sticks?|bars?|bottles?|cans?|pods?|packets?|pouches?|wipes?)\b", re.I)
PACK_OF_RE = re.compile(r"\b(?:pack|case)\s+of\s+(\d+)\b", re.I)
PACK_N_RE = re.compile(r"\b(\d+)\s*[- ]?(?:pack|pk)\b", re.I)
MULTIPLIER_PREFIX_RE = re.compile(r"^\s*(\d+)\s*x\s*[-:]\s*", re.I)
LEADING_PACK_RE = re.compile(r"^\s*\(?\s*(\d+)\s*[- ]?pack\s*\)?\s*", re.I)
PACK_WORD_RE = re.compile(r"\b(twin|double|triple)\s+pack\b", re.I)
LEAN_RE = re.compile(r"\b(\d{2})\s*%\s*lean(?:\s*[/,-]\s*(\d{1,2})\s*%\s*fat)?\b", re.I)
STORAGE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(tb|gb|mb)\b", re.I)
JUICE_RE = re.compile(r"\b(\d{1,3})\s*%\s*(?:juice|fruit juice)\b", re.I)
SHADE_RE = re.compile(
    r"\b(?:(soft|deep|medium|dark|light|very|natural|warm|cool|ash|golden|neutral)\s+)?"
    r"(brown|blonde|black|beige|ivory|tan|red|pink|rose|nude|coral|plum|berry|mauve|wine|chestnut|auburn|taupe|gray|grey|blue|green|purple)\b",
    re.I,
)

Q_RE = re.compile(
    r"(?<!\w)(\d+(?:\.\d+)?)\s*"
    r"(fl\.?\s*oz|fluid\s*ounces?|ounces?|oz|pounds?|lbs?|lb|kilograms?|kg|"
    r"grams?|g|milligrams?|mg|micrograms?|mcg|milliliters?|ml|liters?|l|"
    r"counts?|ct|cnt|packs?|pack|pk|inches?|inch|feet|foot|ft|cm|mm)\b",
    re.I,
)
UNIT_CANON = {
    "ounces":"oz","ounce":"oz","oz":"oz",
    "pounds":"lb","pound":"lb","lbs":"lb","lb":"lb",
    "grams":"g","gram":"g","g":"g","kilograms":"kg","kilogram":"kg","kg":"kg",
    "milligrams":"mg","milligram":"mg","mg":"mg","micrograms":"mcg","microgram":"mcg","mcg":"mcg",
    "milliliters":"ml","milliliter":"ml","ml":"ml","liters":"l","liter":"l","l":"l",
    "counts":"ct","count":"ct","ct":"ct","cnt":"ct","packs":"pack","pack":"pack","pk":"pack",
    "inches":"in","inch":"in","feet":"ft","foot":"ft","ft":"ft","cm":"cm","mm":"mm",
}
STOP = {
    "the","a","an","and","or","with","for","of","in","on","by","from","to","at",
    "new","premium","classic","original","style","assorted","each","per","size",
    "pack","count","ct","pk","oz","ounce","ounces","lb","lbs","g","kg","ml","l",
}

PRIVATE_LABELS = {
    # Store A / Walmart-associated
    "great value","equate","marketside","sam's choice","sams choice","parent's choice",
    "parents choice","mainstays","special kitty","ol' roy","ol roy","george",
    "better homes and gardens","onn","hyper tough","athletic works","expert gardener",
    # Store B / Wegmans
    "wegmans","food you feel good about",
}
BRAND_ALIASES = {
    "coca cola":"coca-cola", "coca-cola":"coca-cola",
    "loreal paris":"l'oreal paris", "l'oreal paris":"l'oreal paris",
    "dr pepper":"dr pepper", "dr. pepper":"dr pepper",
    "m and ms":"m&m's", "m&ms":"m&m's",
}

BROAD_FAMILY_RULES = {
    "medicine": ["medicine","medication","pain relief","cold cough","allergy","pharmacy","ibuprofen","acetaminophen","aspirin"],
    "beauty_personal": ["makeup","cosmetic","mascara","concealer","foundation","lip","nail","shampoo","conditioner","deodorant","toothbrush","oral care","skin care","personal care"],
    "baby": ["baby","infant","diaper","formula","toddler","training pants"],
    "pet": ["pet","dog","cat","small animal","litter","pet food","hay"],
    "alcohol": ["wine","beer","spirits","liquor","champagne","vodka","whiskey","whisky","rum","tequila"],
    "beverage": ["beverage","water","juice","coffee","tea","soda","drink","milk"],
    "food": ["food","grocery","frozen","dairy","bakery","meat","seafood","produce","snack","cereal","cheese","yogurt","sauce","pasta"],
    "household_tech": ["light bulb","battery","electronics","cable","charger","adapter","storage media"],
    "household": ["home","household","cleaning","paper towel","toilet paper","trash bag","storage bag","kitchen"],
    "apparel": ["apparel","clothing","shoe","footwear","sock","shirt","pants","dress"],
}
FAMILY_PRIORITY = ["medicine","beauty_personal","baby","pet","alcohol","beverage","food","household_tech","household","apparel"]

TERM_MAP = {
    "flavor": ["vanilla","chocolate","dark chocolate","milk chocolate","strawberry","strawberries","blueberry","blueberries","raspberry","raspberries","lemon","lime","orange","apple","cherry","cherries","peach","mango","honey","cinnamon","garlic","basil","ranch","bbq","barbecue","cheddar","mozzarella","mint","peppermint","coconut","pineapple","banana","caramel","hazelnut","pumpkin","grape","watermelon","berry","latte","cafe latte","café latte","mocha"],
    "scent": ["unscented","fragrance free","lavender","linen","fresh scent","citrus","floral","ocean","vanilla","coconut","rose","eucalyptus"],
    "color": ["black","white","red","blue","green","pink","purple","brown","gold","silver","gray","grey","beige","clear","orange","yellow"],
    "texture": ["smooth","creamy","crunchy","crispy","chunky","soft","firm","gel","foam","mousse"],
    "style_or_cut": ["sliced","diced","chopped","minced","shredded","grated","julienne","cubed","spiral","whole"],
    "shape": ["round","square","rectangle","rectangular","oval","heart","star"],
    "physical_state": ["liquid","powder","powdered","gel","cream","lotion","spray","aerosol","tablet","capsule","caplet","gummy","wipes","bar"],
    "storage_state": ["frozen","refrigerated","chilled","fresh","shelf stable","shelf-stable"],
    "preparation_state": ["raw","cooked","ready to eat","ready-to-eat","roasted","baked","fried","steamed"],
    "concentrate_status": ["concentrate","from concentrate","not from concentrate"],
    "spice_heat_level": ["mild","medium","hot","spicy","extra hot"],
    "coating_breading": ["breaded","battered","unbreaded"],
    "filled_stuffed_status": ["stuffed","filled","unfilled"],
    "smoked_cured_status": ["smoked","cured","uncured"],
    "organic_status": ["organic"],
    "gluten_status": ["gluten free","gluten-free"],
    "sugar_status": ["sugar free","sugar-free","zero sugar","no sugar added","unsweetened","sweetened","diet"],
    "sodium_status": ["low sodium","reduced sodium","no salt added","unsalted","salted"],
    "fat_level": ["fat free","nonfat","low fat","low-fat","reduced fat","reduced-fat","whole milk","2%","1%","skim"],
    "lactose_status": ["lactose free","lactose-free"],
    "vegan_vegetarian_status": ["vegan","vegetarian","plant based","plant-based"],
    "kosher_halal_status": ["kosher","halal"],
    "caffeine_status": ["decaf","decaffeinated","caffeine free","caffeine-free","caffeinated"],
    "carbonation_status": ["sparkling","carbonated","still"],
    "coffee_roast": ["light roast","medium roast","dark roast","espresso roast","french roast"],
    "coffee_format": ["whole bean","ground coffee","coffee pods","coffee pod","k-cup","k cup","instant coffee","capsule","capsules"],
    "tea_type": ["black tea","green tea","white tea","herbal tea","oolong","chai"],
    "alcohol_class": ["beer","wine","vodka","whiskey","whisky","rum","tequila","gin","brandy","liqueur","champagne","prosecco"],
    "dosage_form": ["tablet","tablets","capsule","capsules","caplet","caplets","gummy","gummies","liquid","solution","powder","powdered","syrup","cream","ointment","gel","spray","patch","suppository"],
    "route_of_administration": ["oral","topical","nasal","ophthalmic","eye drops","otic","ear drops"],
    "release_type": ["extended release","extended-release","er","xr","delayed release","delayed-release","immediate release"],
    "absorbency": ["light","regular","moderate","heavy","overnight","maximum","ultimate"],
    "finish": ["matte","gloss","glossy","satin","shimmer","dewy","natural finish"],
    "coverage_level": ["sheer","light coverage","medium coverage","full coverage"],
    "hold_level": ["light hold","medium hold","strong hold","maximum hold","extra hold"],
    "hair_type": ["curly","wavy","straight","coily","fine hair","thick hair","dry hair","oily hair","color treated"],
    "skin_type": ["dry skin","oily skin","combination skin","sensitive skin","normal skin"],
    "waterproof_status": ["waterproof","water resistant","water-resistant"],
    "washable_longwear_status": ["washable","longwear","long wear","24h","24 hour","24-hour"],
    "disposable_reusable_status": ["disposable","reusable"],
    "pet_species": ["dog","dogs","cat","cats","kitten","puppy","rabbit","hamster","guinea pig","bird","fish"],
    "pet_life_stage": ["puppy","kitten","adult","senior","all life stages"],
    "pet_food_texture": ["pate","paté","chunks","shreds","kibble","wet food","dry food"],
    "pet_litter_clumping_status": ["clumping","non-clumping","non clumping"],
    "bristle_firmness": ["soft","medium","firm","extra soft"],
    "closure_style": ["drawstring","zipper","zip top","slider","tie","flap tie","resealable"],
    "egg_production_method": ["cage free","cage-free","free range","free-range","pasture raised","pasture-raised"],
    "water_source_treatment_type": ["spring water","purified water","distilled water","mineral water","alkaline water","artesian water"],
    "non_gmo_status": ["non-gmo","non gmo","no genetically modified"],
}
PRODUCT_LINE_MARKERS = ["simply","advanced care","advancedcare","sport","kids","baby","professional","therapeutic","rapid detection","max protein","zero sugar","diet","ultra","complete","original"]
MEAT_SPECIES = ["beef","chicken","turkey","pork","lamb","veal","bison","duck"]
SEAFOOD_SPECIES = ["salmon","tuna","shrimp","cod","tilapia","haddock","crab","lobster","scallop","mussel","clam","sardine"]
MEAT_CUTS = ["breast","breasts","thigh","thighs","wing","wings","drumstick","drumsticks","tenderloin","tenderloins","sirloin","ribeye","rib eye","chuck","round","loin","shoulder","brisket","ribs","steak","steaks"]
PRODUCE_SPECIES = ["strawberry","strawberries","raspberry","raspberries","blueberry","blueberries","blackberry","blackberries","cherry","cherries","apple","apples","banana","bananas","orange","oranges","grape","grapes","watermelon","melon","peach","peaches","pear","pears","pineapple","mango","mangoes","tomato","tomatoes","potato","potatoes","onion","onions","carrot","carrots","corn","lettuce","spinach","broccoli","cauliflower","cucumber","cucumbers","pepper","peppers","avocado","avocados"]
PRODUCE_CANON = {"strawberries":"strawberry","raspberries":"raspberry","blueberries":"blueberry","blackberries":"blackberry","cherries":"cherry","apples":"apple","bananas":"banana","oranges":"orange","grapes":"grape","peaches":"peach","pears":"pear","mangoes":"mango","tomatoes":"tomato","potatoes":"potato","onions":"onion","carrots":"carrot","cucumbers":"cucumber","peppers":"pepper","avocados":"avocado"}
PROTEIN_SOURCES = ["whey protein","whey","casein","pea protein","pea","soy protein","soy","plant based","plant-based","grass fed","grass-fed","milk protein","collagen"]
PROTEIN_CANON = {"plant-based":"plant_based","plant based":"plant_based","pea protein":"pea","soy protein":"soy","whey protein":"whey","grass-fed":"grass_fed_dairy","grass fed":"grass_fed_dairy","milk protein":"dairy"}
PRODUCE_FORMS = ["whole","sliced","diced","chopped","cut","halves","wedges","spears"]
MILK_TYPES = ["whole milk","2%","1%","skim","oat milk","almond milk","soy milk","coconut milk","lactose free"]
CHEESE_TYPES = ["cheddar","mozzarella","parmesan","provolone","swiss","gouda","brie","feta","ricotta","cream cheese","american cheese"]
FORMULA_BASES = ["milk based","milk-based","soy based","soy-based","goat milk","hypoallergenic","gentle","sensitive"]
BULB_BASES = ["e26","e12","gu10","gu24","g9","g4","mr16"]
BULB_SHAPES = ["a19","a15","br30","br40","par20","par30","par38","g25","st19","t8","t12"]
CONNECTORS = ["usb-c","usb c","usb-a","usb a","lightning","hdmi","displayport","micro usb","micro-usb","3.5mm"]
BATTERY = ["aa","aaa","c battery","d battery","9v","cr2032","cr2025","lithium","alkaline","rechargeable"]
OCCASIONS = ["christmas","halloween","valentine","valentine's day","easter","thanksgiving","birthday","graduation","wedding","hanukkah"]
AUDIENCE = ["men","women","boys","girls","kids","children","adult","unisex"]
MATERIALS = ["cotton","polyester","stainless steel","steel","aluminum","plastic","glass","wood","silicone","ceramic","leather","rubber"]
PACKAGING = ["bottle","jar","can","box","bag","pouch","tube","carton","tray","tub","cup","packet"]

# ---------- fact representation ----------

@dataclass
class Fact:
    value: Any
    quality: float
    source: str

def fact(v, q=1.0, source="deterministic"):
    if v is None or v == "" or v == [] or v == set():
        return None
    return Fact(v, float(max(0.0, min(1.0, q))), source)

def safe_str(x):
    if x is None: return ""
    if isinstance(x, float) and math.isnan(x): return ""
    return str(x)

def parse_jsonish(x):
    s = safe_str(x).strip()
    if not s: return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, (dict,list)) else {}
    except Exception:
        try:
            v = ast.literal_eval(s)
            return v if isinstance(v, (dict,list)) else {}
        except Exception:
            return {}

def _norm_text_cached(s: str):
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unicodedata.normalize("NFKC", s).lower().replace("&"," and ").replace("×","x")
    s = re.sub(r"[‐‑‒–—−]", "-", s)
    s = re.sub(r"[^a-z0-9%+\-./' ]+", " ", s)
    return re.sub(r"\s+"," ",s).strip()

def norm_text(x):
    return _norm_text_cached(safe_str(x))

def _boundary_text_cached(s: str):
    # Treat punctuation/hyphens as token boundaries for dictionary phrase lookup.
    return " " + re.sub(r"[^a-z0-9]+", " ", _norm_text_cached(s)).strip() + " "

def boundary_text(x):
    return _boundary_text_cached(safe_str(x))

def toks(x): return TOK_RE.findall(norm_text(x))

def norm_brand(x):
    s = norm_text(x).replace("®","").replace("™","")
    s = re.sub(r"\b(company|co|inc|llc|corp|corporation)\b"," ",s)
    s = re.sub(r"\s+"," ",s).strip(" .-'")
    return BRAND_ALIASES.get(s,s)

def taxonomy(info):
    d=parse_jsonish(info)
    if not isinstance(d,dict): return ""
    return norm_text(" ".join(safe_str(d.get(f"category_{i}")) for i in range(4) if d.get(f"category_{i}")))

def sizing(d):
    x=parse_jsonish(d)
    return x if isinstance(x,dict) else {}

def infer_brand(row):
    br=norm_brand(row.get("brand_raw",""))
    title=norm_text(row.get("name",""))
    if br: return fact(br,0.98,"brand_raw")
    for p in sorted(PRIVATE_LABELS,key=len,reverse=True):
        if re.search(rf"\b{re.escape(p)}\b",title):
            return fact(norm_brand(p),0.92,"title_private_label")
    return None

def brand_type(brand_fact):
    if not brand_fact: return None
    b=norm_brand(brand_fact.value)
    if b in _PRIVATE_LABEL_NORM:
        return fact("private_label",1.0,"retailer_brand_dictionary")
    return fact("national_or_manufacturer",0.86,"observed_nonretailer_brand")

_BROAD_FAMILY_TERMS = {
    fam: [(norm_text(t), boundary_text(t).strip()) for t in terms]
    for fam, terms in BROAD_FAMILY_RULES.items()
}
_PRIVATE_LABEL_NORM = {norm_brand(x) for x in PRIVATE_LABELS}

# Compile the general title-term ontology into one n-gram lookup. The previous
# implementation rescanned the same title once per attribute (dozens of full
# string scans per product). This preserves the same explicit phrase semantics
# but resolves all TERM_MAP attributes in one pass.
_TERM_LOOKUP = {}
_TERM_MAX_N = 1
for _attr, _terms in TERM_MAP.items():
    for _term in _terms:
        _key = tuple(re.findall(r"[a-z0-9]+", boundary_text(_term)))
        if not _key: continue
        _TERM_LOOKUP.setdefault(_key, []).append((_attr, norm_text(_term)))
        _TERM_MAX_N = max(_TERM_MAX_N, len(_key))

def term_map_values(text):
    ts = re.findall(r"[a-z0-9]+", boundary_text(text))
    out = {}
    L = len(ts)
    for n in range(1, min(_TERM_MAX_N, L)+1):
        for i in range(0, L-n+1):
            hit = _TERM_LOOKUP.get(tuple(ts[i:i+n]))
            if not hit: continue
            for attr, val in hit:
                out.setdefault(attr, []).append(val)
    return {k: sorted(set(v)) for k,v in out.items()}

def term_map_values_boundary(s):
    ts=re.findall(r"[a-z0-9]+",s);out={};L=len(ts)
    for n in range(1,min(_TERM_MAX_N,L)+1):
        for i in range(0,L-n+1):
            hit=_TERM_LOOKUP.get(tuple(ts[i:i+n]))
            if hit:
                for attr,val in hit:out.setdefault(attr,[]).append(val)
    return {k:sorted(set(v)) for k,v in out.items()}

def broad_family(text):
    s=boundary_text(text)
    scores={}
    for fam,terms in _BROAD_FAMILY_TERMS.items():
        score=sum(1 for _, bt in terms if f" {bt} " in s)
        scores[fam]=score
    best=max(scores,key=scores.get) if scores else None
    return fact(best,0.82,"taxonomy_title_rules") if best and scores[best]>0 else None

def core_tokens(name, brand=None):
    b=set(toks(brand.value if brand else ""))
    out=[]
    for t in toks(name):
        if t in STOP or t in b or re.fullmatch(r"\d+(?:\.\d+)?",t): continue
        out.append(t)
    return set(out)

def extract_quantities(text):
    out=[]
    for m in Q_RE.finditer(norm_text(text)):
        v=float(m.group(1)); u=re.sub(r"[.\s]+"," ",m.group(2).lower()).strip()
        if u in ("fl oz","fluid ounce","fluid ounces"):
            out.append(("volume_ml", v*29.5735295625)); continue
        cu=UNIT_CANON.get(u,u)
        if cu=="oz": out.append(("mass_g",v*28.349523125))
        elif cu=="lb": out.append(("mass_g",v*453.59237))
        elif cu=="g": out.append(("mass_g",v))
        elif cu=="kg": out.append(("mass_g",v*1000))
        elif cu=="ml": out.append(("volume_ml",v))
        elif cu=="l": out.append(("volume_ml",v*1000))
        elif cu=="mg": out.append(("strength_mg",v))
        elif cu=="mcg": out.append(("strength_mg",v/1000))
        elif cu=="ct": out.append(("count",v))
        elif cu=="pack": out.append(("pack",v))
        elif cu=="in": out.append(("length_mm",v*25.4))
        elif cu=="ft": out.append(("length_mm",v*304.8))
        elif cu=="cm": out.append(("length_mm",v*10))
        elif cu=="mm": out.append(("length_mm",v))
    return out

def first_quantity(qs,dim):
    xs=[v for d,v in qs if d==dim]
    return xs[0] if xs else None

@lru_cache(maxsize=4096)
def _normalized_term_tuple(terms_tuple):
    return tuple((norm_text(t), boundary_text(t).strip()) for t in terms_tuple)

def term_values(text, terms):
    s=boundary_text(text)
    vals=[]
    tt=tuple(terms)
    for nt, bt in _normalized_term_tuple(tt):
        if f" {bt} " in s:
            vals.append(nt)
    return sorted(set(vals))

def boundary_from_norm(s):
    return " " + re.sub(r"[^a-z0-9]+", " ", s).strip() + " "

def term_values_boundary(s, terms):
    vals=[]
    for nt, bt in _normalized_term_tuple(tuple(terms)):
        if f" {bt} " in s: vals.append(nt)
    return sorted(set(vals))

def regex_num_norm(regex, s, group=1, scale=1.0):
    m=regex.search(s)
    return float(m.group(group))*scale if m else None

def broad_family_boundary(s):
    scores={}
    for fam,terms in _BROAD_FAMILY_TERMS.items():
        scores[fam]=sum(1 for _,bt in terms if f" {bt} " in s)
    best=max(scores,key=scores.get) if scores else None
    return fact(best,0.82,"taxonomy_title_rules") if best and scores[best]>0 else None

def regex_num(regex,text,group=1,scale=1.0):
    m=regex.search(norm_text(text))
    return float(m.group(group))*scale if m else None

def explicit_pack_count(text):
    s=norm_text(text);outer=1.0;rest=s
    m=MULTIPLIER_PREFIX_RE.search(s) or LEADING_PACK_RE.search(s)
    if m:
        outer=float(m.group(1));rest=s[m.end():]
    inner=None
    m=PACK_OF_RE.search(rest)
    if m:inner=float(m.group(1))
    else:
        m=PACK_N_RE.search(rest)
        if m:inner=float(m.group(1))
        else:
            m=PACK_WORD_RE.search(rest)
            if m:inner={"twin":2.0,"double":2.0,"triple":3.0}[m.group(1).lower()]
    if outer>1:return outer*(inner if inner and inner>1 else 1.0)
    return inner

def _canonical_terms(attr, vals):
    vals=list(vals or [])
    if attr=="flavor":
        mp={"strawberries":"strawberry","blueberries":"blueberry","raspberries":"raspberry",
            "cherries":"cherry","barbecue":"bbq","cafe latte":"latte","café latte":"latte"}
        vals=[mp.get(x,x) for x in vals]
        # Generic chocolate is redundant when a specific chocolate style is explicit.
        if "chocolate" in vals and any(x in vals for x in ("dark chocolate","milk chocolate","white chocolate")):
            vals=[x for x in vals if x!="chocolate"]
    return sorted(set(vals))

def product_text(row):
    # Bound free-form fields. Some catalog rows contain extremely long descriptions;
    # scanning them repeatedly across dozens of attribute dictionaries creates severe
    # data-dependent latency with little identity value beyond the first section.
    return " ".join([
        safe_str(row.get("name",""))[:600], safe_str(row.get("brand_raw",""))[:200],
        taxonomy(row.get("item_info","")), safe_str(row.get("description",""))[:2000],
        safe_str(row.get("tags",""))[:600], safe_str(row.get("sizing_comp",""))[:800]
    ])

def model_ids(text):
    out=set()
    for m in ALNUM_RE.finditer(norm_text(text)):
        t=m.group(0)
        # discard tokens that are clearly measurements/time marketing
        if re.fullmatch(r"\d+(?:mg|ml|oz|lb|ct|pk|h)",t): continue
        if len(t)>=3: out.add(t)
    return sorted(out)

def parse_ingredient_signature(desc):
    s=norm_text(desc)
    m=re.search(r"\bingredients?\s*[:\-]\s*(.{10,500})",s)
    if not m: return None
    z=m.group(1)
    z=re.split(r"\b(?:contains|allergen|distributed by|manufactured by)\b",z)[0]
    vals=[t for t in toks(z) if t not in STOP and len(t)>2]
    return sorted(set(vals[:80]))

def active_ingredient(desc):
    s=norm_text(desc)
    m=re.search(r"\bactive ingredient(?:s)?\s*[:\-]\s*([a-z0-9 ,.-]{3,120})",s)
    if not m: return None
    return norm_text(re.split(r"\bpurpose\b|\binactive\b",m.group(1))[0])[:100]

def active_strength(text):
    s=norm_text(text)
    m=re.search(r"\b(\d+(?:\.\d+)?)\s*(mg|mcg|g)\b",s)
    if not m:return None
    v=float(m.group(1));u=m.group(2)
    return v if u=="mg" else v/1000 if u=="mcg" else v*1000

def dimension_signature(text):
    m=DIM_RE.search(norm_text(text))
    if not m:return None
    vals=[float(x) for x in m.groups()[:3] if x]
    unit=(m.group(4) or "").lower()
    if unit.startswith("in"): mult=25.4
    elif unit=="ft":mult=304.8
    elif unit=="cm":mult=10
    elif unit=="mm":mult=1
    else:mult=1
    return tuple(round(v*mult,3) for v in vals)

WINE_VARIETALS=["cabernet sauvignon","sauvignon blanc","pinot noir","pinot grigio","pinot gris","chardonnay","merlot","riesling","malbec","syrah","shiraz","zinfandel","moscato","prosecco"]
def wine_varietal(text):
    vals=term_values(text,WINE_VARIETALS)
    return vals[0] if vals else None
def wine_varietal_boundary(s):
    vals=term_values_boundary(s,WINE_VARIETALS)
    return vals[0] if vals else None

def functional_signature(name, brand):
    return sorted(core_tokens(name,brand))

def canonical_subcategory(row):
    d=parse_jsonish(row.get("item_info",""))
    if not isinstance(d,dict): return None
    for i in (3,2,1):
        if d.get(f"category_{i}"): return norm_text(d[f"category_{i}"])
    return None

def extract_facts(row: Dict[str,Any], criteria: Dict[str,Any]) -> Dict[str,Optional[Fact]]:
    name=safe_str(row.get("name",""))
    desc=safe_str(row.get("description",""))[:2000]
    nt=norm_text(name); ntb=boundary_from_norm(nt)
    info=parse_jsonish(row.get("item_info","")); info=info if isinstance(info,dict) else {}
    tax=norm_text(" ".join(safe_str(info.get(f"category_{i}")) for i in range(4) if info.get(f"category_{i}")))
    text=" ".join([name[:600],safe_str(row.get("brand_raw",""))[:200],tax,desc,safe_str(row.get("tags",""))[:600],safe_str(row.get("sizing_comp",""))[:800]])
    textn=norm_text(text); textb=boundary_from_norm(textn)
    br=infer_brand(row)
    bt=brand_type(br)
    fam_title=broad_family_boundary(ntb); fam=fam_title or broad_family_boundary(boundary_from_norm(tax))
    if fam: fam.source = "title_family_rules" if fam_title else "taxonomy_family_rules"
    sz=sizing(row.get("sizing_comp",""))
    friendly=norm_text(sz.get("size_user_friendly","") or sz.get("size","") or "")
    q_title=extract_quantities(nt)
    q_struct=extract_quantities(friendly)
    qs=q_struct+q_title

    facts={a["name"]:None for a in criteria["attributes"]}
    facts["brand"]=br
    facts["brand_type"]=bt
    facts["functional_name"]=fact(functional_signature(name,br),0.88,"title_core_tokens")
    facts["product_family"]=fam
    facts["canonical_category"]=fam
    subcat=next((norm_text(info[f"category_{i}"]) for i in (3,2,1) if info.get(f"category_{i}")),None)
    facts["canonical_subcategory"]=fact(subcat,0.82,"taxonomy") if subcat else None
    mids=model_ids(nt)
    facts["model_or_part_identifier"]=fact(mids,0.90,"title_alphanumeric") if mids else None

    # quantity / packaging
    # Prefer explicit structured net content, but calculate a tagged TOTAL quantity from
    # title per-unit quantity × count when both are present. Tagged dimensions prevent
    # nonsensical comparisons such as 12-count versus 12-fluid-ounces.
    mass_struct=first_quantity(q_struct,"mass_g"); vol_struct=first_quantity(q_struct,"volume_ml")
    mass_title=first_quantity(q_title,"mass_g"); vol_title=first_quantity(q_title,"volume_ml")
    cnt_q=first_quantity(q_title,"count") or first_quantity(q_struct,"count")
    cnt_noun=None
    cm=COUNT_NOUN_RE.search(nt)
    if cm:cnt_noun=float(cm.group(1))
    cnt=cnt_q if cnt_q is not None else cnt_noun
    # Retail multipack is distinct from an internal piece/count declaration.
    # "16 oz, 24 slices" is one 16-oz package; "pack of 3, 16 oz" is 48 oz total.
    pk=explicit_pack_count(nt) or first_quantity(q_title,"pack") or first_quantity(q_struct,"pack")
    mass=mass_struct if mass_struct is not None else mass_title
    vol=vol_struct if vol_struct is not None else vol_title
    facts["net_weight"]=fact(mass,0.94,"structured_size" if mass_struct is not None else "title_size") if mass is not None else None
    facts["net_volume"]=fact(vol,0.94,"structured_size" if vol_struct is not None else "title_size") if vol is not None else None
    facts["declared_count"]=fact(cnt,0.96,"title/size") if cnt is not None else None
    if pk is not None:
        facts["pack_structure"]=fact(pk,0.96,"explicit_multipack")
    elif cnt_q is None and (mass is not None or vol is not None or cnt_noun is not None):
        # A generic "12 Ct" next to a per-unit size may itself be a retail multipack,
        # so do not infer single-package structure in that ambiguous case.
        facts["pack_structure"]=fact(1.0,0.86,"single_package_inference")
    total=None
    # Only an EXPLICIT retail multipack safely multiplies a per-unit mass/volume.
    if pk and pk>1 and mass_title is not None: total=("mass_g",mass_title*pk)
    elif pk and pk>1 and vol_title is not None: total=("volume_ml",vol_title*pk)
    elif cnt_q and cnt_q>1 and mass_title is not None and mass_struct is not None and numeric_sim(mass_struct,mass_title*cnt_q,0.03)>=.90:
        total=("mass_g",mass_struct)
    elif cnt_q and cnt_q>1 and vol_title is not None and vol_struct is not None and numeric_sim(vol_struct,vol_title*cnt_q,0.03)>=.90:
        total=("volume_ml",vol_struct)
    elif cnt_q and cnt_q>1 and (mass_title is not None or vol_title is not None):
        # Ambiguous generic count + size: leave total UNKNOWN rather than inventing a
        # package total. Declared_count remains available as independent evidence.
        total=None
    elif mass_struct is not None: total=("mass_g",mass_struct)
    elif vol_struct is not None: total=("volume_ml",vol_struct)
    elif mass_title is not None: total=("mass_g",mass_title)
    elif vol_title is not None: total=("volume_ml",vol_title)
    elif cnt is not None: total=("count",cnt)
    elif pk is not None: total=("count",pk)
    facts["normalized_total_quantity"]=fact(total,0.95,"normalized_total_quantity") if total is not None else None
    if sz.get("avg_size_per_piece") not in (None,""):
        try:facts["piece_weight"]=fact(float(sz["avg_size_per_piece"]),0.95,"sizing_comp")
        except:pass
    for k in ["billed_by_weight","ordered_by_weight"]:
        if k in sz and sz[k] is not None:facts[k]=fact(bool(sz[k]),1.0,"sizing_comp")
    if facts.get("billed_by_weight") or facts.get("ordered_by_weight"):
        bw=(facts.get("billed_by_weight").value if facts.get("billed_by_weight") else False) or (facts.get("ordered_by_weight").value if facts.get("ordered_by_weight") else False)
        facts["variable_weight_flag"]=fact(bool(bw),0.95,"sizing_comp")
    dimsig=dimension_signature(friendly+" "+nt)
    facts["dimension_signature"]=fact(dimsig,0.94,"size/title") if dimsig else None
    p=term_values_boundary(ntb,PACKAGING); facts["packaging_form"]=fact(p[0],0.82,"title") if p else None
    if "refill" in nt:facts["refill_vs_starter_kit"]=fact("refill",0.95,"title")
    elif "starter kit" in nt or "starter set" in nt:facts["refill_vs_starter_kit"]=fact("starter_kit",0.95,"title")
    elif "bundle" in nt or "kit" in nt:facts["bundle_flag"]=fact(True,0.90,"title")
    if "assorted" in nt or "variety pack" in nt:facts["assortment_flag"]=fact(True,0.92,"title")

    # Identity-bearing variants come from the title, not taxonomy or free-form description.
    # This prevents unrelated category/marketing words from creating fake conflicts.
    status_attrs={"organic_status","gluten_status","sugar_status","sodium_status","fat_level",
                  "lactose_status","vegan_vegetarian_status","kosher_halal_status",
                  "caffeine_status","carbonation_status","non_gmo_status"}
    title_term_hits=term_map_values_boundary(ntb)
    status_term_hits=term_map_values_boundary(boundary_from_norm(nt + " " + norm_text(row.get("tags",""))))
    for attr in TERM_MAP:
        vals=_canonical_terms(attr,(status_term_hits if attr in status_attrs else title_term_hits).get(attr,[]))
        if vals:facts[attr]=fact(vals,0.94 if attr not in status_attrs else 0.90,"explicit_title_terms")
    pl=term_values_boundary(ntb,PRODUCT_LINE_MARKERS)
    if pl:facts["product_line"]=fact(pl,0.86,"explicit_product_line_marker")

    # food specifics
    vals=term_values_boundary(ntb,MEAT_SPECIES); facts["meat_species"]=fact(vals,0.94,"explicit_title_terms") if vals else None
    vals=term_values_boundary(ntb,MEAT_CUTS);
    if vals:
        vals=[{"breasts":"breast","thighs":"thigh","wings":"wing","drumsticks":"drumstick","tenderloins":"tenderloin","steaks":"steak"}.get(x,x) for x in vals]
        facts["meat_cut"]=fact(sorted(set(vals)),0.94,"explicit_title_terms")
    vals=term_values_boundary(ntb,PRODUCE_SPECIES)
    if vals:facts["produce_species"]=fact(sorted(set(PRODUCE_CANON.get(x,x) for x in vals)),0.94,"explicit_title_terms")
    vals=term_values_boundary(ntb,PROTEIN_SOURCES)
    if vals:facts["protein_source"]=fact(sorted(set(PROTEIN_CANON.get(x,x) for x in vals)),0.92,"explicit_title_terms")
    if "ground" in nt:facts["grind_type"]=fact("ground",0.95,"title")
    if "boneless" in nt:facts["bone_state"]=fact("boneless",0.98,"title")
    elif "bone in" in nt or "bone-in" in nt:facts["bone_state"]=fact("bone_in",0.98,"title")
    if "skinless" in nt:facts["skin_state"]=fact("skinless",0.98,"title")
    elif "skin on" in nt or "skin-on" in nt:facts["skin_state"]=fact("skin_on",0.98,"title")
    vals=term_values_boundary(ntb,SEAFOOD_SPECIES); facts["seafood_species"]=fact(vals,0.94,"explicit_title_terms") if vals else None
    vals=term_values_boundary(ntb,PRODUCE_FORMS); facts["produce_form"]=fact(vals,0.82,"title") if vals else None
    vals=term_values_boundary(ntb,MILK_TYPES); facts["milk_type"]=fact(vals,0.90,"explicit_terms") if vals else None
    vals=term_values_boundary(ntb,CHEESE_TYPES); facts["cheese_type"]=fact(vals,0.90,"explicit_terms") if vals else None
    jp=regex_num_norm(JUICE_RE,nt); facts["juice_percentage"]=fact(jp,0.98,"explicit_percent") if jp is not None else None
    ing=parse_ingredient_signature(desc); facts["ingredient_signature"]=fact(ing,0.82,"description_ingredients") if ing else None

    # alcohol
    wv=wine_varietal_boundary(textb)
    facts["wine_varietal"]=fact(wv,0.95,"title/description") if wv else None
    abv=regex_num_norm(ABV_RE,textn); facts["alcohol_strength_abv"]=fact(abv,0.99,"explicit_abv") if abv is not None else None
    vint=re.search(r"\b(19|20)\d{2}\b",nt)
    if vint and fam and fam.value=="alcohol":facts["vintage"]=fact(int(vint.group(0)),0.92,"title")

    # health/beauty
    ai=active_ingredient(desc); facts["active_ingredient"]=fact(ai,0.96,"description_active_ingredient") if ai else None
    st=active_strength(nt+" "+desc); facts["active_strength"]=fact(st,0.95,"explicit_strength") if st is not None else None
    spf=regex_num_norm(SPF_RE,textn); facts["spf"]=fact(spf,0.99,"explicit_spf") if spf is not None else None
    # Beauty shades are often arbitrary title suffixes rather than standardized fields.
    beauty_hint = bool(fam and fam.value=="beauty_personal") or any(
        x in nt for x in ["makeup","mascara","eyebrow","brow ","lipstick","lip color","foundation","concealer","eyeliner"]
    )
    if beauty_hint:
        shade_hits=[]
        for m in SHADE_RE.finditer(nt):
            shade_hits.append(" ".join(x for x in m.groups() if x).lower())
        # Prefer the last explicit color phrase; product titles commonly put shade at the end.
        if shade_hits:
            facts["shade"]=fact(shade_hits[-1],0.96,"explicit_beauty_shade")

    # baby/pet
    baby_formula_hint = bool(fam and fam.value=="baby") or any(x in nt for x in ["infant formula","baby formula","toddler formula"])
    if baby_formula_hint:
        vals=term_values_boundary(textb,FORMULA_BASES); facts["formula_base"]=fact(vals,0.90,"explicit_terms") if vals else None
    ds=re.search(r"\b(?:diaper|training pants?).{0,25}\b(?:size\s*)?([nN]|\d{1,2}[tT]?)\b",nt)
    if ds:facts["diaper_size"]=fact(ds.group(1).lower(),0.90,"title")
    bs=re.search(r"\b(?:stage|step)\s*(\d)\b",nt)
    if bs:facts["baby_stage"]=fact(bs.group(1),0.88,"title")

    # durable / household / tech
    vals=term_values_boundary(textb,MATERIALS); facts["durable_material"]=fact(vals,0.80,"explicit_terms") if vals else None
    vals=term_values_boundary(textb,AUDIENCE); facts["target_audience"]=fact(vals,0.78,"explicit_terms") if vals else None
    vals=term_values_boundary(textb,OCCASIONS); facts["occasion_or_holiday"]=fact(vals,0.92,"explicit_terms") if vals else None
    facts["theme_or_franchise"]=None  # optional GPT enrichment is safer than an incomplete franchise dictionary
    v=regex_num_norm(VOLT_RE,textn); facts["electrical_voltage"]=fact(v,0.98,"explicit_voltage") if v is not None else None
    w=regex_num_norm(WATT_RE,textn); facts["wattage"]=fact(w,0.96,"explicit_wattage") if w is not None else None
    lum=regex_num_norm(LUMEN_RE,textn); facts["light_output_lumens"]=fact(lum,0.99,"explicit_lumens") if lum is not None else None
    kel=regex_num_norm(KELVIN_RE,textn); facts["light_color_temperature"]=fact(kel,0.99,"explicit_kelvin") if kel is not None else None
    vals=term_values_boundary(textb,BULB_BASES); facts["light_bulb_base"]=fact(vals,0.98,"explicit_code") if vals else None
    vals=term_values_boundary(textb,BULB_SHAPES); facts["light_bulb_shape"]=fact(vals,0.98,"explicit_code") if vals else None
    vals=term_values_boundary(textb,CONNECTORS); facts["connector_type"]=fact(vals,0.95,"explicit_code") if vals else None
    vals=term_values_boundary(textb,BATTERY); facts["battery_spec"]=fact(vals,0.90,"explicit_terms") if vals else None
    sm=STORAGE_RE.search(nt)
    if sm:
        mult={"mb":1,"gb":1024,"tb":1024*1024}[sm.group(2).lower()]
        facts["storage_capacity"]=fact(float(sm.group(1))*mult,0.98,"explicit_capacity")
    ply=regex_num_norm(PLY_RE,textn); facts["paper_ply"]=fact(ply,0.99,"explicit_ply") if ply is not None else None
    uses=regex_num_norm(LOAD_RE,textn); facts["rated_use_count"]=fact(uses,0.98,"explicit_use_count") if uses is not None else None
    lean=LEAN_RE.search(nt)
    if lean:
        leanp=float(lean.group(1)); fatp=float(lean.group(2)) if lean.group(2) else 100-leanp
        facts["ground_meat_lean_fat_ratio"]=fact((leanp,fatp),0.99,"explicit_ratio")
    # eggs
    egg_size=term_values_boundary(ntb,["peewee","small","medium","large","extra large","jumbo"])
    egg_grade=term_values_boundary(ntb,["grade aa","grade a"])
    if "egg" in nt and (egg_size or egg_grade):
        facts["egg_size_grade"]=fact(sorted(set(egg_size+egg_grade)),0.92,"title")

    # Optional GPT facts pre-extracted by the orchestration layer. They can
    # fill only UNKNOWN attributes; deterministic evidence always wins.
    overlay=parse_jsonish(row.get("gpt_enrichment_json", ""))
    if isinstance(overlay,dict):
        for k,v in overlay.items():
            if k not in facts or facts[k] is not None or not isinstance(v,dict):
                continue
            val=v.get("value")
            try: conf=float(v.get("confidence",0.0))
            except Exception: conf=0.0
            if val not in (None,"",[],{}) and conf>=0.75:
                facts[k]=fact(val,min(0.90,conf),"gpt5_nano_review_extraction")

    return facts

# ---------- GPT enrichment ----------


# ---------- comparison ----------

def token_sim(a,b):
    sa=set(a if isinstance(a,(list,set,tuple)) else toks(a))
    sb=set(b if isinstance(b,(list,set,tuple)) else toks(b))
    if not sa or not sb:return 0.0
    inter=len(sa&sb)
    jac=inter/len(sa|sb)
    contain=inter/min(len(sa),len(sb))
    # Product titles are often the same core identity with retailer-specific extra words.
    # Containment is both faster and more appropriate than generic edit-distance matching.
    return max(jac,contain*0.92)

def numeric_sim(a,b,rel_tol=0.03):
    try:
        av=float(a);bv=float(b)
    except:return 0.0
    den=max(abs(av),abs(bv),1e-9); rel=abs(av-bv)/den
    if rel<=0.005:return 1.0
    if rel<=rel_tol:return 0.90
    if rel<=0.10:return 0.45
    return 0.0

def value_positive_similarity(attr, av, bv):
    if av is None or bv is None:return 0.0
    if attr=="normalized_total_quantity":
        if isinstance(av,(list,tuple)) and isinstance(bv,(list,tuple)) and len(av)==2 and len(bv)==2:
            if av[0]!=bv[0]:return 0.0
            return numeric_sim(av[1],bv[1])
        return 0.0
    if attr in {"net_weight","net_volume","declared_count","pack_structure",
                "piece_weight","capacity","active_strength","spf","alcohol_strength_abv","juice_percentage",
                "electrical_voltage","wattage","storage_capacity","paper_ply","rated_use_count",
                "light_output_lumens","light_color_temperature","light_watt_equivalent"}:
        return numeric_sim(av,bv)
    if attr=="dimension_signature":
        if len(av)!=len(bv):return 0.0
        return sum(numeric_sim(x,y,0.02) for x,y in zip(av,bv))/len(av)
    if attr=="ground_meat_lean_fat_ratio":
        return 1.0 if tuple(av)==tuple(bv) else 0.0
    if isinstance(av,bool) or isinstance(bv,bool):
        return 1.0 if av==bv else 0.0
    if attr in {"functional_name","ingredient_signature","model_or_part_identifier"}:
        return token_sim(av,bv)
    if isinstance(av,(list,set,tuple)) or isinstance(bv,(list,set,tuple)):
        return token_sim(av,bv)
    na,nb=norm_text(av),norm_text(bv)
    if not na or not nb:return 0.0
    if na==nb:return 1.0
    return token_sim(na,nb)

def values_conflict(attr,av,bv):
    if av is None or bv is None:return False,0.0
    sim=value_positive_similarity(attr,av,bv)
    # Quantity conflicts require same dimension by construction; comparison only occurs same attr.
    if attr=="normalized_total_quantity":
        if isinstance(av,(list,tuple)) and isinstance(bv,(list,tuple)) and len(av)==2 and len(bv)==2:
            if av[0]!=bv[0]: return False,0.0  # incomparable dimensions => UNKNOWN, not contradiction
            sim=value_positive_similarity(attr,av,bv)
            return sim<0.45,1.0-sim
        return False,0.0
    if attr in {"net_weight","net_volume","declared_count","pack_structure",
                "active_strength","spf","alcohol_strength_abv","juice_percentage","electrical_voltage",
                "wattage","storage_capacity","paper_ply","rated_use_count","light_output_lumens",
                "light_color_temperature","light_watt_equivalent"}:
        return sim<0.45, 1.0-sim
    if attr=="dimension_signature":
        return sim<0.50,1.0-sim
    if attr in {"functional_name"}:
        # conservative: only a very low overlap counts as explicit conflict
        return sim<0.18,1.0-sim
    if attr in {"product_family","canonical_category"}:
        a,b=norm_text(av),norm_text(bv)
        if a==b:return False,0.0
        # food/beverage/alcohol taxonomies overlap heavily across retailers; never treat
        # those neighboring labels as a deterministic contradiction.
        edible={"food","beverage","alcohol"}
        if a in edible and b in edible:return False,0.0
        return True,0.65
    if attr in {"brand"}:
        return norm_brand(av)!=norm_brand(bv), 1.0 if norm_brand(av)!=norm_brand(bv) else 0.0
    if isinstance(av,(list,set,tuple)) or isinstance(bv,(list,set,tuple)):
        sa=set(map(norm_text,av if isinstance(av,(list,set,tuple)) else [av]))
        sb=set(map(norm_text,bv if isinstance(bv,(list,set,tuple)) else [bv]))
        if sa and sb and sa.isdisjoint(sb): return True,1.0
        return False,0.0
    return (norm_text(av)!=norm_text(bv), 1.0 if norm_text(av)!=norm_text(bv) else 0.0)

def criticality_weight(s):
    s=(s or "").lower()
    if "fatal" in s:return 7.0
    if "strong" in s:return 3.5
    if "contextual" in s:return 1.5
    return 2.0

def applicable(attr, fa, fb):
    g=attr["group"]; name=attr["name"]
    if g in {"identity","quantity_packaging","routing","form_variant","variant"}:return True
    A=fa.get("product_family");B=fb.get("product_family")
    fams={x.value for x in (A,B) if x}
    if not fams:return False
    if g in {"food_composition","food","alcohol","beverage"}:
        return bool(fams & {"food","beverage","alcohol"})
    if g in {"health_personal_care","personal_care"}:
        return bool(fams & {"medicine","beauty_personal"})
    if g in {"baby_pet","pet"}:
        return bool(fams & {"baby","pet"})
    if g in {"durable_apparel"}:
        return bool(fams & {"household_tech","household","apparel"})
    if g in {"household","household_tech"}:
        return bool(fams & {"household_tech","household"})
    return True

def fact_quality_pair(a,b):
    return math.sqrt(a.quality*b.quality)

# Mapping canonical attributes -> explicit v2 veto IDs.
VETO_BY_ATTR = {
    "canonical_category":"V001","brand":"V002","functional_name":"V004","product_family":"V005",
    "model_or_part_identifier":"V006","variant_name":"V007","normalized_total_quantity":"V008","declared_count":"V008",
    "pack_structure":"V009","refill_vs_starter_kit":"V010","storage_state":"V011",
    "physical_state":"V012","flavor":"V013","scent":"V013","protein_source":"V066","produce_species":"V015",
    "produce_form":"V016","meat_species":"V017","meat_cut":"V017","grind_type":"V018",
    "bone_state":"V018","skin_state":"V018","ground_meat_lean_fat_ratio":"V019",
    "seafood_species":"V020","egg_size_grade":"V021","egg_production_method":"V022",
    "beverage_type":"V023","water_source_treatment_type":"V024","carbonation_status":"V025",
    "caffeine_status":"V026","juice_percentage":"V027","milk_type":"V028","fat_level":"V028",
    "cheese_type":"V029","alcohol_class":"V030","wine_varietal":"V031",
    "alcohol_strength_abv":"V032","vintage":"V033","origin_region_appellation":"V033",
    "active_ingredient":"V034","active_strength":"V035","dosage_form":"V036","coffee_format":"V067",
    "route_of_administration":"V037","release_type":"V038","spf":"V039","shade":"V040",
    "finish":"V041","hair_type":"V042","skin_type":"V042","absorbency":"V043",
    "bristle_firmness":"V044","baby_stage":"V045","formula_base":"V046","diaper_size":"V047",
    "pet_species":"V048","pet_life_stage":"V049","pet_litter_clumping_status":"V050",
    "footwear_size_compatibility":"V051","paper_ply":"V052","rated_use_count":"V053",
    "dimension_signature":"V054","electrical_voltage":"V055","light_bulb_base":"V056",
    "light_bulb_shape":"V057","light_output_lumens":"V058","light_color_temperature":"V058",
    "light_watt_equivalent":"V058","connector_type":"V059","battery_spec":"V060",
    "storage_capacity":"V061","theme_or_franchise":"V062","occasion_or_holiday":"V063",
}
DIET_ATTRS={"organic_status","gluten_status","sugar_status","sodium_status","fat_level",
            "lactose_status","vegan_vegetarian_status","non_gmo_status"}

def load_criteria(path):
    return json.load(open(path,encoding="utf-8"))

def serialize_fact(f):
    return None if not f else {"value":f.value,"quality":round(f.quality,4),"source":f.source}

# Runtime cache maintenance retained as a compatibility no-op.
def clear_runtime_text_caches():
    return None
