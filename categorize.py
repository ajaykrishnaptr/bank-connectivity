_RULES = [
    ("Groceries",        ["dmart", "big bazaar", "reliance fresh", "more supermarket", "spencer",
                           "k market", "wallmart", "walmart", "lidl", "spar", "prisma", "alepa", "s-market"]),
    ("Food Delivery",    ["swiggy", "zomato"]),
    ("Dining",           ["café", "cafe", "coffee day", "starbucks", "mcdonald", "kfc", "pizza",
                           "domino", "restaurant", "bistro", "eatery"]),
    ("Transport",        ["ola cabs", "uber", "irctc", "indigo", "air india", "avis", "taxi",
                           "metro", "bus ", "train", "ryanair", "finnair"]),
    ("ATM / Cash",       ["atm", "nosto"]),
    ("Shopping",         ["flipkart", "amazon", "myntra", "ajio", "heinemann", "retail", "shop", "store"]),
    ("Entertainment",    ["bookmyshow", "netflix", "hotstar", "prime video", "spotify", "cinema", "pvr"]),
    ("Utilities",        ["jio fiber", "jio ", "bses", "airtel", "tata power", "electricity",
                           "broadband", "fiber", "water board"]),
    ("Healthcare",       ["apollo", "medplus", "1mg", "pharmacy", "hospital", "clinic"]),
    ("Health & Fitness", ["sports club", "gym", "fitness", "cross  sports"]),
    ("Housing",          ["appartment", "vuokra", "rent", "housing"]),
    ("Charity",          ["helsinkimissio", " ry", " rf "]),
    ("Income",           ["salary", "kela", "fpa", "kansaneläke", "bonus", "freelance"]),
]


def categorize(merchant: str) -> str:
    if not merchant:
        return "Other"
    m = merchant.lower()
    for category, keywords in _RULES:
        if any(kw in m for kw in keywords):
            return category
    return "Transfers / Other"
