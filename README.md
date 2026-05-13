# oss-crawler

Crawler for [Online-Schule Saarland](https://online-schule.saarland/) — a
learning platform protected by SAML2 SSO via Shibboleth IdP at
`idp.online-schule.saarland` (SP at `meine.online-schule.saarland`).

**This iteration handles login + session persistence only.** Course discovery
and material downloads will follow in later iterations.

## Setup

```bash
cd /home/thomas/Repos/oss-crawler
python -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
cp .env.example .env   # optional: fill OSS_USERNAME / OSS_PASSWORD for auto-login
```

## Authentication

Three ways to log in, in increasing automation:

```bash
# 1) First-time interactive login — visible browser, manual entry, no creds
#    in .env required. The session is saved to .auth.json for reuse.
oss-crawler --login --auth-only

# 2) Reuse the saved session — silent verify, exit 0 if still valid.
oss-crawler

# 3) Fully automated login from .env credentials (requires OSS_USERNAME and
#    OSS_PASSWORD set). Falls back to the interactive flow on failure.
oss-crawler --auth-only

# Refresh an expired session:
oss-crawler --login --auth-only
```

`--login` forces the interactive browser even when a saved session exists.
`--auth-only` is the default behaviour this iteration (login, verify, exit),
so running `oss-crawler` with no flags has the same effect.

## How login works

`oss_crawler/auth.py` tries three tiers in order:

1. **Session reuse** — load `.auth.json` (Playwright `storage_state`), open
   `${OSS_BASE_URL}/`, and verify: SP host (no redirect to IdP), no
   Shibboleth login form, and a `_shibsession_*` cookie is set.
2. **Auto-login** — open `${OSS_BASE_URL}/`, follow the redirect to the IdP,
   fill `j_username` / `j_password`, click submit, wait for the SAMLResponse
   POST to put us back on the SP host. Falls through to tier 3 on any
   failure (wrong creds, IdP theme changes, CAPTCHA/MFA prompts).
3. **Interactive login** — open a visible Chromium window, let the user log
   in manually (2FA supported), poll until a tab is on the SP host, on a
   non-login path, with no Shibboleth password input visible, and a
   `_shibsession_*` cookie exists. Then save the session.

## Debugging

Auth failures dump screenshot + HTML into `.debug/`:

- `.debug/login-fields-missing.{png,html}` — IdP form selectors didn't match;
  edit `LOGIN_SELECTORS` in `oss_crawler/auth.py`.
- `.debug/login-submit-missing.{png,html}` — submit button selectors didn't
  match.
- `.debug/login-no-navigation.{png,html}` — submitted credentials but the
  browser stayed on the IdP (wrong creds, CAPTCHA, MFA).

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `OSS_USERNAME` | _empty_ | IdP username (optional; enables auto-login) |
| `OSS_PASSWORD` | _empty_ | IdP password (optional; enables auto-login) |
| `OSS_BASE_URL` | `https://meine.online-schule.saarland` | OSS SP base URL |
| `OSS_IDP_HOST` | `idp.online-schule.saarland` | Shibboleth IdP hostname (used to detect SSO redirects) |
| `HEADLESS` | `true` | Run Chromium headless during auto-login (ignored for `--login`, which is always visible) |

## What's next

Intentionally NOT here yet:

- Course / class discovery (Moodle dashboard scraping)
- Material download (Moodle resource / folder modules)
- Persistent state tracker for incremental sync
- CLI flags for selecting a specific class

Add them on top of the `authenticated_context()` context manager exposed by
`oss_crawler.auth`.
