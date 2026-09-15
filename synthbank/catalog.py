"""
Reference data for the synthetic bank.

Everything here is static and hand-written: countries, the known merchant
pool per category, name parts for generating merchants the categoriser has
never seen, and hard cases (payment-facilitator prefixes, truncation, typos,
merchants whose name points at the wrong category).

Labels use FintNet's own 14 categories (categorize.CATEGORIES), so evaluation
measures the taxonomy the app actually uses.

No language model is used to build any of this: a model writing the merchants
it later classifies would grade its own homework.
"""
from __future__ import annotations

CATEGORIES = [
    "Groceries", "Food Delivery", "Dining", "Transport", "ATM / Cash",
    "Shopping", "Entertainment", "Utilities", "Healthcare",
    "Health & Fitness", "Housing", "Charity", "Income",
    "Transfers / Other",
]

# Synthetic bank identity per country. Bank codes are placeholders for a
# test bank; IBANs are built with a valid mod-97 check digit.
COUNTRIES: dict[str, dict] = {
    "DE": {"currency": "EUR", "fx": 1.0, "bank_code": "99010000", "bic": "SYNBDEFFXXX",
           "cities": ["Berlin", "Muenchen", "Hamburg", "Koeln", "Frankfurt", "Stuttgart", "Leipzig"]},
    "FI": {"currency": "EUR", "fx": 1.0, "bank_code": "990100", "bic": "SYNBFIHHXXX",
           "cities": ["Helsinki", "Espoo", "Tampere", "Turku", "Oulu", "Vantaa"]},
    "SE": {"currency": "SEK", "fx": 11.2, "bank_code": "990", "bic": "SYNBSESSXXX",
           "cities": ["Stockholm", "Goeteborg", "Malmoe", "Uppsala", "Vaesteraas"]},
    "NL": {"currency": "EUR", "fx": 1.0, "bank_code": "SYNB", "bic": "SYNBNL2AXXX",
           "cities": ["Amsterdam", "Rotterdam", "Utrecht", "Den Haag", "Eindhoven"]},
    "IT": {"currency": "EUR", "fx": 1.0, "bank_code": "99010", "bic": "SYNBITMMXXX",
           "cities": ["Milano", "Roma", "Torino", "Bologna", "Napoli", "Firenze"]},
}

# Known merchant pool: (country, category) -> merchant names.
KNOWN: dict[str, dict[str, list[str]]] = {
    "DE": {
        "Groceries": ["REWE", "EDEKA", "Lidl", "Aldi Sued", "Kaufland", "Netto Marken-Discount", "Penny"],
        "Food Delivery": ["Lieferando", "Wolt", "Uber Eats"],
        "Dining": ["Vapiano", "L'Osteria", "McDonald's", "Burger King", "Starbucks", "Block House"],
        "Transport": ["Deutsche Bahn", "BVG", "MVG", "Flixbus", "Shell", "Aral", "FREE NOW"],
        "Shopping": ["Zalando", "Amazon EU", "MediaMarkt", "IKEA", "H&M", "dm-drogerie markt", "Otto"],
        "Entertainment": ["Netflix", "Spotify", "Disney+", "CinemaxX", "DAZN", "Audible"],
        "Utilities": ["Vattenfall", "E.ON Energie", "Deutsche Telekom", "Vodafone", "O2 Telefonica"],
        "Healthcare": ["DocMorris", "Shop Apotheke", "Techniker Krankenkasse"],
        "Health & Fitness": ["McFit", "FitX", "Urban Sports Club"],
        "Housing": ["Vonovia", "Deutsche Wohnen"],
        "Charity": ["Deutsches Rotes Kreuz", "UNICEF Deutschland"],
    },
    "FI": {
        "Groceries": ["K-Citymarket", "Prisma", "S-market", "Lidl Suomi", "Alepa", "K-Market"],
        "Food Delivery": ["Wolt", "Foodora"],
        "Dining": ["Hesburger", "Fazer Cafe", "Robert's Coffee", "Kotipizza"],
        "Transport": ["HSL", "VR", "Neste", "Finnair", "Bolt"],
        "Shopping": ["Stockmann", "Tokmanni", "Verkkokauppa.com", "Clas Ohlson", "Zalando"],
        "Entertainment": ["Netflix", "Spotify", "Finnkino", "Elisa Viihde"],
        "Utilities": ["Helen Oy", "Fortum", "Elisa", "DNA Oyj", "Telia Finland"],
        "Healthcare": ["Yliopiston Apteekki", "Terveystalo", "Mehilainen"],
        "Health & Fitness": ["Elixia", "Fitness24Seven"],
        "Housing": ["SATO", "Lumo Kodit"],
        "Charity": ["Suomen Punainen Risti", "Helsinkimissio"],
    },
    "SE": {
        "Groceries": ["ICA Naera", "Coop", "Willys", "Hemkoep", "Lidl Sverige"],
        "Food Delivery": ["Foodora", "Wolt"],
        "Dining": ["Max Hamburgare", "Espresso House", "Wayne's Coffee"],
        "Transport": ["SL", "SJ", "Circle K", "Uber", "Voi"],
        "Shopping": ["H&M", "IKEA", "Elgiganten", "Ahlens", "Clas Ohlson"],
        "Entertainment": ["Spotify", "Netflix", "SF Bio", "Viaplay"],
        "Utilities": ["Vattenfall", "Ellevio", "Telia", "Tele2", "Bredband2"],
        "Healthcare": ["Apotek Hjartat", "Apoteket", "Kry"],
        "Health & Fitness": ["SATS", "Nordic Wellness"],
        "Housing": ["Stockholmshem", "Heimstaden"],
        "Charity": ["Roeda Korset", "Laekare Utan Graenser"],
    },
    "NL": {
        "Groceries": ["Albert Heijn", "Jumbo", "Lidl", "Plus", "Dirk"],
        "Food Delivery": ["Thuisbezorgd.nl", "Uber Eats"],
        "Dining": ["Febo", "La Place", "Starbucks"],
        "Transport": ["NS", "GVB", "Shell", "OV-chipkaart", "Swapfiets"],
        "Shopping": ["Bol.com", "HEMA", "Coolblue", "Action", "Kruidvat"],
        "Entertainment": ["Netflix", "Spotify", "Pathe", "Videoland"],
        "Utilities": ["Eneco", "Vattenfall", "KPN", "Ziggo", "Vitens"],
        "Healthcare": ["Zilveren Kruis", "Apotheek Centraal"],
        "Health & Fitness": ["Basic-Fit", "SportCity"],
        "Housing": ["Ymere", "Rochdale"],
        "Charity": ["KWF Kankerbestrijding", "Rode Kruis"],
    },
    "IT": {
        "Groceries": ["Esselunga", "Conad", "Coop Italia", "Carrefour Market", "Lidl Italia"],
        "Food Delivery": ["Glovo", "Just Eat", "Deliveroo"],
        "Dining": ["Autogrill", "Old Wild West", "Spizzico"],
        "Transport": ["Trenitalia", "Italo", "ATM Milano", "Eni Station", "Q8"],
        "Shopping": ["Amazon.it", "Zara", "MediaWorld", "OVS", "Decathlon"],
        "Entertainment": ["Netflix", "Spotify", "DAZN", "UCI Cinemas"],
        "Utilities": ["Enel Energia", "A2A", "TIM", "Vodafone Italia", "Iren"],
        "Healthcare": ["Farmacia Comunale", "Unisalute"],
        "Health & Fitness": ["McFit Italia", "Virgin Active"],
        "Housing": ["Gabetti Property", "Immobiliare.it Affitti"],
        "Charity": ["Emergency ONG", "Croce Rossa Italiana"],
    },
}

EMPLOYERS: dict[str, list[str]] = {
    "DE": ["SAP SE", "Siemens AG", "Allianz SE", "BMW AG", "Bosch GmbH"],
    "FI": ["Nokia Oyj", "Kone Oyj", "UPM-Kymmene Oyj", "Wartsila Oyj"],
    "SE": ["Volvo Cars AB", "Ericsson AB", "Spotify AB", "Scania AB"],
    "NL": ["Philips Nederland BV", "ASML Netherlands BV", "Unilever Nederland BV"],
    "IT": ["Enel SpA", "Eni SpA", "Generali Italia SpA", "Leonardo SpA"],
}

PENSION_PAYERS: dict[str, str] = {
    "DE": "Deutsche Rentenversicherung", "FI": "Kela", "SE": "Pensionsmyndigheten",
    "NL": "SVB Sociale Verzekeringsbank", "IT": "INPS",
}

# Name parts for people (P2P transfers) and for new merchants.
FIRST: dict[str, list[str]] = {
    "DE": ["Lena", "Jonas", "Anna", "Lukas", "Sophie", "Felix", "Marie", "Paul"],
    "FI": ["Aino", "Eetu", "Emilia", "Juho", "Sanna", "Mikko", "Laura", "Ville"],
    "SE": ["Elsa", "Oskar", "Maja", "Erik", "Ebba", "Lars", "Ingrid", "Nils"],
    "NL": ["Sanne", "Daan", "Emma", "Bram", "Lotte", "Sem", "Fleur", "Joris"],
    "IT": ["Giulia", "Marco", "Chiara", "Luca", "Sara", "Matteo", "Elena", "Paolo"],
}
LAST: dict[str, list[str]] = {
    "DE": ["Hoffmann", "Becker", "Schulz", "Wagner", "Krueger", "Hartmann", "Lange", "Brandt"],
    "FI": ["Virtanen", "Korhonen", "Nieminen", "Makinen", "Heikkinen", "Laine", "Koskinen"],
    "SE": ["Lindqvist", "Berg", "Holm", "Sjoeberg", "Nystroem", "Engstroem", "Lund"],
    "NL": ["de Vries", "Bakker", "Visser", "Smit", "Meijer", "Mulder", "Bos"],
    "IT": ["Bianchi", "Ferrari", "Esposito", "Romano", "Colombo", "Ricci", "Marino"],
}
PLACE: dict[str, list[str]] = {
    "DE": ["Marktplatz", "Rathaus", "Bahnhof", "Schlossgarten", "Lindenhof", "Am Park"],
    "FI": ["Kallio", "Toolo", "Keskusta", "Rantakatu", "Kauppatori"],
    "SE": ["Soedermalm", "Vasastan", "Torget", "Hamnen", "Stortorget"],
    "NL": ["de Markt", "het Plein", "de Gracht", "Centrum", "de Dam"],
    "IT": ["Duomo", "Piazza Garibaldi", "Stazione", "Porta Nuova", "Corso Italia"],
}

# Templates for merchants the categoriser has never seen, per country and
# category. Placeholders: {last} {first} {place} {city}.
NOVEL: dict[str, dict[str, list[str]]] = {
    "DE": {
        "Groceries": ["Hofladen {last}", "Bio-Markt {place}", "Getraenke {last}", "Obst & Gemuese {first}"],
        "Food Delivery": ["{last} Lieferservice", "Pizza-Taxi {city}", "Sushi Bote {place}"],
        "Dining": ["Gasthaus zum {last}", "Pizzeria {place}", "Cafe {first}", "Brauhaus {last}"],
        "Transport": ["Taxi {last}", "Parkhaus {place}", "Tankstelle {last}", "Fahrradverleih {city}"],
        "Shopping": ["{last} Mode", "Buchhandlung {last}", "Schuhhaus {last}", "Elektro {last}"],
        "Entertainment": ["Kino am {place}", "Theater {city}", "Bowling Center {last}"],
        "Utilities": ["Stadtwerke {city}", "{city} Netz GmbH", "Glasfaser {city}"],
        "Healthcare": ["Apotheke am {place}", "Zahnarztpraxis Dr. {last}", "Physiotherapie {last}"],
        "Health & Fitness": ["{last} Fitness", "Yoga Studio {first}", "Kletterhalle {city}"],
        "Housing": ["Hausverwaltung {last}", "Immobilien {last} GmbH"],
        "Charity": ["Tafel {city} e.V.", "Tierheim {city} e.V."],
    },
    "FI": {
        "Groceries": ["Lahikauppa {place}", "Leipomo {last}", "Kauppahalli {city}"],
        "Food Delivery": ["{city} Ruokalahetti", "Pizza Express {place}"],
        "Dining": ["Kahvila {first}", "Ravintola {place}", "Grilli {last}"],
        "Transport": ["Taksi {last}", "Pysakointi {place}", "Pyoravuokraamo {city}"],
        "Shopping": ["Kirjakauppa {last}", "Vaateliike {first}", "Kodinkone {last}"],
        "Entertainment": ["Elokuvateatteri {place}", "Keilahalli {city}"],
        "Utilities": ["{city} Energia Oy", "Kuitu {city}"],
        "Healthcare": ["Apteekki {place}", "Hammaslaakari {last}"],
        "Health & Fitness": ["Kuntosali {place}", "Joogastudio {first}"],
        "Housing": ["Isannointi {last} Oy", "Asunto Oy {place}"],
        "Charity": ["{city}n Ruoka-apu ry", "Elainsuojeluyhdistys {city} ry"],
    },
    "SE": {
        "Groceries": ["Livs {place}", "Bageri {last}", "Frukt & Gront {first}"],
        "Food Delivery": ["{city} Matleverans", "Pizzabud {place}"],
        "Dining": ["Konditori {first}", "Restaurang {place}", "Grill {last}"],
        "Transport": ["Taxi {city}", "Parkering {place}", "Cykeluthyrning {last}"],
        "Shopping": ["Bokhandel {last}", "Klaedbutik {first}", "Elektronik {last}"],
        "Entertainment": ["Biograf {place}", "Bowlinghall {city}"],
        "Utilities": ["{city} Energi AB", "Stadsnaet {city}"],
        "Healthcare": ["Apotek {place}", "Tandlaekare {last}"],
        "Health & Fitness": ["Gym {place}", "Yogastudio {first}"],
        "Housing": ["Fastighets AB {last}", "Bostadsbolaget {city}"],
        "Charity": ["Stadsmissionen {city}", "Djurskyddet {city}"],
    },
    "NL": {
        "Groceries": ["Bakkerij {last}", "Groenteboer {first}", "Toko {place}"],
        "Food Delivery": ["{city} Bezorgservice", "Pizzeria {place} Bezorging"],
        "Dining": ["Eetcafe {place}", "Brasserie {last}", "Snackbar {first}"],
        "Transport": ["Taxi {last}", "Parkeergarage {place}", "Fietsverhuur {city}"],
        "Shopping": ["Boekhandel {last}", "Kledingzaak {first}", "Electro {last}"],
        "Entertainment": ["Filmhuis {city}", "Bowling {place}"],
        "Utilities": ["Energie {city}", "Glasvezel {city}"],
        "Healthcare": ["Apotheek {place}", "Tandarts {last}", "Fysiotherapie {last}"],
        "Health & Fitness": ["Sportschool {place}", "Yoga {first}"],
        "Housing": ["Woningstichting {city}", "Vastgoed {last} BV"],
        "Charity": ["Voedselbank {city}", "Dierenasiel {city}"],
    },
    "IT": {
        "Groceries": ["Panificio {last}", "Alimentari {place}", "Frutta e Verdura {first}"],
        "Food Delivery": ["{city} Consegne", "Pizza a Domicilio {place}"],
        "Dining": ["Trattoria {first}", "Bar {place}", "Osteria {last}"],
        "Transport": ["Taxi {city}", "Parcheggio {place}", "Noleggio Bici {last}"],
        "Shopping": ["Libreria {last}", "Boutique {first}", "Elettronica {last}"],
        "Entertainment": ["Cinema {place}", "Teatro {city}"],
        "Utilities": ["{city} Energia Srl", "Fibra {city}"],
        "Healthcare": ["Farmacia {place}", "Studio Dentistico {last}"],
        "Health & Fitness": ["Palestra {place}", "Yoga {first}"],
        "Housing": ["Immobiliare {last}", "Condominio {place}"],
        "Charity": ["Caritas {city}", "Canile {city} Onlus"],
    },
}

# Hard cases with honest labels: the name misleads, is wrapped by a payment
# facilitator, or points at a different category than its brand suggests.
HARD_FIXED: list[tuple[str, str]] = [
    ("IKEA Restaurant", "Dining"),
    ("Lidl Connect", "Utilities"),
    ("Aral Store Backshop", "Dining"),
    ("Apple.com/bill", "Entertainment"),
    ("Google *YouTube Premium", "Entertainment"),
    ("Amazon Prime Video", "Entertainment"),
    ("Amazon Fresh", "Groceries"),
    ("Tesla Supercharger", "Transport"),
    ("DB Vertrieb GmbH", "Transport"),
    ("Bolt Food", "Food Delivery"),
    ("Uber *Trip", "Transport"),
    ("Uber *Eats", "Food Delivery"),
    ("Booking.com Hotel", "Transport"),
    ("PayPal *Spotify", "Entertainment"),
    ("Klarna *Zalando", "Shopping"),
    ("Apotheke im Hauptbahnhof", "Healthcare"),
]

FACILITATOR_PREFIXES = ["SUMUP *", "SQ *", "ZETTLE_*", "PAYPAL *", "STRIPE* "]

SUBSCRIPTIONS = ["Netflix", "Spotify", "Disney+", "DAZN", "Audible", "Viaplay", "Videoland", "Apple.com/bill"]

# Amount ranges in EUR before FX: (min, max).
AMOUNTS: dict[str, tuple[float, float]] = {
    "Groceries": (6, 95), "Food Delivery": (12, 48), "Dining": (4, 65), "Transport": (2, 85),
    "ATM / Cash": (20, 200), "Shopping": (9, 180), "Entertainment": (8, 35), "Utilities": (25, 120),
    "Healthcare": (5, 70), "Health & Fitness": (15, 60), "Housing": (600, 1500), "Charity": (5, 40),
    "Transfers / Other": (10, 150),
}

# Discretionary transactions per month by persona, per category.
MONTHLY_RATE: dict[str, dict[str, float]] = {
    "salaried":  {"Groceries": 10, "Food Delivery": 2, "Dining": 5, "Transport": 6, "Shopping": 4,
                  "Entertainment": 1, "Healthcare": 1, "ATM / Cash": 1, "Transfers / Other": 2},
    "family":    {"Groceries": 14, "Food Delivery": 2, "Dining": 3, "Transport": 7, "Shopping": 6,
                  "Entertainment": 1.5, "Healthcare": 2, "ATM / Cash": 1.5, "Transfers / Other": 2},
    "student":   {"Groceries": 8, "Food Delivery": 4, "Dining": 6, "Transport": 5, "Shopping": 2,
                  "Entertainment": 1.5, "Healthcare": 0.5, "ATM / Cash": 1, "Transfers / Other": 3},
    "pensioner": {"Groceries": 11, "Food Delivery": 0.3, "Dining": 2, "Transport": 3, "Shopping": 2,
                  "Entertainment": 0.5, "Healthcare": 3, "ATM / Cash": 2, "Transfers / Other": 1},
    "freelancer": {"Groceries": 9, "Food Delivery": 3, "Dining": 6, "Transport": 5, "Shopping": 4,
                   "Entertainment": 1, "Healthcare": 1, "ATM / Cash": 1, "Transfers / Other": 3},
}

# Currency conversion used when a generated amount (drawn in EUR) is booked
# on an account in another currency.
FX = {"EUR": 1.0, "SEK": 11.2}

# The banks FintNet connects, with the country their generated IBANs use.
BANK_COUNTRY = {"unicredit": "IT", "commerzbank": "DE", "ing": "NL"}   # nordea follows the customer (FI or SE)
BANK_LABEL = {"unicredit": "UniCredit", "commerzbank": "Commerzbank", "nordea": "Nordea", "ing": "ING"}

# The 5 demo logins. Each is the test user in one bank's PSD2 sandbox
# (`home_bank`) and also holds generated accounts at one or more of the banks
# FintNet connects (`accounts`: bank, role, currency). Roles decide which
# transactions an account books:
#   main     salary, rent, utilities, insurance, cash, transfers
#   savings  the monthly savings transfer
#   spend    subscriptions, gym, shopping, food delivery
#   daily    dining, transport and half of the groceries
DEMO_CUSTOMERS: list[dict] = [
    {"customer_id": "DEMO-DE-THOMASMANN", "name": "Thomas Mann", "country": "DE", "persona": "salaried",
     "email": "thomas.mann@example.de", "employer": "SAP SE", "sandbox": "Commerzbank", "home_bank": "commerzbank",
     "accounts": [("commerzbank", "main", "EUR"), ("commerzbank", "savings", "EUR"), ("ing", "spend", "EUR")]},
    {"customer_id": "DEMO-FI-AINOSALO", "name": "Aino Salo", "country": "FI", "persona": "salaried",
     "email": "aino.salo@example.fi", "employer": "Nokia Oyj", "sandbox": "Nordea FI", "home_bank": "nordea",
     "accounts": [("nordea", "main", "EUR"), ("nordea", "savings", "EUR"), ("unicredit", "daily", "EUR")]},
    {"customer_id": "DEMO-SE-MARGITALROS", "name": "Margit Alros", "country": "SE", "persona": "family",
     "email": "margit.alros@example.se", "employer": "Volvo Cars AB", "sandbox": "Nordea SE", "home_bank": "nordea",
     "accounts": [("nordea", "main", "SEK"), ("ing", "savings", "EUR"), ("commerzbank", "spend", "EUR"),
                  ("unicredit", "daily", "EUR")]},
    {"customer_id": "DEMO-NL-VANDIJK", "name": "Hr A van Dijk, Mw B Mol-van Dijk", "country": "NL",
     "persona": "family", "email": "a.vandijk@example.nl", "employer": "Philips Nederland BV", "sandbox": "ING NL",
     "home_bank": "ing",
     "accounts": [("ing", "main", "EUR"), ("ing", "savings", "EUR"), ("commerzbank", "spend", "EUR")]},
    {"customer_id": "DEMO-IT-MARIOROSSI", "name": "Mario Rossi", "country": "IT", "persona": "salaried",
     "email": "mario.rossi@example.it", "employer": "Enel SpA", "sandbox": "UniCredit IT", "home_bank": "unicredit",
     "accounts": [("unicredit", "main", "EUR"), ("unicredit", "savings", "EUR"), ("commerzbank", "spend", "EUR")]},
]


def demo_customer(customer_id: str) -> dict | None:
    return next((d for d in DEMO_CUSTOMERS if d["customer_id"] == customer_id), None)
