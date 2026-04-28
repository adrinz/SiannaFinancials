"""Copy-trade V1: curated institutional 13F-style holdings and static themes.

Data is for research only — not real-time, not trade execution. See /api
responses and the UI disclaimer for 13F reporting lag and limitations.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# SEC requires a descriptive User-Agent with contact: https://www.sec.gov/os/webmaster-faq#code-support
SEC_USER_AGENT = (
    "SiannaFinancials/0.1 (https://github.com/adrinz/SiannaFinancials; contact: tech.united85@gmail.com)"
)
SEC_DATA_BASE = "https://data.sec.gov"
SEC_WWW = "https://www.sec.gov"

# Local persistence (snapshots + recent signals)
_DATA_DIR = Path(__file__).resolve().parent / "data"
_STATE_PATH = _DATA_DIR / "copy_trade_state.json"
_STATE_LOCK = threading.Lock()
_FETCH_CACHE: dict[str, tuple[float, Any]] = {}
_FETCH_CACHE_TTL = 6 * 3600  # 6h for 13F fetch / submissions JSON

# Curated "creators" and themed baskets (MVP: one 13F, one static).
COPY_TRADE_CREATORS: list[dict[str, Any]] = [
    {
        "id": "berkshire-13f",
        "name": "Berkshire Hathaway (13F-HR)",
        "type": "13f",
        "description": (
            "Quarterly Form 13F long-equity positions reported by "
            "Berkshire Hathaway Inc. (CIK 0001067983). Values are in thousands of "
            "U.S. dollars as filed. Subject to 13F coverage and filing lag."
        ),
        "cik": 1067983,
    },
    {
        "id": "thematic-mega-quality",
        "name": "Themed: Mega-cap quality (static)",
        "type": "static_basket",
        "description": (
            "A fixed basket of large-cap names from the app’s tracked universe, "
            "for comparing allocation ideas — not a live third-party portfolio."
        ),
        "symbols": ["AAPL", "MSFT", "JPM", "XOM", "AMZN", "GOOGL", "META"],
    },
]


@dataclass
class HoldingRow:
    name: str
    cusip: str
    value_000s: int  # thousands of USD as filed
    shares: Optional[float] = None
    symbol: Optional[str] = None
    value_usd: float = 0.0  # value_000s * 1000
    weight_pct: float = 0.0


@dataclass
class CopyTradeSignal:
    creator_id: str
    kind: str  # NEW, EXIT, INCREASED, DECREASED, REFRESH
    as_of: str
    message: str
    symbol: Optional[str] = None
    cusip: Optional[str] = None
    detail: str = ""


def _http_get(
    url: str,
    *,
    timeout: float = 30.0,
) -> bytes:
    # SEC blocks requests without a descriptive User-Agent; see
    # https://www.sec.gov/os/accessing-edgar-data
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": SEC_USER_AGENT,
            "Accept": "application/json, text/xml, application/xml;q=0.9, text/plain, */*;q=0.8",
            "Accept-Encoding": "identity",
            "Connection": "close",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.read()


def _http_get_text(url: str, timeout: float = 30.0) -> str:
    return _http_get(url, timeout=timeout).decode("utf-8", errors="replace")


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _child_text(parent: ET.Element, *names: str) -> str:
    for ch in list(parent):
        if _local_tag(ch.tag) in names:
            t = (ch.text or "").strip()
            if t:
                return t
    return ""


def _child_float(parent: ET.Element, *names: str) -> Optional[float]:
    t = _child_text(parent, *names)
    if not t:
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def _parse_13f_infotable_xml(data: bytes) -> list[dict[str, Any]]:
    """Parse 13F information table XML; tolerate namespace variations."""
    root = ET.fromstring(data)
    rows: list[dict[str, Any]] = []
    for node in root.iter():
        if _local_tag(node.tag) != "infoTable":
            continue
        name = _child_text(node, "nameOfIssuer")
        cusip = _child_text(node, "cusip")
        val = _child_text(node, "value")
        v_000: int = 0
        if val:
            try:
                v_000 = int(float(val.replace(",", "")))
            except ValueError:
                v_000 = 0
        sh = None
        for ch in list(node):
            if _local_tag(ch.tag) == "shrsOrPrnAmt":
                sh = _child_float(ch, "sshPrnamt")
        rows.append(
            {
                "name": name,
                "cusip": cusip,
                "value_000s": v_000,
                "shares": sh,
            }
        )
    return rows


def _resolve_symbol(issuer: str) -> Optional[str]:
    """Best-effort link to a Yahoo-friendly symbol (tracked universe)."""
    from .constants import TICKER_MAP
    s = (issuer or "").upper().strip()
    if not s:
        return None
    best: tuple[int, str] | None = None
    for sym, meta in TICKER_MAP.items():
        mname = (meta.get("name") or "").upper()
        if len(mname) < 3:
            continue
        if mname in s or (len(mname) >= 4 and s in mname):
            n = len(mname)
            if best is None or n > best[0]:
                best = (n, sym)
    return best[1] if best else None


def _holdings_to_rows(
    raw: list[dict[str, Any]],
) -> list[HoldingRow]:
    total_000s = max(sum(r.get("value_000s", 0) for r in raw), 1)
    out: list[HoldingRow] = []
    for r in raw:
        v_000 = int(r.get("value_000s") or 0)
        name = r.get("name") or "—"
        cus = (r.get("cusip") or "").strip() or "—"
        sym = r.get("symbol")
        if not sym:
            sym = _resolve_symbol(name)
        vr = HoldingRow(
            name=name,
            cusip=cus,
            value_000s=v_000,
            shares=r.get("shares"),
            symbol=sym,
            value_usd=float(v_000) * 1000.0,
            weight_pct=round(100.0 * v_000 / total_000s, 4) if total_000s else 0.0,
        )
        out.append(vr)
    out.sort(key=lambda h: h.value_000s, reverse=True)
    return out


def _cik_10(cik: int) -> str:
    return str(int(cik)).zfill(10)


def _fetch_submissions_json(cik: int) -> dict[str, Any]:
    key = f"sub_{cik}"
    now = time.time()
    with _STATE_LOCK:
        hit = _FETCH_CACHE.get(key)
        if hit and (now - hit[0] < 600):  # 10 min for submissions list
            return deepcopy(hit[1])
    cik10 = _cik_10(cik)
    url = f"{SEC_DATA_BASE}/submissions/CIK{cik10}.json"
    try:
        data = _http_get(url, timeout=25.0)
        payload = json.loads(data.decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Never bubble to FastAPI as 500 — common causes: 403 missing/blocked UA,
        # rate limits, or egress filters.
        hint = ""
        if e.code == 403:
            hint = (
                " (SEC often requires a browser-like User-Agent and may block "
                "datacenter IPs; try again later or from another network.)"
            )
        err = (
            f"SEC HTTP {e.code} on submissions CIK{cik10}{hint}: "
            f"{getattr(e, 'reason', '') or 'forbidden'}"
        )
        return {"_sec_fetch_error": err}
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return {"_sec_fetch_error": f"SEC submissions fetch failed: {e}"}
    with _STATE_LOCK:
        _FETCH_CACHE[key] = (now, deepcopy(payload))
    return payload


def _find_latest_13f_accession(sub: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return (accession, filing_date, primary_doc) for latest 13F-HR."""
    recent = (sub.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    prims = recent.get("primaryDocument") or []
    for i, form in enumerate(forms):
        fu = (str(form).upper() if form else "")
        if fu.startswith("13F-HR") and i < len(accs):
            acc = accs[i]
            fdate = dates[i] if i < len(dates) else ""
            prim = prims[i] if i < len(prims) else ""
            return str(acc), str(fdate), str(prim)
    return None


def _filing_index_url(cik: int, accession: str) -> str:
    nodash = accession.replace("-", "")
    return f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{nodash}/index.json"


def _filing_dir_url(cik: int, accession: str) -> str:
    nodash = accession.replace("-", "")
    return f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{nodash}/"


def _discover_infotable_href(
    cik: int, accession: str, primary_doc: str
) -> str | None:
    idx_url = _filing_index_url(cik, accession)
    try:
        jtxt = _http_get_text(idx_url, timeout=20.0)
        idx = json.loads(jtxt)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        idx = None
    if isinstance(idx, dict):
        items = (idx.get("directory") or {}).get("item") or []
        if not isinstance(items, list):
            items = [items] if items else []
        for it in items:
            if not isinstance(it, dict):
                continue
            n = (it.get("name") or "").lower()
            if "infotable" in n and n.endswith((".xml", ".htm", ".html")):
                return _filing_dir_url(cik, accession) + (it.get("name") or "")
    # Heuristic filenames often used
    base = _filing_dir_url(cik, accession)
    for guess in (
        "Form13FInfoTable.xml",
        "filing.txt",
    ):
        u = base + guess
        try:
            _http_get(u, timeout=8.0)
            return u
        except Exception:
            continue
    if primary_doc and primary_doc.lower().endswith(".xml"):
        return base + primary_doc
    # Filing index page lists infotable file names; HTML is common.
    for hname in ("index.htm", "index.html"):
        try:
            html = _http_get_text(base + hname, timeout=15.0)
        except Exception:
            continue
        m = re.search(
            r'href="([^"]*infotable[^"]*?\.xml)"',
            html,
            re.IGNORECASE,
        ) or re.search(
            r'href="([^"]*Form13F[^"]*?\.xml)"',
            html,
            re.IGNORECASE,
        )
        if m:
            return base + m.group(1).lstrip("/")
    return None


def fetch_13f_holdings(
    cik: int,
) -> tuple[list[dict[str, Any]], str, str, str, str]:
    """
    Return (raw_rows, source, as_of, accession, note).
    as_of = filing date when known.
    """
    try:
        return _fetch_13f_holdings_impl(cik)
    except urllib.error.HTTPError as e:
        return (
            [],
            "unavailable",
            "",
            "",
            f"SEC HTTP {e.code} ({getattr(e, 'reason', '') or 'error'}).",
        )
    except urllib.error.URLError as e:
        return [], "unavailable", "", "", f"SEC request failed: {e.reason!s}"


def _fetch_13f_holdings_impl(
    cik: int,
) -> tuple[list[dict[str, Any]], str, str, str, str]:
    sub = _fetch_submissions_json(cik)
    err = sub.get("_sec_fetch_error")
    if err:
        return [], "unavailable", "", "", err
    found = _find_latest_13f_accession(sub)
    if not found:
        return [], "unavailable", "", "", "No 13F-HR filing found in recent submissions."
    accession, filing_date, primary = found
    href = _discover_infotable_href(cik, accession, primary)
    if not href:
        return [], "unavailable", filing_date, accession, "Could not find 13F information table file."
    try:
        raw_bytes = _http_get(href, timeout=45.0)
    except Exception as e:  # noqa: BLE001
        return (
            [],
            "unavailable",
            filing_date,
            accession,
            f"Failed to download filing table: {e}",
        )
    if b"informationTable" not in raw_bytes and b"infoTable" not in raw_bytes:
        return (
            [],
            "unavailable",
            filing_date,
            accession,
            "Downloaded file did not look like a 13F information table.",
        )
    try:
        parsed = _parse_13f_infotable_xml(raw_bytes)
    except Exception as e:  # noqa: BLE001
        return (
            [],
            "unavailable",
            filing_date,
            accession,
            f"XML parse error: {e}",
        )
    if not parsed:
        return (
            [],
            "unavailable",
            filing_date,
            accession,
            "Parsed zero holdings (unexpected format).",
        )
    return parsed, "sec-13f", filing_date, accession, ""


def static_basket_holdings(symbols: list[str]) -> tuple[list[dict[str, Any]], str, str, str, str]:
    from .constants import TICKER_MAP
    from .data import get_ohlcv

    raw: list[dict[str, Any]] = []
    for sym in symbols:
        s = (sym or "").upper().strip()
        meta = TICKER_MAP.get(s)
        if not meta:
            continue
        last = 0.0
        v_000 = 0
        try:
            o = get_ohlcv(s, "daily")
            if o and o.closes:
                last = float(o.closes[-1])
            v_000 = int(last * 100)  # synthetic scale for weights
        except Exception:
            pass
        raw.append(
            {
                "name": meta.get("name", s),
                "cusip": "—",
                "value_000s": max(v_000, 1),
                "shares": None,
                "symbol": s,
            }
        )
    as_of = time.strftime("%Y-%m-%d", time.gmtime())
    if not raw:
        return [], "unavailable", as_of, "", "No symbols in basket resolved to TICKER_MAP."
    return raw, "static_basket", as_of, "", ""


def _load_state() -> dict[str, Any]:
    with _STATE_LOCK:
        if not _STATE_PATH.exists():
            return {"snapshots": {}, "signals": []}
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"snapshots": {}, "signals": []}


def _save_state(st: dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=0), encoding="utf-8")
    tmp.replace(_STATE_PATH)


def _snap_key(h: HoldingRow) -> str:
    if h.symbol:
        return h.symbol
    return f"CUSIP:{h.cusip}"


def _holdings_to_snapshot_dict(rows: list[HoldingRow], meta: dict[str, str]) -> dict[str, Any]:
    return {
        "ts": time.time(),
        "meta": meta,
        "items": [asdict(x) for x in rows],
    }


def _diff_to_signals(
    creator_id: str,
    old_snap: dict[str, Any] | None,
    new_rows: list[HoldingRow],
    as_of: str,
) -> list[CopyTradeSignal]:
    if not new_rows:
        return []
    if not old_snap or not old_snap.get("items"):
        return [
            CopyTradeSignal(
                creator_id=creator_id,
                kind="REFRESH",
                as_of=as_of,
                message="Initial snapshot stored — future refreshes will compare for changes.",
                detail="",
            )
        ]
    old_items: dict[str, Any] = {}
    for it in old_snap.get("items") or []:
        key = it.get("symbol") or f"CUSIP:{it.get('cusip')}"
        old_items[key] = it
    new_map = {_snap_key(r): r for r in new_rows}
    old_map = {k: v for k, v in old_items.items()}

    sigs: list[CopyTradeSignal] = []
    for k, row in new_map.items():
        o = old_map.get(k)
        if o is None:
            sigs.append(
                CopyTradeSignal(
                    creator_id=creator_id,
                    kind="NEW",
                    as_of=as_of,
                    symbol=row.symbol,
                    cusip=row.cusip,
                    message=f"New position (vs prior snapshot): {row.name}",
                    detail=f"Weight ~{row.weight_pct:.2f}% of table.",
                )
            )
        else:
            wp, ow = float(o.get("weight_pct") or 0), float(row.weight_pct)
            if abs(wp - ow) > 0.15:
                if ow > wp + 0.1:
                    sigs.append(
                        CopyTradeSignal(
                            creator_id=creator_id,
                            kind="INCREASED",
                            as_of=as_of,
                            symbol=row.symbol,
                            cusip=row.cusip,
                            message=f"Higher weight: {row.name}",
                            detail=f"~{ow:.2f}% vs prior ~{wp:.2f}%.",
                        )
                    )
                elif ow < wp - 0.1:
                    sigs.append(
                        CopyTradeSignal(
                            creator_id=creator_id,
                            kind="DECREASED",
                            as_of=as_of,
                            symbol=row.symbol,
                            cusip=row.cusip,
                            message=f"Lower weight: {row.name}",
                            detail=f"~{ow:.2f}% vs prior ~{wp:.2f}%.",
                        )
                    )
    for k, o in old_map.items():
        if k not in new_map and k and not k.startswith("CUSIP:—"):
            sigs.append(
                CopyTradeSignal(
                    creator_id=creator_id,
                    kind="EXIT",
                    as_of=as_of,
                    symbol=o.get("symbol"),
                    cusip=o.get("cusip"),
                    message=f"Position no longer in table (vs prior): {o.get('name', k)}",
                    detail="",
                )
            )
    if not sigs:
        sigs.append(
            CopyTradeSignal(
                creator_id=creator_id,
                kind="REFRESH",
                as_of=as_of,
                message="Snapshot updated — no material weight change vs prior file.",
            )
        )
    return sigs


def get_creator_by_id(creator_id: str) -> dict[str, Any] | None:
    for c in COPY_TRADE_CREATORS:
        if c.get("id") == creator_id:
            return c
    return None


def get_holdings_for_creator(creator_id: str) -> tuple[
    list[HoldingRow],
    str,
    str,
    str,
    str,
    str,
]:
    """rows, source, as_of, accession, message, filing_or_note."""
    c = get_creator_by_id(creator_id)
    if not c:
        return [], "unavailable", "", "", "Unknown creator", ""
    t = c.get("type")
    if t == "13f":
        cik = int(c.get("cik") or 0)
        raw, src, fdate, acc, err = fetch_13f_holdings(cik)
        if err:
            return [], "unavailable", fdate, acc, err, fdate
        rows = _holdings_to_rows(raw)
        as_of = fdate or time.strftime("%Y-%m-%d", time.gmtime())
        msg = f"SEC 13F-HR {acc}" if acc else ""
        return rows, src, as_of, acc, msg, fdate
    if t == "static_basket":
        syms = list(c.get("symbols") or [])
        raw, src, fdate, acc, err = static_basket_holdings(syms)
        if err:
            return [], "unavailable", fdate, acc, err, fdate
        rows = _holdings_to_rows(raw)
        return rows, src, fdate, acc, "Synthetic basket weights from last prices", fdate
    return [], "unavailable", "", "", "Unsupported creator type", ""


def refresh_creator_and_signals(creator_id: str) -> tuple[
    list[HoldingRow], list[CopyTradeSignal], str, str, str, str, str
]:
    """Recompute holdings, update snapshot, append signals, persist."""
    rows, src, as_of, acc, err, fdate = get_holdings_for_creator(creator_id)
    c = get_creator_by_id(creator_id)
    label = c.get("name", creator_id) if c else creator_id
    if (src == "unavailable" or not rows) and not rows:
        return rows, [], src, as_of, acc, err or "No holdings available.", fdate
    st = _load_state()
    snaps: dict[str, Any] = st.setdefault("snapshots", {})
    old = snaps.get(creator_id)
    meta = {
        "accession": acc,
        "filing": fdate,
        "label": label,
    }
    as_iso = as_of or time.strftime("%Y-%m-%d", time.gmtime())
    meta["as_of"] = as_iso
    new_snap = _holdings_to_snapshot_dict(rows, meta)
    sigs = _diff_to_signals(creator_id, old, rows, as_iso)
    for s in sigs:
        s.detail = s.detail or ""
    snaps[creator_id] = new_snap
    ring = st.setdefault("signals", [])
    for s in sigs:
        item = {
            "creator_id": s.creator_id,
            "kind": s.kind,
            "as_of": s.as_of,
            "message": s.message,
            "symbol": s.symbol,
            "cusip": s.cusip,
            "detail": s.detail,
            "ts": time.time(),
        }
        ring.insert(0, item)
    del ring[200:]
    _save_state(st)
    return rows, sigs, src, as_of, acc, err or f"{label} · {len(rows)} line(s)", fdate


def get_stored_snapshot(
    creator_id: str,
) -> tuple[list[HoldingRow], dict[str, Any]] | None:
    """Return last saved holdings + meta, or None."""
    st = _load_state()
    s = (st.get("snapshots") or {}).get(creator_id)
    if not s or not s.get("items"):
        return None
    try:
        rows = [HoldingRow(**it) for it in s["items"]]
    except (TypeError, KeyError, ValueError):
        return None
    return rows, s.get("meta") or {}


def get_signals(creator_id: str | None, limit: int) -> list[dict[str, Any]]:
    st = _load_state()
    ring: list[dict[str, Any]] = list(st.get("signals") or [])
    if creator_id:
        ring = [x for x in ring if x.get("creator_id") == creator_id]
    return ring[: max(0, min(limit, 200))]


def list_creators() -> list[dict[str, Any]]:
    return [dict(x) for x in COPY_TRADE_CREATORS]


def reset_copy_trade_state() -> None:
    """Test helper."""
    with _STATE_LOCK:
        _FETCH_CACHE.clear()
        if _STATE_PATH.exists():
            _STATE_PATH.unlink()
