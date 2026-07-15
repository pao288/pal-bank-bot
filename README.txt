PAL BANK TRUE FINAL

ユーザー機能
- PAL / CHIP口座・残高
- BANKプロフィール
  PAL残高 / CHIP残高 / 利用可能PAL / 審査中PAL / 総資産PAL換算 / 資産順位 / 口座開設日 / 未読通知
- 運営審査制PAL送金
- 審査中PALを利用可能残高から除外
- 2分以内の同一送金申請重複防止
- 高額・短時間連続送金は審査パネルへ警告表示
- 送金許可 / 却下 / 受取通知BOX
- PALポチ袋
  ランダム / 均等 / テキストチャンネル選択
- PAL ↔ CHIP交換
  交換全体を1DBトランザクションで確定
- 個人取引履歴
  通貨交換は非表示

ランキング
- PAL
- CHIP
- 総資産PAL換算
- 0残高除外
- 同額は同順位
- 総資産ランキングに現在交換レート表示
- 1時間ごと自動更新

管理機能
- PAL / CHIP付与・回収
- 管理操作の理由入力
- ユーザー残高照会
- BANK全体履歴
- 通貨総量
- 交換レート / 手数料 / 最低交換額設定
- 取引検索
  ユーザーID / 通貨 / SHOP・CASINO・VOICE・ADMIN等で絞り込み
- 取引取消・返金
  元取引は削除せず逆取引を作成して紐付け
- 通貨統計
- メンテナンスモード
- 通貨移動ログチャンネル
  SHOP / CASINO / VOICE / ADMIN / SEND / BANKを種類表示
- BANKステータス固定パネル
  ONLINE / MAINTENANCE / レート / 手数料 / 口座数 / 24時間取引
- CSV出力
  最大5000件の取引を表計算用ファイルとしてDiscordへ出力
  Excel等で集計・確認する時に使う

他Bot連携
- PAL SHOP
- PAL CASINO
- PAL VOICE
- 共通DBゲートウェイ
- 外部処理二重実行防止
- BANKメンテナンスモード連動

設置
1. !bankpanel
2. !adminpanel
3. ランキングchで !rankingpanel
4. 送金審査chで管理パネル → このchを送金審査chに設定
5. 通貨移動ログchで管理パネル → このchを移動ログchに設定
6. BANK状態chで管理パネル → このchをBANK状態chに設定

Railway
DISCORD_TOKEN
DATABASE_URL

固定BANKパネル自動設置を使う場合
BANK_PANEL_CHANNEL_ID
BANK_ADMIN_CHANNEL_ID
