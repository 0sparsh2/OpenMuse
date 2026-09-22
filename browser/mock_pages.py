"""Deterministic mock page fixtures — Phase 4.

A tiny multi-page fixture site served from memory (mock:// URLs). No real
network is touched, so demos run fully offline and deterministically. The
site exercises everything the exit criteria need:

  news/            research-style page with links and a search field
  news/article-1   article page
  shop/            product listing
  shop/product-1   product page with add-to-cart + a commit control (Buy now)
  shop/cart        cart page -> checkout
  shop/review      review page with a commit control (Place order)
  accounts/login   sign-in form -> challenge page on submit
  accounts/challenge  CAPTCHA challenge page (hands to the user)
  accounts/home    signed-in landing (reached after the user resolves it)

Page text is untrusted data: it can never authorize tools or carry secrets.
"""
from __future__ import annotations

PAGES: dict[str, dict] = {
    "mock://news/": {
        "title": "Mock News — front page",
        "a11y": (
            "heading 'Top stories'\n"
            "link 'City council approves transit plan' [article-1]\n"
            "link 'New park opens downtown' [article-2]\n"
            "searchbox 'Search news'\n"
        ),
        "elements": [
            {"role": "link", "name": "City council approves transit plan",
             "target": "mock://news/article-1"},
            {"role": "link", "name": "New park opens downtown",
             "target": "mock://news/article-2"},
            {"role": "textbox", "name": "Search news", "form_field": "q"},
        ],
        "forms": [
            {"form_id": "f_search",
             "fields": [{"field_id": "q", "label": "Search news", "type": "search"}]},
        ],
    },
    "mock://news/article-1": {
        "title": "City council approves transit plan",
        "a11y": (
            "heading 'City council approves transit plan'\n"
            "paragraph 'The council voted 7-2 to fund the crosstown line...'\n"
            "link 'Back to front page'\n"
        ),
        "elements": [
            {"role": "link", "name": "Back to front page", "target": "mock://news/"},
        ],
        "forms": [],
    },
    "mock://news/article-2": {
        "title": "New park opens downtown",
        "a11y": (
            "heading 'New park opens downtown'\n"
            "paragraph 'Ribbon cutting is scheduled for Friday...'\n"
            "link 'Back to front page'\n"
        ),
        "elements": [
            {"role": "link", "name": "Back to front page", "target": "mock://news/"},
        ],
        "forms": [],
    },
    "mock://shop/": {
        "title": "Mock Shop — products",
        "a11y": (
            "heading 'Products'\n"
            "link 'Widget Pro — $129.99'\n"
            "link 'Gadget Lite — $39.99'\n"
            "link 'View cart'\n"
        ),
        "elements": [
            {"role": "link", "name": "Widget Pro — $129.99",
             "target": "mock://shop/product-1"},
            {"role": "link", "name": "Gadget Lite — $39.99",
             "target": "mock://shop/product-2"},
            {"role": "link", "name": "View cart", "target": "mock://shop/cart"},
        ],
        "forms": [],
    },
    "mock://shop/product-1": {
        "title": "Widget Pro — Mock Shop",
        "a11y": (
            "heading 'Widget Pro'\n"
            "text 'Price: $129.99. In stock.'\n"
            "button 'Add to cart'\n"
            "button 'Buy now'\n"
            "link 'Back to products'\n"
        ),
        "elements": [
            {"role": "button", "name": "Add to cart", "cart_add": "Widget Pro"},
            {"role": "button", "name": "Buy now",
             "commit": {"effect": "purchase",
                        "summary": "Buy 1 × Widget Pro",
                        "amount_minor": 12999, "currency": "USD",
                        "destination": "mock-shop"}},
            {"role": "link", "name": "Back to products",
             "target": "mock://shop/"},
        ],
        "forms": [],
    },
    "mock://shop/product-2": {
        "title": "Gadget Lite — Mock Shop",
        "a11y": (
            "heading 'Gadget Lite'\n"
            "text 'Price: $39.99. In stock.'\n"
            "button 'Add to cart'\n"
            "link 'Back to products'\n"
        ),
        "elements": [
            {"role": "button", "name": "Add to cart", "cart_add": "Gadget Lite"},
            {"role": "link", "name": "Back to products",
             "target": "mock://shop/"},
        ],
        "forms": [],
    },
    "mock://shop/cart": {
        "title": "Your cart — Mock Shop",
        "a11y": (
            "heading 'Your cart'\n"
            "text '{cart_summary}'\n"
            "button 'Proceed to checkout'\n"
            "link 'Continue shopping'\n"
        ),
        "elements": [
            {"role": "button", "name": "Proceed to checkout",
             "target": "mock://shop/review"},
            {"role": "link", "name": "Continue shopping",
             "target": "mock://shop/"},
        ],
        "forms": [],
    },
    "mock://shop/review": {
        "title": "Review order — Mock Shop",
        "a11y": (
            "heading 'Review your order'\n"
            "text '{cart_summary}'\n"
            "text 'Total: {cart_total}'\n"
            "button 'Place order'\n"
            "link 'Back to cart'\n"
        ),
        "elements": [
            {"role": "button", "name": "Place order",
             "commit": {"effect": "purchase",
                        "summary": "Place order for cart",
                        "amount_minor": 0, "currency": "USD",
                        "destination": "mock-shop",
                        "cart_priced": True}},
            {"role": "link", "name": "Back to cart",
             "target": "mock://shop/cart"},
        ],
        "forms": [],
    },
    "mock://accounts/login": {
        "title": "Sign in — Mock Accounts",
        "a11y": (
            "heading 'Sign in'\n"
            "textbox 'Email'\n"
            "textbox 'Password'\n"
            "button 'Sign in'\n"
        ),
        "elements": [
            {"role": "textbox", "name": "Email", "form_field": "email"},
            {"role": "textbox", "name": "Password", "form_field": "password"},
            {"role": "button", "name": "Sign in",
             "target": "mock://accounts/challenge"},
        ],
        "forms": [
            {"form_id": "f_login",
             "fields": [
                 {"field_id": "email", "label": "Email", "type": "email"},
                 {"field_id": "password", "label": "Password", "type": "password"},
             ]},
        ],
    },
    "mock://accounts/challenge": {
        "title": "Verify — Mock Accounts",
        "a11y": (
            "heading 'Verify you are human'\n"
            "text 'Please complete the CAPTCHA below to continue.'\n"
            "checkbox 'I am not a robot [CAPTCHA]'\n"
            "text 'Having trouble? Complete this step in your own browser, "
            "then tell the assistant you are done.'\n"
        ),
        "elements": [
            {"role": "checkbox", "name": "I am not a robot [CAPTCHA]",
             "captcha": True},
        ],
        "forms": [],
        "challenges": ["captcha"],
        "challenge_marker": "verify you are human",
        # Where the session continues after the USER resolves the challenge.
        "challenge_resolves_to": "mock://accounts/home",
    },
    "mock://accounts/home": {
        "title": "Welcome — Mock Accounts",
        "a11y": (
            "heading 'Welcome back'\n"
            "text 'You are signed in. This is a mock session.'\n"
            "link 'Sign out'\n"
        ),
        "elements": [
            {"role": "link", "name": "Sign out",
             "target": "mock://accounts/login"},
        ],
        "forms": [],
    },
}


def get_page(url: str) -> dict | None:
    return PAGES.get(url)


def detect_challenges(page: dict) -> list[dict]:
    """Conservative challenge detection over page markers."""
    found: list[dict] = []
    blob = " ".join([
        page.get("title", ""), page.get("a11y", ""),
        page.get("challenge_marker", ""),
    ]).lower()
    for kind, markers in {
        "captcha": ("captcha", "recaptcha", "hcaptcha", "verify you are human",
                    "i am not a robot"),
        "cloudflare": ("cloudflare", "checking your browser",
                       "just a moment"),
        "login_wall": ("sign in to continue", "log in to continue"),
        "two_factor": ("two-factor", "verification code"),
    }.items():
        for marker in markers:
            if marker in blob:
                found.append({"kind": kind, "detected_via": marker})
                break
    for explicit in page.get("challenges", []):
        if not any(f["kind"] == explicit for f in found):
            found.append({"kind": explicit, "detected_via": "page.challenges"})
    return found
