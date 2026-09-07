"""solana-upgrade-watch: who can upgrade your money?

Resolve any Solana program's upgrade control: authority, multisig
configuration, timelock, last deploy, and queued-but-unexecuted upgrades.
"""
from upgrade_watch.core import resolve, Rpc, find_squads_settings, queued_upgrades

__version__ = "1.0.0"
__all__ = ["resolve", "Rpc", "find_squads_settings", "queued_upgrades"]
