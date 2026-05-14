# oss-crawler

Crawler for [Online-Schule Saarland](https://online-schule.saarland/) — a
learning platform protected by SAML2 SSO via Shibboleth IdP at
`idp.online-schule.saarland` (SP at `meine.online-schule.saarland`).

**This iteration handles login + session persistence + school selection +
course selection + module (section) selection + per-module material
download with incremental sync.**

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

## School selection

OSS accounts can be associated with multiple schools (teachers especially:
Studienseminar + one or more Gymnasien). The dashboard shows the active
school as a badge; the sidebar offers a "Schulwechsel" drawer to change it.

```bash
# List the schools your account can access (one per line):
oss-crawler --list-schools

# Switch to a school by short alias (built-in aliases below):
oss-crawler --school asg     # Albert-Schweitzer-Gymnasium Dillingen
oss-crawler --school sgs     # Gymnasium am Stadtgarten Saarlouis

# Or pass the full name exactly as shown in --list-schools:
oss-crawler --school "Albert-Schweitzer-Gymnasium Dillingen"

# Combine with --login (force interactive login first):
oss-crawler --login --school asg
```

The switch is idempotent: if the requested school is already active, nothing
happens. Unknown school → exit code 3 with a list of accessible schools.

Aliases are defined in `oss_crawler/school.py:SCHOOL_ALIASES`. Add more by
editing that dict.

## Course selection

Each school has its own Moodle instance (e.g.
`lms-gym-albert-schweitzer.online-schule.saarland`). After the active school
is set, `--list-courses` reads its Moodle Dashboard and prints every course
that account has access to. `--course` selects one by full name
(case-insensitive) and navigates the browser there.

```bash
# List courses on the currently-active school (one name per line):
oss-crawler --list-courses

# Switch school + list, in one shot:
oss-crawler --school asg --list-courses

# Select a specific course (case-insensitive exact match):
oss-crawler --school asg --course "8 Informatik 2025-26 GRS"
#   → prints "Kurs: 8 Informatik 2025-26 GRS — https://lms-…/course/view.php?id=1618"

# Misspelt name → exit 4 with the list of available courses:
oss-crawler --school asg --course "Quatsch"
```

The course list is read from the Moodle Dashboard's "Alle"-filter view, so
past/future/favourite courses are included; only user-hidden courses are
excluded.

## Module (section) selection & download

Once a course is open, `--list-modules` prints every module on its page;
`--module` selects one and **downloads all its materials** into
`./<school>/<course>/<module>/` (folder and file names sanitized — see
below). Both flags require `--course` in the same invocation.

```bash
# List modules of a specific course (no download):
oss-crawler --school asg --course "8 Informatik 2025-26 GRS" --list-modules
#   → one module name per line, in dashboard order, including "Allgemein"

# Select a module by full name and download all its materials:
oss-crawler --school asg --course "8 Informatik 2025-26 GRS" \
    --module "Modellieren und Implementieren"
#   → "+ <file>" per new download, "= <file> (skip)" per existing,
#     ending with "Download fertig: N neu, M übersprungen, K fehlgeschlagen."

# Misspelt module → exit 5 with the list of available modules:
oss-crawler --school asg --course "8 Informatik 2025-26 GRS" --module "Quatsch"

# Forgetting --course → clear refusal, exit 5:
oss-crawler --school asg --list-modules
```

What gets downloaded:

- **Resources** (PDF, XML, …): the actual file, fetched via Moodle's
  `forcedownload=1` URL through the authenticated browser context.
- **URL activities**: a small `.html` file containing a meta-refresh to the
  external URL. Double-click in any file manager opens it in your default
  browser, which then jumps to the destination. Use `--url-format windows`
  to emit classic Windows `.url` (`[InternetShortcut]`) files instead.
- **Labels**: skipped (they're inline section headers, not materials).

Incremental sync: a material is downloaded only if a file with its
sanitized expected name doesn't already exist in the target folder. Local
files are never deleted, even if removed on OSS.

Folder and file names follow the rules in
`oss_crawler/sanitize.py` (a port of
[`sanitizeNames.sh`](../linux-config/aliases/alias-scripts/sanitizeNames/sanitizeNames.sh)):
spaces → `_`, umlauts → `ae/oe/ue`, forbidden chars stripped, directories
in `Upper_Snake_Case` (abbreviations + numeric tokens preserved, German
function words kept lowercase), files all lowercase.

In Moodle parlance modules are "sections" (topics/chapters). Extraction is
format-agnostic across Grid / Topics / Weekly formats.

## How login works

`oss_crawler/auth.py` tries three tiers in order:

1. **Session reuse** — load `.auth.json` (Playwright `storage_state`), open
   `${OSS_BASE_URL}/`, and verify: SP host (no redirect to IdP), no
   Shibboleth login form, and at least one cookie is set on the SP host.
2. **Auto-login** — open `${OSS_BASE_URL}/`, follow the redirect to the IdP,
   fill `j_username` / `j_password`, click submit, wait for the SAMLResponse
   POST to put us back on the SP host. Falls through to tier 3 on any
   failure (wrong creds, IdP theme changes, CAPTCHA/MFA prompts).
3. **Interactive login** — open a visible Chromium window, let the user log
   in manually (2FA supported), poll until a tab is on the SP host, on a
   non-login path, with no Shibboleth password input visible, and at least
   one cookie is set on the SP host. Then save the session.

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

- `--prune` to delete local files removed on OSS
- Whole-course download (`--course` without `--module` triggering all modules)
- Folder activities (`modtype_folder`) and assignments
- Pagination support for accounts with more than 12 courses per school
- `--include-restricted` to surface sections lacking a direct link

Build on top of `oss_crawler.auth.authenticated_context()`,
`oss_crawler.school.switch_school()`, `oss_crawler.course.goto_course()`,
`oss_crawler.module.goto_module()`, and `oss_crawler.download.download_module()`.
