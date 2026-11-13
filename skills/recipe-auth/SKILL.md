name: recipe-auth
description: Building-block recipe — wire local session login (register / login / logout / who-am-I) into the owner's project with a local user store, no cloud auth service. Use when the owner asks for logins, accounts, sign-in, or "users" in a project being built.

# Recipe: Auth — local session login (C10 building block)

A named, tested scaffold. Local only: a SQLite user store in the
project itself, passwords hashed with the standard library
(`hashlib.scrypt`), sessions as random tokens in a cookie or a
returned bearer token. No external account, no cloud tenant —
Forge's answer to hosted auth provisioning. Python stdlib only;
for another stack, port the SHAPE (same tables, same flows, same
checks), never invent a weaker one.

## When the owner asks

Ask for "auth", "login", or the recipe by name in Code mode.
State the shape before building: store location, session style
(cookie for web apps, bearer token for APIs/CLIs), and that
passwords are never stored or logged in plain text.

## The scaffold (Python, stdlib only)

Create `auth.py` in the project with exactly these parts:

1. **Store** — SQLite file `users.db` in the project's data
   folder (created on first use):
   `users(id INTEGER PRIMARY KEY, name TEXT UNIQUE, pw_hash TEXT,
   created TEXT)` and `sessions(token TEXT PRIMARY KEY,
   user_id INTEGER, expires TEXT)`.
2. **Hashing** — `hashlib.scrypt(password, salt=os.urandom(16),
   n=16384, r=8, p=1)`, stored as `salt_hex$hash_hex`; verify with
   `hmac.compare_digest`. Never MD5/SHA1/plaintext, never a
   home-rolled cipher.
3. **Functions** — `register(name, password)`, `login(name,
   password) -> token`, `user_for_token(token)`, `logout(token)`.
   Tokens: `secrets.token_urlsafe(32)`. Sessions expire (default
   7 days); expired tokens are deleted on sight.
4. **Rules** — duplicate names refused with a plain message;
   wrong password and unknown user return the SAME message
   ("name or password incorrect") so the login cannot be used
   to enumerate accounts; minimum password length 8, refused
   with the reason stated.
5. **Wiring** — web app: set the token as an HttpOnly cookie on
   login, clear it on logout, gate private routes on
   `user_for_token`. API/CLI: return the token, read it back
   from the Authorization header / `--token` argument.

## Verify (the recipe is not done until these pass)

- Register a user, log in, `user_for_token` returns that user.
- Wrong password is refused; unknown user gets the same message.
- The database file contains no plaintext password (grep it).
- Logout kills the token; an expired token is refused.
- Run the project's own tests after wiring — a broken login that
  ships is worse than no login.

## Never

No passwords in logs, URLs, or error messages. No "temporary"
plaintext or unsalted hashing, even in a prototype. No rolling
your own session crypto — the token is random, the store is the
session, that is the whole design.
