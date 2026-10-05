# 1. 先确认加载没问题
python -m pytest tests/test_jpy_composite_index.py::TestRealConfig::test_env_weights_loaded -v -s

# 2. Golden基准
python -m pytest tests/test_jpy_composite_index.py::TestGoldenExample -v -s

# 3. 真实权重×Golden数据
python -m pytest tests/test_jpy_composite_index.py::TestRealConfig::test_golden_scores_actual_weights -v -s

# 4. 强/弱方向场景
python -m pytest tests/test_jpy_composite_index.py::TestRealConfig::test_all_positive_jpy_strong -v -s
python -m pytest tests/test_jpy_composite_index.py::TestRealConfig::test_all_negative_jpy_weak -v -s

# 5. Gate对照场景
python -m pytest tests/test_jpy_composite_index.py::TestRealConfig::test_above_env_threshold_scenario -v -s
python -m pytest tests/test_jpy_composite_index.py::TestRealConfig::test_below_env_threshold_scenario -v -s

# 6. 单币种主导
python -m pytest tests/test_jpy_composite_index.py::TestRealConfig::test_single_currency_dominance_eur -v -s

# 7. 全部跑完
python -m pytest tests/test_jpy_composite_index.py -v -s
