#改267
import discord
from discord.ext import commands
import asyncio
from groq import Groq
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import datetime
import pytz 
import os

# === 保持 Render 在線 ===
from keep_alive import keep_alive
keep_alive()

# ================= 設定區 =================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

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
            "last_player_id": None,
            "temp_msg_id": None,    # 用來存「要被刪掉的紅色按鈕」ID
            "ticket_owner_id": None # 用來記住「誰開的單」
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
            if channel.topic == "【故事專用】每天早上8點，擷取過去24小時內接龍頻道的所有詞彙，編成一個故事。":
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
        
        # === [修改] 字數運算與限制 ===
        # 1. 先算出原始長度 (每1個詞換50個字)
        raw_length = word_count * 50
        
        # 2. 使用 min/max 限制範圍 (最小 200，最大 2000)
        target_length = max(200, min(raw_length, 2000))
        # ===========================

        all_words_str = "、".join(words)
        
        prompt = f"""
        【角色設定】：你是一位擅長一本正經胡說八道的說書人。
        
        【任務】：請將以下「指定詞彙」串連起來，編寫一個短篇故事。
        
        【指定詞彙】：{all_words_str}
        
        【寫作規則】：
        1. **風格要求**：故事必須充滿「荒謬的邏輯性」。也就是說，雖然劇情發展很離譜，但你要用非常嚴肅、理所當然的語氣把它講得頭頭是道。
        2. **禁止事項**：**絕對不要**在故事中提到「一本正經胡說八道」、「說書人」、「作者」或「接龍」等詞彙。不要打破第四面牆，直接進入故事世界。
        3. **詞彙運用**：必須包含所有指定詞彙，且要自然融入，不要像是在列清單。
        4. **字數限制**：大約 {target_length} 字。
        5. **幽默感**：請加入一些諷刺或意想不到的反轉，讓讀者覺得「我看了什麼，但好像又有道理」。
        6. **結尾**：故事結束了就結束了，不要加入結語。
        現在，請直接開始講故事：
        """
        
        try:
            chat_completion = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                temperature=0.7, 
            )
            story = chat_completion.choices[0].message.content
            
            embed = discord.Embed(
                title=f"📜 {yesterday.strftime('%m/%d')} 的宇宙故事",
                description=story,
                color=0xFFD700
            )
            embed.set_footer(text=f"擷取自 #{source_channel.name} • {yesterday.strftime('%m/%d %H:%M')} 至今 • 共 {word_count} 個詞")
            await target_output_channel.send(embed=embed)
            
        except Exception as e:
            print(f"AI 生成失敗: {e}")

# ================= 1. 客服單系統邏輯 =================

# --- 管理員專用按鈕 (灰色) ---
class AdminTicketCloser(discord.ui.View):
    """ 管理員專用的關閉按鈕 (灰色 + 權限檢查) """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="關閉客服單", style=discord.ButtonStyle.secondary, custom_id="close_ticket_admin", emoji="🔒")
    async def close_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 權限檢查
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ 只有管理員可以使用此按鈕！", ephemeral=True)
            return

        await interaction.response.send_message("🔒 管理員執行關閉...", ephemeral=True)
        await asyncio.sleep(2)
        await interaction.channel.delete()


class TicketControlView(discord.ui.View):
    """ 藍色退出按鈕 (給開啟者用) """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="退出此客服單", style=discord.ButtonStyle.primary, custom_id="leave_ticket", emoji="👋")
    async def leave_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 0. 檢查是不是管理員
        if interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 您是管理員，無法退出頻道 (權限最高級)。", ephemeral=True)
            return

        # 1. 公開回覆 (修改這裡：改成所有人可見的公告)
        embed = discord.Embed(
            description=f"👋 **{interaction.user.mention}** 已自行退出此客服單。",
            color=0x99aab5 # 灰色系
        )
        await interaction.response.send_message(embed=embed) # 預設 ephemeral=False，所以大家看得到
        
        # 2. 修改權限：將該使用者的「讀取訊息」權限設為 False -> 頻道直接消失
        await interaction.channel.set_permissions(interaction.user, read_messages=False)


class TicketCloser(discord.ui.View):
    """ 紅色臨時關閉按鈕 (給使用者誤觸時取消用) """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="點此關閉客服單", style=discord.ButtonStyle.danger, custom_id="close_ticket_internal", emoji="🔒")
    async def close_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 客服單關閉中...", ephemeral=True)
        await asyncio.sleep(2)
        await interaction.channel.delete()


class TicketLauncher(discord.ui.View):
    """ 大廳的綠色按鈕 """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="開啟客服單", style=discord.ButtonStyle.success, custom_id="create_ticket", emoji="🎫")
    async def create_ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        ticket_name = f"客服單：{interaction.user.display_name.lower()}"
        
        # 1. 檢查是否已存在
        existing = discord.utils.get(guild.channels, name=ticket_name)
        if existing:
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

            # 3. 記錄開單者 ID
            config = get_channel_config(chan.id)
            config["ticket_owner_id"] = interaction.user.id

            # 4. 準備時間
            tz = pytz.timezone('Asia/Taipei')
            time_str = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

            # --- [訊息 1] 詳細資料 (土黃色) ---
            info_embed = discord.Embed(
                title="新的客服單已開啟",
                description="請稍候，管理員將會盡快為您服務。",
                color=0xdc8f65
            )
            info_embed.add_field(
                name="🥜 詳細資料", 
                value=f"╰ 開啟者: {interaction.user.display_name}\n╰ 開啟時間: {time_str}", 
                inline=False
            )
            await chan.send(content=f"@🪐宇宙的起源", embed=info_embed)

            # --- [訊息 2] 管理員控制台 (土黃色 + 灰色按鈕) ---
            admin_embed = discord.Embed(
                title="🔒 管理員控制台",
                description="此按鈕永久有效，問題解決後請點擊下方按鈕關閉頻道。",
                color=0xdc8f65
            )
            await chan.send(embed=admin_embed, view=AdminTicketCloser())

            # --- [訊息 3] 給開啟者的「退出按鈕」 (藍色) ---
            leave_embed = discord.Embed(
                description="如果您不需要協助了，可以點擊下方按鈕直接**退出**此頻道。\n(頻道不會被刪除，管理員仍可看到內容)",
                color=0x3498db
            )
            await chan.send(content=f"{interaction.user.mention}", embed=leave_embed, view=TicketControlView())

            # --- [訊息 4] 給開啟者的「臨時紅色按鈕」 ---
            temp_embed = discord.Embed(
                description=f"🛑 **{interaction.user.mention} 專用選項**\n在您**開始對話前**，若發現誤觸，可直接點此關閉房間。\n(此按鈕將在您發言後自動消失)",
                color=0xff0000
            )
            temp_msg = await chan.send(content=f"{interaction.user.mention}", embed=temp_embed, view=TicketCloser())

            # 5. 記錄訊息 ID
            config["temp_msg_id"] = temp_msg.id

            # 6. 回覆大廳
            msg = await interaction.followup.send(
                f"✅ 客服單已建立。\n(此訊息將在 1 分鐘後自動刪除)", 
                ephemeral=True
            )
            asyncio.create_task(delete_after_delay(msg, 60))

        except Exception as e:
            await interaction.followup.send(f"❌ 建立失敗：{e}", ephemeral=True)

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
                try: await channel.edit(topic="【故事測試】")
                except: pass

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
                        if is_valid: words.append(msg.content)
                except: pass

            if not words:
                await interaction.followup.send(f"⚠️ 在 {', '.join(scanned_channels)} 找不到任何被機器人打勾的詞彙。", ephemeral=True)
                return

            all_words_str = "、".join(words)
            await interaction.followup.send(f"✅ 抓取成功，正在生成...", ephemeral=True)

            prompt = f"請根據以下詞彙寫一個超現實短篇故事：{all_words_str}"
            try:
                chat_completion = client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="llama-3.3-70b-versatile",
                    temperature=0.9, 
                )
                story = chat_completion.choices[0].message.content
                embed = discord.Embed(title=f"🧪 故事測試", description=story, color=0x00FFFF)
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ AI 生成失敗：{e}", ephemeral=True)
            return

        # --- 設定面板 ---
        if new_mode == "setup_panel":
            if channel.topic != "【請勿濫用客服單】":
                try: await channel.edit(topic="【請勿濫用客服單】")
                except: pass
            try: await interaction.message.delete()
            except: pass

            embed = discord.Embed(
                title="如果您需要幫助或有任何問題，請點擊下方的按鈕開啟客服單。", 
                description="Click the button below to open a ticket.", 
                color=0x2b2d31
            )
            await channel.send(embed=embed, view=TicketLauncher())
            await interaction.response.send_message(f"✅ 已發送客服面板！", ephemeral=True)
            return

        # --- 設定故事頻道 ---
        if new_mode == "set_story_channel":
            try:
                await channel.edit(topic="【故事專用】每天早上8點，擷取過去24小時內接龍頻道的所有詞彙，編成一個故事。")
                await interaction.response.send_message(f"✅ 設定成功！", ephemeral=True)
            except:
                await interaction.response.send_message(f"❌ 設定失敗", ephemeral=True)
            return

        # --- 接龍模式 ---
        if new_mode == "game":
            await interaction.response.defer()
            config["mode"] = "game"
            if channel.topic != "【接龍模式】":
                try: await channel.edit(topic="【接龍模式】")
                except: pass
            config["game_last_word"] = ""
            config["last_player_id"] = None
            await interaction.followup.send(f"✅ 已切換為：**接龍遊戲模式**")
            return

        # --- AI 聊天模式 ---
        if new_mode == "ai":
            await interaction.response.defer()
            config["mode"] = "ai"
            if channel.topic != "【AI聊天模式】":
                try: await channel.edit(topic="【AI聊天模式】")
                except: pass
            await interaction.followup.send(f"✅ 已切換為：**AI 聊天模式**")
            return
        
        # --- 關閉功能 ---
        if new_mode == "idle":
            await interaction.response.defer()
            config["mode"] = "idle"
            known_topics = ["【接龍模式】", "【AI聊天模式】", "【故事測試】", "【客服面板】"]
            if channel.topic in known_topics:
                try: await channel.edit(topic=None)
                except: pass
            await interaction.followup.send(f"💤 功能已關閉。")
            return

# ================= 3. 主程式邏輯 =================
@bot.event
async def on_ready():
    print(f'機器人 {bot.user} 已上線！')
    
    # 註冊所有按鈕
    bot.add_view(TicketLauncher())
    bot.add_view(TicketCloser())       # 紅
    bot.add_view(TicketControlView())  # 藍
    bot.add_view(AdminTicketCloser())  # 灰
    
    print("🔄 正在恢復設定...")
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.topic == "【接龍模式】":
                config = get_channel_config(channel.id)
                config["mode"] = "game"
                print(f"   └─ 發現接龍頻道：{channel.name}，正在讀取進度...")

                # === 👇 新增：恢復記憶功能 (抓最後一個打勾的字) ===
                try:
                    # 往回翻最近 10 則訊息
                    async for msg in channel.history(limit=10):
                        if msg.author.bot: continue # 跳過機器人自己的訊息
                        
                        # 檢查這則訊息有沒有被機器人(me)打勾 ✅
                        is_valid = False
                        for reaction in msg.reactions:
                            if reaction.me and str(reaction.emoji) == "✅":
                                is_valid = True
                                break
                        
                        # 如果找到打勾的訊息，它就是「上一個字」！
                        if is_valid:
                            config["game_last_word"] = msg.content.strip()
                            config["last_player_id"] = msg.author.id
                            print(f"      └─ 已恢復進度：{config['game_last_word']}")
                            break # 找到了就跳出迴圈，不用再翻更舊的
                            
                except Exception as e:
                    print(f"      └─ 讀取歷史失敗：{e}")
                # ===============================================

    await bot.change_presence(activity=discord.Game(name="等待指令..."))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(generate_daily_story, CronTrigger(hour=8, minute=0, timezone=pytz.timezone('Asia/Taipei')))
    scheduler.start()
    print("⏰ 排程器已啟動")

@bot.command()
@commands.has_permissions(administrator=True)
async def menu(ctx):
    await ctx.send("🔧 **管理員控制台**：", view=ModeSelectView())
    

# === [監聽刪除訊息] (抓包刪留言) ===
@bot.event
async def on_message_delete(message):
    if message.author.bot or not isinstance(message.channel, discord.TextChannel): return
    config = get_channel_config(message.channel.id)

    if config["mode"] == "game":
        # 檢查是否為有效留言
        is_valid_message = False
        for reaction in message.reactions:
            if reaction.me and str(reaction.emoji) == "✅":
                is_valid_message = True
                break
        
        # 如果是被刪除的留言 且 是目前的最新進度
        if is_valid_message and message.content.strip() == config["game_last_word"]:
            last_char = config["game_last_word"][-1]
            user_name = message.author.display_name
            await message.channel.send(
                f"😡 **{user_name}** 太壞了，偷偷刪掉已經通過的留言，滾出去！\n"
                f"👉 下一個字還是要接「**{last_char}**」喔！"
            )

# === [監聽編輯訊息] (抓包偷改留言) ===
@bot.event
async def on_message_edit(before, after):
    if before.author.bot or not isinstance(before.channel, discord.TextChannel): return
    config = get_channel_config(before.channel.id)

    if config["mode"] == "game":
        is_valid_message = False
        for reaction in before.reactions:
            if reaction.me and str(reaction.emoji) == "✅":
                is_valid_message = True
                break
        
        if is_valid_message and before.content.strip() == config["game_last_word"]:
            last_char = config["game_last_word"][-1]
            user_name = before.author.display_name
            await before.channel.send(
                f"👀 **{user_name}** 別以為我沒看到！想偷改已經通過的答案？不可饒恕！\n"
                f"👉 下一個字還是要接「**{last_char}**」喔！"
            )

@bot.event
async def on_message(message):
    if message.author == bot.user: return

    await bot.process_commands(message)

    if not isinstance(message.channel, discord.TextChannel):
        return
    
    if message.channel.topic in ["【故事專用】每天早上8點，擷取過去24小時內接龍頻道的所有詞彙，編成一個故事。"]:
        return

    config = get_channel_config(message.channel.id)

    # === 偵測客服單開單者說話，刪除臨時按鈕 ===
    if config["ticket_owner_id"] and message.author.id == config["ticket_owner_id"]:
        if config["temp_msg_id"]:
            try:
                msg_to_delete = await message.channel.fetch_message(config["temp_msg_id"])
                await msg_to_delete.delete()
                config["temp_msg_id"] = None
            except:
                config["temp_msg_id"] = None

    # ================= 遊戲邏輯 =================
    if message.content.startswith('!'): return

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
                await message.channel.send("裁判：起頭至少要兩個字啦！")
                return
            pass 

        else:
            if config["last_player_id"] == message.author.id:
                 await message.add_reaction("❌")
                 await message.channel.send("不能自己接自己的龍！給別人一點機會！")
                 return
            
            if len(current_word) < 2:
                 await message.add_reaction("❌")
                 await message.channel.send("裁判：太短了！請至少輸入兩個字。")
                 return

            if current_word[0] == current_word[-1]:
                await message.add_reaction("❌")
                await message.channel.send(f"裁判：又來了！「{current_word}」首尾字相同，禁止無限迴圈！")
                return

            if current_word[0] != last_word[-1]:
                await message.add_reaction("❌")
                await message.channel.send(f"裁判：眼睛還好嗎？上一句結尾是「**{last_word[-1]}**」，你接「**{current_word[0]}**」是想去哪？")
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
        3. 注意:
            如「游泳」、「喜歡」可以是名詞也能是動詞，詞性請根據上下文判斷。
        【回應格式】：
        1. 通過 -> 只回傳 "YES"。
        2. 不通過 -> 回傳 "NO" 並且「狠狠地酸他一句」(請發揮毒舌創意，酸他的"詞彙貧乏"或"亂打字"，但不要酸他的邏輯，字數限制20~35字)。
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
                reason = result.replace("NO", "").strip().lstrip(",，:： ").strip()
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
