"""Per-book configuration, typed by the adapter that reads it."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The only book so far. A second one is a row, not a rewrite.
TIPPMIXPRO = "tippmixpro"


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
        if value and not value.startswith("wss://"):
            raise ValueError("ws must be a wss:// URL")

        return value

    @field_validator("site", "api", "loader")
    @classmethod
    def _over_tls(cls, value: str) -> str:
        """The password goes to `api` and session material to `loader`."""
        if value and not value.startswith("https://"):
            raise ValueError("must be an https:// URL")

        return value
