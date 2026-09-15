"""
📐 JPY综合强度 JCS · 实战精简版
================================
不用查NEER！不用等月度数据！
纯实时算、纯自己算、纯够用！
"""

# ========== 直接填你的数据 ==========
JPY  = 1.0036   # 日元强度
USD  = 0.9477   # 最强外币
AUD_gap = 1.9368 # 最强对 |gap|
THRESHOLD = 1.5  # 开仓门槛
# =================================

# A. 横截面差距 (50%) — JPY vs USD 差多少
gap_top = JPY - USD
A_score = max(0, min(100, gap_top / 0.15 * 100))

# B. 最强对差距 (30%) — 够不够1.5
B_score = min(100, AUD_gap / THRESHOLD * 50 + 50) if AUD_gap >= THRESHOLD else 0

# C. 警戒线 (20%) — USD差太小就扣分
C_score = 0 if gap_top < 0.1 else 100

# 综合 JCS
JCS = 0.50*A_score + 0.30*B_score + 0.20*C_score

# 输出
print("=" * 50)
print(f"A JPY-USD差距: {gap_top:.4f} → 得分: {A_score:.1f}/100")
print(f"B 最强对差距 : {AUD_gap:.4f} → 得分: {B_score:.1f}/100")
print(f"C USD警戒线   : {'差太小' if C_score==0 else '安全'}   → 得分: {C_score:.0f}/100")
print("-" * 50)
print(f"📐 综合JCS = {JCS:.1f}/100")
print("=" * 50)

if JCS >= 80:
    print("✅ JPY强势 → 持有+新开")
elif JCS >= 50:
    print("⚠️  强势减弱 → 只持有、不开新单")
else:
    print("🔴 强势不再 → 全部平仓！")