現状の「使えば使うほど賢くなる」は
   「外部記憶・会話履歴・再利用可能な手順(= skill)が蓄積され、次回以降の判断材料になる」
   という実装です。

   要するに今の Hermes は主に次の3層で“賢くなる”ように作られています。

   1. memory
   2. skills
   3. session_search / SessionDB による過去会話の想起
   4. さらに任意で Honcho という外部メモリ統合もある

   どのファイルで何をしているか

   1) system prompt に memory / skill を注入している場所
     agent/prompt_builder.py
     run_agent.py

   a. prompt_builder.py
     agent/prompt_builder.py:73-89
     ここで MEMORY_GUIDANCE / SESSION_SEARCH_GUIDANCE / SKILLS_GUIDANCE を定義しています。
     つまりモデルに対して
     ・重要なことは memory に保存しろ
     ・過去会話が関係しそうなら session_search を使え
     ・複雑な作業をこなしたら skill に残せ
     という基本方針を与えています。

   b. skills の一覧を system prompt に載せる
     agent/prompt_builder.py:234-326
     build_skills_system_prompt() が ~/.hermes/skills/ 以下を走査して、
     使える skill の名前と説明だけをコンパクトに system prompt へ入れます。
     ここで重要なのは progressive disclosure 方式で、
     最初から全文を入れず「一覧だけ」を載せ、
     必要になったら skill_view(name) で個別に全文を読む設計です。

   c. 実際に system prompt を組み立てる
     run_agent.py:1751-1892
     AIAgent._build_system_prompt() で
     ・基本人格
     ・memory 関連ガイダンス
     ・session_search ガイダンス
     ・skills ガイダンス
     ・MEMORY.md / USER.md の内容
     ・skills index
     ・AGENTS.md などの context files
     をまとめて system prompt に入れています。

     特に重要なのは run_agent.py:1853-1862 で、
     self._memory_store.format_for_system_prompt("memory")
     self._memory_store.format_for_system_prompt("user")
     を system prompt に追加しています。

   2) memory そのものの実装
     tools/memory_tool.py
     run_agent.py

   a. 永続保存先と基本設計
     tools/memory_tool.py:3-24
     tools/memory_tool.py:37
     memory は ~/.hermes/memories/ 以下の
     ・MEMORY.md
     ・USER.md
     に保存されます。

     役割は分かれていて、
     ・MEMORY.md = エージェント側の覚え書き
       例: 環境、プロジェクトの癖、前回直した不具合、道具の癖
     ・USER.md = ユーザーについての記憶
       例: 好み、話し方、嫌がること、よく使う workflow

   b. MemoryStore
     tools/memory_tool.py:87-124
     MemoryStore が
     ・disk から読む
     ・live state を持つ
     ・system prompt 用 snapshot を持つ
     ことを担当します。

     特に大事なのは
     tools/memory_tool.py:91-95
     tools/memory_tool.py:106-121
     tools/memory_tool.py:281-292
     で、
     「system prompt に入る memory は session 開始時の frozen snapshot」
     になっていることです。

     つまりこの session 中に memory を追加しても、
     その場で system prompt は書き換えません。
     これは prompt cache を壊さないためです。
     次の session か、prompt 再構築時に反映されます。

   c. memory の add/replace/remove
     tools/memory_tool.py:154-279
     ここで
     ・add
     ・replace
     ・remove
     が実装されています。

   d. memory 内容の安全性チェック
     tools/memory_tool.py:42-84
     memory は system prompt に入るので、
     prompt injection 的な内容や secrets exfiltration 的な文字列を
     簡易スキャンしてブロックしています。

   e. memory tool の入口
     tools/memory_tool.py:385-423
     memory(action, target, content, old_text) が tool の実体です。

   f. agent 起動時に memory を読む
     run_agent.py:691-713
     AIAgent 初期化時に config を見て memory を有効化し、
     MemoryStore を作って load_from_disk() しています。

   3) memory を「どう活かしているか」
     現状は主に次の形です。

   a. system prompt に埋め込んで毎ターン参照させる
     run_agent.py:1853-1862
     これが基本です。
     つまり memory は「次回以降の初期知識」になります。

   b. 一定ターンごとに memory 保存を促す
     run_agent.py:4158-4169
     memory をしばらく使っていないと、
     user_message の末尾に
     「何か覚えるべきことがあるか考えて」
     という system note を足します。

   c. context compression 前に memory flush
     run_agent.py:3168-3337
     会話圧縮の前に、一度だけ memory tool だけ使える追加 API call を行い、
     圧縮で失われる前に「覚えるべきことを保存してから」圧縮します。
     これはかなり実用的な実装です。

   d. memory 自体は mid-session では live に書き込まれるが、
      system prompt は frozen snapshot
     つまり
     ・ファイルには即保存
     ・ただし現在の会話の prompt cache は壊さない
     というトレードオフ設計です。

   4) skill の実装
     tools/skills_tool.py
     tools/skill_manager_tool.py
     agent/prompt_builder.py
     agent/skill_commands.py

   a. skill の読み出し
     tools/skills_tool.py:736-799
     skills_list() は skill の一覧だけ返します。

     tools/skills_tool.py:804-
     skill_view() は必要な skill 本文や supporting file を返します。

     つまり skill は
     まず一覧だけ見せる
     必要なときだけ全文をロードする
     という token-efficient な設計です。

   b. skill の保管先
     tools/skills_tool.py:85-90
     ~/.hermes/skills/ が単一の保存場所です。

   c. skill を system prompt にどう活かすか
     agent/prompt_builder.py:234-326
     ここで skills index を作り、
     run_agent.py:1864-1874 で system prompt に差し込みます。

     なので、毎回全文を覚えさせるのではなく、
     「今ある手札一覧」をモデルに見せて、
     必要なら skill_view を使わせる実装です。

   d. skill の作成・編集
     tools/skill_manager_tool.py:3-12
     ここで skill は procedural memory、つまり
     「どうやるかの再利用可能手順」
     と明記されています。

     tools/skill_manager_tool.py:230-276
     _create_skill() が新しい skill を ~/.hermes/skills/ に作成します。

     patch/edit/delete/write_file/remove_file も同ファイルにあります。
     つまり skill は agent 自身が後から増やせます。

   e. slash command からも skill を使う
     agent/skill_commands.py:67-159
     /skill-name 形式のコマンドが来ると、
     skill 本文を user message として注入して実行します。
     これは CLI / gateway 共通です。

   5) skill を「どう活かしているか」
     現状は主に次の形です。

   a. system prompt に skill index を常時載せる
     どんな skill があるかを最初からモデルに知らせます。

   b. 必要時だけ skill_view() で読む
     全文を常時入れないので context を食いすぎません。

   c. 複雑な作業のあとに skill 化を促す
     run_agent.py:785-790
     creation_nudge_interval を読みます。デフォルトは 15 扱い。

     run_agent.py:4171-4180
     前の task が長い tool loop だった場合、
     次の user message 時に
     「再利用できる workflow なら skill に保存を検討して」
     という nudge を足します。

   d. 実際に procedural memory として使う
     memory は「事実を覚える」
     skill は「手順を覚える」
     という分担です。

   6) 過去会話からの“想起”
     これも “賢くなる” の重要部分です。

   a. 会話保存
     hermes_state.py:35-70
     sessions / messages テーブルを持ち、
     messages_fts という FTS5 テーブルも作っています。

     hermes_state.py:457-510
     append_message() で各 message を SQLite に保存します。

   b. 全文検索
     hermes_state.py:586-679
     search_messages() が FTS5 で過去メッセージ検索をします。

   c. session_search tool
     tools/session_search_tool.py:3-16
     実装の流れは
     ・FTS5 で候補セッションを探す
     ・一致した session を読む
     ・補助モデルで要約する
     です。

     tools/session_search_tool.py:177-240
     session_search() 本体です。

   つまり memory に明示保存していない内容でも、
   過去会話に残っていれば session_search で「思い出す」ことができます。

   7) Honcho 統合
     run_agent.py:715-783
     ここで Honcho という外部 memory 層も統合されています。
     これは optional ですが、
     有効なら user 観測や continuity 情報を別系統でも扱えます。

   結論として、「使えば使うほど賢くなる」の中身

   かなり正確に言うと、現状はこうです。

   1. モデル自体は学習していない
     重み更新や fine-tune はしていません。

   2. 代わりに外部記憶を増やしている
     MEMORY.md / USER.md に重要事項を蓄積します。

   3. 手順知識を skill 化している
     一度うまくいったやり方を ~/.hermes/skills/ に残せます。

   4. 過去会話を全文検索・要約して想起できる
     SessionDB + FTS5 + session_search により、
     明示 memory 以外も後から掘り起こせます。

   5. prompt cache を壊さないよう慎重に設計している
     memory は session start snapshot を system prompt に入れ、
     mid-session では live file だけ更新します。

   現状 memory/skill の役割分担を一言でいうと

   ・memory = 事実の記憶
     「このユーザーはこういう人」「この repo ではこうする」「前回こう直した」

   ・skill = 手順の記憶
     「この種類の作業はこう進める」「このツールはこの手順で使う」

   ・session_search = 会話ログからの想起
     「前に何をやったっけ？」を検索して要約
