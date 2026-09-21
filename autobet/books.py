"""Per-book configuration, typed by the adapter that reads it."""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The only book so far. A second one is a row, not a rewrite.
TIPPMIXPRO = "tippmixpro"

_HOSTS = ("tippmixpro.hu", "nwacdn.com", "everymatrix.com")


def _endpoint(value: str, scheme: str) -> str:
    """One endpoint of this book: encrypted, and at a host the book answers on."""
    if not value:
        return value

    if not value.startswith(scheme):
        raise ValueError(f"must be a {scheme} URL")

    host = urlsplit(value).hostname or ""

    if not any(host == known or host.endswith(f".{known}") for known in _HOSTS):
        raise ValueError(f"host {host} is not one of {', '.join(_HOSTS)}")

    return value


class Tippmixpro(BaseModel):
    """What the tippmixpro adapter needs to reach the book."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    site: str = Field(default="", description="Public site, sent as Origin and Referer.")
    api: str = Field(default="", description="Player API, where the login POST goes.")
    loader: str = Field(
        default="", description="EveryMatrix loader that mints ceSession."
    )
    ws: str = Field(
        default="", description="Feed socket; wss:// only, it carries the token."
    )
    realm: str = Field(default="", description="WAMP realm the socket says hello with.")
    operator: str = Field(default="", description="Operator id every topic is scoped by.")

    @field_validator("ws")
    @classmethod
    def _encrypted(cls, value: str) -> str:
        """The betting session is sent on this socket, so it cannot be plaintext."""
        return _endpoint(value, "wss://")

    @field_validator("site", "api", "loader")
    @classmethod
    def _over_tls(cls, value: str) -> str:
        """The password goes to `api` and session material to `loader`."""
        return _endpoint(value, "https://")
