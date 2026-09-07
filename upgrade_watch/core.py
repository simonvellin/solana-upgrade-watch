"""Core resolution: who can upgrade a Solana program, and how.

Pure-stdlib Python. Everything here talks directly to a Solana JSON-RPC
endpoint of your choosing — nothing else is contacted, nothing is stored.

Resolution pipeline for a program id:
  1. Loader classification (BPFLoaderUpgradeable / legacy immutable / v4).
  2. ProgramData header -> upgradable flag, last deploy slot (+ time, tx),
     upgrade authority.
  3. Authority classification:
       - none                -> program is immutable
       - on-curve pubkey     -> a plain wallet key can upgrade the program
       - off-curve (PDA)     -> program-controlled; Squads v4 multisigs are
                                identified by deriving every candidate
                                multisig's vault PDA locally and matching
  4. For Squads v4: threshold (approvals / voting members), timelock,
     member count, and any ACTIVE/APPROVED proposals — i.e. upgrades
     already queued but not yet executed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import urllib.request
from datetime import datetime, timezone

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

BPFLOADER_UPGRADEABLE = "BPFLoaderUpgradeab1e11111111111111111111111"
BPFLOADER_V2 = "BPFLoader2111111111111111111111111111111111"
BPFLOADER_V1 = "BPFLoader1111111111111111111111111111111111"
LOADER_V4 = "LoaderV411111111111111111111111111111111111"
SQUADS_V4 = "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf"

PROPOSAL_STATUS = {0: "Draft", 1: "Active", 2: "Rejected", 3: "Approved",
                   4: "Executing", 5: "Executed", 6: "Cancelled"}
_STATUS_HAS_TS = {0, 1, 2, 3, 5, 6}

# ── base58 / ed25519 curve / PDA ─────────────────────────────────────────────

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    b = b"\0" * (len(s) - len(s.lstrip("1"))) + b
    return b.rjust(32, b"\0") if len(b) < 32 else b


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def is_on_curve(b32: bytes) -> bool:
    """True when the 32 bytes decompress to a valid ed25519 point — i.e. the
    address can be a real keypair. PDAs are deliberately off-curve."""
    y = int.from_bytes(b32, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    x2 = (y2 - 1) % _P * pow((_D * y2 + 1) % _P, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


def find_pda(seeds: list[bytes], program: str) -> str:
    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog
                           + b"ProgramDerivedAddress").digest()
        if not is_on_curve(h):
            return b58encode(h)
    raise ValueError("no valid bump")


def squads_vault_pda(settings: str, index: int = 0) -> str:
    return find_pda([b"multisig", b58decode(settings), b"vault", bytes([index])], SQUADS_V4)


def squads_proposal_pda(settings: str, tx_index: int) -> str:
    return find_pda([b"multisig", b58decode(settings), b"transaction",
                     tx_index.to_bytes(8, "little"), b"proposal"], SQUADS_V4)


def squads_transaction_pda(settings: str, tx_index: int) -> str:
    return find_pda([b"multisig", b58decode(settings), b"transaction",
                     tx_index.to_bytes(8, "little")], SQUADS_V4)


# ── RPC ──────────────────────────────────────────────────────────────────────

class Rpc:
    def __init__(self, url: str = DEFAULT_RPC):
        self.url = url

    def call(self, method: str, params: list):
        req = urllib.request.Request(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1,
                             "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "solana-upgrade-watch/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        if "error" in resp:
            raise RuntimeError(f"RPC {method}: {resp['error']}")
        return resp.get("result")

    def account_b64(self, pubkey: str, offset: int | None = None,
                    length: int | None = None) -> bytes | None:
        cfg: dict = {"encoding": "base64", "commitment": "confirmed"}
        if offset is not None:
            cfg["dataSlice"] = {"offset": offset, "length": length}
        v = (self.call("getAccountInfo", [pubkey, cfg]) or {}).get("value")
        if not v:
            return None
        return base64.b64decode(v["data"][0])

    def account_parsed(self, pubkey: str) -> dict | None:
        return (self.call("getAccountInfo",
                          [pubkey, {"encoding": "jsonParsed",
                                    "commitment": "confirmed"}]) or {}).get("value")


# ── Squads v4 decoding ───────────────────────────────────────────────────────

def decode_multisig(raw: bytes) -> dict:
    """threshold / time_lock / transaction_index / member count from a
    Squads v4 Multisig account (FULL raw bytes, discriminator included:
    disc 8 + create_key 32 + config_authority 32 puts threshold at 72).
    The members vec offset varies with the rent_collector Option, so the
    length prefix is located empirically."""
    threshold = struct.unpack_from("<H", raw, 72)[0]
    time_lock = struct.unpack_from("<I", raw, 74)[0]
    tx_index = struct.unpack_from("<Q", raw, 78)[0]
    members = None
    members_off = None
    for off in range(94, min(140, len(raw) - 4)):
        n = struct.unpack_from("<I", raw, off)[0]
        if 1 <= n <= 40 and off + 4 + n * 33 <= len(raw) and any(raw[off + 4:off + 36]):
            members, members_off = n, off
            break
    voting = None
    if members is not None:
        # Each member is 32 bytes pubkey + 1 byte permission mask; bit 1 (2)
        # is Vote. The Squads UI's "m/n" denominator counts VOTING members.
        voting = 0
        for i in range(members):
            mask = raw[members_off + 4 + i * 33 + 32]
            if mask & 2:
                voting += 1
    return {"threshold": threshold, "time_lock_s": time_lock,
            "transaction_index": tx_index,
            "members": members, "voting_members": voting}


def decode_proposal(raw: bytes) -> dict:
    index = struct.unpack_from("<Q", raw, 40)[0]
    tag = raw[48]
    off = 49
    ts = None
    if tag in _STATUS_HAS_TS:
        ts = struct.unpack_from("<q", raw, off)[0]
        off += 8
    off += 1  # bump
    approved = struct.unpack_from("<I", raw, off)[0]
    return {"index": index, "status": PROPOSAL_STATUS.get(tag, f"tag{tag}"),
            "status_ts": ts, "approvals": approved}


_MULTISIG_DISCRIMINATOR = hashlib.sha256(b"account:Multisig").digest()[:8]


def _settings_from_history(rpc: Rpc, authority: str, vault_indexes: int) -> str | None:
    try:
        sigs = rpc.call("getSignaturesForAddress", [authority, {"limit": 5}]) or []
        for sig in sigs:
            tx = rpc.call("getTransaction", [sig["signature"], {
                "encoding": "jsonParsed", "commitment": "confirmed",
                "maxSupportedTransactionVersion": 0}])
            keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
            for k in keys:
                pk = k.get("pubkey") if isinstance(k, dict) else k
                if not pk or pk == authority:
                    continue
                for idx in range(vault_indexes):
                    if squads_vault_pda(pk, idx) == authority:
                        return pk
    except Exception:
        pass
    return None


def find_squads_settings(rpc: Rpc, authority: str, vault_indexes: int = 4) -> str | None:
    """Reverse-lookup: which Squads v4 multisig OWNS this authority (vault)?

    Fast path first: the vault's own transaction history — any tx executed
    through the vault references its Multisig settings account, so reading
    one transaction's account keys usually answers in two RPC calls.
    Fallback: list every v4 Multisig account (pubkeys only) and derive each
    one's first few vault PDAs locally until one matches (heavy on public
    RPC endpoints; instant local hashing once fetched)."""
    fast = _settings_from_history(rpc, authority, vault_indexes)
    if fast:
        return fast
    accounts = rpc.call("getProgramAccounts", [SQUADS_V4, {
        "encoding": "base64",
        "dataSlice": {"offset": 0, "length": 0},
        "filters": [{"memcmp": {"offset": 0,
                                "bytes": b58encode(_MULTISIG_DISCRIMINATOR)}}],
    }]) or []
    for acc in accounts:
        settings = acc["pubkey"]
        for idx in range(vault_indexes):
            if squads_vault_pda(settings, idx) == authority:
                return settings
    return None


def queued_upgrades(rpc: Rpc, settings: str, programdata: str,
                    lookback: int = 12) -> list:
    """ACTIVE / APPROVED proposals on the multisig whose transaction touches
    this program's ProgramData — upgrades queued but not yet executed."""
    raw = rpc.account_b64(settings)
    if raw is None:
        return []
    ms = decode_multisig(raw)
    pd_bytes = b58decode(programdata)
    out = []
    top = ms["transaction_index"]
    for i in range(max(1, top - lookback + 1), top + 1):
        praw = rpc.account_b64(squads_proposal_pda(settings, i))
        if praw is None:
            continue
        prop = decode_proposal(praw)
        if prop["status"] not in ("Active", "Approved"):
            continue
        traw = rpc.account_b64(squads_transaction_pda(settings, i))
        prop["is_upgrade"] = bool(traw and pd_bytes in traw)
        prop["approvals_required"] = ms["threshold"]
        out.append(prop)
    return out


# ── Main resolve ─────────────────────────────────────────────────────────────

def resolve(program_id: str, rpc_url: str = DEFAULT_RPC,
            deep: bool = True) -> dict:
    """Full upgrade-control report for a program id.

    deep=True adds the Squads reverse-lookup and queued-upgrade scan
    (a getProgramAccounts call plus a handful of account reads)."""
    rpc = Rpc(rpc_url)
    out: dict = {"program": program_id, "checked_at":
                 datetime.now(timezone.utc).isoformat(timespec="seconds")}

    info = rpc.account_parsed(program_id)
    if info is None:
        out["error"] = "program account not found"
        return out
    owner = info.get("owner")
    if owner in (BPFLOADER_V1, BPFLOADER_V2):
        out.update(loader="legacy", upgradable=False,
                   verdict="Immutable: legacy loader, nobody can upgrade this program.")
        return out
    if owner == LOADER_V4:
        out.update(loader="loader-v4", error="LoaderV4 not yet supported")
        return out
    if owner != BPFLOADER_UPGRADEABLE:
        out.update(error=f"unknown loader: {owner}")
        return out

    out["loader"] = "upgradeable"
    programdata = ((info.get("data") or {}).get("parsed") or {}).get("info", {}).get("programData")
    if not programdata:
        out["error"] = "programData not found"
        return out
    out["programdata"] = programdata

    header = rpc.account_b64(programdata, 0, 45)
    if header is None or len(header) < 45:
        out["error"] = "programData header read failed"
        return out
    slot = struct.unpack_from("<Q", header, 4)[0]
    out["last_deploy_slot"] = slot
    ts = None
    try:
        ts = rpc.call("getBlockTime", [slot])
    except RuntimeError:
        sigs = rpc.call("getSignaturesForAddress", [programdata, {"limit": 100}]) or []
        match = next((s for s in sigs if s.get("slot") == slot), None) or (sigs[0] if sigs else None)
        if match:
            ts = match.get("blockTime")
    if ts:
        out["last_deploy_time"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")

    if header[12] != 1:
        out.update(upgradable=False,
                   verdict="Immutable: the upgrade authority has been burned.")
        return out

    authority = b58encode(header[13:45])
    out.update(upgradable=True, upgrade_authority=authority)

    if is_on_curve(b58decode(authority)):
        out["authority_kind"] = "wallet"
        out["verdict"] = ("A PLAIN WALLET KEY can upgrade this program - "
                          "no multisig, no timelock stands between that key and the code.")
        return out

    out["authority_kind"] = "pda"
    if not deep:
        out["verdict"] = "Program-controlled authority (PDA); rerun with deep=True to identify it."
        return out

    settings = find_squads_settings(rpc, authority)
    if settings is None:
        auth_acc = rpc.account_parsed(authority)
        out["authority_owner_program"] = auth_acc.get("owner") if auth_acc else None
        out["verdict"] = ("Authority is a PDA not derived from any Squads v4 multisig "
                          "vault - governed by another program (see authority_owner_program).")
        return out

    out["authority_kind"] = "squads_v4_vault"
    out["multisig"] = {"settings": settings}
    raw = rpc.account_b64(settings)
    if raw:
        ms = decode_multisig(raw)
        out["multisig"].update(
            threshold=ms["threshold"],
            voting_members=ms["voting_members"],
            total_members=ms["members"],
            threshold_str=f"{ms['threshold']}/{ms['voting_members'] or ms['members']}",
            timelock_s=ms["time_lock_s"])
    queued = queued_upgrades(rpc, settings, programdata)
    out["queued_proposals"] = queued
    n_upg = sum(1 for q in queued if q.get("is_upgrade"))
    tl = out["multisig"].get("timelock_s") or 0
    out["verdict"] = (
        f"Squads v4 multisig {out['multisig'].get('threshold_str', '?')}"
        + (f" with a {tl // 3600}h timelock" if tl else " with NO timelock")
        + (f"; WARNING: {n_upg} upgrade proposal(s) already queued." if n_upg
           else "; no upgrade currently queued."))
    return out
