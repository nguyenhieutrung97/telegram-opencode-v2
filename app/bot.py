import asyncio, html, json, logging, os, re, sqlite3, time, uuid
from pathlib import Path
import httpx, psutil
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

TOKEN=os.environ['TELEGRAM_BOT_TOKEN']
OWNER=int(os.environ['TELEGRAM_OWNER_ID'])
CHAT=int(os.environ.get('TELEGRAM_CHAT_ID',str(OWNER)))
URL=os.environ.get('OPENCODE_URL','http://127.0.0.1:4096').rstrip('/')
PASSWORD=os.environ['OPENCODE_SERVER_PASSWORD']
DB=Path(os.environ.get('BOT_DB','/var/lib/telegram-opencode/bot.sqlite3'))
MODEL=os.environ.get('OPENCODE_MODEL','')
POLL=int(os.environ.get('MONITOR_SECONDS','60'))
logging.basicConfig(level=logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)
log=logging.getLogger('telegram-opencode')
DB.parent.mkdir(parents=True,exist_ok=True)
con=sqlite3.connect(DB)
con.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
con.execute('CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, prompt TEXT NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL)')
con.commit()
lock=asyncio.Lock()
RUNNING={}
START_TS=time.time()

def state_get(k):
    r=con.execute('SELECT value FROM state WHERE key=?',(k,)).fetchone()
    return r[0] if r else None

def state_set(k,v):
    con.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(k,v)); con.commit()

def unwrap(obj):
    return obj.get('data',obj) if isinstance(obj,dict) else obj

async def api(method,path,payload=None,timeout=25):
    async with httpx.AsyncClient(auth=('opencode',PASSWORD),timeout=timeout,trust_env=False) as client:
        r=await client.request(method,URL+path,json=payload)
        r.raise_for_status()
        return unwrap(r.json()) if r.content else None

def allowed(update):
    user=update.effective_user
    chat=update.effective_chat
    return bool(user and chat and user.id==OWNER and chat.id==CHAT and chat.type=='private')

async def guard(update):
    if not allowed(update):
        log.warning('Unauthorized telegram request user=%s chat=%s',getattr(update.effective_user,'id',None),getattr(update.effective_chat,'id',None))
        return False
    return True

def md_to_telegram_html(text):
    # Convert OpenCode's markdown to Telegram HTML. Tables become aligned
    # <pre> blocks (Telegram has no table markup). Falls back to plain text
    # in send_chunks if Telegram rejects the result.
    s=html.escape(text)
    stash=[]
    def keep_pre(m):
        stash.append(m.group(1)); return f'\x00{len(stash)-1}\x00'
    def keep_code(m):
        stash.append(m.group(1)); return f'\x01{len(stash)-1}\x01'
    s=re.sub(r'```[^\n]*\n?(.*?)```',keep_pre,s,flags=re.S)
    s=re.sub(r'`([^`\n]+)`',keep_code,s)
    def table_sub(m):
        def unstash(t):
            return re.sub(r'[\x00\x01](\d+)[\x00\x01]',lambda x:stash[int(x.group(1))],t)
        rows=[]
        for line in m.group(0).strip().splitlines():
            cells=[unstash(c.strip()) for c in line.strip().strip('|').split('|')]
            if cells and all(re.fullmatch(r':?-{2,}:?',c) for c in cells): continue
            rows.append(cells)
        if not rows: return ''
        width=max(len(r) for r in rows)
        cols=[max(len(r[i]) if i<len(r) else 0 for r in rows) for i in range(width)]
        lines=[' | '.join((r[i] if i<len(r) else '').ljust(cols[i]) for i in range(width)) for r in rows]
        return '<pre>'+'\n'.join(lines)+'</pre>\n'
    s=re.sub(r'(?:^\|.*\|$\n?){2,}',table_sub,s,flags=re.M)
    s=re.sub(r'^#{1,6}\s*(.+)$',r'<b>\1</b>',s,flags=re.M)
    s=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',s,flags=re.S)
    s=re.sub(r'(?<![\w*])\*([^*\n]+)\*(?!\*)',r'<i>\1</i>',s)
    s=re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)',r'<a href="\2">\1</a>',s)
    s=re.sub(r'\x00(\d+)\x00',lambda m:'<pre>'+stash[int(m.group(1))]+'</pre>',s)
    s=re.sub(r'\x01(\d+)\x01',lambda m:'<code>'+stash[int(m.group(1))]+'</code>',s)
    return s

async def send_chunks(bot,text):
    text=str(text or '(Không có nội dung)')
    for i in range(0,len(text),3800):
        part=text[i:i+3800]
        try:
            await bot.send_message(CHAT,md_to_telegram_html(part),parse_mode='HTML')
        except Exception:
            await bot.send_message(CHAT,part)

async def ensure_session():
    sid=state_get('session')
    if sid:
        try:
            await api('GET',f'/api/session/{sid}')
            return sid
        except httpx.HTTPStatusError as e:
            if e.response.status_code!=404: raise
    obj=await api('POST','/api/session',{'title':'Telegram HP t740 Operator'})
    sid=obj.get('id')
    if not sid: raise RuntimeError(f'Cannot parse session ID: {str(obj)[:300]}')
    state_set('session',sid)
    return sid

def flatten_messages(payload):
    obj=unwrap(payload)
    if isinstance(obj,dict): obj=obj.get('items',obj.get('messages',[]))
    if not isinstance(obj,list): return []
    return obj

def extract_answer(items,cutoff=0):
    # V2 shape: items have type ('user'|'assistant'|'idle'), finish ('stop'|'tool-calls'),
    # and content parts [{type:'text'|'reasoning'|'tool'}]. Older builds used role/parts.
    # cutoff: ignore assistant items created at/before this ms — prevents re-serving
    # the previous answer as soon as the new user message lands.
    best=None; best_t=-1
    for item in items:
        if not isinstance(item,dict): continue
        info=item.get('info',item)
        if not isinstance(info,dict): continue
        kind=info.get('type',info.get('role'))
        if kind!='assistant': continue
        if info.get('finish')=='tool-calls': continue
        parts=info.get('content',item.get('content',info.get('parts',item.get('parts',[]))))
        if not isinstance(parts,list): continue
        text='\n'.join(p.get('text','') for p in parts if isinstance(p,dict) and p.get('type')=='text')
        if not text.strip(): continue
        t=((info.get('time') or {}).get('created') or 0)
        if t<=cutoff: continue
        if t>=best_t: best_t=t; best=text
    return best

async def ask(prompt,bot,progress=None):
    if lock.locked():
        try:
            wait_note='⏳ Có yêu cầu khác đang chạy, đợi tới lượt...'
            if progress: await progress.edit_text(wait_note)
            else: progress=await bot.send_message(CHAT,wait_note)
        except Exception: pass
    async with lock:
        t0=time.time()
        sid=await ensure_session()
        before=flatten_messages(await api('GET',f'/api/session/{sid}/message'))
        # Only accept assistant items newer than anything already in the session,
        # otherwise the previous answer is re-served instantly (cache bug).
        cutoff=max([((m.get('time') or {}).get('created') or 0) for m in before if isinstance(m,dict) and m.get('type')=='assistant']+[0])
        # V2 prompt endpoint enqueues work, not a synchronous completion.
        await api('POST',f'/api/session/{sid}/prompt',{'text':prompt})
        for i in range(300):
            await asyncio.sleep(2)
            if i%2==0:
                try: await bot.send_chat_action(CHAT,'typing')
                except Exception: pass
            messages=flatten_messages(await api('GET',f'/api/session/{sid}/message'))
            answer=extract_answer(messages,cutoff)
            if answer:
                models=[m.get('model',{}).get('id') for m in messages if isinstance(m,dict) and m.get('type')=='assistant' and isinstance(m.get('model'),dict)]
                await send_chunks(bot,answer+f'\n\n— {models[-1] if models else "opencode"} · {int(time.time()-t0)}s')
                return
            if i%30==29:
                tools=sum(1 for m in messages if isinstance(m,dict) and isinstance(m.get('content'),list) for p in m['content'] if isinstance(p,dict) and p.get('type')=='tool')
                text=f'⏳ OpenCode vẫn đang chạy... {i*2+2}s, {tools} bước công cụ.'
                try:
                    if progress: await bot.edit_message_text(text,CHAT,progress.message_id)
                    else: progress=await bot.send_message(CHAT,text)
                except Exception: pass
        await send_chunks(bot,'Yêu cầu đã gửi nhưng quá 600 giây chưa xong. OpenCode có thể vẫn đang chạy trong session; dùng /new để reset hoặc thử lại sau.')

async def start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    if await guard(update):
        await update.message.reply_text('HP t740 Operator sẵn sàng. Nhắn bất kỳ câu hỏi nào = hỏi OpenCode. Gõ /help để xem hướng dẫn.')

HELP_TEXT=('📖 Hướng dẫn HP t740 Operator\n\n'
 '• Nhắn tin bất kỳ (hoặc /ask <câu hỏi>) → hỏi OpenCode, chỉ đọc/phân tích, không sửa gì.\n'
 '   Ví dụ: ram đang dùng bao nhiêu\n'
 '• /propose <tác vụ> → yêu cầu thay đổi thật (cài package, sửa config…). Bấm Phê duyệt trong 5 phút để chạy.\n'
 '• /status → CPU/RAM/swap/disk, load, tiến trình nặng nhất, OpenCode online?\n'
 '• /stop → hủy yêu cầu đang chạy\n'
 '• /new → reset session OpenCode (ngữ cảnh mới)\n\n'
 'ℹ️ Mỗi câu hỏi mất 1–5 phút vì model chạy tool. Trong lúc chờ sẽ có ⏳ báo tiến độ mỗi phút và trạng thái "typing".')

async def help_cmd(update,ctx):
    if await guard(update): await update.message.reply_text(HELP_TEXT)

async def status(update,ctx):
    if not await guard(update): return
    disk=psutil.disk_usage('/'); vm=psutil.virtual_memory(); sm=psutil.swap_memory()
    l1,l5,l15=os.getloadavg()
    top=None
    for p in psutil.process_iter(['name','memory_info']):
        try:
            rss=p.info['memory_info'].rss
            if top is None or rss>top[1]: top=(p.info['name'],rss)
        except Exception: pass
    up=int(time.time()-psutil.boot_time()); bu=int(time.time()-START_TS)
    msg=(f'CPU: {psutil.cpu_percent(interval=.2)}% (load {l1:.1f}/{l5:.1f}/{l15:.1f})\n'
         f'RAM: {vm.percent}% ({round(vm.used/2**30,1)}/{round(vm.total/2**30,1)} GB)\n'
         f'Swap: {sm.percent}%\n'
         f'Disk /: {disk.percent}%\n'
         f'Top process: {top[0]} {round(top[1]/2**20)} MB\n'
         f'Uptime máy: {up//86400}d {(up%86400)//3600}h · bot: {bu//60}p')
    try:
        await api('GET','/api/session')
        msg+='\nOpenCode API: online'
    except Exception:
        msg+='\nOpenCode API: unavailable'
    sid=state_get('session')
    if sid:
        try:
            s=await api('GET',f'/api/session/{sid}')
            m=(s or {}).get('model') or {}
            if m.get('id'): msg+=f"\nModel: {m.get('id')}"
        except Exception: pass
    await update.message.reply_text(msg)

async def do_ask(update,ctx,prompt):
    if not await guard(update): return
    prompt=prompt.strip()
    if not prompt: return await update.message.reply_text('Dùng /ask <yêu cầu đọc/giải thích>')
    notice=await update.message.reply_text('Đang xử lý...')
    RUNNING['ask']=asyncio.current_task()
    try:
        await ask('Chỉ phân tích và đọc dữ liệu. Không thay đổi file, package, container, service hay cấu hình. '+prompt,ctx.bot,notice)
    except asyncio.CancelledError:
        try: await notice.edit_text('⏹ Đã hủy yêu cầu.')
        except Exception: pass
    except Exception as e:
        log.exception('Ask failed'); await update.message.reply_text(f'API lỗi: {type(e).__name__}. Xem journalctl để biết chi tiết.')
    finally:
        RUNNING.pop('ask',None)

async def ask_cmd(update,ctx):
    await do_ask(update,ctx,' '.join(ctx.args))

async def propose(update,ctx):
    if not await guard(update): return
    prompt=' '.join(ctx.args).strip()
    if not prompt: return await update.message.reply_text('Dùng /propose <tác vụ thay đổi>')
    aid=uuid.uuid4().hex[:12]
    con.execute('INSERT INTO approvals VALUES (?,?,?,?)',(aid,prompt,int(time.time())+300,'pending')); con.commit()
    keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('Phê duyệt',callback_data='yes:'+aid),InlineKeyboardButton('Từ chối',callback_data='no:'+aid)]])
    await update.message.reply_text('Yêu cầu thay đổi (hết hạn sau 5 phút):\n'+prompt[:2800],reply_markup=keyboard)

async def approve(update,ctx):
    if not await guard(update): return
    q=update.callback_query
    await q.answer()
    try: decision,aid=q.data.split(':',1)
    except ValueError: return
    row=con.execute('SELECT prompt,expires,status FROM approvals WHERE id=?',(aid,)).fetchone()
    if not row or row[2]!='pending' or row[1]<time.time():
        return await q.edit_message_text('Yêu cầu đã hết hạn hoặc đã xử lý.')
    status='approved' if decision=='yes' else 'rejected'
    # Atomic claim avoids repeated callbacks starting the same action.
    result=con.execute('UPDATE approvals SET status=? WHERE id=? AND status=?',(status,aid,'pending'))
    con.commit()
    if result.rowcount!=1: return await q.edit_message_text('Đã xử lý.')
    await q.edit_message_text('Đã '+('phê duyệt' if decision=='yes' else 'từ chối')+': '+row[0][:1000])
    if decision=='yes':
        try:
            await ask('Người quản trị đã phê duyệt tác vụ sau. Trước khi chạy, kiểm tra tác động và không phá hủy dữ liệu ngoài phạm vi yêu cầu. Tác vụ: '+row[0],ctx.bot)
        except Exception:
            log.exception('Approved task failed'); await send_chunks(ctx.bot,'Tác vụ gặp lỗi, kiểm tra log. Không tự thử lại.')

async def new(update,ctx):
    if not await guard(update): return
    state_set('session','')
    await update.message.reply_text('Đã reset session cho yêu cầu tiếp theo.')

async def stop_cmd(update,ctx):
    if not await guard(update): return
    task=RUNNING.get('ask')
    if task and not task.done():
        task.cancel()
        await update.message.reply_text('Đang hủy yêu cầu...')
    else:
        await update.message.reply_text('Không có yêu cầu nào đang chạy.')

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand('status','CPU/RAM/disk + OpenCode online?'),
        BotCommand('propose','Yêu cầu thay đổi (cần phê duyệt)'),
        BotCommand('new','Reset session OpenCode'),
        BotCommand('stop','Hủy yêu cầu đang chạy'),
        BotCommand('help','Hướng dẫn sử dụng'),
    ])

async def plain(update,ctx):
    # Plain messages behave like /ask (owner-only, read-only intent).
    await do_ask(update,ctx,update.message.text)

async def monitor(ctx):
    now=int(time.time())
    metrics={'ram':psutil.virtual_memory().percent,'disk':psutil.disk_usage('/').percent}
    for key,value in metrics.items():
        threshold=90 if key=='disk' else 95
        last=int(state_get('alert_'+key) or '0')
        if value>=threshold and now-last>=3600:
            state_set('alert_'+key,str(now))
            await send_chunks(ctx.bot,f'⚠️ HP t740: {key.upper()} {value:.1f}% (ngưỡng {threshold}%).')

async def on_error(update,ctx): log.error('Telegram handler error: %s',ctx.error,exc_info=ctx.error)

def main():
    app=Application.builder().token(TOKEN).post_init(post_init).build()
    for cmd,fn in [('start',start),('help',help_cmd),('status',status),('ask',ask_cmd),('propose',propose),('new',new),('stop',stop_cmd)]: app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(CallbackQueryHandler(approve,pattern=r'^(yes|no):[a-f0-9]{12}$'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,plain))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(monitor,interval=POLL,first=15)
    app.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=True)

if __name__=='__main__': main()
