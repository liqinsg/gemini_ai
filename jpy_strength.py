"""
📐 JPY综合强度 JCS = 0.40×横截面 + 0.35×趋势 + 0.25×持仓支撑
阈值: ≥80持有 / 50-80观察 / <50全平
"""

# 你的数据
JPY = 1.0036
USD = 0.9477
AUD_gap = 1.9368

# A. 横截面: JPY vs 最强外币(USD)差距
gap_top = JPY - USD
A_score = max(0, min(100, gap_top / 0.15 * 100))  # 差距≥0.15才满分

# B. NEER趋势: 暂用USD差距替代，以后接入BOJ官方数据
B_score = 40  # ← 以后替换成 NEER 20/60日趋势分

# C. 持仓支撑: 最强对达标程度
C_score = min(100, AUD_gap / 2.0 * 100)

# 综合 JCS
JCS = 0.40*A_score + 0.35*B_score + 0.25*C_score

print("=" * 55)
print(f"A 横截面差距(JPY-USD): {gap_top:.4f} → 得分: {A_score:.1f}/100")
print(f"B 官方NEER趋势      : 待接入BOJ  → 暂给: {B_score:.1f}/100")
print(f"C 最强对AUD_JPY差距 : {AUD_gap:.4f}    → 得分: {C_score:.1f}/100")
print("-" * 55)
print(f"综合JCS = {JCS:.1f}/100")
print("=" * 55)

if JCS >= 80:
    print("✅ JPY强势 → 持有")
elif JCS >= 50:
    print("⚠️  强势减弱 → 观察、少开新单")
else:
    print("🔴 强势不再 → 全部平仓！")