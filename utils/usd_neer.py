"""
📐 DXY 美元指数实时读取 + JPY方向校验
====================================
DXY ↑=美元走强 / DXY ↓=美元走弱
DXY涨 + JPY强 = 矛盾 → JPY可能是假象 → 避险
"""
import requests

def get_dxy_change():
    """
    免费实时 DXY 变化率（多个源备用）
    返回: 今日相对变化率(%) + 方向
    """
    # 源1: Alpha Vantage (免费API，需注册给key)
    # 源2: Yahoo Finance (CSV)
    try:
        # 先用 Yahoo Finance — 无需key、免费、实时
        url = "https://query1.finance.yahoo.com/v7/finance/download/%5EDXY?period1=1720000000&period2=1726450000&interval=1d&events=history"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200 and "Date,Open,High,Low,Close" in resp.text:
            lines = resp.text.strip().splitlines()
            if len(lines) >= 3:
                latest = lines[-1].split(",")
                prev = lines[-2].split(",")
                dxy_today = float(latest[4])
                dxy_prev = float(prev[4])
                change_pct = (dxy_today - dxy_prev) / dxy_prev * 100
                return dxy_today, change_pct
    except:
        pass
    return None, None


def main():
    print("=" * 55)
    print("📊 DXY 美元指数 · JPY方向校验")
    print("=" * 55)

    dxy_now, dxy_chg = get_dxy_change()

    if dxy_now is None:
        print("⚠️  实时读取失败 → 请手动查 DXY")
        print("   网址: https://finance.yahoo.com/quote/%5EDXY/")
        print("=" * 55)
        print("📌 判断规则:")
        print("   DXY ↓(美元跌) + JPY强 = ✅ 真实强势 → 可持有")
        print("   DXY ↑(美元涨) + JPY强 = ⚠️  假象！→ 减仓/平仓")
        return

    print(f"DXY 最新: {dxy_now:.2f}")
    print(f"今日变化: {dxy_chg:+.2f}%")
    print("-" * 55)

    # 核心判断
    if dxy_chg < -0.1:
        print("✅ DXY走弱(美元跌) → JPY强势大概率真实 → 加分")
        dxy_score = 90
    elif dxy_chg > +0.1:
        print("🔴 DXY走强(美元涨) → JPY" "强势" "可能是假象 → 大幅减分！")
        dxy_score = 20
    else:
        print("⚠️  DXY横盘 → 中性对待")
        dxy_score = 50

    print(f"📊 DXY方向匹配分: {dxy_score}/100")
    print("=" * 55)
    print("📌 规则: DXY↑=美元全面强 → JPY单独强不可信 → 平仓优先")


if __name__ == "__main__":
    main()
