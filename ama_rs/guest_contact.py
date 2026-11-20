"""Timer identities for unresolved guest-contact attempts."""

from hashlib import sha256


def guest_timer_keys(order: str) -> tuple[str, str]:
    digest = sha256(order.encode()).hexdigest()[:24]
    return (f"guest_first:{digest}", f"guest_second:{digest}")


def pending_guest_orders(card) -> dict[str, str]:
    return {key: order
            for order, outcome in card["data"].get("order_results", {}).items()
            if outcome == "not_reached" for key in guest_timer_keys(order)}


def first_call_keys(order: str) -> tuple[str, str, str]:
    digest = sha256(order.encode()).hexdigest()[:24]
    return (f"first_call_1:{digest}", f"first_call_2:{digest}", f"first_call_3:{digest}")


def pending_first_calls(card) -> dict[str, str]:
    data = card["data"]
    return {
        key: order for order in data.get("first_call_ready", {})
        if data.get("order_results", {}).get(order) is None
        for key in (*first_call_keys(order), first_call_followup(order))
    }


def first_call_followup(order: str) -> str:
    return first_call_keys(order)[2].replace("first_call_3:", "first_call_4:")
