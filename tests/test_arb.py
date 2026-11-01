import json
from dataclasses import replace
from pathlib import Path

import pytest

from tbf.arb.config import load_config
from tbf.arb.opportunity import EpisodeTracker, Opportunity
from tbf.arb.quotes import EXACT_IN, EXACT_OUT, check_all
from tbf.arb.report import build_report
from tbf.arb.scanner import Scanner
from tbf.arb.store import ArbStore
from tbf.onchain.abi import decode_string, decode_words, encode_call, selector
from tbf.onchain.keccak import keccak256
from tbf.onchain.rpc import Rpc

ROOT = Path(__file__).resolve().parent.parent
CHAIN_IDS = {"arbitrum": 42161, "base": 8453, "optimism": 10}


def test_keccak_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    # 複数ブロック (136 バイト超) の入力
    assert keccak256(b"a" * 300) != keccak256(b"a" * 299)
    assert selector("transfer(address,uint256)").hex() == "a9059cbb"
    assert selector("decimals()").hex() == "313ce567"


def test_abi_roundtrip():
    data = encode_call("f(address,uint24)", ["address", "uint24"], ["0x" + "11" * 20, 500])
    assert data.startswith("0x" + selector("f(address,uint24)").hex())
    assert decode_words("0x" + data[10:]) == [int("11" * 20, 16), 500]
    with pytest.raises(ValueError):
        encode_call("f(uint24)", ["uint24"], [1 << 24])
    s = "USDC".encode()
    raw = (32).to_bytes(32, "big") + len(s).to_bytes(32, "big") + s.ljust(32, b"\0")
    assert decode_string("0x" + raw.hex()) == "USDC"


class FakeChainRpc:
    """1 チェーン分の JSON-RPC を価格 price (USDC/WETH) で真似る"""

    def __init__(self, name, cfg, price):
        self.name, self.cfg, self.price = name, cfg, price
        chain = next(c for c in cfg.chains if c.name == name)
        self.tokens = {v.lower(): k for k, v in chain.tokens.items()}
        self.block = 100
        self.fail = False

    def answer(self, method, params):
        if method == "eth_chainId":
            return hex(CHAIN_IDS[self.name])
        if method == "eth_blockNumber":
            self.block += 1
            return hex(self.block)
        if method == "eth_gasPrice":
            return hex(10_000_000)  # 0.01 gwei
        to, data = params[0]["to"].lower(), params[0]["data"]
        sel = data[2:10]
        if sel == selector("decimals()").hex():
            return "0x" + (18 if self.tokens[to] == "WETH" else 6).to_bytes(32, "big").hex()
        if sel == selector("symbol()").hex():
            s = self.tokens[to].encode()
            return "0x" + ((32).to_bytes(32, "big") + len(s).to_bytes(32, "big") + s.ljust(32, b"\0")).hex()
        amount = decode_words("0x" + data[10:])[2]
        fee = 0.9995
        if sel == selector(EXACT_IN).hex():  # WETH -> USDC
            out = int(amount / 1e18 * self.price * fee * 1e6)
        elif sel == selector(EXACT_OUT).hex():  # USDC -> WETH (exact out)
            out = int(amount / 1e18 * self.price / fee * 1e6)
        else:
            raise AssertionError(sel)
        return "0x" + b"".join(v.to_bytes(32, "big") for v in (out, 0, 1, 80_000)).hex()


class FakeHttp:
    def __init__(self, chains):
        self.chains = chains  # url -> FakeChainRpc

    async def request(self, method, url, headers=None, body=None):
        chain = self.chains[url]
        if chain.fail:
            return 503, "down"
        reqs = json.loads(body)
        return 200, json.dumps([{"jsonrpc": "2.0", "id": r["id"], "result": chain.answer(r["method"], r["params"])} for r in reqs])


def setup(tmp_path, prices):
    cfg = load_config(ROOT / "arb.toml")
    fakes = {c.rpc: FakeChainRpc(c.name, cfg, prices[c.name]) for c in cfg.chains}
    http = FakeHttp(fakes)
    rpcs = {c.name: Rpc(http, c.rpc) for c in cfg.chains}
    by_name = {f.name: f for f in fakes.values()}
    return cfg, rpcs, ArbStore(tmp_path / "arb.sqlite"), by_name


async def test_check_fills_decimals_and_rejects_wrong_chain(tmp_path):
    cfg, rpcs, _, fakes = setup(tmp_path, {"arbitrum": 3000, "base": 3000, "optimism": 3000})
    checked, lines = await check_all(rpcs, cfg)
    assert all(c.decimals == {"WETH": 18, "USDC": 6} for c in checked.chains)
    assert any("arbitrum: block" in l for l in lines)
    CHAIN_IDS["base"] = 1
    try:
        with pytest.raises(ValueError, match="chain id mismatch"):
            await check_all(rpcs, cfg)
    finally:
        CHAIN_IDS["base"] = 8453


async def test_scanner_records_episodes_and_report(tmp_path):
    # base が 1% 高い -> base で売って arbitrum / optimism で買い戻すと儲かる
    cfg, rpcs, store, fakes = setup(tmp_path, {"arbitrum": 3000, "base": 3030, "optimism": 3000})
    cfg, _ = await check_all(rpcs, cfg)
    t = [1_000_000]
    scanner = Scanner(cfg, rpcs, store, clock=lambda: t[0], log=lambda m: None)
    for _ in range(3):
        await scanner.poll_once()
        t[0] += 3000
    fakes["base"].price = 3000  # 価格差が消える
    await scanner.poll_once()

    eps = store.query("SELECT * FROM episodes ORDER BY sell_chain, buy_chain")
    assert {(e["sell_chain"], e["buy_chain"]) for e in eps} == {("base", "arbitrum"), ("base", "optimism")}
    e = eps[0]
    assert e["polls"] == 3 and e["best_size"] == 3.0
    # 3 ETH * 30 USD の差から手数料 0.05% x2 とガス・リバランスを引いた値
    assert 75 < e["max_net_usd"] < 81
    assert store.query("SELECT COUNT(*) FROM opportunities")[0][0] > 0

    report = build_report(store, 0, min_persist_polls=2)
    assert "base -> arbitrum" in report and "仮想利益" in report


async def test_scanner_survives_rpc_failure(tmp_path):
    cfg, rpcs, store, fakes = setup(tmp_path, {"arbitrum": 3000, "base": 3000, "optimism": 3000})
    cfg, _ = await check_all(rpcs, cfg)
    fakes["optimism"].fail = True
    cfg = replace(cfg, poll_interval_sec=0)
    scanner = Scanner(cfg, rpcs, store, clock=lambda: 1, log=lambda m: None)
    await scanner.run(max_polls=2)
    errors = store.query("SELECT COUNT(*) FROM polls WHERE error IS NOT NULL")[0][0]
    ok = store.query("SELECT COUNT(*) FROM polls WHERE error IS NULL")[0][0]
    assert errors == 2 and ok == 4
    assert not store.query("SELECT * FROM episodes")  # 価格差なし


def test_episode_tracker():
    tr = EpisodeTracker(min_profit_usd=1)
    o = lambda net: Opportunity("a", "b", 1, 0, 0, net, 0, net)  # noqa: E731
    assert tr.update(1, {("a", "b"): o(5)}) == []
    assert tr.update(2, {("a", "b"): o(7)}) == []
    (ep,) = tr.update(3, {("a", "b"): o(0.5)})
    assert (ep.start, ep.end, ep.polls, ep.first_net_usd, ep.max_net_usd) == (1, 2, 2, 5, 7)
