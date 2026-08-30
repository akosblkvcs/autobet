"""Turn a message into a tip.

Stage 2. The real formats have to be learned from the backfilled corpus rather
than guessed, so this recognises nothing yet and the pipeline simply archives.
Stage 2b (screenshot tips through a vision model) forks here too: text takes
the fast path, media takes the slow one.
"""

from autobet.models import IncomingMessage, Tip


def parse_tip(message: IncomingMessage) -> Tip | None:
    """Extract a tip from a message, or None if it is not one."""
    return None
