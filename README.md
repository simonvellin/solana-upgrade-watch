# solana-upgrade-watch

Every upgradeable Solana program has an upgrade authority that can replace
its code, including the code holding your deposits. This tool resolves,
for any program id, exactly who that is and what stands between them and
your funds:

- the **upgrade authority** — and whether it is a plain wallet key, burned
  (immutable), or program-controlled;
- for **Squads v4 multisigs**: the real threshold (approvals required /
  voting members — the Squads UI convention), member count, and **timelock**;
- the **last deploy** (slot and time);
- **upgrades already queued but not executed** — active or approved
  multisig proposals whose transaction touches the program's code.

Built and used in production by [YieldCompass](https://yieldcompass.fi),
the independent risk-rating platform for Solana DeFi, where these exact
resolutions power the Code Status section of every rated strategy.

## Install

```
pip install git+https://github.com/simonvellin/solana-upgrade-watch
```

Or just clone — it is pure standard-library Python (3.10+), no dependencies.

## Use

```
$ solana-upgrade-watch KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD

program           KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD
loader            upgradeable
upgradable        True
upgrade_authority GzFgdRJXmawPhGeBsyRCDLx4jAKPsvbUqoqitzppkzkW
authority_kind    squads_v4_vault
last_deploy_slot  440486775
last_deploy_time  2026-08-20T14:04:04+00:00
multisig          6hhBGCtmg7tPWUSgp3LG6X2rsmYWAc4tNsA6G4CnfQbM
threshold         5/10 (10 members total)
timelock          24h

Squads v4 multisig 5/10 with a 24h timelock; no upgrade currently queued.
```

Watch mode — poll and alert on any change (authority rotation, deploy,
new queued proposal):

```
solana-upgrade-watch <PROGRAM_ID> --watch 300 --webhook https://hooks.slack.com/services/...
```

Options: `--rpc <url>` (recommended: your own endpoint; the default public
RPC rate-limits the Squads scan), `--json`, `--shallow` (skip multisig
identification).

As a library:

```python
from upgrade_watch import resolve
report = resolve("KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD",
                 rpc_url="https://your-rpc")
print(report["verdict"])
```

## How it works

Everything is resolved from raw on-chain accounts — no third-party APIs,
no indexer, nothing stored, nothing phoned home:

1. The program account names its ProgramData; its header carries the last
   deploy slot and the upgrade authority (or its absence).
2. An authority that lies **on the ed25519 curve** is a plain keypair —
   a single hot key can upgrade the program. Off-curve means a PDA.
3. Squads v4 vault PDAs cannot be reversed mathematically, so the multisig
   is identified either from the vault's transaction history (fast path)
   or by deriving every v4 multisig's vault PDAs locally until one
   matches (fallback; pure local hashing).
4. Threshold, voting members, and timelock are decoded from the raw
   Multisig account; queued upgrades come from walking recent proposal
   PDAs and checking whether their transaction references the ProgramData.

## Honest limitations

- LoaderV4 programs are not yet supported.
- Governance systems other than Squads v4 (DAO programs, Squads v3,
  custom governors) are reported as "PDA governed by another program"
  with the owning program surfaced — identification, not decoding.
- The queued-upgrade scan walks the most recent proposals (default 12);
  a proposal older than that window is not shown.
