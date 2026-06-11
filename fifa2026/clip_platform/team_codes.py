"""Map FIFA video titles to ISO flag codes for promo match cards."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEAM_CODES_JSON = ROOT / "data" / "team_codes.json"

# FIFA / broadcast naming → flagcdn.com code
DEFAULT_ALIASES: dict[str, str] = {
    "argentina": "ar",
    "australia": "au",
    "austria": "at",
    "belgium": "be",
    "brazil": "br",
    "cameroon": "cm",
    "canada": "ca",
    "chile": "cl",
    "china": "cn",
    "colombia": "co",
    "costa rica": "cr",
    "croatia": "hr",
    "czechia": "cz",
    "czech republic": "cz",
    "denmark": "dk",
    "ecuador": "ec",
    "egypt": "eg",
    "england": "gb-eng",
    "france": "fr",
    "germany": "de",
    "ghana": "gh",
    "greece": "gr",
    "honduras": "hn",
    "iran": "ir",
    "iraq": "iq",
    "italy": "it",
    "ivory coast": "ci",
    "cote d'ivoire": "ci",
    "jamaica": "jm",
    "japan": "jp",
    "korea republic": "kr",
    "republic of korea": "kr",
    "south korea": "kr",
    "mexico": "mx",
    "morocco": "ma",
    "netherlands": "nl",
    "holland": "nl",
    "new zealand": "nz",
    "nigeria": "ng",
    "norway": "no",
    "panama": "pa",
    "paraguay": "py",
    "peru": "pe",
    "poland": "pl",
    "portugal": "pt",
    "qatar": "qa",
    "saudi arabia": "sa",
    "scotland": "gb-sct",
    "senegal": "sn",
    "serbia": "rs",
    "south africa": "za",
    "spain": "es",
    "sweden": "se",
    "switzerland": "ch",
    "tunisia": "tn",
    "turkey": "tr",
    "ukraine": "ua",
    "united states": "us",
    "usa": "us",
    "uruguay": "uy",
    "wales": "gb-wls",
}


@lru_cache(maxsize=1)
def _load_aliases() -> dict[str, str]:
    aliases = dict(DEFAULT_ALIASES)
    if TEAM_CODES_JSON.exists():
        with TEAM_CODES_JSON.open(encoding="utf-8") as f:
            extra = json.load(f)
        for key, code in extra.items():
            aliases[key.strip().lower()] = code.strip().lower()
    return aliases


def _normalize(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s*\|.*$", "", name)
    name = re.sub(r"\s*#.*$", "", name)
    name = re.sub(r"\s+FIFA.*$", "", name, flags=re.I)
    name = re.sub(r"\s+World Cup.*$", "", name, flags=re.I)
    return name.strip(" -–—")


def team_code(name: str) -> str | None:
    key = _normalize(name).lower()
    if not key:
        return None
    aliases = _load_aliases()
    if key in aliases:
        return aliases[key]
    # Partial match: "Korea Republic Train" → korea republic
    for alias, code in sorted(aliases.items(), key=lambda x: -len(x[0])):
        if alias in key or key in alias:
            return code
    return None


def teams_from_title(title: str) -> tuple[str | None, str | None]:
    """Extract up to two team names from common FIFA upload title patterns."""
    clean = re.sub(r"\s*#shorts.*$", "", title, flags=re.I).strip()
    patterns = [
        r"Match Preview:\s*(.+?)\s+vs\.?\s+(.+?)(?:\s*\||$)",
        r"(.+?)\s+vs\.?\s+(.+?)(?:\s*\||$)",
        r"(.+?)\s+Train Before\s+(.+?)(?:\s*\||$)",
        r"(.+?)\s+On Playing\s+(.+?)(?:\s*\||$)",
        r"(.+?)\s+face\s+(.+?)(?:\s*\||$)",
        r"(.+?)\s+meet\s+(.+?)(?:\s*\||$)",
    ]
    for pat in patterns:
        m = re.search(pat, clean, re.I)
        if m:
            return _normalize(m.group(1)), _normalize(m.group(2))

    # Single-team training / press titles
    single_patterns = [
        r"^(.+?)\s+Train Before\b",
        r"^(.+?)\s+On Playing\b",
        r"^(.+?)\s+answers questions\b",
    ]
    for pat in single_patterns:
        m = re.search(pat, clean, re.I)
        if m:
            return _normalize(m.group(1)), None
    return None, None


def resolve_match_teams(title: str) -> list[tuple[str, str]]:
    """Return [(display_name, flag_code), ...] for teams found in title."""
    team_a, team_b = teams_from_title(title)
    out: list[tuple[str, str]] = []
    for name in (team_a, team_b):
        if not name:
            continue
        code = team_code(name)
        if code:
            out.append((name, code))
    return out
