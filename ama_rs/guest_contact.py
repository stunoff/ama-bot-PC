"""Timer identities for unresolved guest-contact attempts."""

from hashlib import sha256


def guest_timer_keys(order: str) -> tuple[str, str]:
    digest = sha256(order.encode()).hexdigest()[:24]
    return (f"guest_first:{digest}", f"guest_second:{digest}")


def pending_guest_orders(card) -> dict[str, str]:
    return {key: order
            for order, outcome in card["data"].get("order_results", {}).items()
            if outcome == "not_reached" for key in guest_timer_keys(order)}
