import requests
import time
import os
import re
import sys
import shutil
import random
import csv
from datetime import datetime
from tqdm import tqdm
from requests.adapters import HTTPAdapter

# ===================== ⚙️ 核心参数配置 =====================
# 建议使用者根据实际需求修改扫描区间
START_UID = 5000000  
END_UID = 8508999
# 每抓取多少个有效/无效数据进行一次物理存盘，防止内存溢出和数据丢失
SAVE_INTERVAL = 30   

# ===================== 📁 本地存储架构 =====================
# 获取当前脚本所在目录，确保路径绝对稳定
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 采用双轨制存储机制：Cache 负责底层静默写入，View 负责提供给用户随时打开查看
CACHE_FILE = os.path.join(SCRIPT_DIR, "小号_有效数据_缓存(勿动).csv")
VIEW_FILE = os.path.join(SCRIPT_DIR, "小号_有效数据_阅览.csv")
DIRTY_CACHE = os.path.join(SCRIPT_DIR, "小号_脏数据_缓存(勿动).csv")
DIRTY_VIEW = os.path.join(SCRIPT_DIR, "小号_脏数据_阅览.csv")
WAF_LOG_CACHE = os.path.join(SCRIPT_DIR, "小号_风控记录_缓存(勿动).csv")
WAF_LOG_VIEW = os.path.join(SCRIPT_DIR, "小号_风控记录_阅览.csv")

# ===================== 🔑 身份指纹注入区 =====================
# 【高危预警】使用者必须在这里填入自己的 B 站 Web 端 Cookie！
# 获取方式：网页端登录 B 站 -> F12 打开开发者工具 -> Network (网络) -> 刷新页面 -> 随便点一个请求，在 Request Headers 中复制 Cookie 的值。
RAW_COOKIE = "请在此处粘贴你的完整 Cookie 字符串"

# 伪装请求头，模拟真实浏览器行为
HEADERS_TEMPLATE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
    "Cookie": RAW_COOKIE
}

# 预编译正则：用于过滤只有英文字母和数字的用户名（可根据需求修改）
PATTERN = re.compile(r'^[a-zA-Z0-9]+$')

# 建立全局会话池，并挂载 HTTPAdapter 开启底层单次重试，提升网络握手稳定性
session = requests.Session()
adapter = HTTPAdapter(max_retries=1)
session.mount('http://', adapter)
session.mount('https://', adapter)

# ----------------- 🛡️ 增强版：UID 级看门狗 (Watchdog) -----------------
def check_login_state(print_func=print):
    """
    底层身份比对机制：
    防止因 Cookie 过期或风控导致的假性登录。
    它会提取你本地 Cookie 中的 UID，去和服务器实时返回的 UID 交叉验证。
    """
    url = "https://api.bilibili.com/x/web-interface/nav"
    try:
        # 尝试从用户的 Cookie 中提取专属的 DedeUserID
        match = re.search(r'DedeUserID=(\d+)', RAW_COOKIE)
        expected_uid = match.group(1) if match else None
        
        if not expected_uid:
            print_func("⚠️ 警告：无法从 RAW_COOKIE 中提取到 DedeUserID，请检查是否完整复制了 Cookie。")
            return False

        # 向服务器请求当前登录状态
        res = requests.get(url, headers=HEADERS_TEMPLATE, timeout=10)
        data = res.json()
        
        # 严格比对：不仅要登录态为 True，UID 也必须完全一致
        if data.get("code") == 0 and data.get("data", {}).get("isLogin", False):
            server_uid = str(data["data"]["mid"])
            if server_uid == expected_uid:
                return True
            else:
                print_func(f"❌ 警告：发生串号或 Cookie 异常！期望 UID: {expected_uid}，实际服务器返回: {server_uid}")
                return False
    except Exception as e:
        print_func(f"网络探测异常: {e}")
    return False

# ----------------- 💾 存储与断点续传组件 -----------------
def append_csv(data_list, filepath):
    """将内存中的数据列表追加到 CSV 文件中"""
    if not data_list: return
    file_exists = os.path.isfile(filepath)
    # 强制使用 utf-8-sig 编码，防止 Windows 下用 Excel 打开直接乱码
    with open(filepath, 'a', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        if not file_exists: writer.writerow(["UID", "用户名", "状态"]) # 首次创建时写入表头
        for row in data_list: writer.writerow([row["UID"], row["用户名"], row["状态"]])

def sync_views():
    """视图同步：将后端的缓存文件复制一份给用户阅览，实现读写分离"""
    for src, dst in [(CACHE_FILE, VIEW_FILE), (DIRTY_CACHE, DIRTY_VIEW), (WAF_LOG_CACHE, WAF_LOG_VIEW)]:
        if os.path.exists(src):
            try: shutil.copy2(src, dst)
            except: pass

def load_finished_uids():
    """
    断点续传核心逻辑：
    暴力扫描现有的本地 CSV，提取所有已经爬取过的 UID，以便跳过。
    """
    finished = set()
    for fp in [CACHE_FILE, DIRTY_CACHE]:
        if os.path.exists(fp):
            loaded = False
            # 兼容性设计：循环尝试不同的编码格式，防止用户用 Excel 修改后强行保存导致编码破坏
            for enc in ['utf-8-sig', 'gbk', 'utf-8']:
                try:
                    with open(fp, 'r', encoding=enc) as f:
                        reader = csv.reader(f); next(reader, None) # 跳过表头
                        for r in reader: 
                            if r and r[0].isdigit(): finished.add(int(r[0]))
                    loaded = True
                    break 
                except Exception:
                    pass
            if not loaded:
                print(f"⚠️ 警告：文件 {fp} 读取失败，断点续传可能丢失这部分进度！")
    return finished

# ----------------- 🕸️ 核心爬取函数 -----------------
def fetch_user(uid):
    """请求目标 UID 的卡片信息，包含异常状态的精细化捕获"""
    url = f"https://api.bilibili.com/x/web-interface/card?mid={uid}"
    req_headers = HEADERS_TEMPLATE.copy()
    # 动态伪造 Referer，降低被风控系统识别为机器人的概率
    req_headers["Referer"] = f"https://space.bilibili.com/{uid}"
    
    try:
        res = session.get(url, headers=req_headers, timeout=8)
        # 412/403 通常是触发了反爬盾或 IP 限流
        if res.status_code in [412, 403]: return uid, None, "拦截"
        
        data = res.json()
        code = data.get("code")
        
        if code in [-412, -352]: return uid, None, "拦截"
        if code == 0:
            name = data["data"]["card"]["name"]
            # 状态分流：符合正则规则的标记为"有效"，否则为"杂类"
            return uid, name, "有效" if PATTERN.match(name) else "杂类"
        elif code == -404: 
            return uid, None, "注销"
        else: 
            return uid, None, f"其他_{code}"
    except: 
        return uid, None, "异常"

# ===================== 🚀 引擎主入口 =====================
if __name__ == "__main__":
    print(f"===== 小号版 (UID 500w+) | 死磕重试 & 深度防御模式 =====")
    
    print("正在对您的 Cookie 进行底层身份比对...")
    if RAW_COOKIE == "请在此处粘贴你的完整 Cookie 字符串":
        print("❌ 启动失败：请先在源码的 RAW_COOKIE 变量中填入你的 B 站 Cookie！")
        sys.exit(1)
        
    if not check_login_state():
        print("❌ 启动失败：Cookie 身份比对失败或网络未连接，请重试！")
        sys.exit(1)
    
    match_uid = re.search(r'DedeUserID=(\d+)', RAW_COOKIE)
    my_uid = match_uid.group(1) if match_uid else "未知"
    print(f"✅ 核身通过，您的挂载 UID: {my_uid}，准备发车！\n")
    
    finished = load_finished_uids()
    print(f"📦 已从本地成功读取 {len(finished)} 条历史记录，将自动跳过这些 UID...")
    
    # 差集计算：得出真正需要爬取的任务列表
    todo = [u for u in range(START_UID, END_UID+1) if u not in finished]
    
    valid_buffer, dirty_buffer = [], []
    stats = {"有效": 0, "拦截次数": 0} 
    in_penalty, p_start_time, continuous_blocks = False, 0, 0
    last_valid_time = time.time()

    try:
        # 使用 tqdm 渲染进度条
        with tqdm(total=len(todo), desc="进度", bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{postfix}]') as pbar:
            for uid in todo:
                
                # 🔥 死磕循环：只要是被风控拦截，就锁死在这个循环里不断重试，绝不遗漏目标
                while True:
                    # --- 看门狗防御：长时间未拿到正常数据时，探测账号存活状态 ---
                    if time.time() - last_valid_time > 30:
                        pbar.write(f"\n⏳ 超时检测：正在呼叫 B 站服务器核实身份...")
                        if check_login_state(print_func=pbar.write): 
                            pbar.write(f"✅ 核身通过，权限依然完好，继续死磕当前 UID...")
                            last_valid_time = time.time()
                        else:
                            pbar.write("❌ 致命异常：登录态已损毁！正在强制存盘...")
                            # 账号挂了，立刻把内存里的数据落盘后自杀
                            append_csv(valid_buffer, CACHE_FILE); append_csv(dirty_buffer, DIRTY_CACHE)
                            sync_views(); os._exit(1)

                    # 发起请求
                    _, name, status = fetch_user(uid)

                    # --- 遭遇拦截处理：重度退避策略 ---
                    if status == "拦截":
                        continuous_blocks += 1
                        # 动态休眠算法：120秒起步 + 随失败次数递增的阶梯惩罚 + 随机抖动秒数防指纹
                        sleep_time = 120 + (continuous_blocks // 2) * 30 + random.uniform(5, 15)
                        
                        if not in_penalty:
                            in_penalty = True; p_start_time = time.time()
                            pbar.write(f"\n🚨 遭遇拦截！目标 UID: {uid} 已被卡住。启动重度退避，休眠 {sleep_time:.1f} 秒...")
                        else:
                            pbar.write(f"🔒 对 UID {uid} 突围失败 (撞墙{continuous_blocks}次): 继续休眠 {sleep_time:.1f} 秒...")
                        
                        stats["拦截次数"] += 1
                        time.sleep(sleep_time)
                        last_valid_time = time.time() 
                        
                        continue # 继续请求同一个 UID

                    # --- 突围成功 / 刑满释放 ---
                    if in_penalty and status not in ["拦截", "异常"]:
                        pbar.write(f"🟢 突围成功！当前 UID: {uid} 已成功获取。本次累积关押 {round(time.time() - p_start_time,1)} 秒")
                        in_penalty = False; continuous_blocks = 0

                    # --- 正常数据分流落盘 ---
                    if status == "有效":
                        pbar.write(f"✨ [有效 -> 缓存] UID: {uid:<10} | 用户名: {name}")
                        valid_buffer.append({"UID": uid, "用户名": name, "状态": status})
                        stats["有效"] += 1
                    else:
                        pbar.write(f"📁 [{status} -> 脏数据] UID: {uid:<10} | 信息: {name or '无'}")
                        dirty_buffer.append({"UID": uid, "用户名": name or "无", "状态": status})

                    last_valid_time = time.time()
                    
                    break # 拿到确切结果，跳出死磕循环，进入下一个 UID

                # 周期报告与物理存盘 (满 SAVE_INTERVAL 的数量即存盘一次)
                if (len(valid_buffer) + len(dirty_buffer)) >= SAVE_INTERVAL:
                    v_count, d_count = len(valid_buffer), len(dirty_buffer)
                    append_csv(valid_buffer, CACHE_FILE)
                    append_csv(dirty_buffer, DIRTY_CACHE)
                    valid_buffer.clear(); dirty_buffer.clear() # 清空内存
                    sync_views() # 同步给阅览文件
                    pbar.write(f"📝 [存盘成功] 同步至阅览文件：有效 {v_count} 条，杂项 {d_count} 条。")

                pbar.set_postfix(stats, refresh=False); pbar.update(1)
                # 正常频率下的随机休眠，保护账号
                time.sleep(random.uniform(2.0, 4.5))

    except KeyboardInterrupt:
        # 🚀 终极护城河：拦截用户按下的 Ctrl+C
        # 确保强制退出的一瞬间，内存中的暂存数据也能安全写入硬盘
        print("\n🛑 收到强制停止指令 (Ctrl+C)！")
        print(f"💾 正在将内存中剩余的 {len(valid_buffer)} 条有效数据和 {len(dirty_buffer)} 条脏数据紧急落盘...")
        append_csv(valid_buffer, CACHE_FILE)
        append_csv(dirty_buffer, DIRTY_CACHE)
        sync_views()
        print("✅ 紧急存盘完毕，数据零丢失，安全退出！")
        sys.exit(0)

    # 正常遍历结束收尾
    append_csv(valid_buffer, CACHE_FILE); append_csv(dirty_buffer, DIRTY_CACHE); sync_views()
    print("\n🎉 任务全部完成！")