# -*- coding: utf-8 -*-
import json
# time_precision: 'second' | 'hour' | 'date'
# ref: 'binance' (spot USDT) | 'hyperliquid' (perp) | None
E=[]
def ev(**k): E.append(k)

# ---------------- bitbank (publishedAt = microCMS, second precision, UTC) ----------------
ev(id='bb-2024-11-19-SOL-CYBER', venue='bitbank', kind='new_listing',
   title='【予告】ソラナ（SOL）、サイバー（CYBER）の取扱い開始のお知らせ',
   announce_utc='2024-11-19T03:00:05Z', time_precision='second',
   start_date_jst='2024-11-21', start_utc='2024-11-21T03:00:00Z', start_precision='minute',
   start_note='予告本文の提供開始表（12:00〜 新規上場モード／14:00〜 成行・販売所）より 12:00 JST を採用',
   listing_type='取引所（板）＋販売所＋貸して増やす',
   source='https://bitbank.cc/blog/articles/790023809',
   assets=[dict(ticker='SOL',name='ソラナ',chain='Solana',first_in_jp=False,ref='binance',ref_symbol='SOLUSDT',hl_perp=True),
           dict(ticker='CYBER',name='サイバー',chain='OP Mainnet',first_in_jp=True,ref='binance',ref_symbol='CYBERUSDT',hl_perp=True)])
ev(id='bb-2024-12-03-RENDER', venue='bitbank', kind='migration', is_migration=True,
   title='【再予告】レンダー（RENDER）の取扱い開始のお知らせ（RNDR→RENDER 移行）',
   announce_utc='2024-12-03T06:13:45Z', time_precision='second',
   start_date_jst='2024-12-11', start_utc=None, start_precision='date',
   listing_type='取引所（板）＋販売所', source='https://bitbank.cc/blog/articles/197102182',
   note='RNDR(Ethereum)からRENDER(Solana)へのトークン移行。新規資産ではない。'
        '当初予告は2024-11-27（延期記事本文「昨日お知らせいたしました」より）だが当該記事は公告一覧から取得不能＝unknown',
   assets=[dict(ticker='RENDER',name='レンダー',chain='Solana',first_in_jp=False,ref='binance',ref_symbol='RENDERUSDT',hl_perp=True)])
ev(id='bb-2025-01-31-TRX', venue='bitbank', kind='new_listing',
   title='【再予告】トロン（TRX）の取扱い開始のお知らせ',
   announce_utc='2025-01-31T03:00:05Z', time_precision='second',
   start_date_jst='2025-02-03', start_utc=None, start_precision='date',
   listing_type='取引所（板）＋販売所＋貸して増やす', source='https://bitbank.cc/blog/articles/615327154',
   note='当初予告（2025-01-27 14:00 JST 開始予定）は同日13:30に延期。当初予告記事は公告一覧から取得不能＝unknown。'
        'ここで測るのは「再予告」の発表。',
   assets=[dict(ticker='TRX',name='トロン',chain='Tron',first_in_jp=False,ref='binance',ref_symbol='TRXUSDT',hl_perp=True)])
ev(id='bb-2025-02-28-LPT', venue='bitbank', kind='new_listing',
   title='【予告】ライブピア（LPT）の取扱い開始のお知らせ',
   announce_utc='2025-02-28T03:00:05Z', time_precision='second',
   start_date_jst='2025-03-04', start_utc=None, start_precision='date',
   listing_type='取引所（板）＋販売所', source='https://bitbank.cc/blog/articles/234926644',
   assets=[dict(ticker='LPT',name='ライブピア',chain='Arbitrum/Ethereum',first_in_jp=True,ref='binance',ref_symbol='LPTUSDT',hl_perp=False)])
ev(id='bb-2025-05-09-ATOM', venue='bitbank', kind='new_listing',
   title='【予告】コスモス（ATOM）の取扱い開始のお知らせ',
   announce_utc='2025-05-09T03:00:05Z', time_precision='second',
   start_date_jst='2025-05-13', start_utc=None, start_precision='date',
   listing_type='取引所（板）＋販売所', source='https://bitbank.cc/blog/articles/547384416',
   assets=[dict(ticker='ATOM',name='コスモス',chain='Cosmos',first_in_jp=False,ref='binance',ref_symbol='ATOMUSDT',hl_perp=True)])
ev(id='bb-2025-08-08-SUI', venue='bitbank', kind='new_listing',
   title='【予告】スイ（SUI）の取扱い開始のお知らせ',
   announce_utc='2025-08-08T06:00:04Z', time_precision='second',
   start_date_jst='2025-08-14', start_utc=None, start_precision='date',
   listing_type='取引所（板）＋販売所', source='https://bitbank.cc/blog/articles/180660873',
   assets=[dict(ticker='SUI',name='スイ',chain='Sui',first_in_jp=False,ref='binance',ref_symbol='SUIUSDT',hl_perp=True)])
ev(id='bb-2025-09-11-SKY', venue='bitbank', kind='migration', is_migration=True,
   title='【予告】MKRの取扱廃止およびSKYへの移行に関するお知らせ',
   announce_utc='2025-09-11T08:56:15Z', time_precision='second',
   start_date_jst='2025-09-18', start_utc=None, start_precision='date',
   listing_type='取引所（板）＋販売所', source='https://bitbank.cc/blog/articles/647148499',
   note='MKR→SKY のトークン移行。新規資産ではない。',
   assets=[dict(ticker='SKY',name='スカイ',chain='Ethereum',first_in_jp=True,ref='binance',ref_symbol='SKYUSDT',hl_perp=True)])
ev(id='bb-2025-04-11-DOGESOL-margin', venue='bitbank', kind='new_service_type',
   title='信用取引におけるDOGE・SOLの取扱い開始のお知らせ',
   announce_utc='2025-04-11T03:00:05Z', time_precision='second',
   start_date_jst='2025-04-14', start_utc=None, start_precision='date',
   listing_type='信用取引（レバレッジ）', source='https://bitbank.cc/blog/articles/763325546',
   note='既に現物で取扱済の銘柄に対する取引種類の追加。新規取扱ではない。',
   assets=[dict(ticker='DOGE',name='ドージコイン',chain='Dogecoin',first_in_jp=False,ref='binance',ref_symbol='DOGEUSDT',hl_perp=True),
           dict(ticker='SOL',name='ソラナ',chain='Solana',first_in_jp=False,ref='binance',ref_symbol='SOLUSDT',hl_perp=True)])

# ---------------- Coincheck (STUDIO publishedAt, second precision, UTC) ----------------
ev(id='cc-2024-12-09-BC', venue='Coincheck', kind='new_listing',
   title='Coincheck販売所におけるブラッドクリスタル（BC）取扱い開始のお知らせ',
   announce_utc='2024-12-09T08:00:08Z', time_precision='second',
   start_date_jst='2024-12-12', start_utc=None, start_precision='date',
   listing_type='販売所のみ', source='https://corporate.coincheck.com/news/SE1yBt1k',
   assets=[dict(ticker='BC',name='ブラッドクリスタル',chain='unknown',first_in_jp=True,ref=None,ref_symbol=None,
                hl_perp=False,no_ref_reason='Binance現物・Hyperliquid perp いずれにも存在しない（exchangeInfo/メタに該当なし）')])
ev(id='cc-2025-01-15-DOGE', venue='Coincheck', kind='new_listing',
   title='Coincheck、ドージコイン（DOGE）取扱い開始のお知らせ（販売所）',
   announce_utc='2025-01-15T05:31:46Z', time_precision='second',
   start_date_jst='2025-01-15', start_utc=None, start_precision='date',
   listing_type='販売所のみ', source='https://corporate.coincheck.com/news/GrSm0knj',
   assets=[dict(ticker='DOGE',name='ドージコイン',chain='Dogecoin',first_in_jp=False,ref='binance',ref_symbol='DOGEUSDT',hl_perp=True)])
ev(id='cc-2025-01-20-DOGE-exch', venue='Coincheck', kind='new_service_type',
   title='Coincheck取引所におけるドージコイン（DOGE）取扱い開始のお知らせ',
   announce_utc='2025-01-20T02:01:12Z', time_precision='second',
   start_date_jst='2025-01-20', start_utc=None, start_precision='date',
   listing_type='取引所（板）', source='https://corporate.coincheck.com/news/4JTQ6eiY',
   note='同社で既に販売所で取扱中の銘柄に板取引を追加。新規取扱ではない。',
   assets=[dict(ticker='DOGE',name='ドージコイン',chain='Dogecoin',first_in_jp=False,ref='binance',ref_symbol='DOGEUSDT',hl_perp=True)])
ev(id='cc-2025-04-07-LSK', venue='Coincheck', kind='migration', is_migration=True,
   title='旧Liskチェーンのリスク（LSK）の取扱い廃止、並びにEthereumチェーンのリスク（LSK）の付与および取扱い開始のお知らせ',
   announce_utc='2025-04-07T03:03:01Z', time_precision='second',
   start_date_jst='unknown', start_utc=None, start_precision='unknown',
   listing_type='販売所／取引所', source='https://corporate.coincheck.com/news/t-YYA9_1',
   note='LSKのチェーン移行。新規資産ではない。',
   assets=[dict(ticker='LSK',name='リスク',chain='Ethereum',first_in_jp=False,ref='binance',ref_symbol='LSKUSDT',hl_perp=False)])
ev(id='cc-2025-04-25-PEPE-MASK-MANA-GRT', venue='Coincheck', kind='new_listing',
   title='PEPE・MASK・MANA・GRTをCoincheck販売所および取引所で5月13日取扱い開始',
   announce_utc='2025-04-25T07:00:08Z', time_precision='second',
   start_date_jst='2025-05-13', start_utc=None, start_precision='date',
   listing_type='販売所＋取引所（板）', source='https://corporate.coincheck.com/news/jkLXbp0V',
   assets=[dict(ticker='PEPE',name='ペペ',chain='Ethereum',first_in_jp=False,ref='binance',ref_symbol='PEPEUSDT',hl_perp=True,hl_symbol='kPEPE'),
           dict(ticker='MASK',name='マスクネットワーク',chain='Ethereum',first_in_jp=True,ref='binance',ref_symbol='MASKUSDT',hl_perp=False),
           dict(ticker='MANA',name='ディセントラランド',chain='Ethereum',first_in_jp=False,ref='binance',ref_symbol='MANAUSDT',hl_perp=False),
           dict(ticker='GRT',name='ザ・グラフ',chain='Ethereum',first_in_jp=False,ref='binance',ref_symbol='GRTUSDT',hl_perp=False)])
ev(id='cc-2025-11-11-FPL', venue='Coincheck', kind='new_listing',
   title='ファンプラ（FPL）、本日11月11日よりCoincheck取引所・販売所にて取扱い開始',
   announce_utc='2025-11-11T03:16:00Z', time_precision='second',
   start_date_jst='2025-11-11', start_utc=None, start_precision='date',
   listing_type='販売所＋取引所（板）', source='https://corporate.coincheck.com/news/asblEnCn',
   note='IEO銘柄（2025-10-14にIEO開始を公表）',
   assets=[dict(ticker='FPL',name='ファンプラ',chain='unknown',first_in_jp=True,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='Binance現物・Hyperliquid perp いずれにも存在しない')])
ev(id='cc-2025-12-11-SOL', venue='Coincheck', kind='new_listing',
   title='ソラナ（SOL）をCoincheck販売所および取引所で12月11日取扱い開始',
   announce_utc='2025-12-11T06:55:49Z', time_precision='second',
   start_date_jst='2025-12-11', start_utc=None, start_precision='date',
   listing_type='販売所＋取引所（板）', source='https://corporate.coincheck.com/news/bBwbDn38',
   assets=[dict(ticker='SOL',name='ソラナ',chain='Solana',first_in_jp=False,ref='binance',ref_symbol='SOLUSDT',hl_perp=True)])
ev(id='cc-2026-01-29-TRX', venue='Coincheck', kind='new_listing',
   title='トロン（TRX）をCoincheck販売所および取引所で1月29日取扱い開始',
   announce_utc='2026-01-29T04:44:57Z', time_precision='second',
   start_date_jst='2026-01-29', start_utc=None, start_precision='date',
   listing_type='販売所＋取引所（板）', source='https://corporate.coincheck.com/news/66jDFyF4',
   assets=[dict(ticker='TRX',name='トロン',chain='Tron',first_in_jp=False,ref='binance',ref_symbol='TRXUSDT',hl_perp=True)])
ev(id='cc-2026-04-23-SUI', venue='Coincheck', kind='new_listing',
   title='スイ（SUI）をCoincheck販売所および取引所で4月23日取扱い開始',
   announce_utc='2026-04-23T03:02:02Z', time_precision='second',
   start_date_jst='2026-04-23', start_utc=None, start_precision='date',
   listing_type='販売所＋取引所（板）', source='https://corporate.coincheck.com/news/6A22fKmO',
   assets=[dict(ticker='SUI',name='スイ',chain='Sui',first_in_jp=False,ref='binance',ref_symbol='SUIUSDT',hl_perp=True)])
ev(id='cc-2026-08-31-ZPG', venue='Coincheck', kind='new_listing',
   title='ジパングコイン（ZPG）をCoincheck販売所で8月31日取扱い開始',
   announce_utc='2026-08-31T03:26:03Z', time_precision='second',
   start_date_jst='2026-08-31', start_utc=None, start_precision='date',
   listing_type='販売所のみ', source='https://corporate.coincheck.com/news/CZ-ymsqm',
   assets=[dict(ticker='ZPG',name='ジパングコイン',chain='unknown',first_in_jp=False,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='国内専用銘柄。Binance現物・Hyperliquid perp いずれにも存在しない')])
ev(id='cc-2026-10-08-NAC', venue='Coincheck', kind='new_listing',
   title='Coincheck販売所において10月8日よりNOT A HOTEL COIN（NAC）取扱い開始',
   announce_utc='2026-10-08T06:23:43Z', time_precision='second',
   start_date_jst='2026-10-08', start_utc=None, start_precision='date',
   listing_type='販売所のみ', source='https://corporate.coincheck.com/news/YlUTQPmi',
   assets=[dict(ticker='NAC',name='NOT A HOTEL COIN',chain='unknown',first_in_jp=False,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='国内専用銘柄。Binance現物・Hyperliquid perp いずれにも存在しない')])

# ---------------- GMOコイン ----------------
ev(id='gmo-2024-12-13-NAC', venue='GMOコイン', kind='new_listing',
   title='「NOT A HOTEL COIN（NAC）」取扱開始のお知らせ',
   announce_utc=None, announce_jst_hour='2024-12-13 10:00-10:59 JST', time_precision='hour',
   start_date_jst='2024-12-13', start_utc=None, start_precision='date',
   listing_type='販売所／取引所', source='https://coin.z.com/jp/news/2024/12/13841/',
   note='分までの発表時刻は取得不能（記事の time 要素は時までで分の位置に月が入る実装、RSS は直近10件のみ、sitemap lastmod は後日編集で上書き）',
   assets=[dict(ticker='NAC',name='NOT A HOTEL COIN',chain='unknown',first_in_jp=True,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='Binance現物・Hyperliquid perp いずれにも存在しない')])
ev(id='gmo-2026-01-15-SUI', venue='GMOコイン', kind='new_listing',
   title='「スイ（SUI）」取扱開始のお知らせ ～販売所・取引所・つみたて暗号資産にて、1月17日より取扱開始～',
   announce_utc='2026-01-15T02:25:36Z', time_precision='second',
   start_date_jst='2026-01-17', start_utc=None, start_precision='date',
   listing_type='販売所＋取引所（板）＋つみたて', source='https://coin.z.com/jp/news/2026/01/15442/',
   note='発表時刻は GMO 公式 RSS の pubDate「Thu, 15 Jan 2026 11:25:36」をJSTとして採用（WordPress が +0000 と誤表示。別記事で lastmod(UTC)=pubDate-9h が秒単位一致することを確認）',
   assets=[dict(ticker='SUI',name='スイ',chain='Sui',first_in_jp=False,ref='binance',ref_symbol='SUIUSDT',hl_perp=True)])
ev(id='gmo-2026-03-17-WILD', venue='GMOコイン', kind='new_listing',
   title='国内初！「ワイルダーワールド（WILD）」取扱開始のお知らせ ～取引所にて、3月23日より取扱開始～',
   announce_utc='2026-03-17T02:00:44Z', time_precision='second',
   start_date_jst='2026-03-23', start_utc=None, start_precision='date',
   listing_type='取引所（板）', source='https://coin.z.com/jp/news/2026/03/15697/',
   note='RSS pubDate 11:00:44 JST と sitemap lastmod 2026-03-17T02:00:44Z が秒単位一致',
   assets=[dict(ticker='WILD',name='ワイルダーワールド',chain='unknown',first_in_jp=True,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='Binance現物・Hyperliquid perp いずれにも存在しない')])
ev(id='gmo-2026-04-15-ZPG', venue='GMOコイン', kind='new_listing',
   title='「ジパングコイン（ZPG）」シリーズ取扱開始のお知らせ ～販売所にて、4月20日（月）より取扱開始～',
   announce_utc='2026-04-15T02:55:53Z', time_precision='second',
   start_date_jst='2026-04-20', start_utc=None, start_precision='date',
   listing_type='販売所のみ', source='https://coin.z.com/jp/news/2026/04/15790/',
   assets=[dict(ticker='ZPG',name='ジパングコイン',chain='unknown',first_in_jp=False,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='国内専用銘柄。Binance現物・Hyperliquid perp いずれにも存在しない')])
ev(id='gmo-2026-10-29-MNT-BNB', venue='GMOコイン', kind='new_listing',
   title='マントル（MNT）、ビルドアンドビルド（BNB）の取扱開始および取引ルールに関するお知らせ',
   announce_utc=None, announce_jst_hour='2026-10-29 14:00-14:59 JST', time_precision='hour',
   start_date_jst='2026-10-29', start_utc=None, start_precision='date',
   listing_type='販売所／取引所', source='https://coin.z.com/jp/news/2026/10/16693/',
   note='事前予告は一次情報で確認できず（news サイトマップ全419件を走査）。分までの時刻も取得不能',
   assets=[dict(ticker='MNT',name='マントル',chain='Mantle',first_in_jp=True,ref='hyperliquid',ref_symbol='MNT',hl_perp=True,
                identity_note='Binance の現物ティッカー MNT は "Mongolian Tughrik"（法定通貨）であり Mantle ではない。'
                              'MNTUSDT 等の現物ペアは存在しない。Hyperliquid の MNT perp は価格水準 $0.40-0.72 で Mantle と整合'),
           dict(ticker='BNB',name='ビルドアンドビルド',chain='BNB Chain',first_in_jp=False,ref='binance',ref_symbol='BNBUSDT',hl_perp=True)])

# ---------------- bitFlyer (PDF公告, 日付のみ) ----------------
ev(id='bf-2025-01-28-PEPE', venue='bitFlyer', kind='new_listing',
   title='ペペ（PEPE）取扱い開始予定のお知らせ（訂正版）',
   announce_utc=None, announce_date_jst='2025-01-28', time_precision='date',
   start_date_jst='2025-01-30', start_utc='2025-01-30T06:00:00Z', start_precision='minute',
   start_note='PDF「売買：1月30日（木）15:00 頃」',
   listing_type='販売所のみ', source='https://bitflyer.com/pub/20250128-handling-PEPE-ja.pdf',
   assets=[dict(ticker='PEPE',name='ペペ',chain='Ethereum',first_in_jp=False,ref='binance',ref_symbol='PEPEUSDT',hl_perp=True)])
ev(id='bf-2026-06-15-SOL', venue='bitFlyer', kind='new_listing',
   title='ソラナ（SOL）取扱い開始予定のお知らせ',
   announce_utc=None, announce_date_jst='2026-06-15', time_precision='date',
   start_date_jst='2026-06-24', start_utc='2026-06-24T04:00:00Z', start_precision='hour',
   start_note='PDF「売買：6月24日（水）13 時頃」',
   listing_type='販売所のみ', source='https://bitflyer.com/pub/20260615-handling-SOL-ja.pdf',
   assets=[dict(ticker='SOL',name='ソラナ',chain='Solana',first_in_jp=False,ref='binance',ref_symbol='SOLUSDT',hl_perp=True)])
ev(id='bf-2026-07-13-TRX-ATOM-XDC', venue='bitFlyer', kind='new_listing',
   title='トロン（TRX）、コスモス（ATOM）、XDCネットワーク（XDC）取扱い開始予定のお知らせ',
   announce_utc=None, announce_date_jst='2026-07-13', time_precision='date',
   start_date_jst='unknown', start_utc=None, start_precision='unknown',
   listing_type='販売所のみ', source='https://bitflyer.com/pub/20260713-handling-TRX-ATOM-XDC-ja.pdf',
   note='この予告の後、2026-09-16 に GRAM を追加した再予告が出ている',
   assets=[dict(ticker='TRX',name='トロン',chain='Tron',first_in_jp=False,ref='binance',ref_symbol='TRXUSDT',hl_perp=True),
           dict(ticker='ATOM',name='コスモス',chain='Cosmos',first_in_jp=False,ref='binance',ref_symbol='ATOMUSDT',hl_perp=True),
           dict(ticker='XDC',name='XDCネットワーク',chain='XDC',first_in_jp=False,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='Binance現物・Hyperliquid perp いずれにも存在しない')])
ev(id='bf-2026-09-16-TRX-GRAM-ATOM-XDC', venue='bitFlyer', kind='new_listing',
   title='トロン（TRX）、グラム（GRAM）、コスモス（ATOM）XDCネットワーク（XDC）の取扱い開始予定のお知らせ',
   announce_utc=None, announce_date_jst='2026-09-16', time_precision='date',
   start_date_jst='2026-09-29', start_utc='2026-09-29T04:00:00Z', start_precision='hour',
   start_note='PDF「売買：9月29日（火）13 時頃」',
   listing_type='販売所のみ', source='https://bitflyer.com/pub/20260916-handling-TRX-GRAM-ATOM-XDC-ja.pdf',
   assets=[dict(ticker='TRX',name='トロン',chain='Tron',first_in_jp=False,ref='binance',ref_symbol='TRXUSDT',hl_perp=True),
           dict(ticker='GRAM',name='グラム',chain='TON',first_in_jp=True,ref='binance',ref_symbol='GRAMUSDT',hl_perp=True,
                identity_note='bitFlyer PDF は GRAM を「The Open Network（TON）のネイティブトークン」と明記。'
                              'Binance の GRAM はネットワーク TON/TONMEMO。Binance TONUSDT は status=BREAK で1分足が一切返らず、'
                              'GRAMUSDT の1分足は 2026-07-02 開始。Hyperliquid では TON perp が 2026-06-15($1.81)まで、'
                              'GRAM perp が 2026-07-02($1.67)から。価格水準が連続しており Toncoin の改称と整合'),
           dict(ticker='ATOM',name='コスモス',chain='Cosmos',first_in_jp=False,ref='binance',ref_symbol='ATOMUSDT',hl_perp=True),
           dict(ticker='XDC',name='XDCネットワーク',chain='XDC',first_in_jp=False,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='Binance現物・Hyperliquid perp いずれにも存在しない')])

# ---------------- SBI VCトレード (日付のみ) ----------------
ev(id='sbivc-2025-02-12-14coins', venue='SBI VCトレード', kind='new_listing',
   title='【重要】取扱い銘柄の追加に伴う、『暗号資産取引説明書』改訂のお知らせ（14銘柄取扱い開始）',
   announce_utc=None, announce_date_jst='2025-02-12', time_precision='date',
   start_date_jst='2025-02-26', start_utc=None, start_precision='date',
   listing_type='販売所＋取引所（板）', source='https://www.sbivc.co.jp/newsview/3xfpd9er0',
   note='銘柄名は本文に記載がなく、同社が同時公開した新旧対照表PDF '
        'https://www.sbivc.co.jp/assets/docs/comparison20250212_manual-ex_tt.pdf の下線部から特定。'
        '対照表の下線部から14銘柄すべてを特定: ALGO, APE, AXS, BAT, CHZ, ENJ, ETC, FCR, MKR, MONA, NIDT, OMG, SAND, TRX。'
        'いずれも他の国内業者で既に取扱いのある銘柄（SBIによるBITPOINT取得後のラインナップ統合）',
   assets=[dict(ticker=t,name=n,chain='unknown',first_in_jp=False,ref=r,ref_symbol=s,hl_perp=h,no_ref_reason=nr) for t,n,r,s,h,nr in [
       ('ALGO','アルゴランド','binance','ALGOUSDT',True,None),
       ('APE','エイプコイン','binance','APEUSDT',True,None),
       ('AXS','アクシーインフィニティ','binance','AXSUSDT',True,None),
       ('BAT','ベーシックアテンショントークン','binance','BATUSDT',False,None),
       ('CHZ','チリーズ','binance','CHZUSDT',False,None),
       ('ENJ','エンジンコイン','binance','ENJUSDT',False,None),
       ('ETC','イーサリアムクラシック','binance','ETCUSDT',True,None),
       ('SAND','ザ・サンドボックス','binance','SANDUSDT',True,None),
       ('TRX','トロン','binance','TRXUSDT',True,None),
       ('MKR','メイカー',None,None,True,'Binance MKRUSDT は status=BREAK で1分足が返らない（計測不能）'),
       ('OMG','オーエムジー',None,None,False,'Binance OMGUSDT は status=BREAK で1分足が返らない（計測不能）'),
       ('FCR','エフシーアールコイン',None,None,False,'Binance現物・Hyperliquid perp いずれにも存在しない'),
       ('MONA','モナ',None,None,False,'Binance現物・Hyperliquid perp いずれにも存在しない'),
       ('NIDT','ニッポンアイドルトークン',None,None,False,'国内専用銘柄。Binance現物・Hyperliquid perp いずれにも存在しない')]])
ev(id='sbivc-2025-03-25-USDC', venue='SBI VCトレード', kind='new_listing',
   title='【国内初】ユーエスディーシー（USDC）一般向け取扱い開始のお知らせ',
   announce_utc=None, announce_date_jst='2025-03-25', time_precision='date',
   start_date_jst='2025-03-25', start_utc=None, start_precision='date',
   listing_type='販売所＋取引所（板）', source='https://www.sbivc.co.jp/newsview/2mjic5dcvjtv',
   note='ステーブルコイン。海外価格はドルペッグで事実上動かないため価格反応の計測対象として意味を持たない',
   assets=[dict(ticker='USDC',name='ユーエスディーシー',chain='multi',first_in_jp=True,ref='binance',ref_symbol='USDCUSDT',hl_perp=False,
                identity_note='ドルペッグのステーブルコイン')])
ev(id='sbivc-2026-02-27-CANTON', venue='SBI VCトレード', kind='new_listing',
   title='2026年3月25日(水)より 国内初※カントンコイン（Canton）取扱い開始を目指す',
   announce_utc=None, announce_date_jst='2026-02-27', time_precision='date',
   start_date_jst='2026-03-25', start_utc=None, start_precision='date',
   listing_type='販売所／取引所', source='https://www.sbivc.co.jp/newsview/4ji6fu9b63q',
   assets=[dict(ticker='CANTON',name='カントンコイン',chain='Canton',first_in_jp=True,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='Binance現物・Hyperliquid perp いずれにも存在しない')])
ev(id='sbivc-2026-03-11-TON-SUI', venue='SBI VCトレード', kind='new_listing',
   title='トンコイン（TON）、スイ（SUI）の取扱い開始のお知らせ',
   announce_utc=None, announce_date_jst='2026-03-11', time_precision='date',
   start_date_jst='2026-03-11', start_utc=None, start_precision='date',
   listing_type='販売所＋取引所（板）', source='https://www.sbivc.co.jp/newsview/2avqyrpszh',
   assets=[dict(ticker='TON',name='トンコイン',chain='TON',first_in_jp=False,ref='hyperliquid',ref_symbol='TON',hl_perp=True,
                identity_note='Binance TONUSDT は現在 status=BREAK で1分足が一切返らないため、Hyperliquid の TON perp を参照に使用'),
           dict(ticker='SUI',name='スイ',chain='Sui',first_in_jp=False,ref='binance',ref_symbol='SUIUSDT',hl_perp=True)])
ev(id='sbivc-2026-06-24-RLUSD-JPYSC', venue='SBI VCトレード', kind='new_listing',
   title='アールエルユーエスディー（RLUSD）及びジェイピーワイエスシー(JPYSC)取扱い開始',
   announce_utc=None, announce_date_jst='2026-06-24', time_precision='date',
   start_date_jst='unknown', start_utc=None, start_precision='unknown',
   listing_type='電子決済手段（ステーブルコイン）', source='https://www.sbivc.co.jp/newsview/skwdcxjbfjab',
   note='電子決済手段（ステーブルコイン）。価格反応の計測対象として意味を持たない',
   assets=[dict(ticker='RLUSD',name='アールエルユーエスディー',chain='Ethereum/XRPL',first_in_jp=True,ref='binance',ref_symbol='RLUSDUSDT',hl_perp=False,
                identity_note='ドルペッグのステーブルコイン'),
           dict(ticker='JPYSC',name='ジェイピーワイエスシー',chain='unknown',first_in_jp=True,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='国内専用の円建てステーブルコイン。海外市場に存在しない')])

# ---------------- BITPOINT (日付のみ) ----------------
for i,(d,ttl,tk,nm,ref,sym,hl,fij,url,typ) in enumerate([
  ('2024-12-25','BNB取り扱い開始のお知らせ','BNB','ビルドアンドビルド','binance','BNBUSDT',True,False,'https://www.bitpoint.co.jp/news/info/info-2024122501/','販売所／取引所'),
  ('2025-02-20','HBAR（ヘデラ）取り扱い開始のお知らせ','HBAR','ヘデラ','binance','HBARUSDT',True,False,'https://www.bitpoint.co.jp/news/info/info-2025022001/','販売所／取引所'),
  ('2025-06-13','日本初解禁！トランプ取り扱い開始のお知らせ','TRUMP','オフィシャルトランプ','binance','TRUMPUSDT',True,True,'https://www.bitpoint.co.jp/news/info/info-2025061301/','販売所／取引所'),
  ('2025-07-30','ステラルーメン（XLM）取り扱い開始のお知らせ','XLM','ステラルーメン','binance','XLMUSDT',True,False,'https://www.bitpoint.co.jp/news/info/info-2025073001/','販売所／取引所'),
]):
    ev(id=f'bp-{d}-{tk}', venue='BITPOINT', kind='new_listing', title=ttl,
       announce_utc=None, announce_date_jst=d, time_precision='date',
       start_date_jst='unknown', start_utc=None, start_precision='unknown',
       listing_type=typ, source=url,
       assets=[dict(ticker=tk,name=nm,chain='unknown',first_in_jp=fij,ref=ref,ref_symbol=sym,hl_perp=hl)])
ev(id='bp-2024-12-27-PEPE-pro', venue='BITPOINT', kind='new_service_type',
   title='PEPE 日本初の板取引！BITPOINT PRO取り扱い開始のお知らせ',
   announce_utc=None, announce_date_jst='2024-12-27', time_precision='date',
   start_date_jst='unknown', start_utc=None, start_precision='unknown',
   listing_type='取引所（板, BITPOINT PRO）', source='https://www.bitpoint.co.jp/news/info/info-2024122701/',
   note='既に同社で取扱中の銘柄に板取引を追加。新規取扱ではない。',
   assets=[dict(ticker='PEPE',name='ペペ',chain='Ethereum',first_in_jp=False,ref='binance',ref_symbol='PEPEUSDT',hl_perp=True)])
ev(id='bp-2025-02-20-ATOM-TSUGT-pro', venue='BITPOINT', kind='new_service_type',
   title='ATOM・TSUGT BITPOINT PRO取り扱い開始のお知らせ',
   announce_utc=None, announce_date_jst='2025-02-20', time_precision='date',
   start_date_jst='unknown', start_utc=None, start_precision='unknown',
   listing_type='取引所（板, BITPOINT PRO）', source='https://www.bitpoint.co.jp/news/info/info-2025022002/',
   note='既に同社で取扱中の銘柄に板取引を追加。新規取扱ではない。',
   assets=[dict(ticker='ATOM',name='コスモス',chain='Cosmos',first_in_jp=False,ref='binance',ref_symbol='ATOMUSDT',hl_perp=True),
           dict(ticker='TSUGT',name='ツグツグ',chain='unknown',first_in_jp=False,ref=None,ref_symbol=None,hl_perp=False,
                no_ref_reason='Binance現物・Hyperliquid perp いずれにも存在しない')])
ev(id='bp-2025-08-27-TRUMP-pro', venue='BITPOINT', kind='new_service_type',
   title='TRUMP日本初の板取引！BITPOINT PRO取り扱い開始のお知らせ',
   announce_utc=None, announce_date_jst='2025-08-27', time_precision='date',
   start_date_jst='unknown', start_utc=None, start_precision='unknown',
   listing_type='取引所（板, BITPOINT PRO）', source='https://www.bitpoint.co.jp/news/info/info-2025082701/',
   note='既に同社で取扱中の銘柄に板取引を追加。新規取扱ではない。',
   assets=[dict(ticker='TRUMP',name='オフィシャルトランプ',chain='Solana',first_in_jp=False,ref='binance',ref_symbol='TRUMPUSDT',hl_perp=True)])

json.dump(E, open('events.json','w'), ensure_ascii=False, indent=1)
print("events:",len(E))
from collections import Counter
print(Counter(e['venue'] for e in E))
print(Counter(e['kind'] for e in E))
print("assets total:", sum(len(e['assets']) for e in E))
print("minute-precision events:", sum(1 for e in E if e['time_precision']=='second'))
