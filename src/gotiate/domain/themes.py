"""Default V1 theme content — fictional companies. This is content/config, not
game rules (domain model §02, ThemeSet/ThemeEntityDefinition). A real ThemeSet
table with versioning is future work; for now `theme_key` just stores the
display name directly.

18 entities — enough headroom for the largest V1 market (17, at 6 players).
"""

from __future__ import annotations

FICTIONAL_COMPANIES_V1: list[tuple[str, str]] = [
    ("daveco", "DaveCo"),
    ("tiny_horse_motors", "Tiny Horse Motors"),
    ("grandmas_pharma", "Grandma's Pharmaceuticals"),
    ("cryptomatic", "Crypt-o-Matic"),
    ("big_tuna", "Big Tuna Seafood"),
    ("moonshot_ai", "Moonshot AI"),
    ("questionable_meats", "Questionable Meats"),
    ("emotional_support_robotics", "Emotional Support Robotics"),
    ("uncle_larrys_lasers", "Uncle Larry's Lasers"),
    ("nocturnal_notary", "Nocturnal Notary Co."),
    ("spite_industries", "Spite Industries"),
    ("driftwood_analytics", "Driftwood Analytics"),
    ("hush_money_hvac", "Hush Money HVAC"),
    ("feral_logistics", "Feral Logistics"),
    ("second_breakfast_capital", "Second Breakfast Capital"),
    ("gullible_gullwing", "Gullible Gullwing Motors"),
    ("pterodactyl_courier", "Pterodactyl Courier"),
    ("aunt_ruths_arbitrage", "Aunt Ruth's Arbitrage"),
]
