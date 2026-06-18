"""Balance computation and settle-up suggestions. All amounts in integer paise.

NOTE: spliti/static/index.html mirrors this logic in JS (splitEqually /
recomputeDetail / suggestSettlements) so balances recompute offline. Any change to
the rounding or settle-up algorithm here must be reflected there to avoid drift."""


def split_equally(amount_paise: int, member_ids: list[int]) -> dict[int, int]:
    """Split an amount as evenly as possible, distributing leftover paise to the
    first members so the shares always sum back to the exact total."""
    if not member_ids:
        raise ValueError("need at least one member to split among")
    n = len(member_ids)
    base, remainder = divmod(amount_paise, n)
    return {
        mid: base + (1 if i < remainder else 0)
        for i, mid in enumerate(member_ids)
    }


def net_balances(
    member_ids: list[int],
    expenses: list[dict],
    shares: list[dict],
    settlements: list[dict],
) -> dict[int, int]:
    """Net paise per member. Positive => owed money (paid more than their share)."""
    bal = {mid: 0 for mid in member_ids}
    for e in expenses:
        if e["paid_by"] in bal:
            bal[e["paid_by"]] += e["amount_paise"]
    for s in shares:
        if s["member_id"] in bal:
            bal[s["member_id"]] -= s["share_paise"]
    for st in settlements:
        # paying down a debt: the payer's net goes up, the receiver's goes down
        if st["from_member"] in bal:
            bal[st["from_member"]] += st["amount_paise"]
        if st["to_member"] in bal:
            bal[st["to_member"]] -= st["amount_paise"]
    return bal


def suggest_settlements(balances: dict[int, int]) -> list[dict]:
    """Greedy min-cash-flow: match biggest debtor to biggest creditor until flat."""
    creditors = sorted(
        ([m, b] for m, b in balances.items() if b > 0), key=lambda x: -x[1]
    )
    debtors = sorted(
        ([m, -b] for m, b in balances.items() if b < 0), key=lambda x: -x[1]
    )
    transfers: list[dict] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        debtor, owe = debtors[i]
        creditor, due = creditors[j]
        pay = min(owe, due)
        transfers.append({"from": debtor, "to": creditor, "amount_paise": pay})
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return transfers
