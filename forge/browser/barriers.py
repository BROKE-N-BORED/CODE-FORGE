"""Seamless site handling — get past the furniture, hand over the gates.

What the engine clears on its own:

* Cookie / consent banners — the dismiss button is found by its
  label and clicked (reject-all preferred when offered, accept
  otherwise). This is the same click a person makes.
* Lazy pages — navigation already waits for the load event, and a
  scroll pass can be run so below-fold content loads before reading.

What it does NOT clear on its own: checks built to prove a human is
present (CAPTCHA / "verify you are human"), and logins it has no
vault entry for. Those are detected, named in plain words, and
handed to the person — the session runs on a PERSISTENT profile, so
once the person clears the gate or logs in once (headed handoff),
the same profile sails through on later visits, cookies and all.
That handoff is the seamless path: one human minute, once, instead
of the agent fighting a gate it should not fight.

detect_barriers works on raw HTML (fetch path) or a live session's
rendered HTML, so Research mode can report "this source is behind a
login" instead of silently reading a login page as if it were the
article.
"""

_CONSENT_REJECT = ["reject all", "reject cookies", "decline", "only essential",
                   "essential only", "refuse"]
_CONSENT_ACCEPT = ["accept all", "accept cookies", "allow all", "i agree",
                   "agree and close", "got it", "ok, thanks"]

_CAPTCHA_MARKERS = ["recaptcha", "hcaptcha", "cf-turnstile", "challenges.cloudflare",
                    "verify you are human", "i'm not a robot", "i am not a robot",
                    "are you a robot", "human verification"]
_LOGIN_MARKERS = ['type="password"', "type='password'", "sign in to continue",
                  "log in to continue", "you must be logged in"]
_PAYWALL_MARKERS = ["subscribe to continue reading", "subscribe to read",
                    "this article is for subscribers", "paywall"]
_AGE_MARKERS = ["confirm you are over 18", "confirm your age", "age verification",
                "you must be 18 or older", "are you over 18"]


def detect_barriers(html, url=""):
    """Name the gates a page presents, from its HTML. [] = clear."""
    lowered = (html or "").lower()
    found = []
    if any(marker in lowered for marker in _CAPTCHA_MARKERS):
        found.append({"kind": "captcha", "needs_human": True,
                      "note": "the page asks for a human check (CAPTCHA) — "
                              "it needs the person, once, in a visible window"})
    if any(marker in lowered for marker in _LOGIN_MARKERS):
        found.append({"kind": "login", "needs_human": True,
                      "note": "the page wants a login — a vault entry fills it, "
                              "otherwise the person logs in once and the "
                              "persistent profile remembers"})
    if any(marker in lowered for marker in _PAYWALL_MARKERS):
        found.append({"kind": "paywall", "needs_human": False,
                      "note": "the page is behind a paywall — only the "
                              "preview text is readable"})
    if any(marker in lowered for marker in _AGE_MARKERS):
        found.append({"kind": "age_gate", "needs_human": False,
                      "note": "the page shows an age gate — its confirm "
                              "button is a normal clickable element"})
    return found


def _click_labelled(session, phrases):
    elements = session.elements()
    for phrase in phrases:
        for element in elements:
            if element.get("tag") in ("button", "a", "input") and \
                    phrase in (element.get("text") or "").lower():
                try:
                    session.click(element["i"])
                    return element.get("text") or phrase
                except Exception:
                    return None
    return None


def dismiss_consent(session):
    """Click the consent banner's dismiss button; return its label or None."""
    return _click_labelled(session, _CONSENT_REJECT) or \
        _click_labelled(session, _CONSENT_ACCEPT)


def scroll_page(session, steps=3):
    """Scroll so lazy-loaded content renders before the page is read."""
    for _ in range(steps):
        session._evaluate("window.scrollBy(0, window.innerHeight); true")
    session._evaluate("window.scrollTo(0, 0); true")


def handle_page(session, url, scroll=False):
    """Navigate, clear what clears, and report what remains.

    Returns {url, consent_dismissed, barriers, needs_human}. A login
    barrier is cleared first when the vault has an entry for the
    site — pass vault= and passphrase-free opened Vault to enable it.
    """
    session.navigate(url)
    if scroll:
        scroll_page(session)
    clicked = dismiss_consent(session)
    barriers = detect_barriers(session.html(), url)
    return {
        "url": url,
        "consent_dismissed": clicked,
        "barriers": barriers,
        "needs_human": any(b["needs_human"] for b in barriers),
    }
