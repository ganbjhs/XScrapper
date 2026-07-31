"""
config.py — accounts and streams, loaded from config.toml.

Secrets are referenced by env-var NAME, never stored here, so config.toml is
safe to back up while .env is not. Uses stdlib tomllib (3.11+), so no new
dependency.

config.toml is required. There is deliberately no fallback that invents one:
a config guessed from stray environment variables is how an account ends up
collecting under a label nobody chose.
"""

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Every command this program prints back at the user is built from these, so
# the suggestion is always runnable as-is. Hardcoding "python" breaks on macOS
# and most modern Linux distros, which ship only `python3`; hardcoding
# "python3" breaks inside a venv where the binary is `python`. Deriving it from
# the running interpreter is correct in both cases.
PY = Path(sys.executable).name
CLI = f"{PY} main.py"

CONFIG_FILENAME = "config.toml"

# Only the time-ordered tab can be watermarked. See StreamCfg.validate.
TABS = ("Latest", "Top", "Media")


class ConfigError(Exception):
    """Raised for anything a human needs to fix in config.toml or .env."""


@dataclass
class Defaults:
    db_accounts: str = "accounts.db"
    db_results: str = "results.db"
    profiles_dir: str = "profiles"
    page_size: int = 20
    max_pages_per_poll: int = 5
    min_interval_s: float = 5.0
    max_interval_s: float = 900.0
    overlap_ms: int = 60_000

    # The timeline-entry wrapper around each tweet. Measured on a real store it
    # is 60% of the database and nothing in this project reads it — raw_json
    # already satisfies "keep the payload so a parser fix can be replayed"
    # (RULEBOOK R9). Off by default; turn it on only if you need X's own
    # sortIndex for ordering forensics.
    keep_entry_json: bool = False


@dataclass
class AccountCfg:
    label: str
    username: str = ""
    password_env: str = ""
    email: str = ""
    email_password_env: str = ""
    profile_dir: str = ""
    proxy: str = ""
    locale: str = "en-US"
    timezone: str = "UTC"
    enabled: bool = True

    # Resolved absolute path; filled in by Config.
    profile_path: Path = field(default=Path("."), repr=False)

    @property
    def password(self) -> str:
        """Only needed for the first browser login; blank afterwards is fine."""
        return os.getenv(self.password_env, "") if self.password_env else ""

    @property
    def email_password(self) -> str:
        return os.getenv(self.email_password_env, "") if self.email_password_env else ""

    @property
    def proxy_or_none(self) -> str | None:
        return self.proxy or None


@dataclass
class StreamCfg:
    label: str
    query: str = ""
    list_id: str = ""       # set instead of query to poll an X List timeline
    tab: str = "Latest"
    watermark: bool = True
    enabled: bool = True

    # None means "inherit from [defaults]"; Config resolves them.
    page_size: int = 20
    max_pages_per_poll: int = 5
    min_interval_s: float = 5.0
    max_interval_s: float = 900.0
    overlap_ms: int = 60_000


@dataclass
class WebhookCfg:
    """One place we POST new tweets to as they are collected."""
    label: str
    url: str
    secret_env: str = ""
    streams: list = field(default_factory=list)   # empty = every visible stream
    batch_size: int = 50
    timeout_s: float = 10.0
    include_hidden: bool = False
    enabled: bool = True

    @property
    def secret(self) -> str:
        return os.getenv(self.secret_env, "") if self.secret_env else ""


@dataclass
class Config:
    root: Path
    defaults: Defaults
    accounts: list[AccountCfg]
    streams: list[StreamCfg]
    source: Path       # the config.toml this came from
    webhooks: list = field(default_factory=list)

    def enabled_webhooks(self) -> list:
        return [w for w in self.webhooks if w.enabled]

    @property
    def db_accounts(self) -> Path:
        return self._resolve(self.defaults.db_accounts)

    @property
    def db_results(self) -> Path:
        return self._resolve(self.defaults.db_results)

    @property
    def profiles_dir(self) -> Path:
        return self._resolve(self.defaults.profiles_dir)

    def _resolve(self, p: str) -> Path:
        path = Path(p).expanduser()
        return path if path.is_absolute() else (self.root / path)

    def enabled_accounts(self) -> list[AccountCfg]:
        return [a for a in self.accounts if a.enabled]

    def enabled_streams(self) -> list[StreamCfg]:
        return [s for s in self.streams if s.enabled]

    def account(self, label: str) -> AccountCfg:
        for a in self.accounts:
            if a.label == label:
                return a
        known = ", ".join(a.label for a in self.accounts) or "(none)"
        raise ConfigError(f"No account labelled {label!r}. Known accounts: {known}")

    def stream(self, label: str) -> StreamCfg:
        for s in self.streams:
            if s.label == label:
                return s
        known = ", ".join(s.label for s in self.streams) or "(none)"
        raise ConfigError(f"No stream labelled {label!r}. Known streams: {known}")


def _pick(raw: dict, key: str, default):
    """Read a key, treating an explicit null/empty-string as absent."""
    val = raw.get(key)
    return default if val is None or val == "" else val


def _parse_account(raw: dict, idx: int, defaults: Defaults) -> AccountCfg:
    label = str(raw.get("label") or "").strip()
    if not label:
        raise ConfigError(f"[[accounts]] #{idx + 1} is missing a `label`.")

    profile_dir = str(_pick(raw, "profile_dir", f"{defaults.profiles_dir}/{label}"))
    return AccountCfg(
        label=label,
        username=str(raw.get("username") or "").strip(),
        password_env=str(raw.get("password_env") or "").strip(),
        email=str(raw.get("email") or "").strip(),
        email_password_env=str(raw.get("email_password_env") or "").strip(),
        profile_dir=profile_dir,
        proxy=str(raw.get("proxy") or "").strip(),
        locale=str(_pick(raw, "locale", "en-US")),
        timezone=str(_pick(raw, "timezone", "UTC")),
        enabled=bool(_pick(raw, "enabled", True)),
    )


def _parse_list_id(value, label: str) -> str:
    """
    Accept a bare id or a full URL — people copy the URL from the address bar.

        1234567890123456789
        https://x.com/i/lists/1234567890123456789
        https://twitter.com/i/lists/1234567890?ref_src=...
    """
    if value is None or value == "":
        return ""
    text = str(value).strip()

    if "/" in text:
        m = re.search(r"/lists/(\d+)", text)
        if not m:
            raise ConfigError(
                f"Stream {label!r}: could not find a list id in {text!r}.\n"
                f"  Expected something like https://x.com/i/lists/1234567890 "
                f"or the bare number."
            )
        text = m.group(1)

    if not text.isdigit():
        raise ConfigError(
            f"Stream {label!r}: list_id must be numeric, got {text!r}.\n"
            f"  Open the list on x.com — the id is the number in the URL. "
            f"A list's @name is not usable here."
        )
    return text


def _parse_stream(raw: dict, idx: int, defaults: Defaults) -> StreamCfg:
    label = str(raw.get("label") or "").strip()
    if not label:
        raise ConfigError(f"[[streams]] #{idx + 1} is missing a `label`.")

    query = str(raw.get("query") or "").strip()
    list_id = _parse_list_id(raw.get("list_id"), label)

    # Exactly one source. Accepting both would leave the precedence rule
    # invisible in config.toml, and silently ignoring one of them is the kind
    # of quiet wrongness this project refuses (RULEBOOK R6).
    if query and list_id:
        raise ConfigError(
            f"Stream {label!r} sets both `query` and `list_id`. A stream has one "
            f"source.\n  Use `query` for advanced search, or `list_id` for an X "
            f"List timeline. Split them into two streams if you want both."
        )
    if not query and not list_id:
        raise ConfigError(
            f"Stream {label!r} needs either a `query` or a `list_id`."
        )

    tab = str(_pick(raw, "tab", "Latest"))
    if list_id:
        # ListLatestTweetsTimeline has no product parameter; there is only one
        # ordering, and it is chronological.
        if "tab" in raw and tab != "Latest":
            raise ConfigError(
                f"Stream {label!r}: a list timeline has no {tab!r} tab. "
                f"Lists are always reverse-chronological — drop `tab`."
            )
        tab = "Latest"
    if tab not in TABS:
        raise ConfigError(f"Stream {label!r}: tab={tab!r} is not one of {TABS}.")

    # Watermarking (stop paginating at the first already-seen tweet) is only
    # correct on a time-ordered tab. Rather than silently downgrading a ranked
    # tab to a bounded sweep — a different collection mode with different
    # freshness behaviour — make the operator say so explicitly.
    if tab != "Latest" and "watermark" not in raw:
        raise ConfigError(
            f"Stream {label!r}: tab={tab!r} cannot be watermarked, so you must "
            f"choose the collection mode explicitly.\n"
            f"  Only the Latest tab is time-ordered. On a ranked tab, stopping "
            f"early at the watermark would silently drop results.\n"
            f"  Fix: set tab = \"Latest\" for freshness monitoring, or add "
            f"watermark = false to sweep a bounded number of pages with dedup "
            f"instead (no early stop, higher request cost per poll)."
        )
    watermark = bool(_pick(raw, "watermark", tab == "Latest"))
    if watermark and tab != "Latest":
        raise ConfigError(
            f"Stream {label!r}: watermark = true requires tab = \"Latest\", got {tab!r}."
        )

    return StreamCfg(
        label=label,
        query=query,
        list_id=list_id,
        tab=tab,
        watermark=watermark,
        enabled=bool(_pick(raw, "enabled", True)),
        page_size=int(_pick(raw, "page_size", defaults.page_size)),
        max_pages_per_poll=int(_pick(raw, "max_pages_per_poll", defaults.max_pages_per_poll)),
        min_interval_s=float(_pick(raw, "min_interval_s", defaults.min_interval_s)),
        max_interval_s=float(_pick(raw, "max_interval_s", defaults.max_interval_s)),
        overlap_ms=int(_pick(raw, "overlap_ms", defaults.overlap_ms)),
    )


def _parse_webhook(raw: dict, idx: int) -> WebhookCfg:
    label = str(raw.get("label") or "").strip() or f"webhook_{idx + 1}"
    url = str(raw.get("url") or "").strip()
    if not url:
        raise ConfigError(f"[[webhooks]] {label!r} is missing a `url`.")
    if not re.match(r"^https?://", url):
        raise ConfigError(
            f"[[webhooks]] {label!r}: url must start with http:// or https://, got {url!r}."
        )

    secret_env = str(raw.get("secret_env") or "").strip()
    if not secret_env:
        raise ConfigError(
            f"[[webhooks]] {label!r} has no `secret_env`.\n"
            f"  Every delivery is signed so the receiver can prove it came from\n"
            f"  you. Without a secret anyone who learns the URL can post fake\n"
            f"  tweets into your system, and nothing downstream could tell.\n"
            f"  Fix: put a long random value in .env and name it here:\n"
            f"      secret_env = \"WEBHOOK_SECRET_{label.upper()}\""
        )

    streams = raw.get("streams") or []
    if not isinstance(streams, list) or any(not isinstance(s, str) for s in streams):
        raise ConfigError(
            f"[[webhooks]] {label!r}: `streams` must be a list of stream labels, "
            f"e.g. streams = [\"politicians\"]. Leave it out to send everything."
        )

    batch = int(_pick(raw, "batch_size", 50))
    if not 1 <= batch <= 500:
        raise ConfigError(f"[[webhooks]] {label!r}: batch_size must be 1-500, got {batch}.")

    return WebhookCfg(
        label=label,
        url=url,
        secret_env=secret_env,
        streams=[str(s) for s in streams],
        batch_size=batch,
        timeout_s=float(_pick(raw, "timeout_s", 10.0)),
        include_hidden=bool(_pick(raw, "include_hidden", False)),
        enabled=bool(_pick(raw, "enabled", True)),
    )


def _validate(cfg: Config) -> None:
    known_streams = {s.label for s in cfg.streams}
    for w in cfg.webhooks:
        # A typo here means a webhook that silently never fires, which is the
        # worst possible outcome for something whose whole job is delivery.
        missing = [s for s in w.streams if s not in known_streams]
        if missing:
            raise ConfigError(
                f"[[webhooks]] {w.label!r} filters on stream(s) {missing} that are "
                f"not declared.\n  Known streams: "
                f"{', '.join(sorted(known_streams)) or '(none)'}"
            )

    for kind, items in (("account", cfg.accounts), ("stream", cfg.streams),
                        ("webhook", cfg.webhooks)):
        seen: set[str] = set()
        for it in items:
            if it.label in seen:
                raise ConfigError(f"Duplicate {kind} label {it.label!r}.")
            seen.add(it.label)

    # Two Chromes on one user_data_dir corrupt the profile, and Chrome will
    # refuse the second launch anyway.
    profiles: dict[str, str] = {}
    for a in cfg.accounts:
        key = str(a.profile_path)
        if key in profiles:
            raise ConfigError(
                f"Accounts {profiles[key]!r} and {a.label!r} share profile_dir "
                f"{a.profile_dir!r}. Each account needs its own Chrome profile."
            )
        profiles[key] = a.label

    for s in cfg.streams:
        if s.min_interval_s > s.max_interval_s:
            raise ConfigError(
                f"Stream {s.label!r}: min_interval_s ({s.min_interval_s}) "
                f"exceeds max_interval_s ({s.max_interval_s})."
            )
        if s.page_size < 1 or s.max_pages_per_poll < 1:
            raise ConfigError(
                f"Stream {s.label!r}: page_size and max_pages_per_poll must be >= 1."
            )

    if os.getenv("TWS_PROXY"):
        raise ConfigError(
            "TWS_PROXY is set in the environment. twscrape gives it precedence "
            "over every per-account proxy (account.py:58-61), so your whole "
            "pool would share one exit IP.\n"
            "  Fix: unset TWS_PROXY and set `proxy` per account in config.toml."
        )


def find_config(root: Path, explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        p = p if p.is_absolute() else (root / p)
        if not p.exists():
            raise ConfigError(f"Config file not found: {p}")
        return p
    p = root / CONFIG_FILENAME
    try:
        return p if p.exists() else None
    except PermissionError:
        # Path.exists() raises rather than returning False when the DIRECTORY
        # is unreadable. Running `sudo -u xscraper main.py doctor` from /root
        # crashed with a raw traceback about '/root/config.toml' — technically
        # accurate, useless as guidance. Say what actually went wrong.
        raise ConfigError(
            f"Cannot look for {CONFIG_FILENAME} in {root} — permission denied.\n"
            f"  You are probably running as a different user than owns this "
            f"directory.\n"
            f"  Fix: cd to the app directory first, or pass --config explicitly:\n"
            f"      cd /opt/xscraper/app && {CLI} doctor\n"
            f"      {CLI} doctor --config /opt/xscraper/app/config.toml"
        ) from None


def load_config(explicit: str | None = None, root: Path | None = None) -> Config:
    """
    Load config.toml.

    Always calls load_dotenv() first so `password_env` lookups resolve.

    A missing config.toml is an error, not something to paper over. This used to
    synthesize a one-account config named "legacy" from bare env vars, and the
    result was exactly the confusion R6 exists to prevent: the account that was
    really doing the collecting lived under a label nobody had written down,
    with a profile directory nobody had chosen, invisible to config.toml. Say
    what is missing instead.
    """
    root = (root or Path.cwd()).resolve()
    load_dotenv(root / ".env")

    path = find_config(root, explicit)
    if path is None:
        raise ConfigError(
            f"No {CONFIG_FILENAME} in {root}.\n"
            f"  Create one from the template:\n"
            f"      cp {CONFIG_FILENAME}.example {CONFIG_FILENAME}\n"
            f"  Then edit it to name your account and what you want to watch."
        )

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e

    known = {"defaults", "accounts", "streams", "webhooks"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(
            f"{path}: unknown top-level key(s) {sorted(unknown)}. Expected {sorted(known)}."
        )

    d_raw = raw.get("defaults") or {}
    unknown_d = set(d_raw) - set(Defaults().__dict__)
    if unknown_d:
        raise ConfigError(f"{path}: unknown key(s) in [defaults]: {sorted(unknown_d)}")
    defaults = Defaults(**{k: v for k, v in d_raw.items() if v is not None})

    accounts = [_parse_account(a, i, defaults) for i, a in enumerate(raw.get("accounts") or [])]
    streams = [_parse_stream(s, i, defaults) for i, s in enumerate(raw.get("streams") or [])]
    webhooks = [_parse_webhook(w, i) for i, w in enumerate(raw.get("webhooks") or [])]

    if not accounts:
        raise ConfigError(
            f"{path} declares no [[accounts]]. At least one is required — "
            f"see config.toml.example."
        )

    cfg = Config(root=root, defaults=defaults, accounts=accounts, streams=streams,
                 source=path, webhooks=webhooks)
    for a in cfg.accounts:
        a.profile_path = cfg._resolve(a.profile_dir)
    _validate(cfg)
    return cfg
