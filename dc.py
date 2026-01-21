import discord
from discord.ext import commands
import asyncio
from groq import Groq
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import datetime
import pytz 

# === 新增這兩行 ===
from keep_alive import keep_alive
keep_alive()
# =================

# ================= 設定區 =================
# 注意：上傳到雲端時，這裡建議改成讀取環境變數 (稍後在 Render 設定)，比較安全
# 但如果你暫時不想改代碼，保持原樣也可以，只是要注意 Key 不要洩漏
import os
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# 如果你懶得改環境變數寫法，保持你原本的字串也可以，但在 Render 上設定變數會無效，
# 你必須直接把 Key 寫死在代碼裡上傳 (風險自負，不推薦)。
# 強烈建議改成上面 os.environ.get 的寫法！

# 設定 Groq
client = Groq(api_key=GROQ_API_KEY)

# 設定機器人
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ================= 資料結構 =================
channel_data = {}

def get_channel_config(channel_id):
    if channel_id not in channel_data:
        channel_data[channel_id] = {
            "mode": "idle", # 預設掛機
            "game_last_word": "",
            "last_player_id": None
        }
    return channel_data[channel_id]

# ================= 工具函式：延遲刪除訊息 =================
async def delete_after_delay(message, delay):
    """ 等待指定秒數後刪除訊息 """
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except:
        pass

# ================= 每日故事系統 =================

async def generate_daily_story():
    """ 每天早上8點執行的任務 (抓取最新) """
    print(f"⏰ [排程啟動] 開始生成每日故事 - {datetime.datetime.now()}")

    story_output_channels = {} 
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.topic == "【故事專用】":
                story_output_channels[guild.id] = channel
                break
    
    if not story_output_channels:
        print("⚠️ 找不到任何【故事專用】頻道，跳過生成。")
        return

    tz = pytz.timezone('Asia/Taipei')
    now = datetime.datetime.now(tz)
    yesterday = now - datetime.timedelta(days=1)
    
    target_game_channels = []
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.topic == "【接龍模式】":
                target_game_channels.append(channel)

    if not target_game_channels:
        print("⚠️ 找不到任何【接龍模式】頻道，跳過生成。")
        return

    for source_channel in target_game_channels:
        if source_channel.guild.id not in story_output_channels: continue
        target_output_channel = story_output_channels[source_channel.guild.id]

        print(f"🔍 正在掃描頻道 {source_channel.name} 的歷史訊息 (由新到舊)...")
        
        words = []
        try:
            async for msg in source_channel.history(limit=None):
                if msg.author.bot: continue
                
                if msg.created_at < yesterday:
                    break

                is_valid_word = False
                for reaction in msg.reactions:
                    if reaction.me and str(reaction.emoji) == "✅":
                        is_valid_word = True
                        break
                
                if is_valid_word:
                    words.append(msg.content)
        except Exception as e:
            print(f"爬取失敗: {e}")
            continue

        if not words:
            continue

        word_count = len(words)
        target_length = word_count * 50
        all_words_str = "、".join(words)
        
        prompt = f"""
        請你發揮天馬行空的創意，根據以下這些詞彙，編寫一個「極具創意、腦洞大開」的短篇故事。
        
        【指定詞彙】：{all_words_str}
        
        【要求】：
        1. 故事長度大約 {target_length} 字左右。
        2. 必須把上面所有的詞彙都用進去。
        3. 邏輯不重要！越荒謬、越超現實越好，要有強烈的趣味性。
        4. 請用說書人的口吻開頭。
        5. 故事結束了就結束了，不要加入結語。
        """
        
        try:
            chat_completion = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                temperature=0.9, 
            )
            story = chat_completion.choices[0].message.content
            
            embed = discord.Embed(
                title=f"📜 來自 #{source_channel.name} 的昨日傳奇",
                description=story,
                color=0xFFD700
            )
            embed.set_footer(text=f"擷取自 {yesterday.strftime('%m/%d %H:%M')} 至今 • 共 {word_count} 個詞")
            await target_output_channel.send(embed=embed)
            
        except Exception as e:
            print(f"AI 生成失敗: {e}")

# ================= 1. 客服單系統邏輯 =================

class TicketLauncher(discord.ui.View):
    """ 大廳的綠色按鈕 """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="開啟客服單", style=discord.ButtonStyle.success, custom_id="create_ticket", emoji="🎫")
    async def create_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        ticket_name = f"客服單：{interaction.user.display_name.lower()}"
        
        # 1. 檢查是否已存在 (失敗情況)
        existing = discord.utils.get(guild.channels, name=ticket_name)
        if existing:
            # 發送失敗提示，並排程 60 秒後刪除
            msg = await interaction.followup.send(
                f"❌ 您已經有一個客服單囉：{existing.mention}\n(此訊息將在 1 分鐘後自動刪除)", 
                ephemeral=True
            )
            asyncio.create_task(delete_after_delay(msg, 60))
            return

        # 2. 建立新頻道
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        try:
            cat = interaction.channel.category
            chan = await guild.create_text_channel(
                name=ticket_name, 
                overwrites=overwrites, 
                category=cat
            )

            # 3. [關鍵修改] 呼叫線下管理員 (Call Offline Admins)
            admin_role = None
            for role in guild.roles:
                # 排除預設的 everyone 角色 (role.id != guild.id)
                if role.permissions.administrator and not role.managed and role.id != guild.id:
                    admin_role = role
                    break 

            tz = pytz.timezone('Asia/Taipei')
            time_str = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

            info_embed = discord.Embed(color=0xdc8f65)
            info_embed.add_field(
                name="🥜 詳細資料", 
                value=f"╰ 開啟者: {interaction.user.mention}\n╰ 開啟時間: {time_str}", 
                inline=False
            )

            # 發送 Ping (呼叫線下) + Embed + 紅色按鈕
            await chan.send(content=f"@everyone 新的客服單已開啟！", embed=info_embed, view=TicketCloser())

            # 4. 回覆點擊者 (成功情況 - 1分鐘後刪除)
            msg = await interaction.followup.send(
                f"✅ 客服單已建立：{chan.mention}\n(此訊息將在 1 分鐘後自動刪除)", 
                ephemeral=True
            )
            asyncio.create_task(delete_after_delay(msg, 60))

        except Exception as e:
            await interaction.followup.send(f"❌ 建立失敗：{e}", ephemeral=True)

class TicketCloser(discord.ui.View):
    """ 房間內的紅色關閉按鈕 """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="點此關閉客服單", style=discord.ButtonStyle.danger, custom_id="close_ticket_internal", emoji="🔒")
    async def close_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚠️ 客服單關閉中...", ephemeral=True)
        await asyncio.sleep(2)
        await interaction.channel.delete()

# ================= 2. 模式切換選單 =================
class ModeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="請選擇功能...",
        options=[
            discord.SelectOption(label="🔴 關閉功能 (掛機)", value="idle", description="停止回應", emoji="💤"),
            discord.SelectOption(label="📢 發送客服面板", value="setup_panel", description="在該頻道產生按鈕", emoji="🎫"),
            discord.SelectOption(label="📜 設定此頻道為故事館", value="set_story_channel", description="將該頻道設定為每日故事發布區", emoji="📖"),
            discord.SelectOption(label="🧪 測試故事功能 (抓最新10詞)", value="test_story", description="搜尋最新接龍紀錄", emoji="🧬"),
            discord.SelectOption(label="🎮 接龍遊戲", value="game", description="開啟接龍模式", emoji="🎮"),
            discord.SelectOption(label="🤖 AI 聊天", value="ai", description="開啟 AI 對話", emoji="🤖"),
        ]
    )
    async def select_callback(self, interaction, select):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 只有管理員可以使用此選單！", ephemeral=True)
            return

        new_mode = select.values[0]
        channel = interaction.channel
        cid = channel.id
        config = get_channel_config(cid)

        # 預設模式重置
        config["mode"] = "idle"

        # --- 測試故事功能 ---
        if new_mode == "test_story":
            if channel.topic != "【故事測試】":
                try:
                    await channel.edit(topic="【故事測試】")
                except:
                    pass

            await interaction.response.defer(ephemeral=True) 
            
            game_channels = []
            for ch in interaction.guild.text_channels:
                if ch.topic in ["【接龍模式】", "【故事測試】"]:
                    game_channels.append(ch)
            
            if not game_channels:
                await interaction.followup.send("⚠️ 找不到任何主題為 `【接龍模式】` 或 `【故事測試】` 的頻道！", ephemeral=True)
                return

            words = []
            scanned_channels = []

            for game_ch in game_channels:
                if len(words) >= 10: break
                scanned_channels.append(game_ch.name)
                
                try:
                    async for msg in game_ch.history(limit=500):
                        if len(words) >= 10: break
                        if msg.author.bot: continue
                        
                        is_valid = False
                        for reaction in msg.reactions:
                            if reaction.me and str(reaction.emoji) == "✅":
                                is_valid = True
                                break
                        
                        if is_valid:
                            words.append(msg.content)
                except Exception as e:
                    print(f"掃描頻道 {game_ch.name} 失敗: {e}")

            if not words:
                await interaction.followup.send(f"⚠️ 在 {', '.join(scanned_channels)} 找不到任何被機器人打勾的詞彙。", ephemeral=True)
                return

            all_words_str = "、".join(words)
            await interaction.followup.send(f"✅ 掃描了頻道：{', '.join(scanned_channels)}\n📦 成功抓取最新 {len(words)} 個詞：{all_words_str}\n⏳ 正在撰寫故事中...", ephemeral=True)

            prompt = f"""
            請你發揮天馬行空的創意，根據以下這些詞彙，編寫一個「極具創意、腦洞大開」的短篇故事。
            
            【指定詞彙】：{all_words_str}
            
            【要求】：
            1. 故事長度大約 {len(words)*50} 字左右。
            2. 必須把上面所有的詞彙都用進去。
            3. 邏輯不重要！越荒謬、越超現實越好。
            4. 請用說書人的口吻開頭。
            5. 故事結束了就結束了，不要加入結語。
            """
            
            try:
                chat_completion = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="llama-3.3-70b-versatile",
                    temperature=0.9, 
                )
                story = chat_completion.choices[0].message.content
                
                embed = discord.Embed(
                    title=f"🧪 故事功能測試報告 (僅您可見)",
                    description=story,
                    color=0x00FFFF
                )
                embed.set_footer(text=f"來源：{', '.join(scanned_channels)} (最新詞彙)")
                await interaction.followup.send(embed=embed, ephemeral=True)
                
            except Exception as e:
                await interaction.followup.send(f"❌ AI 生成失敗：{e}", ephemeral=True)
            return

        # --- 設定面板 ---
        if new_mode == "setup_panel":
            if channel.topic != "【請勿濫用客服單】":
                try:
                    await channel.edit(topic="【請勿濫用客服單】")
                except:
                    pass

            try:
                await interaction.message.delete()
            except:
                pass

            embed = discord.Embed(
                title="如果您需要幫助或有任何問題，請點擊下方的按鈕開啟客服單。", 
                description="Click the button below to open a ticket.", 
                color=0x2b2d31
            )
            await channel.send(embed=embed, view=TicketLauncher())
            await interaction.response.send_message(f"✅ 已發送客服面板 (主題已更新)！", ephemeral=True)
            return

        # --- 設定故事頻道 ---
        if new_mode == "set_story_channel":
            if channel.topic == "【故事專用】":
                await interaction.response.send_message("⚠️ 這裡已經是故事頻道囉，不用重複設定。", ephemeral=True)
                return
            
            try:
                await channel.edit(topic="【故事專用】")
                await interaction.response.send_message(f"✅ 設定成功！**#{channel.name}** 已成為故事專用頻道。", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ 設定失敗：{e}", ephemeral=True)
            return

        # --- 接龍模式 ---
        if new_mode == "game":
            await interaction.response.defer()
            config["mode"] = "game"
            if channel.topic != "【接龍模式】":
                try:
                    await channel.edit(topic="【接龍模式】")
                except Exception as e:
                    print(f"⚠️ 修改主題失敗: {e}")
            
            config["game_last_word"] = ""
            config["last_player_id"] = None
            await interaction.followup.send(f"✅ 本頻道已切換為：**接龍遊戲模式**")
            return

        # --- AI 聊天模式 ---
        if new_mode == "ai":
            await interaction.response.defer()
            config["mode"] = "ai"
            if channel.topic != "【AI聊天模式】":
                try:
                    await channel.edit(topic="【AI聊天模式】")
                except Exception as e:
                    print(f"⚠️ 修改主題失敗: {e}")

            await interaction.followup.send(f"✅ 本頻道已切換為：**AI 聊天模式**")
            return
        
        # --- 關閉功能 (Idle) ---
        if new_mode == "idle":
            await interaction.response.defer()
            config["mode"] = "idle"

            known_topics = ["【接龍模式】", "【AI聊天模式】", "【故事測試】", "【客服面板】"]
            if channel.topic in known_topics:
                try:
                    await channel.edit(topic=None)
                except Exception as e:
                    print(f"⚠️ 清除主題失敗: {e}")
            
            await interaction.followup.send(f"💤 本頻道功能已關閉 (主題已清除)。")
            return

# ================= 3. 主程式邏輯 =================
@bot.event
async def on_ready():
    print(f'機器人 {bot.user} 已上線！')
    
    # 註冊兩個 View
    bot.add_view(TicketLauncher())
    bot.add_view(TicketCloser())
    
    print("🔄 正在從頻道主題恢復狀態...")
    count = 0
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.topic == "【接龍模式】":
                config = get_channel_config(channel.id)
                config["mode"] = "game"
                count += 1
            elif channel.topic == "【AI聊天模式】":
                config = get_channel_config(channel.id)
                config["mode"] = "ai"
                count += 1
    print(f"✅ 已恢復 {count} 個頻道的設定！")

    await bot.change_presence(activity=discord.Game(name="等待指令..."))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(generate_daily_story, CronTrigger(hour=8, minute=0, timezone=pytz.timezone('Asia/Taipei')))
    scheduler.start()
    print("⏰ 每日故事排程器已啟動 (Taipei Time 08:00)")

@bot.command()
@commands.has_permissions(administrator=True)
async def menu(ctx):
    await ctx.send("🔧 **管理員控制台**：", view=ModeSelectView())
    

# === [新增] 監聽刪除訊息事件 (抓包刪留言) ===
# 請把這段貼在 @bot.event async def on_message(message): 的「上面」
@bot.event
async def on_message_delete(message):
    # 1. 基本過濾：如果是機器人自己刪的，或不在文字頻道，就不理會
    if message.author.bot or not isinstance(message.channel, discord.TextChannel):
        return

    # 2. 取得該頻道設定
    config = get_channel_config(message.channel.id)

    # 3. 只有在「接龍模式」下才檢查
    if config["mode"] == "game":
        
        # A. 檢查這則被刪掉的訊息，是不是曾經被機器人打勾 (✅) 過？
        is_valid_message = False
        # 注意：如果訊息太久以前，快取可能抓不到 reactions，但剛刪除的通常抓得到
        for reaction in message.reactions:
            if reaction.me and str(reaction.emoji) == "✅":
                is_valid_message = True
                break
        
        # B. 檢查內容是不是等於「目前的最後一詞」
        # (必須同時符合：是有效接龍詞 + 是最新進度)
        if is_valid_message and message.content.strip() == config["game_last_word"]:
            last_char = config["game_last_word"][-1]
            user_name = message.author.display_name
            
            # C. 發送抓包訊息
            await message.channel.send(
                f"😡 **{user_name}** 太壞了，偷偷刪掉已經通過的留言，滾出去！\n"
                f"👉 下一個字還是要接「**{last_char}**」喔！"
            )

@bot.event
async def on_message(message):
    if message.author == bot.user: return

    if isinstance(message.channel, discord.TextChannel) and message.channel.topic in ["【客服專用】", "【故事專用】"]:
        return

    await bot.process_commands(message)
    if message.content.startswith('!'): return

    if not isinstance(message.channel, discord.TextChannel):
        return

    config = get_channel_config(message.channel.id)
    
    if message.channel.topic == "【接龍模式】":
        config["mode"] = "game"
    elif message.channel.topic == "【AI聊天模式】":
        config["mode"] = "ai"
    
    current_mode = config["mode"]

    if current_mode == "idle":
        return

    # 接龍模式
    elif current_mode == "game":
        last_word = config["game_last_word"]
        current_word = message.content.strip()
        
        if last_word == "":
            if len(current_word) < 2:
                await message.add_reaction("❌")
                await message.channel.send("起頭至少要兩個字啦！")
                return
            pass 

        else:
            if config["last_player_id"] == message.author.id:
                 await message.add_reaction("❌")
                 await message.channel.send("不能自己接自己的龍！給別人一點機會！")
                 return
            if len(current_word) < 2:
                 await message.add_reaction("❌")
                 await message.channel.send("太短了！請至少輸入兩個字。")
                 return

            if current_word[0] == current_word[-1]:
                await message.add_reaction("❌")
                await message.channel.send(f"又來了！「{current_word}」首尾字相同，禁止無限迴圈！")
                return

            if current_word[0] != last_word[-1]:
                await message.add_reaction("❌")
                await message.channel.send(f"眼睛還好嗎？上一句結尾是「**{last_word[-1]}**」，你接「**{current_word[0]}**」是想去哪？")
                return

        prompt = f"""
        你現在不是人類導師，而是一個【嚴格的中文語法結構檢測機】。
        
        使用者輸入：「{current_word}」

        你的任務是判斷：**這串文字的「詞彙」是否存在？且「排列結構」是否符合中文語法？**
        
        【最高指導原則 - 絕對不要做的事】：
        1. ❌ **絕對不要** 檢查現實邏輯！不要管龍是否真的存在，不要管混凝土能不能吃。
        2. ❌ **絕對不要** 因為「不夠真實」或「像是科幻情節」而拒絕。
        3. ❌ **絕對不要** 當科普老師。

        【審核標準】：
        1. ✅ **通過 (YES)**：
           - 只要詞彙是真實存在的，且排列符合中文文法（主詞+動詞+受詞 / 形容詞+名詞），**即使邏輯荒謬也要通過**。
           - 範例通過：「龍棲息在地上」 (龍/棲息/地上 都是真實詞彙，文法正確 -> YES)
           - 範例通過：「義大利麵拌42號混凝土」 (名詞+動詞+名詞，文法正確 -> YES)
           - 範例通過：「我把太陽一口吞了」 (超現實但文法正確 -> YES)
        
        2. ❌ **不通過 (NO)**：
           - 只有在「詞彙根本不存在（亂打）」或「文法完全破碎」時才拒絕。
           - 範例拒絕：「能季去次」 (無意義亂詞 -> NO)
           - 範例拒絕：「大大大吃吃吃」 (贅字堆疊 -> NO)
           - 範例拒絕：「森林跑去兔子」 (文法結構錯誤 -> NO)
           ❌ **拒絕「亂造詞」** (詞彙搭配必須合理)：
           - 即使每個字都認識，但合在一起**不是一個習慣用語**，或者**詞性搭配極度怪異**，必須拒絕。
           - 範例拒絕：「上米」 ("上"跟"米"都認識，但沒人這樣講 -> NO)
           - 範例拒絕：「能季」 (無意義組合 -> NO)
           - 範例拒絕：「什好」 (語意不清 -> NO)

        【回應格式】：
        1. 通過 -> 只回傳 "YES"。
        2. 不通過 -> 回傳 "NO" 並且「狠狠地酸他一句」(請發揮毒舌創意，酸他的"詞彙貧乏"或"亂打字"，但不要酸他的邏輯)。
        """
        
        try:
            chat_completion = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                temperature=0.2, 
            )
            result = chat_completion.choices[0].message.content.strip()
            
            if result.startswith("YES"):
                config["game_last_word"] = current_word
                config["last_player_id"] = message.author.id
                await message.add_reaction("✅")
            else:
                await message.add_reaction("❌")
                reason = result.replace("NO", "").strip()
                reason = reason.lstrip(",，:： ").strip()
                await message.channel.send(reason)
                
        except Exception as e:
            await message.channel.send(f"裁判恍神了: {e}")

    # AI 聊天模式
    elif current_mode == "ai":
        async with message.channel.typing():
            try:
                chat_completion = client.chat.completions.create(
                    messages=[{"role": "user", "content": message.content}],
                    model="llama-3.3-70b-versatile",
                    temperature=0.7,
                )
                await message.channel.send(chat_completion.choices[0].message.content)
            except Exception as e:
                await message.channel.send(f"AI 錯誤：{e}")


bot.run(DISCORD_TOKEN)










