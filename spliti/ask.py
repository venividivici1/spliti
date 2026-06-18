"""Mistral-powered Q&A over a group's expenses. Reuses the shared Mistral client."""

from datetime import datetime, timedelta, timezone

import httpx

from spliti.ai import MODEL, client

IST = timezone(timedelta(hours=5, minutes=30))

# Tiny in-process cache for reverse-geocode lookups, keyed by coords rounded to
# ~100 m. Keeps us well within Nominatim's fair-use policy across repeated calls.
_GEO_CACHE: dict[tuple[float, float], str] = {}


async def reverse_geocode(lat: float, lon: float) -> str | None:
    """Best-effort locality name for a coordinate via OpenStreetMap Nominatim.

    Returns a short label like "Kaza" or "Kaza, Himachal Pradesh", or None on any
    failure — callers must treat the place as optional.
    """
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    key = (round(lat, 3), round(lon, 3))
    if key in _GEO_CACHE:
        return _GEO_CACHE[key] or None
    try:
        async with httpx.AsyncClient(timeout=4.0) as http:
            r = await http.get(
                "https://nominatim.openstreetmap.org/reverse",
                # zoom 16 surfaces neighbourhood/suburb-level detail, not just the city.
                params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 16},
                headers={"User-Agent": "Spliti/1.0 (expense-splitter)"},
            )
            r.raise_for_status()
            addr = (r.json() or {}).get("address", {})
    except (httpx.HTTPError, ValueError):
        return None
    # Prefer the most local name (neighbourhood/suburb), then pair it with the
    # broader town/city for context → e.g. "Carmelaram, Bengaluru".
    local = next(
        (addr[k] for k in (
            "neighbourhood", "suburb", "quarter", "residential", "city_district",
            "hamlet", "village", "town",
        ) if addr.get(k)),
        None,
    )
    broad = next(
        (addr[k] for k in ("city", "town", "municipality", "village", "county", "state")
         if addr.get(k) and addr[k] != local),
        None,
    )
    label = ", ".join(p for p in (local, broad) if p) or (addr.get("state") or "")
    _GEO_CACHE[key] = label
    return label or None


def _ist(s: str) -> str:
    """Convert a stored UTC 'YYYY-MM-DD HH:MM:SS' timestamp to a readable IST string."""
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    except (ValueError, TypeError):
        return s

SYSTEM = (
    "You are a friendly assistant inside a Splitwise-style expense app. This group is a "
    "friends' road trip to Spiti (a Himalayan region in India). Answer the "
    "user's questions about THIS group's expenses, members, balances, and who owes "
    "whom, using only the data provided below. All amounts are in Indian rupees. ALWAYS "
    "format money with the ₹ symbol (e.g. ₹250.00) and NEVER use the $ sign or the word "
    "'dollars'. Do the arithmetic carefully and show the numbers. Be concise and use "
    "Markdown when it helps (short lists, bold names). If the data doesn't contain the "
    "answer, say so."
)


def _d(paise: int) -> str:
    return f"₹{paise / 100:.2f}"


def build_context(detail: dict) -> str:
    """Render a group's detail dict into a compact text context for the model."""
    g = detail["group"]
    lines = [f"Group: {g['name']}"]

    lines.append("\nMembers:")
    for m in detail["members"]:
        lines.append(f"  - {m['name']} (id {m['id']})")

    lines.append("\nExpenses:")
    active = [e for e in detail["expenses"] if not e.get("deleted")]
    if not active:
        lines.append("  (none)")
    for e in active:
        lines.append(
            f"  - {e['description']}: {_d(e['amount_paise'])} paid by "
            f"{e['paid_by_name']} on {_ist(e['created_at'])}"
        )

    lines.append("\nRecorded payments (settlements):")
    if not detail["settlements"]:
        lines.append("  (none)")
    for s in detail["settlements"]:
        lines.append(f"  - {s['from_name']} paid {s['to_name']} {_d(s['amount_paise'])}")

    lines.append("\nCurrent balances (positive = is owed money, negative = owes):")
    for b in detail["balances"]:
        lines.append(f"  - {b['name']}: {_d(b['net_paise'])}")

    lines.append("\nSuggested way to settle up:")
    if not detail["suggestions"]:
        lines.append("  (everyone is settled)")
    for s in detail["suggestions"]:
        lines.append(f"  - {s['from_name']} pays {s['to_name']} {_d(s['amount_paise'])}")

    return "\n".join(lines)


async def suggest_description(
    now: datetime | None = None, place: str | None = None
) -> str:
    """Ask Mistral for a short, time-of-day-appropriate expense description (IST).

    The model returns only the activity (e.g. "Late Night Maggi"); when `place`
    (a reverse-geocoded locality) is given we append " @ <place>", yielding
    "Late Night Maggi @ Bangalore".
    """
    now = now or datetime.now(IST)
    prompt = (
        "This is a shared-expense group for a friends' road trip to Spiti (a Himalayan "
        "region in India). "
        f"It is {now.strftime('%I:%M %p')} on a {now.strftime('%A')}. "
        "Suggest ONE short, common road-trip expense description that fits this time of day, "
        "in Title Case (2-4 words) such as Morning Chai, Breakfast, Fuel Stop, Dhaba Lunch, "
        "Maggi & Chai, Toll, Snacks, Dinner, Hotel, or Late Night Maggi. Do NOT include any "
        "place or location name. Reply with ONLY the description — no quotes, no punctuation, "
        "no explanation."
    )
    resp = await client().chat.complete_async(
        model=MODEL, max_tokens=12, messages=[{"role": "user", "content": prompt}]
    )
    content = resp.choices[0].message.content if resp.choices else ""
    if isinstance(content, list):
        content = "".join(getattr(c, "text", "") for c in content)
    desc = (content or "").strip().strip('."\'').splitlines()[0][:50]
    # Always render location-tagged when we know where they are: "<activity> @ <place>".
    short = (place or "").split(",")[0].strip()
    if desc and short:
        desc = f"{desc} @ {short}"
    return desc[:80]


# Cache classifier outcomes by lowercased description so identical descriptions
# never cost a second model call. Stores "" for a miss (so we don't re-ask for
# text the model couldn't place). Bounded to keep memory flat.
_CAT_CACHE: dict[str, str] = {}
_CAT_CACHE_MAX = 1024


async def suggest_category(description: str, catalog: list[dict]) -> str | None:
    """Map a free-text expense description to ONE id from the exhaustive `catalog`.

    `catalog` is a list of {"id", "label"} dicts (the closed category set). Returns
    a valid id from it, or None when the description is empty, the model is unsure,
    or its reply doesn't match a known id. Callers should fall back to keyword
    matching / "other" on None. Results (hits and misses) are cached by description.
    """
    desc = (description or "").strip()
    if not desc or not catalog:
        return None
    key = desc.lower()
    if key in _CAT_CACHE:
        return _CAT_CACHE[key] or None
    options = "\n".join(f"  {c['id']}: {c['label']}" for c in catalog)
    prompt = (
        "Classify a friends' road-trip expense into exactly ONE category. Below is the "
        "COMPLETE, exhaustive list of allowed categories as `id: label`:\n"
        f"{options}\n\n"
        f'Expense description: "{desc}"\n\n'
        "Reply with ONLY the matching category id (the token before the colon) and nothing "
        "else — no label, quotes, punctuation, or explanation. If none fits, reply: other"
    )
    resp = await client().chat.complete_async(
        model=MODEL, max_tokens=8, messages=[{"role": "user", "content": prompt}]
    )
    content = resp.choices[0].message.content if resp.choices else ""
    if isinstance(content, list):
        content = "".join(getattr(c, "text", "") for c in content)
    # First token, lowercased; tolerate a whitespace-only reply (empty split) and
    # a stray trailing colon if the model echoes "id: label" despite the prompt.
    parts = (content or "").strip().strip('."\'').lower().split()
    cat = parts[0].rstrip(":") if parts else ""
    valid = {c["id"] for c in catalog}
    result = cat if cat in valid else None
    if len(_CAT_CACHE) < _CAT_CACHE_MAX:
        _CAT_CACHE[key] = result or ""
    return result


async def answer_stream(context: str, history: list[dict], question: str):
    """Yield answer text chunks for a question about the group."""
    messages = [{"role": "system", "content": f"{SYSTEM}\n\n--- GROUP DATA ---\n{context}"}]
    for turn in history[-8:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    events = await client().chat.stream_async(model=MODEL, max_tokens=1500, messages=messages)
    async for event in events:
        if not event.data.choices:
            continue
        content = event.data.choices[0].delta.content
        if isinstance(content, str) and content:
            yield content
        elif isinstance(content, list):
            for chunk in content:
                text = getattr(chunk, "text", None)
                if text:
                    yield text
