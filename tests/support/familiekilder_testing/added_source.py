"""A fictional service that lives outside the package, for `sources.register`.

The module is both the service's auth module and its reader. `SOURCE` names
it by its full module path, as an application that embeds the package would.
"""

from contextlib import contextmanager

from familiekilder.private_state import credential_path
from familiekilder.sources import Source

NAME = "notes"
MODULE = __name__

SOURCE = Source(
    NAME,
    "Notater",
    MODULE,
    MODULE,
    letter="N",
    description="Oppdiktede notater",
    writes=False,
    login="fictional",
    streams=(),
    provider_bundle=False,
    listable=False,
    write_permissions=False,
    session_file="notes.json",
    auth_journal=False,
    read_lock_wait=2.5,
)

ITEM = {
    "source": NAME,
    "source_id": "note:1",
    "title": "Oppdiktet notat",
    "body": "Ta med matpakke.",
    "reference": {"kind": "note", "id": "1"},
    "attachments": [],
}

# Arguments of each `run_auth_command` call.
AUTH_COMMANDS = []


def auth_status(root):
    present = credential_path(root, "notes.json").exists()
    return {"configured": True, "authenticated": present, "session_present": present}


def run_auth_command(args, root):
    AUTH_COMMANDS.append((args.auth_command, root))
    return 0


@contextmanager
def connect(root):
    yield None, None


def list_items(root, m, c, since, *, include_detail_hash=False):
    raise AssertionError("The service has no broad listing")


def get_item(root, m, c, reference):
    return dict(ITEM, reference=dict(reference))


def download(root, m, c, item, attachment_id, reference, directory):
    raise AssertionError("The service has no attachments")
