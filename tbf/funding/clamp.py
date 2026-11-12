"""ファンディング上限の張り付きと、会場間の上限差エッジの検出

TRA-41 (INIT) で実測した型をここに規則として落とす。発見はこうだった:

チェーンが止まって各取引所の入出金が閉じると、現物が会場ごとの別々の閉じたプールに
なる。会場の index は自分のプールを参照するので釣り上がり、ファンディングが上限に
張り付く。**そして上限値は会場ごとに違う** (INIT では Binance ±2.0%/h、OKX ±1.0%/h、
Hyperliquid ±0.5%/h)。

最初はこれを「2 会場以上が上限に張り付き、かつ上限値が違う」という条件で書いた。
ところが 48 時間の確定履歴で測り直すと、long Binance / short Hyperliquid は 27 時間中
**26 時間勝っているのに、両者が同時に張り付いていた時間は 3 時間しかなかった**。
条件が厳しすぎて、エッジが出ている時間の 9 割を見逃す。

効いていたのは「両者張り付き」ではなく、**片側だけの非対称**だった:

    こちらが払う側の脚 (adverse leg) が自分の上限に張り付いている間、その脚の
    支払い要求はそれ以上大きくなれない。相手の脚がその上限を越えて払っていれば、
    差は「払う側の都合では詰められない」。

この符号 (repo の約束: apr は **ロングが払うとき +**) で書くと:

  * 会場 P が**下限** (-cap_P) に張り付いている → P は良い**ショート脚**。
    ショートとして払う額は cap_P で止まる。apr_L < -cap_P の会場 L をロングすれば
    差 = apr_P - apr_L > 0 で、P 側はもう悪化できない。
  * 会場 P が**上限** (+cap_P) に張り付いている → P は良い**ロング脚**。
    ロングとして払う額は cap_P で止まる。apr_S > +cap_P の会場 S をショートすればよい。

両方が張り付いている区間だけは完全に決定論的で、そのとき差はちょうど
`cap_loose - cap_tight` になる (`floor_apr`)。ここはどちらの会場も動けない。

上限は会場ごとではなく**銘柄ごと**に違う (Binance の BTCUSDT は ±0.3%/8h、
INITUSDT は ±2.0%/1h)。だから上限は (会場, 銘柄) で持つ。

上限の出どころは 3 つあり、強い順に:
  api      会場の API が公開している値 (binance fundingInfo / paradex markets)
  config   venues.toml に一次情報の出典つきで書いた値
  inferred 確定履歴から推定した値 (同じ値に何時間も張り付き、それを超えない)
API が出す会場で設定値と食い違ったら check を失敗させる。建ての検証と同じ方針で、
「レジストリにそう書いてある」を信用しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from tbf.funding.config import FundingConfig
from tbf.funding.venues import CapQuote, VenueSnapshot, annualize

API = "api"
CONFIG = "config"
INFERRED = "inferred"


@dataclass(frozen=True)
class Cap:
    """(会場, 銘柄) のファンディング上限。rate はその会場の建てぶんの分数 (絶対値)"""

    venue: str
    symbol: str
    rate: float
    interval_hours: float
    source: str
    note: str = ""

    @property
    def apr(self) -> float:
        return annualize(self.rate, self.interval_hours)

    @property
    def key(self) -> tuple[str, str]:
        return (self.venue, self.symbol)


@dataclass(frozen=True)
class Pin:
    """ある会場・銘柄が今、上限に張り付いているか"""

    cap: Cap
    apr: float
    # 張り付いている向き。+1 = 上限 (ロングが払う側) / -1 = 下限 / 0 = 張り付いていない
    side: int

    @property
    def pinned(self) -> bool:
        return self.side != 0


@dataclass(frozen=True)
class ClampEdge:
    """上限の非対称から出ている差。gross は Spread と同じ「ショート側 - ロング側」"""

    symbol: str
    short_venue: str
    long_venue: str
    edge_apr: float
    # 払う側の脚 (張り付いている会場) と、その上限
    pinned_venue: str
    tight_cap_apr: float
    # 相手の脚の上限 (分かっていれば)
    loose_cap_apr: Optional[float]
    both_pinned: bool
    # 両者が張り付いている間だけ成り立つ決定論的な下限 = loose - tight
    floor_apr: Optional[float]
    # 両脚とも売買対象か (参照のみの会場が入っていれば False)
    tradable: bool

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.symbol, self.short_venue, self.long_venue)


def from_quote(q: CapQuote) -> Cap:
    return Cap(q.venue, q.symbol, q.rate, q.interval_hours, API, q.note)


def resolve_caps(
    cfg: FundingConfig,
    api_caps: Optional[dict[tuple[str, str], CapQuote]] = None,
    inferred: Optional[dict[tuple[str, str], Cap]] = None,
) -> dict[tuple[str, str], Cap]:
    """(会場, 銘柄) -> 使う上限。api > config > inferred の順で採る"""
    out: dict[tuple[str, str], Cap] = {}
    for v in cfg.venues:
        for sym in cfg.symbols:
            if not v.covers(sym):
                continue
            key = (v.name, sym)
            quote = (api_caps or {}).get(key)
            hit = from_quote(quote) if quote is not None else None
            if hit is None and v.funding_cap > 0:
                hit = Cap(v.name, sym, v.funding_cap, v.funding_interval_hours, CONFIG,
                          v.funding_cap_source)
            if hit is None:
                hit = (inferred or {}).get(key)
            if hit is not None:
                out[key] = hit
    return out


def pins(
    snapshots: Iterable[VenueSnapshot],
    caps: dict[tuple[str, str], Cap],
    tol_frac: float = 0.001,
) -> dict[tuple[str, str], Pin]:
    """上限に張り付いている (会場, 銘柄) を拾う

    tol_frac は上限に対する相対の許容。会場が返す値は丸めで上限をわずかに
    下回ることがあるので、絶対値ではなく比で見る。
    """
    out: dict[tuple[str, str], Pin] = {}
    for snap in snapshots:
        for sym, ss in snap.symbols.items():
            cap = caps.get((snap.venue, sym))
            if cap is None or cap.rate <= 0:
                continue
            side = 0
            if abs(ss.raw_rate) >= cap.rate * (1 - tol_frac):
                side = 1 if ss.raw_rate > 0 else -1
            out[(snap.venue, sym)] = Pin(cap, ss.funding_apr, side)
    return out


def detect(
    snapshots: list[VenueSnapshot],
    cfg: FundingConfig,
    caps: dict[tuple[str, str], Cap],
    min_edge_apr: Optional[float] = None,
    tol_frac: Optional[float] = None,
) -> list[ClampEdge]:
    """上限差エッジを全ペア・全銘柄について拾う

    `evaluate()` と違って参照のみの会場も対象にする。歪みの発端になるのは
    index が釣り上がっている会場で、それが参照会場 (CEX) 側であることが
    INIT では実際に起きた。売買できるかは tradable で区別して残し、
    「規則としては検出できていたのに会場の都合で取れなかった」が後から分かるようにする。
    """
    min_edge = cfg.min_clamp_edge_apr if min_edge_apr is None else min_edge_apr
    tol = cfg.clamp_tol_frac if tol_frac is None else tol_frac
    by_venue = {s.venue: s for s in snapshots}
    found = pins(snapshots, caps, tol)
    aprs = {
        (s.venue, sym): ss.funding_apr for s in snapshots for sym, ss in s.symbols.items()
    }
    reference = {v.name for v in cfg.venues if v.reference_only}
    edges: list[ClampEdge] = []

    for (venue, sym), pin in found.items():
        if not pin.pinned or venue not in by_venue:
            continue
        for other in by_venue:
            if other == venue:
                continue
            apr = aprs.get((other, sym))
            if apr is None:
                continue
            # 張り付いている会場 P は、下限なら払う側 = ショート脚、上限ならロング脚。
            # 相手の脚は P の張り付き値を越えて払っていなければエッジにならない。
            if pin.side < 0:
                short_venue, long_venue, edge = venue, other, pin.apr - apr
            else:
                short_venue, long_venue, edge = other, venue, apr - pin.apr
            if edge < min_edge:
                continue
            other_pin = found.get((other, sym))
            other_cap = caps.get((other, sym))
            both = bool(other_pin and other_pin.pinned and other_pin.side == pin.side)
            floor = None
            if both and other_cap is not None and other_cap.apr > pin.cap.apr:
                floor = other_cap.apr - pin.cap.apr
            edges.append(
                ClampEdge(
                    symbol=sym,
                    short_venue=short_venue,
                    long_venue=long_venue,
                    edge_apr=edge,
                    pinned_venue=venue,
                    tight_cap_apr=pin.cap.apr,
                    loose_cap_apr=other_cap.apr if other_cap else None,
                    both_pinned=both,
                    floor_apr=floor,
                    tradable=short_venue not in reference and long_venue not in reference,
                )
            )
    edges.sort(key=lambda e: -e.edge_apr)
    return edges


def infer_caps(
    history: dict[tuple[str, str], list], min_repeats: int = 3
) -> dict[tuple[str, str], Cap]:
    """確定履歴から上限を推定する (上限を公開していない会場のため)

    Hyperliquid / dYdX / Lighter は上限を API に出さない。だが張り付いている会場は
    「同じ値が何時間も続き、その値を一度も超えない」という形で履歴に残る
    (INIT の Hyperliquid は premium が -9% 台でも確定レートが -0.005 のまま並んだ)。
    これを上限の推定値として拾う。推定なので source は inferred にして、
    api / config より弱い扱いにする。
    """
    out: dict[tuple[str, str], Cap] = {}
    for (venue, symbol), points in history.items():
        if not points:
            continue
        extreme = max(abs(p.rate) for p in points)
        if extreme <= 0:
            continue
        # 絶対値が最大の値にちょうど並んでいる区間の数
        repeats = sum(1 for p in points if abs(abs(p.rate) - extreme) < 1e-12)
        if repeats < min_repeats:
            continue
        hours = points[0].hours or 1.0
        out[(venue, symbol)] = Cap(
            venue, symbol, extreme, hours, INFERRED,
            f"確定履歴 {len(points)} 区間のうち {repeats} 区間が絶対値 {extreme:.6g} で並び、"
            f"これを超えた区間が無い",
        )
    return out


@dataclass
class ClampEpisode:
    """上限差エッジが出ていた連続区間"""

    symbol: str
    short_venue: str
    long_venue: str
    start: int
    end: int
    polls: int
    both_pinned_polls: int
    first_edge_apr: float
    max_edge_apr: float
    max_floor_apr: Optional[float]
    tight_cap_apr: float
    loose_cap_apr: Optional[float]
    tradable: bool

    @property
    def duration_sec(self) -> float:
        return (self.end - self.start) / 1000


@dataclass
class ClampTracker:
    """`EpisodeTracker` と同じ考え方。一瞬の張り付きは取れないので継続時間を見る"""

    min_edge_apr: float
    open: dict[tuple[str, str, str], ClampEpisode] = field(default_factory=dict)

    def update(self, now: int, edges: list[ClampEdge]) -> list[ClampEpisode]:
        best: dict[tuple[str, str, str], ClampEdge] = {}
        for e in edges:
            cur = best.get(e.key)
            if cur is None or e.edge_apr > cur.edge_apr:
                best[e.key] = e
        closed = []
        for key in list(self.open):
            if key not in best:
                closed.append(self.open.pop(key))
        for key, e in best.items():
            ep = self.open.get(key)
            if ep is None:
                self.open[key] = ClampEpisode(
                    e.symbol, e.short_venue, e.long_venue, now, now, 1,
                    int(e.both_pinned), e.edge_apr, e.edge_apr, e.floor_apr,
                    e.tight_cap_apr, e.loose_cap_apr, e.tradable,
                )
                continue
            ep.end, ep.polls = now, ep.polls + 1
            ep.both_pinned_polls += int(e.both_pinned)
            ep.max_edge_apr = max(ep.max_edge_apr, e.edge_apr)
            if e.floor_apr is not None:
                ep.max_floor_apr = max(ep.max_floor_apr or e.floor_apr, e.floor_apr)
        return closed

    def flush(self) -> list[ClampEpisode]:
        out = list(self.open.values())
        self.open.clear()
        return out
