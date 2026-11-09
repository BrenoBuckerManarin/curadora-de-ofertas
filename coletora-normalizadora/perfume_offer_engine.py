#!/usr/bin/env python3
"""Pure evidence/identity/reconciliation engine for perfume promotions.

Collectors may create Evidence. Only reconcile_offer may create ResolvedOffer.
No network, HTML parsing, rendering, caches or mutable global state belong here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
import re
from typing import Any, Iterable


class IdentityStatus(str, Enum):
    EXACT_ITEM = "exact_item"
    EXACT_CARD = "exact_card"
    PRODUCT_ONLY = "product_only"
    AMBIGUOUS = "ambiguous"
    CONFLICT = "conflict"


class Relation(str, Enum):
    SAME_OFFER = "same_offer"
    DIFFERENT_OFFER = "different_offer"
    SAME_PRODUCT_ONLY = "same_product_only"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceOffer:
    product: str
    link: str
    price: Decimal | None = None
    original_price: Decimal | None = None
    says_pix: bool = False
    installments: str | None = None
    coupons: tuple[str, ...] = ()
    claimed_store: str | None = None


@dataclass(frozen=True)
class Evidence:
    field: str
    value: Any
    source: str
    item_id: str | None = None
    product_id: str | None = None
    seller_id: int | None = None
    card_role: str = "unknown"
    price_kind: str | None = None
    confidence: str = "context"
    price_base: Decimal | None = None
    no_interest: bool | None = None


@dataclass(frozen=True)
class ResolvedIdentity:
    product_name: str
    item_id: str | None
    product_id: str | None
    seller_id: int | None
    status: IdentityStatus
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldDecision:
    field: str
    value: Any
    status: str
    evidence_sources: tuple[str, ...] = ()
    rejected_sources: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class ResolvedOffer:
    identity: ResolvedIdentity
    price: Decimal | None
    original_price: Decimal | None
    price_kind: str
    installments: str | None
    official_store: str | None
    coupons: tuple[str, ...]
    decisions: dict[str, FieldDecision] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["identity"]["status"] = self.identity.status.value
        for key in ("price", "original_price"):
            if data[key] is not None:
                data[key] = float(data[key])
        for decision in data["decisions"].values():
            if isinstance(decision.get("value"), Decimal):
                decision["value"] = float(decision["value"])
        return data


def money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        return None


def collect_source_prices(body: str) -> tuple[Decimal | None, Decimal | None]:
    """Parse explicit commercial lines only; never headline/catalog numbers."""
    plain = re.sub(r"[*_`~]", "", body or "")
    amount = r"(\d{1,5}(?:[.,]\d{1,2})?)"
    pair = re.search(r"(?is)\bde\s*R\$\s*" + amount + r".{0,45}?\bpor\s*R\$\s*" + amount, plain)
    if pair:
        return (money(pair.group(1).replace(".", "").replace(",", ".")),
                money(pair.group(2).replace(".", "").replace(",", ".")))
    original_match = re.search(r"(?im)^\s*(?:✅\s*)?de\s*R\$\s*" + amount, plain)
    final_match = re.search(r"(?im)^\s*(?:✅\s*)?por\s*R\$\s*" + amount, plain)
    if not final_match:
        final_match = re.search(r"(?im)^\s*✅\s*R\$\s*" + amount + r"\s*$", plain)
    original = money(original_match.group(1).replace(".", "").replace(",", ".")) if original_match else None
    final = money(final_match.group(1).replace(".", "").replace(",", ".")) if final_match else None
    return original, final


def relation(identity: ResolvedIdentity, ev: Evidence) -> Relation:
    # Once product identity conflicts, no commercial field from that candidate
    # may enrich the received promotion, even if item_id is structurally exact.
    if identity.status == IdentityStatus.CONFLICT:
        return Relation.UNKNOWN
    if identity.item_id and ev.item_id:
        return Relation.SAME_OFFER if identity.item_id == ev.item_id else Relation.DIFFERENT_OFFER
    if identity.product_id and ev.product_id:
        return Relation.SAME_PRODUCT_ONLY if identity.product_id == ev.product_id else Relation.DIFFERENT_OFFER
    return Relation.UNKNOWN


_VARIANT_TOKENS = {
    "asad", "bourbon", "elixir", "eclaire", "fakhar", "gold", "platin",
    "malbec", "egeo", "icon", "sabah", "ward", "hawas", "black", "verde",
    "beau", "male", "profondo", "profumo", "scandal", "philos", "pura",
}


def _tokens(text: str) -> set[str]:
    return {x for x in re.sub(r"[^a-z0-9]+", " ", text.lower()).split() if len(x) > 2}


def products_conflict(received: str, exact_title: str) -> bool:
    """Conservative family/flanker conflict gate for the exact linked card."""
    a, b = _tokens(received), _tokens(exact_title)
    av, bv = a & _VARIANT_TOKENS, b & _VARIANT_TOKENS
    if av and bv and not (av & bv):
        return True
    generic = {"perfume", "arabe", "original", "masculino", "feminino", "unissex", "edp", "edt", "100ml", "kit", "presente"}
    meaningful_a, meaningful_b = a - generic, b - generic
    return bool(len(meaningful_a) >= 2 and len(meaningful_b) >= 2 and not (meaningful_a & meaningful_b))


def resolve_identity(source: SourceOffer, evidence: Iterable[Evidence]) -> ResolvedIdentity:
    rows = list(evidence)
    candidates: dict[str, list[Evidence]] = {}
    for ev in rows:
        if ev.item_id and ev.confidence in {"exact", "exact_card"}:
            candidates.setdefault(ev.item_id, []).append(ev)
    if not candidates:
        pids = {ev.product_id for ev in rows if ev.product_id}
        return ResolvedIdentity(source.product, None, next(iter(pids)) if len(pids) == 1 else None,
                                None, IdentityStatus.PRODUCT_ONLY if len(pids) == 1 else IdentityStatus.AMBIGUOUS,
                                ("no exact item evidence",))
    ranked = sorted(candidates.items(), key=lambda kv: (
        any(e.card_role == "featured" for e in kv[1]),
        any(e.confidence == "exact" for e in kv[1]),
        len(kv[1]),
    ), reverse=True)
    item_id, chosen = ranked[0]
    pids = [e.product_id for e in chosen if e.product_id]
    sellers = [e.seller_id for e in chosen if e.seller_id]
    titles = [str(e.value) for e in chosen if e.field == "product_name" and e.value]
    status = IdentityStatus.EXACT_ITEM if any(e.confidence == "exact" for e in chosen) else IdentityStatus.EXACT_CARD
    chosen_title = titles[0] if titles else source.product
    if titles and products_conflict(source.product, chosen_title):
        return ResolvedIdentity(source.product, item_id,
                                pids[0] if pids else None, sellers[0] if sellers else None,
                                IdentityStatus.CONFLICT,
                                (f"received product conflicts with exact linked card: {chosen_title}",))
    return ResolvedIdentity(chosen_title, item_id,
                            pids[0] if pids else None, sellers[0] if sellers else None,
                            status, (f"selected {item_id} from featured/exact evidence",))


def _same(identity: ResolvedIdentity, evidence: Iterable[Evidence], field_name: str) -> tuple[list[Evidence], list[Evidence]]:
    accepted, rejected = [], []
    for ev in evidence:
        if ev.field != field_name:
            continue
        (accepted if relation(identity, ev) == Relation.SAME_OFFER else rejected).append(ev)
    return accepted, rejected


def meaningful_original(original: Decimal | None, price: Decimal | None) -> Decimal | None:
    if original is None or price is None or original <= price:
        return None
    delta = original - price
    if delta < Decimal("30") and original / price < Decimal("1.25"):
        return None
    return original


def reconcile_offer(source: SourceOffer, evidence: Iterable[Evidence]) -> ResolvedOffer:
    rows = list(evidence)
    identity = resolve_identity(source, rows)
    decisions: dict[str, FieldDecision] = {}

    # Source price is editorial authority. Exact evidence confirms/classifies it;
    # a different amount is a conflict, never an automatic replacement.
    price = source.price
    price_ev, price_rejected = _same(identity, rows, "price")
    matching = []
    conflicting = []
    for ev in price_ev:
        ev_price = money(ev.value)
        if ev_price is None or price is None:
            continue
        if abs(ev_price - price) <= Decimal("1.50"):
            matching.append(ev)
        else:
            conflicting.append(ev)
    # The exact linked offer is commercial authority. A received amount is a
    # campaign claim only when a coupon/campaign can explain why it is not
    # publicly reproducible; otherwise the same-item public price corrects it.
    if conflicting and not source.coupons:
        linked_price = money(conflicting[0].value)
        if linked_price is not None:
            price = linked_price
            price_status = "corrected_from_exact_link"
        else:
            price_status = "conflict"
    else:
        price_status = "confirmed" if matching else ("campaign_claim" if conflicting and source.coupons else "unverified_claim")
    decisions["price"] = FieldDecision("price", price, price_status,
        tuple(e.source for e in matching),
        tuple(e.source for e in price_rejected + conflicting),
        "same-item price matches source" if matching else (
            "same-item public price is authoritative; received unsupported amount corrected"
            if price_status == "corrected_from_exact_link" else (
                "same-item public price differs; coupon/campaign claim retained but not marked confirmed"
                if conflicting else "received claim not publicly reproduced"
            )
        ))

    pix_ev, pix_rejected = _same(identity, rows, "pix_price")
    exact_pix = [e for e in pix_ev if money(e.value) is not None]
    says_pix = source.says_pix
    if not source.coupons and exact_pix and identity.status in (IdentityStatus.EXACT_ITEM, IdentityStatus.EXACT_CARD):
        # The linked card's primary current tier is Pix. A received regular/list
        # amount must not survive with a cosmetic "no Pix" suffix (Malbec 258
        # vs 211.56; Vulcan 384.90 vs 307.92).
        exact_pix_amount = money(exact_pix[0].value)
        if exact_pix_amount is not None:
            price = exact_pix_amount
            price_status = "corrected_to_exact_pix"
            says_pix = True
            decisions["price"] = FieldDecision(
                "price", price, price_status, tuple(e.source for e in exact_pix),
                tuple(e.source for e in pix_rejected),
                "exact linked card exposes Pix as the primary current price",
            )
    if not says_pix and source.coupons and exact_pix and price is not None:
        # A coupon price below the exact linked item's public Pix tier inherits
        # that same offer's Pix modality. Official-store status is unrelated:
        # Malbec Magnetic is the exact item but has no official_store_id.
        for ev in exact_pix:
            pix_amount = money(ev.value)
            if pix_amount is not None and price < pix_amount:
                says_pix = True
                break
    price_kind = "pix" if says_pix else "unknown"
    decisions["pix"] = FieldDecision("pix", says_pix, "confirmed" if source.says_pix else ("enriched" if says_pix else "omitted"),
        tuple(e.source for e in exact_pix), tuple(e.source for e in pix_rejected),
        "explicit source Pix" if source.says_pix else ("coupon below exact linked-item Pix tier" if says_pix else "no same-offer Pix proof"))

    orig_ev, orig_rejected = _same(identity, rows, "original_price")
    exact_originals = [money(e.value) for e in orig_ev if money(e.value) is not None]
    exact_original = exact_originals[0] if exact_originals else None
    # The linked item's own previous/list price outranks an arbitrary source
    # "De". Keep the source only when the item exposes no valid previous price.
    original = meaningful_original(exact_original, price) if exact_original is not None else None
    original_status = "confirmed_exact" if original is not None else "omitted"
    if original is None and identity.status == IdentityStatus.AMBIGUOUS:
        original = meaningful_original(source.original_price, price)
        original_status = "source_kept" if original is not None else "omitted"
    elif original is None and identity.status in (IdentityStatus.EXACT_ITEM, IdentityStatus.EXACT_CARD):
        # Coupon campaigns often carry a meaningful source "De" even when the
        # item/catalog API omits previous_price. Preserve that campaign anchor;
        # without a coupon, absence on the exact item remains evidence to omit.
        if source.coupons:
            original = meaningful_original(source.original_price, price)
            original_status = "source_campaign_kept" if original is not None else "omitted_exact_no_previous"
        else:
            original_status = "omitted_exact_no_previous"
    decisions["original_price"] = FieldDecision("original_price", original, original_status,
        tuple(e.source for e in orig_ev), tuple(e.source for e in orig_rejected),
        "exact linked-item previous price" if original_status == "confirmed_exact" else (
            "exact linked item exposes no previous price; source De omitted"
            if original_status == "omitted_exact_no_previous" else "meaningful discount gate"
        ))

    inst_ev, inst_rejected = _same(identity, rows, "installments")
    valid_inst = [e for e in inst_ev if e.no_interest is True and e.price_base is not None and price is not None and (
        abs(e.price_base - price) <= Decimal("1.50")
        # Coupon campaigns rotate quickly; Breno validates the final price. Keep
        # the exact same item's interest-free installment count when a coupon
        # lowers the published total, without claiming a per-installment value.
        or (bool(source.coupons) and price < e.price_base)
    )]
    installments = source.installments or (str(valid_inst[0].value) if valid_inst else None)
    decisions["installments"] = FieldDecision("installments", installments, "source_kept" if source.installments else ("enriched" if valid_inst else "omitted"),
        tuple(e.source for e in valid_inst), tuple(e.source for e in inst_rejected),
        "same item and no_interest=true; lower coupon total may retain installment count")

    store_ev, store_rejected = _same(identity, rows, "official_store")
    if source.claimed_store:
        claimed_tokens = _tokens(source.claimed_store)
        compatible_store = []
        for ev in store_ev:
            store_tokens = _tokens(str(ev.value))
            if claimed_tokens and store_tokens and claimed_tokens & store_tokens:
                compatible_store.append(ev)
            else:
                store_rejected.append(ev)
        store_ev = compatible_store
    official_store = str(store_ev[0].value) if store_ev else None
    decisions["official_store"] = FieldDecision("official_store", official_store, "confirmed" if official_store else "omitted",
        tuple(e.source for e in store_ev), tuple(e.source for e in store_rejected),
        "same-offer official store matches selected seller" if official_store and source.claimed_store else (
            "exact official store conflicts with seller explicitly selected in source"
            if source.claimed_store and store_rejected else (
                "selected seller is a source claim; link has no stable official-store proof"
                if source.claimed_store else "same-offer official-store evidence required"
            )
        ))

    return ResolvedOffer(identity, price, original, price_kind, installments, official_store, source.coupons, decisions)
