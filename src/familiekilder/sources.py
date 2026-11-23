"""One entry per family service. Plain data: no provider imports on load.

To add a service, write its modules and add one `Source` below:

- `auth`: module with `auth_status`, `refresh_session`, `logout` and the
  login functions its `auth_commands` need (see `auth_cli.py`).
- `reader`: module with
  `connect(root)`: context manager yielding `(module, client)`,
  `list_items(root, m, c, since, *, include_detail_hash)` -> `(items, complete)`,
  `get_item(root, m, c, reference)` -> item,
  `download(root, m, c, item, attachment_id, reference, directory)` -> `(path, mime)`.
  Optional: `list_notifications(m, c, cutoff)` when `notifications` is set,
  `discover_chats(runtime, since, until, *, observe=None)` when `streams`
  has "chat", `discover_feed(runtime, since, until, *, observe=None)`
  (`observe(conn, source, stream, item, stamp)` records a page's items in
  its checkpoint transaction; see `chat_discovery.py`), `read_scope(root)` and
  `reference_values(reference)` (see `live_readers.stored_reference`).
- `actions`: modules with `register(server, runtime)`, registered in order.

`live_readers.py` has the shared helpers a reader uses (`ref_item`,
`attachment_metadata`, `checked_detail`, `fetch_attachment`).

`web_script()` renders `label`, `letter`, `description` and `kind` as
`/services.js` for a web front end.

An application that embeds this package can add services of its own with
`register` (internal API); see its docstring.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

COMMON_ACTIONS = ("send_message", "reply_message", "register_absence", "set_participation")


@dataclass(frozen=True)
class Source:
    name: str
    label: str
    auth: str
    reader: str
    # Shown in the web pages: the mark on the service's badge and a short
    # Norwegian description in the service lists.
    letter: str = ""
    description: str = ""
    # Groups the service lists: "school", "sport", "mail" or "other".
    kind: str = "other"
    actions: tuple[str, ...] = ()
    # Action categories with verified wire contracts, and implemented actions.
    verified: frozenset[str] = frozenset()
    implemented: frozenset[str] = frozenset()
    # Actions offered besides COMMON_ACTIONS.
    extra_actions: tuple[str, ...] = ()
    # False when the source is read-only (Flyt).
    writes: bool = True
    auth_commands: frozenset[str] = frozenset()
    logout_revokes: bool = False
    # "browser" or "token" (a personal access token the user pastes). A source
    # added with `register` may name a login of its own that it handles itself.
    login: str = "browser"
    streams: tuple[str, ...] = ("feed",)
    # The feed holds notifications that are read from the list only.
    notifications: bool = False
    # Local request pacing and cooldowns in `live_traffic`.
    paced: bool = False
    # "login_30_days", "refresh_deadline" or "long_lived" (`session_estimates`).
    session_estimate: str = "login_30_days"
    # Part of signed native provider bundles (`provider_bundle.py`).
    provider_bundle: bool = True
    password_origins: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    # False when the source gives no attachments: no `<source>_read_attachment`.
    attachments: bool = True
    # False when the source has no broad listing: no `<source>_list_items`, and
    # list reads are refused. Such a source is searched with a `<source>_search`
    # tool instead, which its application adds.
    listable: bool = True
    # False refuses write permissions for the source in the configuration.
    write_permissions: bool = True
    # The login's file in `.private/credentials`; empty: `<name>-session.json`.
    session_file: str = ""
    # Auth history and response journals in `auth_lifecycle`. False keeps only
    # installation ownership and moved markers, for a login with its own records.
    auth_journal: bool = True
    # Seconds a read waits for the source lock held by a parallel read; None
    # fails at once.
    read_lock_wait: float | None = None


_OAUTH_COMMANDS = frozenset(
    {"browser-login", "configure", "login", "refresh", "status", "logout"}
)
_BOOTSTRAP_COMMANDS = frozenset(
    {"browser-bootstrap", "configure", "import-session", "refresh", "status", "logout"}
)

REGISTRY = {
    source.name: source
    for source in (
        Source(
            "vigilo", "Vigilo", "vigilo_auth", "live_vigilo_reader",
            letter="V", description="Beskjeder fra barnehage og skole",
            kind="school",
            actions=("live_vigilo", "live_vigilo_meetings",
                     "live_vigilo_registrations", "live_vigilo_pickup"),
            verified=frozenset({"messages", "absence", "participation"}),
            implemented=frozenset({"send_message", "reply_message", "register_absence",
                                   "set_participation", "set_registration"}),
            extra_actions=("set_registration",),
            auth_commands=_OAUTH_COMMANDS,
            logout_revokes=True,
            paced=True,
        ),
        Source(
            "skolemelding", "Skolemelding", "skolemelding_auth", "live_skolemelding_reader",
            letter="S", description="Meldinger fra Osloskolen",
            kind="school",
            actions=("live_skolemelding", "live_skolemelding_messages",
                     "live_skolemelding_absence"),
            verified=frozenset({"messages", "absence"}),
            implemented=frozenset({"send_message", "report_absence"}),
            extra_actions=("report_absence",),
            auth_commands=_OAUTH_COMMANDS,
            paced=True,
        ),
        Source(
            "spond", "Spond", "spond_auth", "live_spond_reader",
            letter="s", description="Aktiviteter, grupper og beskjeder",
            kind="sport",
            actions=("live_spond_messages", "live_spond_chat",
                     "live_spond_chat_actions", "live_spond_participation"),
            verified=frozenset({"participation", "messages"}),
            implemented=frozenset({"set_participation", "reply_message",
                                   "send_message", "reply_chat"}),
            extra_actions=("reply_chat",),
            auth_commands=_BOOTSTRAP_COMMANDS,
            streams=("feed", "chat"),
            notifications=True,
            session_estimate="refresh_deadline",
            password_origins=("https://spond.com",),
        ),
        # Hoopit's external API with a personal access token (hoopit_auth.py).
        Source(
            "hoopit", "Hoopit", "hoopit_auth", "live_hoopit_reader",
            letter="h", description="Innlegg, kommentarer og aktiviteter fra klubben",
            kind="sport",
            actions=("live_hoopit",),
            verified=frozenset({"participation"}),
            implemented=frozenset({"set_participation"}),
            auth_commands=frozenset({"token", "status", "logout"}),
            login="token",
            session_estimate="long_lived",
            provider_bundle=False,
            attachments=False,
        ),
        Source(
            "ist-home", "IST Home", "ist_oauth", "live_ist_reader",
            letter="ist", description="Innlegg og samtaler fra barnehagen",
            kind="school",
            actions=("live_ist", "live_ist_bookings"),
            verified=frozenset({"messages", "absence", "participation"}),
            implemented=frozenset({"send_message", "reply_message",
                                   "register_absence", "set_participation"}),
            auth_commands=frozenset(
                {"browser-login", "configure", "login", "refresh", "status", "logout"}),
            logout_revokes=True,
            streams=("feed", "chat"),
            paced=True,
            aliases=("ist_home", "ist home", "isthome"),
        ),
        # Barnehage only so far; school (vfs) reads can be added in flyt.py.
        Source(
            "flyt", "Flyt", "flyt_auth", "live_flyt_reader",
            letter="F", description="Beskjeder og dagbok fra barnehagen",
            kind="school",
            writes=False,
            auth_commands=_OAUTH_COMMANDS,
            paced=True,
            provider_bundle=False,
        ),
    )
}


def web_script() -> str:
    """`/services.js`: labels, letters, descriptions and kinds for the web pages."""
    services = REGISTRY.values()
    catalog = {
        "labels": {s.name: s.label for s in services},
        "letters": {s.name: s.letter for s in services},
        "descriptions": {s.name: s.description for s in services},
        "kinds": {s.name: s.kind for s in services},
        "readOnly": [s.name for s in services if not s.writes],
    }
    # Built from REGISTRY above. The served script names no source file, because
    # the host's web pages may be public.
    return ("'use strict';\n"
            f"globalThis.Services = {json.dumps(catalog, ensure_ascii=False, indent=2)};\n")


def register(source: Source) -> None:
    """Add a service defined outside this package (internal API).

    For an application that embeds these connectors and has services of its
    own. The module names in `auth`, `reader` and `actions` are then full
    import paths. Call it before the application reads or serves sources.
    The package looks the registry up when it is used, also through
    `SourceNames`, so the service is then everywhere the package's own are.
    MCP tools beyond the standard reads are added through the `extensions` of
    `source_server.build_server`.
    """
    if not isinstance(source, Source):
        raise TypeError("register() takes a Source")
    if source.name in REGISTRY:
        raise ValueError(f"{source.name!r} is already registered")
    for name in (source.auth, source.reader, *source.actions):
        if "." not in name:
            raise ValueError(f"{name!r} must be a full module path")
    REGISTRY[source.name] = source


class SourceNames(Sequence):
    """Names of the registered services, read from REGISTRY on every use.

    `where(source)` picks the services, and the `first` names come before
    them. A module keeps one of these instead of a tuple, so services added
    with `register` after the module was imported are included.
    """

    def __init__(self, where: Callable[[Source], bool] | None = None, *,
                 first: tuple[str, ...] = ()):
        self._where = where
        self._first = tuple(first)

    def _names(self) -> tuple[str, ...]:
        return (*self._first, *(
            name for name, source in REGISTRY.items()
            if self._where is None or self._where(source)
        ))

    def __getitem__(self, index):
        return self._names()[index]

    def __len__(self) -> int:
        return len(self._names())

    def __iter__(self):
        return iter(self._names())

    def __contains__(self, value) -> bool:
        return value in self._names()

    def __eq__(self, other) -> bool:
        if isinstance(other, (SourceNames, tuple)):
            return self._names() == tuple(other)
        return NotImplemented

    __hash__ = None

    def __repr__(self) -> str:
        return repr(self._names())


def module_path(name: str) -> str:
    """The import path of a module named in the registry."""
    return name if "." in name else f"{__package__}.{name}"


def module(name: str):
    """Import a service module named in the registry, on first use only."""
    return importlib.import_module(module_path(name))


def session_file(source: str) -> str:
    """The name of the source's login file in `.private/credentials`."""
    return REGISTRY[source].session_file or f"{source}-session.json"


def reader(source: str):
    return module(REGISTRY[source].reader)
